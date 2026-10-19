"""Owner-configured local models and isolated ChatGPT OAuth credentials."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

AUTH_ORIGIN = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
_PENDING: dict[str, dict] = {}
_LOCKS: dict[str, asyncio.Lock] = {}


def ollama_origin(value: str, *, for_runtime: bool = True) -> str:
    parts = urlsplit(value.strip())
    try:
        port = parts.port or 11434
    except ValueError as exc:
        raise ValueError("Invalid Ollama port") from exc
    if (parts.scheme not in {"http", "https"} or parts.hostname not in {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
            or parts.username or parts.password or parts.query or parts.fragment or parts.path not in {"", "/", "/v1", "/v1/"}
            or not 1024 <= port <= 65535):
        raise ValueError("Ollama must use a local host and port, without credentials or a custom path")
    host = parts.hostname
    if for_runtime and os.path.exists("/.dockerenv") and host in {"localhost", "127.0.0.1", "::1"}:
        host = "host.docker.internal"
    if host == "::1":
        host = "[::1]"
    return f"{parts.scheme}://{host}:{port}"


def oauth_record(settings) -> dict:
    try:
        record = json.loads(getattr(settings, "studio_openai_oauth", "") or "{}")
        if (isinstance(record, dict) and record.get("access_token") and record.get("refresh_token")
                and record.get("client_id") and record.get("subject")
                and {"resource.invoke", "chatgpt.tokens.use.direct"}.issubset(record.get("scopes", []))):
            return record
    except (ValueError, TypeError):
        pass
    return {}


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


async def validate_identity(token: str, client_id: str, *, nonce: str | None = None, subject: str | None = None) -> dict:
    if not isinstance(token, str) or len(token) > 32_000:
        raise ValueError("Invalid identity token")
    segments = token.split(".")
    if len(segments) != 3:
        raise ValueError("Invalid identity token")
    header, claims = (json.loads(_decode(item)) for item in segments[:2])
    if not isinstance(header, dict) or not isinstance(claims, dict) or header.get("alg") != "RS256" or not header.get("kid"):
        raise ValueError("Invalid identity signature")
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        response = await client.get(AUTH_ORIGIN + "/.well-known/jwks.json")
        response.raise_for_status()
        if len(response.content) > 128_000:
            raise ValueError("Identity keys unavailable")
        keys = response.json().get("keys", [])
    key = next((item for item in keys if item.get("kid") == header["kid"] and item.get("kty") == "RSA" and item.get("alg", "RS256") == "RS256"), None)
    if not key:
        raise ValueError("Identity key not found")
    public = rsa.RSAPublicNumbers(int.from_bytes(_decode(key["e"]), "big"), int.from_bytes(_decode(key["n"]), "big")).public_key()
    public.verify(_decode(segments[2]), f"{segments[0]}.{segments[1]}".encode(), padding.PKCS1v15(), hashes.SHA256())
    audience = claims.get("aud")
    audience = [audience] if isinstance(audience, str) else audience
    now = time.time()
    if (claims.get("iss") != AUTH_ORIGIN or not isinstance(audience, list) or client_id not in audience
            or not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < now - 30
            or (len(audience) > 1 and claims.get("azp") != client_id)
            or ("nbf" in claims and (not isinstance(claims["nbf"], (int, float)) or claims["nbf"] > now + 30))
            or not isinstance(claims.get("sub"), str) or not claims["sub"]
            or (nonce is not None and not secrets.compare_digest(str(claims.get("nonce", "")), nonce))
            or (subject is not None and claims["sub"] != subject)):
        raise ValueError("Identity validation failed")
    return claims


async def _token(data: dict) -> dict:
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        response = await client.post(AUTH_ORIGIN + "/api/accounts/oauth/token", data=data)
        response.raise_for_status()
        if len(response.content) > 64_000:
            raise ValueError("Token response too large")
        value = response.json()
    if not isinstance(value, dict) or not value.get("access_token") or str(value.get("token_type", "Bearer")).lower() != "bearer":
        raise ValueError("Invalid token response")
    return value


@asynccontextmanager
async def connection_lock(store):
    key = str(store._db.workspace_id)
    async with _LOCKS.setdefault(key, asyncio.Lock()):
        if getattr(store._db, "sessions", None) is None:
            yield
            return
        from sqlalchemy import text
        async with store._db.sessions.session() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": "studio.oauth:" + key})
            await store.load()
            yield


async def disconnect(store):
    async with connection_lock(store):
        for state in list(_PENDING):
            if _PENDING[state]["workspace"] == str(store._db.workspace_id):
                _PENDING.pop(state, None)
        await store.reset("studio.openai_oauth")


async def start_sign_in(store, *, workspace_id: str, user_id: str, callback: str) -> str:
    if store._cipher is None:
        raise ValueError("Encryption is required to connect ChatGPT")
    async with connection_lock(store):
        host = str(store.effective.studio_openai_host_id or "")
        if not host:
            host = "urn:uuid:" + str(uuid.uuid4())
            await store.set("studio.openai_host_id", host)
    record = oauth_record(store.effective)
    state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    # Expired sign-in attempts are dropped; an existing working account is
    # replaced only after its verified identity matches on callback.
    now = time.time()
    for key in list(_PENDING):
        if _PENDING[key]["expires"] < now:
            _PENDING.pop(key, None)
    if len(_PENDING) >= 100:
        raise ValueError("Too many pending sign-in attempts")
    client_id = record.get("client_id", "dynamic_agent_client")
    _PENDING[state] = {"expires": now + 600, "nonce": nonce, "verifier": verifier, "callback": callback,
                       "workspace": workspace_id, "user": user_id, "client_id": client_id, "subject": record.get("subject")}
    params = {"client_id": client_id, "ext_agent_host_id": host, "response_type": "code", "redirect_uri": callback,
              "scope": SCOPES, "resource": RESOURCE, "state": state, "nonce": nonce, "code_challenge_method": "S256",
              "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")}
    if not record:
        params["agent_name_hint"] = "TGhost"
    elif record.get("id_token"):
        params["id_token_hint"] = record["id_token"]
    return AUTH_ORIGIN + "/api/accounts/authorize?" + urlencode(params)


async def complete_sign_in(store, query: dict, *, workspace_id: str, user_id: str) -> None:
    state = str(query.get("state", ""))
    pending = _PENDING.get(state)
    if (not pending or pending["expires"] < time.time() or pending["workspace"] != workspace_id or pending["user"] != user_id):
        raise ValueError("Sign-in expired or belongs to another session")
    if pending.get("claimed"):
        raise ValueError("Sign-in callback already used")
    pending["claimed"] = True
    try:
        if query.get("error") or not query.get("code"):
            raise ValueError("ChatGPT sign-in was not completed")
        client_id = query.get("client_id", pending["client_id"])
        if (client_id == "dynamic_agent_client" or not isinstance(client_id, str) or len(client_id) > 200
                or (pending["client_id"] != "dynamic_agent_client" and client_id != pending["client_id"])):
            raise ValueError("Client registration did not complete")
        token = await _token({"grant_type": "authorization_code", "client_id": client_id, "code": query["code"],
                              "code_verifier": pending["verifier"], "redirect_uri": pending["callback"], "resource": RESOURCE})
        claims = await validate_identity(token.get("id_token", ""), client_id, nonce=pending["nonce"], subject=pending["subject"])
        scopes = str(token.get("scope", "")).split()
        if not {"resource.invoke", "chatgpt.tokens.use.direct"}.issubset(scopes) or not token.get("refresh_token"):
            raise ValueError("ChatGPT plan permission was not granted")
        record = {"client_id": client_id, "subject": claims["sub"], "email": str(claims.get("email", ""))[:320],
                  "access_token": token["access_token"], "refresh_token": token["refresh_token"], "id_token": token["id_token"],
                  "scopes": scopes, "expires_at": time.time() + min(max(int(token.get("expires_in", 3600)), 0), 86400)}
        async with connection_lock(store):
            # A disconnect cancels pending attempts even while code exchange runs.
            # Check the saved identity again before replacing a working account.
            if _PENDING.get(state) is not pending:
                raise ValueError("Sign-in was cancelled")
            current = oauth_record(store.effective)
            if current and current["subject"] != record["subject"]:
                raise ValueError("Account changed during sign-in")
            await store.set("studio.openai_oauth", json.dumps(record))
    finally:
        _PENDING.pop(state, None)


async def refresh_openai(settings, *, force: bool = False) -> None:
    from .model import StudioConfigurationError, provider_name
    if (not force and provider_name(settings) != "openai") or getattr(settings, "studio_test_mode", False):
        return
    record = oauth_record(settings)
    if not record:
        raise StudioConfigurationError("Connect ChatGPT in Settings")
    if float(record.get("expires_at", 0)) > time.time() + 300:
        return
    store = getattr(settings, "_workspace_settings", None)
    if store is None:
        raise StudioConfigurationError("Reconnect ChatGPT in Settings")
    async with connection_lock(store):
        current = oauth_record(store.effective)
        if current and (current["subject"] != record["subject"] or current["client_id"] != record["client_id"]):
            raise StudioConfigurationError("ChatGPT account changed. Start a new run")
        record = current
        if not record:
            raise StudioConfigurationError("Reconnect ChatGPT in Settings")
        snapshot = getattr(settings, "_provider_values", None)
        if snapshot is not None:
            snapshot["studio_openai_oauth"] = json.dumps(record)
        if float(record.get("expires_at", 0)) > time.time() + 300:
            return
        try:
            token = await _token({"grant_type": "refresh_token", "client_id": record["client_id"], "refresh_token": record["refresh_token"], "resource": RESOURCE})
            if token.get("id_token"):
                await validate_identity(token["id_token"], record["client_id"], subject=record["subject"])
            scopes = str(token.get("scope", " ".join(record["scopes"]))).split()
            if not {"resource.invoke", "chatgpt.tokens.use.direct"}.issubset(scopes):
                raise ValueError("ChatGPT plan access is not enabled")
            updated = {**record, "access_token": token["access_token"], "refresh_token": token.get("refresh_token", record["refresh_token"]),
                       "id_token": token.get("id_token", record.get("id_token")), "scopes": scopes,
                       "expires_at": time.time() + min(max(int(token.get("expires_in", 3600)), 0), 86400)}
            await store.set("studio.openai_oauth", json.dumps(updated))
            if snapshot is not None:
                snapshot["studio_openai_oauth"] = json.dumps(updated)
        except Exception as exc:
            raise StudioConfigurationError("ChatGPT connection expired. Reconnect in Settings") from exc


async def available_models(settings, provider: str, *, ollama_url: str | None = None) -> list[dict]:
    if provider == "ollama":
        async with httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False) as client:
            response = await client.get(ollama_origin(ollama_url or settings.ollama_base_url) + "/api/tags")
            response.raise_for_status()
            if len(response.content) > 512_000:
                raise ValueError("Model catalog too large")
            return [{"id": item["name"], "name": item["name"]} for item in response.json().get("models", [])[:200]
                    if isinstance(item, dict) and isinstance(item.get("name"), str) and len(item["name"]) <= 200
                    and not item.get("remote_model") and not item.get("remote_host") and not item["name"].endswith(":cloud")]
    if provider == "openai":
        # Refresh using an OpenAI-specific overlay even when another provider
        # is currently selected. Merely listing models does not switch it.
        await refresh_openai(settings, force=True)
        record = oauth_record(settings)
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            response = await client.get(RESOURCE + "/models", headers={"Authorization": "Bearer " + record["access_token"]})
            response.raise_for_status()
            if len(response.content) > 512_000:
                raise ValueError("Model catalog too large")
            payload = response.json()
            # ChatGPT account catalogs use models/slug; the public Models API
            # uses data/id. Neither format should lose entries to a UI limit.
            entries = payload.get("models", payload.get("data")) if isinstance(payload, dict) else None
            if not isinstance(entries, list):
                raise ValueError("Invalid model catalog")
            models: dict[str, dict[str, str]] = {}
            for item in entries:
                if not isinstance(item, dict) or item.get("visibility", "list") != "list":
                    continue
                model_id = item.get("slug", item.get("id"))
                if not isinstance(model_id, str) or not re.fullmatch(r"[^\s]{1,200}", model_id):
                    continue
                name = item.get("display_name")
                models.setdefault(model_id, {"id": model_id, "name": name if isinstance(name, str) and name else model_id})
            return list(models.values())
    raise ValueError("Unknown provider")


async def prepare_model(settings) -> None:
    """Refresh account tokens or verify that a selected Ollama model is local."""
    from .model import StudioConfigurationError, provider_name
    if getattr(settings, "studio_test_mode", False):
        return
    if provider_name(settings) == "ollama":
        try:
            models = await available_models(settings, "ollama")
            if settings.ollama_model.strip() not in {item["id"] for item in models}:
                raise ValueError("Not an installed local model")
        except Exception as exc:
            raise StudioConfigurationError("Choose an installed local Ollama model and check its server in Settings") from exc
    else:
        await refresh_openai(settings)
