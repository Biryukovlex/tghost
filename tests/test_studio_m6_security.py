from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.helpers.postgres import pg_url

from app.config import Settings
from app.studio.agent import StudioDeps, build_agent
from app.studio.repository import MemoryStudioRepository


ROOT = Path(__file__).resolve().parents[1]

_MUTATION_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _studio_api_routes(app) -> list[tuple[str, set[str]]]:
    routes: list[tuple[str, set[str]]] = []

    def visit(route, prefix: str = "") -> None:
        original = getattr(route, "original_router", None)
        if original is not None:
            inner_prefix = getattr(original, "prefix", "") or ""
            for inner in getattr(original, "routes", []):
                visit(inner, prefix + inner_prefix)
            return
        path = getattr(route, "path", "")
        methods = set(getattr(route, "methods", set()) or set())
        full = path if path.startswith("/studio/api") else prefix + path
        if full.startswith("/studio/api") and methods:
            routes.append((full, methods))

    for route in app.routes:
        visit(route)
    return routes


def _concrete_path(path: str) -> str:
    return (
        path.replace("{conversation_id}", str(uuid.uuid4()))
        .replace("{draft_id}", str(uuid.uuid4()))
        .replace("{run_id}", str(uuid.uuid4()))
    )


@pytest.mark.asyncio
async def test_every_studio_route_requires_auth_and_every_mutation_requires_csrf(client, app, settings):
    routes = _studio_api_routes(app)
    assert len(routes) >= 20
    mutations = [(path, methods) for path, methods in routes if methods & _MUTATION_METHODS]
    assert len(mutations) >= 10

    # No session: every Studio API route answers 401 JSON.
    for path, methods in routes:
        for method in sorted(methods):
            url = _concrete_path(path)
            if method == "GET":
                url += ("&" if "?" in url else "?") + "channel_id=1"
            response = await client.request(method, url, json={})
            assert response.status_code == 401, f"{method} {path} is not auth-gated"

    # Valid session but no CSRF header: every mutation answers 403.
    await _login(client, settings)
    for path, methods in mutations:
        for method in sorted(methods & _MUTATION_METHODS):
            response = await client.request(method, _concrete_path(path), json={})
            assert response.status_code == 403, f"{method} {path} lacks CSRF validation"

    # Positive control: the same mutation with a valid CSRF token is not 403,
    # proving the 403s above come from CSRF validation rather than a blanket block.
    home = await client.get("/studio")
    token = re.search(r'<meta name="studio-csrf-token" content="([^"]+)"', home.text).group(1)
    response = await client.post("/studio/api/setup/validate", headers={"x-csrf-token": token})
    assert response.status_code != 403


@pytest.mark.asyncio
async def test_agent_supplied_ids_cannot_cross_conversation_or_channel_scope():
    repository = MemoryStudioRepository()
    repository.channels.append(
        {"id": 2, "identifier": "@other_channel", "title": "Other channel", "active": True}
    )
    first = await repository.create_conversation(channel_id=1)
    second = await repository.create_conversation(channel_id=2)
    foreign_draft = await repository.create_draft(
        conversation_id=second["id"],
        channel_id=2,
        payload={"body": "Foreign creative draft", "creative": True},
    )
    await repository.upsert_profile(
        {"channel_id": 1, "topics": [], "style_profile": {}, "editorial_rules": {}, "confidence": "low"}
    )
    await repository.upsert_profile(
        {"channel_id": 2, "topics": [], "style_profile": {}, "editorial_rules": {}, "confidence": "low"}
    )
    foreign_change = await repository.create_profile_change(
        {
            "channel_id": 2,
            "base_profile_version": 1,
            "proposed_topics": [{"name": "foreign"}],
            "status": "proposed",
        }
    )
    await repository.confirm_profile_change(foreign_change["id"])

    deps = StudioDeps(
        repository=repository,
        workspace_id=repository.workspace_id,
        conversation_id=first["id"],
        channel_id=1,
        cancel_event=asyncio.Event(),
    )
    ctx = SimpleNamespace(deps=deps)
    agent = build_agent(Settings(studio_test_mode=True))

    save = agent._function_toolset.tools["edit_draft"].function
    blocked_draft = await save(ctx, expected_revision=1, body="Overwrite attempt")
    assert blocked_draft["status"] == "blocked"
    unchanged = await repository.get_draft(foreign_draft["id"])
    assert unchanged and unchanged["body"] == "Foreign creative draft"

    # After T14 the profile change tools no longer exist as agent tools
    assert "apply_confirmed_topic_changes" not in agent._function_toolset.tools
    assert "propose_topic_changes" not in agent._function_toolset.tools
    assert (await repository.get_profile(2))["version"] == 1


async def _login(client, settings) -> None:
    response = await client.post(
        "/login",
        data={"username": settings.admin_username, "password": settings.admin_password},
        follow_redirects=False,
    )
    assert response.status_code == 303


@pytest.mark.asyncio
async def test_browser_and_exports_do_not_return_runtime_secrets(client, settings):
    settings.studio_test_mode = True
    secrets = {
        "openrouter": "or-secret-m6-never-return",
        "telegram": "telethon-session-m6-never-return",
        "api_hash": "telegram-api-hash-m6-never-return",
        "encryption": "encryption-key-m6-never-return-123456789",
    }
    settings.openrouter_api_key = secrets["openrouter"]
    settings.session_string = secrets["telegram"]
    settings.api_hash = secrets["api_hash"]
    settings.telegram_session_encryption_key = secrets["encryption"]
    await _login(client, settings)
    responses = [
        await client.get("/studio/api/setup"),
        await client.get("/studio/api/bootstrap"),
        await client.get("/export.csv"),
        await client.get("/export-comments.csv"),
    ]
    serialized = "\n".join(response.text for response in responses)
    assert all(value not in serialized for value in secrets.values())
    assert "encrypted_session" not in serialized


def test_default_log_calls_do_not_include_private_body_or_prompt_values():
    for relative in ("app/studio/service.py", "app/studio/research.py", "app/studio/sources.py"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        for line in source.splitlines():
            if re.search(r"\b(log|logger)\.(debug|info|warning|error|exception)\(", line):
                lowered = line.lower()
                assert not any(token in lowered for token in ("prompt", "body", "excerpt", "session_string", "api_key"))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_repository_and_routes_fail_closed_across_workspaces():
    database_url = pg_url("M6_POSTGRES_URL")
    if not database_url:
        pytest.skip("set TEST_POSTGRES_URL (or M6_POSTGRES_URL) to run the M6 tenant/encryption proof")

    import httpx

    from app.postgres_db import PostgresDatabase
    from app.session_crypto import SessionCipher
    from app.studio.repository import StudioRepository
    from app.web.routes import create_app

    suffix = uuid.uuid4().hex[:10]
    slug_a, slug_b = f"m6-sec-a-{suffix}", f"m6-sec-b-{suffix}"
    admin_a, admin_b = f"{slug_a}-admin", f"{slug_b}-admin"
    db_a = PostgresDatabase(database_url, workspace_slug=slug_a)
    db_b = PostgresDatabase(database_url, workspace_slug=slug_b)

    class Collector:
        def __init__(self, db):
            self.db = db

        async def poll_all(self, reason="test"):
            return {"reason": reason, "posts_seen": 0}

    try:
        await db_a.init_db(admin_username=admin_a)
        await db_b.init_db(admin_username=admin_b)
        await db_a.upsert_channel(f"@{slug_a}", "Workspace A", 6201)
        channel_b = await db_b.upsert_channel(f"@{slug_b}", "Workspace B", 6202)
        repo_a, repo_b = StudioRepository(db_a), StudioRepository(db_b)
        conversation_b = await repo_b.create_conversation(channel_id=channel_b)
        await repo_b.persist_research_bundle(
            conversation_id=conversation_b["id"],
            channel_id=channel_b,
            bundle={
                "sources": [{"source_id": "workspace-b-source", "url": "https://example.test/b"}],
                "stories": [],
            },
        )
        draft_b = await repo_b.create_draft(
            conversation_id=conversation_b["id"],
            channel_id=channel_b,
            payload={"body": "Workspace B private draft", "creative": True},
        )
        message_b = await repo_b.append_message(
            conversation_id=conversation_b["id"], role="user", content="Workspace B message"
        )
        run_b = await repo_b.create_run(
            conversation_id=conversation_b["id"],
            user_message_id=message_b["id"],
            requested_model="test",
        )

        assert await repo_a.get_conversation(conversation_b["id"]) is None
        assert await repo_a.get_draft(draft_b["id"]) is None
        assert await repo_a.get_research_bundle(
            conversation_id=conversation_b["id"], channel_id=channel_b
        ) is None
        assert await repo_a.get_run(run_b["id"]) is None
        assert await db_a.claim_collection_job(channel_b) is None

        session_plaintext = "m6-plaintext-telegram-session-never-return"
        from cryptography.fernet import Fernet

        encryption_key = Fernet.generate_key().decode("ascii")
        cipher = SessionCipher(encryption_key)
        await db_b.persist_telegram_session(
            label="m6-security",
            api_id=1,
            api_hash="m6-api-hash",
            session_string=session_plaintext,
            cipher=cipher,
        )
        encrypted_row = await db_b._execute(
            "SELECT encrypted_session FROM telegram_connections WHERE workspace_id=:workspace_id AND label=:label",
            {"label": "m6-security"},
        )
        encrypted = bytes(encrypted_row.scalar_one())
        assert session_plaintext.encode() not in encrypted
        assert cipher.decrypt(encrypted) == session_plaintext

        settings = Settings(
            api_id=1,
            api_hash="m6-api-hash",
            session_string="m6-environment-session",
            channels=f"@{slug_a}",
            admin_username=admin_a,
            admin_password="m6-test-password",
            session_secret="m6-http-session-secret",
            database_url=database_url,
            telegram_session_encryption_key=encryption_key,
            local_workspace_slug=slug_a,
            studio_test_mode=True,
        )
        app = create_app(Collector(db_a), settings)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await _login(client, settings)
            responses = [
                await client.get(f"/studio/api/conversations/{conversation_b['id']}/messages"),
                await client.get(f"/studio/api/drafts/{draft_b['id']}"),
                await client.get(f"/studio/api/runs/{run_b['id']}/events"),
            ]
            assert [response.status_code for response in responses] == [404, 404, 404]
            serialized = "\n".join(response.text for response in responses)
            assert "Workspace B" not in serialized
            assert session_plaintext not in serialized
    finally:
        await db_a.close()
        await db_b.close()
