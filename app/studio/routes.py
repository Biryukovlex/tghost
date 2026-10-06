"""Authenticated Studio page and M2 conversation/run API."""

from __future__ import annotations

from .model import provider_name, model_name

import logging
import uuid
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from .. import limits
from ..web.dependencies import require_auth, require_csrf
from .consent import configuration_fingerprint, consent_state, disclosure
from .drafts import DraftConflictError, DraftValidationError
from .markdown import render_markdown_html, render_markdown_plain
from .repository import ActiveRunExists, ConversationNotFound, DraftNotFound, RunNotFound
from .prompts import PROMPT_VERSION
from .schemas import ConversationCreate, DraftPatchRequest, ProviderConsentRequest, StudioSettingsPatch
from .service import StudioService
from .references import ReferenceChannels
from .setup import build_setup_state
from .search_health import check_search_health
from .observability import duration_ms, normalize_usage, safe_error, safe_error_for_code
from .run_ids import resolve_run_id

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "web" / "templates"))
log = logging.getLogger("studio.routes")


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _conversation(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]),
        "channel_id": int(row["channel_id"]),
        "channel_identifier": row.get("channel_identifier", "") or "",
        "channel_title": row.get("channel_title", "") or "",
        "title": row.get("title", "New conversation") or "New conversation",
        "summary": row.get("summary", "") or "",
        "active_draft_id": str(row["active_draft_id"]) if row.get("active_draft_id") else None,
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "archived_at": _iso(row.get("archived_at")),
    }


def _draft(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    value["id"] = str(value["id"])
    for key in ("workspace_id", "conversation_id", "analysis_id"):
        if value.get(key) is not None:
            value[key] = str(value[key])
    for key in ("created_at", "updated_at", "copied_at"):
        value[key] = _iso(value.get(key))
    body = str(value.get("body") or "")
    value["body"] = body
    # T16: character_count/over_limit computed on plain rendering
    plain = render_markdown_plain(body)
    value["body_plain"] = plain
    value["body_html"] = render_markdown_html(body)
    value["plain_character_count"] = len(plain)
    value["character_count"] = len(plain)
    value["over_limit"] = len(plain) > 4096
    value["warning_threshold"] = len(plain) >= 3800
    # Keep legacy plain count under character_count for backward compat
    list_defaults: tuple[tuple[str, list[Any]], ...] = (("source_ids", []), ("claim_support", []), ("assumptions", []), ("warnings", []), ("channel_evidence", []), ("web_evidence", []))
    for key, default in list_defaults:
        raw = value.get(key)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                raw = default
        value[key] = raw if isinstance(raw, (list, dict)) else default
    return value


def _draft_version(row: dict[str, Any]) -> dict[str, Any]:
    value = dict(row)
    value["id"] = int(value["id"])
    value["draft_id"] = str(value["draft_id"])
    value["version"] = int(value["version"])
    value["body"] = str(value.get("body") or "")
    value["character_count"] = len(value["body"])
    value["created_at"] = _iso(value.get("created_at"))
    return value


def _message(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "role": row["role"],
        "content": row["content"],
        "metadata": row.get("metadata_json", {}) or {},
        "created_at": _iso(row.get("created_at")),
    }


def _run(row: dict[str, Any]) -> dict[str, Any]:
    error_code, error_message, _ = safe_error_for_code(row.get("error_code"), row.get("error_message"))
    diagnostics = _run_diagnostics(row)
    return {
        "id": str(row["id"]),
        "conversation_id": str(row["conversation_id"]),
        "status": row["status"],
        "stage": row.get("stage", "") or "",
        "provider": row.get("provider", "openrouter") or "openrouter",
        "requested_model": row.get("requested_model", "") or "",
        "actual_model": row.get("actual_model"),
        "usage": normalize_usage(row.get("usage")),
        "error_code": error_code or None,
        "error_message": error_message or None,
        "diagnostics": diagnostics,
        "created_at": _iso(row.get("created_at")),
        "started_at": _iso(row.get("started_at")),
        "finished_at": _iso(row.get("finished_at")),
        "duration_ms": duration_ms(row.get("started_at"), row.get("finished_at")),
    }


def _run_diagnostics(row: dict[str, Any]) -> dict[str, Any] | None:
    """Project persisted non-secret failure metadata, if a run recorded any."""

    phase = str(row.get("error_phase") or "")
    error_class = str(row.get("error_class") or "")
    status_code = row.get("error_status")
    provider_code = row.get("error_provider_code")
    if not phase and not error_class and status_code is None and provider_code is None:
        return None
    try:
        status_number = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        status_number = None
    try:
        provider_number = int(provider_code) if provider_code is not None else None
    except (TypeError, ValueError):
        provider_number = None
    return {
        "phase": phase[:32] or None,
        "exception_class": error_class[:80] or None,
        "status_code": status_number if status_number is not None and 100 <= status_number <= 599 else None,
        "provider_code": provider_number if provider_number is not None and 100 <= provider_number <= 599 else None,
    }


_PUBLIC_EVENT_KEYS = {
    "tool_name",
    "delta_length",
    "result_length",
    "provider",
    "providers",
    "degraded",
    "cache_hit",
    "persisted",
    "query_count",
    "result_count",
    "source_count",
    "story_count",
    "analysis_id",
    "story_cluster_id",
    "draft_id",
    "code",
    "usage",
    "diagnostics",
}


def _public_event_payload(raw: Any) -> dict[str, Any]:
    """Filter persisted event projections before returning them to a browser."""

    if not isinstance(raw, dict):
        return {}
    payload: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in _PUBLIC_EVENT_KEYS or value is None:
            continue
        if key == "usage":
            payload[key] = normalize_usage(value)
        elif key == "diagnostics" and isinstance(value, dict):
            phase = str(value.get("phase") or "")[:32] or None
            error_class = str(value.get("exception_class") or "")[:80] or None
            error_code = str(value.get("error_code") or "")[:80] or None
            try:
                status_number = int(value["status_code"]) if value.get("status_code") is not None else None
            except (TypeError, ValueError):
                status_number = None
            try:
                provider_number = int(value["provider_code"]) if value.get("provider_code") is not None else None
            except (TypeError, ValueError):
                provider_number = None
            payload[key] = {
                "phase": phase,
                "error_code": error_code,
                "exception_class": error_class,
                "status_code": status_number if status_number is not None and 100 <= status_number <= 599 else None,
                "provider_code": provider_number if provider_number is not None and 100 <= provider_number <= 599 else None,
            }
        elif key == "providers" and isinstance(value, list):
            payload[key] = [str(item)[:80] for item in value[:4]]
        elif key in {"tool_name", "provider", "analysis_id", "story_cluster_id", "draft_id", "code"}:
            payload[key] = str(value)[:160]
        elif key in {"degraded", "cache_hit", "persisted"}:
            payload[key] = bool(value)
        else:
            try:
                payload[key] = max(0, min(int(value), 1_000_000_000))
            except (TypeError, ValueError, OverflowError):
                continue
    return payload


def _run_activity(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate only safe event counters for the collapsed details panel."""

    payloads = [_public_event_payload(event.get("safe_payload", {}) or {}) for event in events]
    tools = sum(1 for event in events if str(event.get("event_type")) == "TOOL_CALL_START")
    source_count = max((int(payload.get("source_count") or 0) for payload in payloads), default=0)
    story_count = max((int(payload.get("story_count") or 0) for payload in payloads), default=0)
    result_count = max((int(payload.get("result_count") or 0) for payload in payloads), default=0)
    cache_hits = sum(1 for payload in payloads if bool(payload.get("cache_hit")))
    degraded = any(bool(payload.get("degraded")) for payload in payloads)
    references: dict[str, list[str]] = {}
    for key in ("analysis_id", "story_cluster_id", "draft_id"):
        values = []
        for payload in payloads:
            value = payload.get(key)
            if value is not None and str(value) not in values:
                values.append(str(value)[:160])
        if values:
            references[key] = values[:12]
    return {
        "event_count": len(events),
        "tool_call_count": tools,
        "result_count": result_count,
        "source_count": source_count,
        "story_count": story_count,
        "cache_hit_count": cache_hits,
        "degraded": degraded,
        "references": references,
    }


def _profile(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": str(row["id"]) if row.get("id") is not None else None,
        "workspace_id": str(row["workspace_id"]) if row.get("workspace_id") is not None else None,
        "channel_id": int(row["channel_id"]),
        "topics": row.get("topics", []) or [],
        "style_profile": row.get("style_profile", {}) or {},
        "editorial_rules": row.get("editorial_rules", {}) or {},
        "confidence": row.get("confidence", "low") or "low",
        "current_analysis_id": str(row["current_analysis_id"]) if row.get("current_analysis_id") else None,
        "version": int(row.get("version", 1)),
        "topics_text": row.get("topics_text", "") or "",
        "editorial_text": row.get("editorial_text", "") or "",
        "style_text": row.get("style_text", "") or "",
        "built_at": _iso(row.get("built_at")),
        "built_from_posts": int(row.get("built_from_posts", 0) or 0),
        "evidence_post_ids": row.get("evidence_post_ids", []) or [],
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


def _profile_change(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]) if row.get("workspace_id") is not None else None,
        "channel_id": int(row["channel_id"]),
        "base_profile_version": int(row["base_profile_version"]),
        "proposed_topics": row.get("proposed_topics", []) or [],
        "style_diff": row.get("style_diff", {}) or {},
        "editorial_rules": row.get("editorial_rules", {}) or {},
        "reason": row.get("reason", "") or "",
        "status": row.get("status", "proposed"),
        "created_at": _iso(row.get("created_at")),
        "confirmed_at": _iso(row.get("confirmed_at")),
        "applied_at": _iso(row.get("applied_at")),
    }


def _safe_error(code: str, message: str, *, status_code: int, retryable: bool = False) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, "retryable": retryable}}, status_code=status_code)


def build_router() -> APIRouter:
    router = APIRouter(prefix="/studio", tags=["studio"])

    def _settings(request: Request):
        return request.app.state.settings

    def _service(request: Request) -> StudioService:
        service = getattr(request.app.state, "studio_service", None)
        if service is None:
            raise HTTPException(status_code=503, detail="Studio service is not ready")
        return service

    async def _selected_channel(
        request: Request, channel_id: int | None
    ) -> tuple[list[dict[str, Any]], int | None]:
        channels = [
            dict(row)
            for row in await _service(request).repository.list_channels()
            if bool(row.get("active", True))
        ]
        if channel_id is None:
            return channels, (int(channels[0]["id"]) if channels else None)
        if channel_id not in {int(row["id"]) for row in channels}:
            raise HTTPException(status_code=404, detail="Channel not found")
        return channels, int(channel_id)

    @router.get("/api/settings")
    async def studio_settings(request: Request, channel_id: int = Query(..., gt=0)):
        require_auth(request)
        _, selected_channel_id = await _selected_channel(request, channel_id)
        if selected_channel_id is None:
            return _safe_error("channel_not_found", "Selected channel is not part of this workspace.", status_code=404)
        return {
            "channel_id": selected_channel_id,
            "system_prompt": await _service(request).repository.get_system_prompt(selected_channel_id),
        }

    @router.patch("/api/settings")
    async def patch_studio_settings(request: Request):
        require_auth(request)
        require_csrf(request)
        try:
            payload = StudioSettingsPatch.model_validate(await request.json())
        except (ValidationError, ValueError):
            return _safe_error("invalid_settings", "System prompt must be text, up to 12,000 characters.", status_code=422)
        _, selected_channel_id = await _selected_channel(request, payload.channel_id)
        if selected_channel_id is None:
            return _safe_error("channel_not_found", "Selected channel is not part of this workspace.", status_code=404)
        from .repository import SystemPromptConflict
        try:
            value = await _service(request).repository.set_system_prompt(
                selected_channel_id, payload.system_prompt, expected_prompt=payload.expected_prompt
            )
        except SystemPromptConflict:
            return _safe_error("system_prompt_conflict", "System Prompt changed in another tab. Your text is preserved; reopen to load the saved instructions.", status_code=409)
        return {"channel_id": selected_channel_id, "system_prompt": value}

    def _ensure_ready(request: Request) -> dict[str, Any]:
        return build_setup_state(_settings(request), request.app.state.db)

    async def _consent(request: Request, context=None) -> dict[str, Any]:
        context = context or require_auth(request)
        getter = getattr(_service(request).repository, "get_provider_consent", None)
        row = None
        if getter is not None:
            row = await getter(
                provider=provider_name(_settings(request)),
                configuration_fingerprint=configuration_fingerprint(_settings(request)),
                user_id=context.user_id,
            )
        return consent_state(_settings(request), row)

    @router.get("", response_class=HTMLResponse)
    async def studio_home(request: Request):
        require_auth(request)
        setup = _ensure_ready(request)
        # can_manage_settings for sidebar link
        ctx = getattr(request.app.state, "workspace_context", None)
        can_manage = getattr(ctx, "role", "") == "owner" if ctx else False
        return _TEMPLATES.TemplateResponse(
            request,
            "studio.html",
            {
                "setup": setup,
                "csrf_token": request.app.state.csrf_token(request),
                "studio_assets": setup["ready"],
                "can_manage_settings": can_manage,
            },
        )

    @router.get("/api/setup")
    async def studio_setup(request: Request):
        require_auth(request)
        return _ensure_ready(request)

    @router.get("/api/research/health")
    async def research_health(request: Request):
        """Return bounded private-search health without blocking setup."""

        require_auth(request)
        return (await check_search_health(_settings(request))).as_dict()

    @router.post("/api/setup/validate")
    async def studio_setup_validate(request: Request):
        require_auth(request)
        require_csrf(request)
        return _ensure_ready(request)

    @router.get("/api/bootstrap")
    async def studio_bootstrap(
        request: Request, channel_id: int | None = Query(default=None, gt=0)
    ):
        context = require_auth(request)
        setup = _ensure_ready(request)
        service = _service(request)
        channels, selected_channel_id = await _selected_channel(request, channel_id)
        conversations: list[dict[str, Any]] = []
        if setup["ready"] and selected_channel_id is not None:
            await service.recover_stale_runs()
            conversations = [
                _conversation(row)
                for row in await service.repository.list_conversations(
                    channel_id=selected_channel_id
                )
            ]
        selected = conversations[0] if conversations else None
        consent = await _consent(request, context)
        profile = None
        profile_status = "no_channel" if selected_channel_id is None else "needs_consent"
        if selected_channel_id is not None and setup["ready"]:
            profile_getter = getattr(service.repository, "get_profile", None)
            profile = await profile_getter(int(selected_channel_id)) if profile_getter is not None else None
            if profile is not None:
                has_text = any(str(profile.get(k) or "").strip() for k in ("topics_text", "editorial_text", "style_text"))
                profile_status = "ready" if has_text else "not_built"
            else:
                profile_status = "not_built"
        current_draft = None
        active_run = None
        if selected is not None and setup["ready"]:
            getter = getattr(service.repository, "get_current_draft", None)
            if getter is not None:
                current_draft = await getter(conversation_id=uuid.UUID(str(selected["id"])), channel_id=int(selected["channel_id"]))
            active_getter = getattr(service.repository, "get_active_run", None)
            if active_getter is not None:
                active_run = await active_getter(uuid.UUID(str(selected["id"])))
        return {
            "setup": setup,
            "workspace": {"id": str(context.workspace_id or service.repository.workspace_id), "slug": context.workspace_slug, "role": context.role},
            "user": {"id": str(context.user_id) if context.user_id else None},
            "provider": {"name": provider_name(_settings(request)), "model": model_name(_settings(request)), "configured": setup["ready"]},
            "research": setup.get("research", {}),
            "channels": channels,
            "selected_channel_id": selected_channel_id,
            "conversations": conversations,
            "current_conversation": selected,
            "draft": _draft(current_draft),
            "active_run": _run(active_run) if active_run is not None else None,
            "consent": consent,
            "profile": _profile(profile),
            "profile_status": profile_status,
        }

    @router.get("/api/consent")
    async def get_consent(request: Request):
        context = require_auth(request)
        _ensure_ready(request)
        return {"consent": await _consent(request, context)}

    @router.post("/api/consent")
    async def grant_consent(request: Request):
        context = require_auth(request)
        require_csrf(request)
        setup = _ensure_ready(request)
        if not setup["ready"]:
            return _safe_error("studio_not_ready", "Finish Studio setup before granting provider consent.", status_code=409)
        try:
            payload = ProviderConsentRequest.model_validate(await request.json())
        except (ValidationError, ValueError, TypeError):
            return _safe_error("invalid_consent", "Consent details are invalid.", status_code=422)
        expected = configuration_fingerprint(_settings(request))
        if not payload.confirm:
            return _safe_error("consent_required", "Confirm the provider disclosure to continue.", status_code=409)
        if payload.configuration_fingerprint and payload.configuration_fingerprint != expected:
            return _safe_error("consent_stale", "Provider configuration changed; review the disclosure again.", status_code=409)
        granter = getattr(_service(request).repository, "grant_provider_consent", None)
        if granter is None or context.user_id is None:
            return _safe_error("consent_unavailable", "Provider consent persistence is unavailable.", status_code=503)
        row = await granter(user_id=context.user_id, provider=provider_name(_settings(request)), configuration_fingerprint=expected)
        # Consent is workspace-wide because the provider configuration is
        # shared, but the profile returned alongside it must stay channel-local.
        profile = None
        try:
            _, selected_channel_id = await _selected_channel(request, payload.channel_id)
            if selected_channel_id is not None:
                profile = await _service(request).repository.get_profile(selected_channel_id)
                if profile is None:
                    # Best-effort: create low-confidence profile without blocking consent.
                    try:
                        profile = await _service(request).ensure_profile(selected_channel_id)
                    except Exception:
                        profile = None
        except Exception:
            profile = None
        resp: dict[str, Any] = {"consent": consent_state(_settings(request), row), "disclosure": disclosure(_settings(request), fingerprint=expected)}
        if profile is not None:
            resp["profile"] = _profile(profile)
        return resp

    @router.post("/api/consent/revoke")
    async def revoke_consent(request: Request):
        context = require_auth(request)
        require_csrf(request)
        _ensure_ready(request)
        revoker = getattr(_service(request).repository, "revoke_provider_consent", None)
        if revoker is None or context.user_id is None:
            return _safe_error("consent_unavailable", "Provider consent persistence is unavailable.", status_code=503)
        row = await revoker(user_id=context.user_id, provider=provider_name(_settings(request)))
        return {"consent": consent_state(_settings(request), row)}

    def _profile_build_blockers(request: Request, channel_id: int | None, setup: dict[str, Any], consent: dict[str, Any] | None, available_posts: int | None = None) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        if not setup.get("ready"):
            blockers.append({"code": "studio_not_ready", "message": "Finish Studio setup before building."})
            return blockers
        if consent is not None and consent.get("required") and not consent.get("granted"):
            blockers.append({"code": "provider_consent_required", "message": "Allow the configured provider in the Studio banner before building."})
        # Check post count
        if available_posts is not None:
            minimum = int(limits.MIN_PROFILE_POSTS)
            if available_posts < minimum:
                blockers.append({"code": "too_few_posts", "message": f"Needs at least {minimum} posts; {available_posts} collected so far.", "available": available_posts, "minimum": minimum})
        return blockers

    @router.get("/api/profile")
    async def get_profile(request: Request, channel_id: int | None = None):
        context = require_auth(request)
        setup = _ensure_ready(request)
        if not setup["ready"]:
            return _safe_error("studio_not_ready", "Finish Studio setup before viewing the channel profile.", status_code=409)
        service = _service(request)
        channels = await service.repository.list_channels()
        selected = channel_id or (int(channels[0]["id"]) if channels else None)
        if selected is None:
            return _safe_error("channel_required", "Configure a Telegram channel before opening Studio.", status_code=409)
        if selected not in {int(row["id"]) for row in channels}:
            return _safe_error("channel_not_found", "Channel not found.", status_code=404)
        profile = await service.repository.get_profile(selected)
        consent = await _consent(request, context)
        # Determine available posts for blocker
        reader = getattr(service.repository, "performance_rows", None)
        available = None
        if reader is not None:
            try:
                rows = await reader(selected)
                available = len(rows)
            except Exception:
                available = None
        blockers = _profile_build_blockers(request, selected, setup, consent, available)
        can_build = len(blockers) == 0
        return {"profile": _profile(profile), "can_build": can_build, "build_blockers": blockers, "channel_id": selected}

    @router.put("/api/profile")
    async def put_profile(request: Request):
        require_auth(request)
        require_csrf(request)
        setup = _ensure_ready(request)
        if not setup["ready"]:
            return _safe_error("studio_not_ready", "Finish Studio setup before saving the channel profile.", status_code=409)
        try:
            from .schemas import ProfileTextPatch
            payload = ProfileTextPatch.model_validate(await request.json())
        except (ValidationError, ValueError, TypeError) as exc:
            err = str(exc)
            # surface field name if present
            return _safe_error("invalid_profile", f"Profile details are invalid: {err}", status_code=422)
        # Validate field limits and line counts before cleaning (spec 422 with field name)
        for field in ["topics_text", "editorial_text", "style_text"]:
            text = getattr(payload, field)
            if len(text) > 2000:
                return _safe_error("invalid_profile", f"{field} exceeds 2000 characters", status_code=422)
            if len(text.splitlines()) > 60:
                return _safe_error("invalid_profile", f"{field} exceeds 60 lines", status_code=422)
        service = _service(request)
        if payload.channel_id not in {int(row["id"]) for row in await service.repository.list_channels()}:
            return _safe_error("channel_not_found", "Channel not found.", status_code=404)
        current = await service.repository.get_profile(payload.channel_id)
        expected = int(payload.expected_version)
        current_version = int(current["version"]) if current else 0
        if expected != current_version:
            return JSONResponse({"error": {"code": "profile_conflict", "message": "Profile changed in another tab.", "retryable": True}, "server_profile": _profile(current)}, status_code=409)
        evidence = payload.evidence_post_ids
        build_count = int((current or {}).get("built_from_posts", 0) or 0)
        if evidence is not None:
            if any(pid <= 0 or pid > 2**63 - 1 for pid in evidence):
                return _safe_error("invalid_profile_evidence", "Supporting posts must belong to this channel's stored history.", status_code=422)
            allowed_ids = await service.repository.profile_evidence_ids(payload.channel_id, evidence)
            if set(evidence) != allowed_ids:
                return _safe_error("invalid_profile_evidence", "Supporting posts must belong to this channel's stored history.", status_code=422)
            evidence = list(dict.fromkeys(evidence))
            build_count = len(evidence)
        def clean_text(v: str) -> str:
            lines = [line.rstrip() for line in v.splitlines()]
            cleaned = "\n".join(line for line in lines if line.strip() != "")
            return cleaned
        topics_text = clean_text(payload.topics_text)
        editorial_text = clean_text(payload.editorial_text)
        style_text = clean_text(payload.style_text)
        try:
            await service.repository.upsert_profile_text({
                "channel_id": payload.channel_id,
                "topics_text": topics_text,
                "editorial_text": editorial_text,
                "style_text": style_text,
                "expected_version": expected,
                "evidence_post_ids": evidence,
                "built_from_posts": build_count,
            })
        except ValueError as exc:
            # Validation from repository (length/line)
            msg = str(exc)
            field = "topics_text" if "topics_text" in msg else "editorial_text" if "editorial_text" in msg else "style_text" if "style_text" in msg else "profile"
            return _safe_error("invalid_profile", f"{field} {msg}", status_code=422)
        except Exception as exc:
            if "DraftConflict" in type(exc).__name__ or "profile_conflict" in str(exc).lower():
                cur = await service.repository.get_profile(payload.channel_id)
                return JSONResponse({"error": {"code": "profile_conflict", "message": "Profile changed in another tab.", "retryable": True}, "server_profile": _profile(cur)}, status_code=409)
            raise
        return {"profile": _profile(await service.repository.get_profile(payload.channel_id))}

    @router.post("/api/profile/build")
    async def build_profile(request: Request):
        context = require_auth(request)
        require_csrf(request)
        setup = _ensure_ready(request)
        if not setup["ready"]:
            return _safe_error("studio_not_ready", "Finish Studio setup first.", status_code=409)
        try:
            from .schemas import ProfileBuildRequest
            payload = ProfileBuildRequest.model_validate(await request.json())
        except (ValidationError, ValueError, TypeError):
            return _safe_error("invalid_profile_build", "Profile build details are invalid.", status_code=422)
        service = _service(request)
        if payload.channel_id not in {int(row["id"]) for row in await service.repository.list_channels()}:
            return _safe_error("channel_not_found", "Channel not found.", status_code=404)
        consent = await _consent(request, context)
        if consent.get("required") and not consent.get("granted"):
            return _safe_error("provider_consent_required", "Allow the configured provider before building.", status_code=409)
        # Check too_few_posts before building
        reader = getattr(service.repository, "performance_rows", None)
        if reader is not None:
            try:
                rows = await reader(payload.channel_id)
                minimum = int(limits.MIN_PROFILE_POSTS)
                if len(rows) < minimum:
                    return _safe_error("too_few_posts", f"Needs at least {minimum} posts; {len(rows)} collected so far.", status_code=409)
            except ConversationNotFound:
                return _safe_error("channel_not_found", "Channel not found.", status_code=404)
        try:
            # Structured output may need a text-JSON compatibility retry on
            # models that do not support tool-based output schemas.
            import asyncio
            from .semantic_profile import PROFILE_BUILD_TIMEOUT_SECONDS
            result = await asyncio.wait_for(service.build_profile_draft(payload.channel_id), timeout=PROFILE_BUILD_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return _safe_error("profile_build_failed", "Profile build timed out. Your previous profile is unchanged. Retry shortly.", status_code=502, retryable=True)
        except Exception as exc:
            if "too few" in str(exc).lower():
                return _safe_error("too_few_posts", str(exc), status_code=409)
            log.warning("Channel profile build failed for channel_id=%s error_class=%s", payload.channel_id, type(exc).__name__)
            code, message, retryable = safe_error(exc)
            return _safe_error(code, message, status_code=502, retryable=retryable)
        return {"draft": result}

    @router.get("/api/conversations")
    async def list_conversations(
        request: Request, channel_id: int | None = Query(default=None, gt=0)
    ):
        require_auth(request)
        setup = _ensure_ready(request)
        if not setup["ready"]:
            return _safe_error("studio_not_ready", "Finish Studio setup before creating a conversation.", status_code=409)
        _, selected_channel_id = await _selected_channel(request, channel_id)
        if selected_channel_id is None:
            return {"channel_id": None, "conversations": []}
        rows = await _service(request).repository.list_conversations(
            channel_id=selected_channel_id
        )
        return {
            "channel_id": selected_channel_id,
            "conversations": [_conversation(row) for row in rows],
        }

    @router.post("/api/conversations")
    async def create_conversation(request: Request):
        require_auth(request)
        require_csrf(request)
        setup = _ensure_ready(request)
        if not setup["ready"]:
            return _safe_error("studio_not_ready", "Finish Studio setup before creating a conversation.", status_code=409)
        try:
            payload = ConversationCreate.model_validate(await request.json())
        except (ValidationError, ValueError, TypeError):
            return _safe_error("invalid_conversation", "Conversation details are invalid.", status_code=422)
        service = _service(request)
        try:
            _, channel_id = await _selected_channel(request, payload.channel_id)
            if channel_id is None:
                return _safe_error("channel_not_found", "Selected channel is not part of this workspace.", status_code=404)
            row = await service.create_conversation(channel_id=channel_id, title=payload.title)
        except ConversationNotFound:
            return _safe_error("channel_not_found", "Selected channel is not part of this workspace.", status_code=404)
        return {"conversation": _conversation(row)}

    @router.get("/api/conversations/{conversation_id}/references")
    async def conversation_references(request: Request, conversation_id: str):
        require_auth(request)
        try:
            references = ReferenceChannels(_service(request).repository, _settings(request))
            refs = await references.list(uuid.UUID(conversation_id))
        except (ConversationNotFound, ValueError):
            return _safe_error("conversation_not_found", "Conversation not found.", status_code=404)
        return {"references": refs}

    @router.put("/api/conversations/{conversation_id}/references/{channel_id}")
    async def set_conversation_reference(request: Request, conversation_id: str, channel_id: str):
        context = require_auth(request)
        require_csrf(request)
        consent = await _consent(request, context)
        try:
            reference_conversation_id = uuid.UUID(conversation_id)
            reference_channel_id = int(channel_id)
            payload = await request.json()
            if (
                not isinstance(payload, dict) or set(payload) - {"enabled", "permission"}
                or not isinstance(payload.get("enabled"), bool)
                or not isinstance(payload.get("permission", False), bool)
            ):
                raise ValueError("Invalid reference selection.")
            if payload["enabled"] and not consent["granted"]:
                return _safe_error("consent_required", "Allow the configured provider in Studio before adding a reference.", status_code=409)
            references = ReferenceChannels(_service(request).repository, _settings(request))
            refs = await references.set(
                reference_conversation_id, reference_channel_id, enabled=payload["enabled"],
                permission=payload.get("permission", False), user_id=context.user_id,
            )
        except ConversationNotFound:
            return _safe_error("reference_not_found", "Reference channel or conversation not found.", status_code=404)
        except (ValueError, TypeError):
            return _safe_error("invalid_reference", "Confirm permission to use this channel's posts, and stop any active run before changing references.", status_code=422)
        return {"references": refs}

    @router.patch("/api/conversations/{conversation_id}")
    async def rename_conversation(request: Request, conversation_id: str):
        require_auth(request)
        require_csrf(request)
        try:
            parsed = uuid.UUID(conversation_id)
            payload = await request.json()
            title = payload.get("title")
            if set(payload) != {"title"} or not isinstance(title, str) or not title.strip() or len(title) > 160:
                raise ValueError("invalid title")
        except (ValueError, TypeError, AttributeError):
            return _safe_error("invalid_conversation_title", "Enter a title between 1 and 160 characters.", status_code=422)
        row = await _service(request).repository.rename_conversation(parsed, title=title.strip())
        if row is None or row.get("archived_at") is not None:
            return _safe_error("conversation_not_found", "Conversation not found.", status_code=404)
        return {"conversation": _conversation(row)}

    @router.delete("/api/conversations/{conversation_id}")
    async def delete_conversation(request: Request, conversation_id: str):
        """Permanently remove one conversation and its Studio-only records."""

        require_auth(request)
        require_csrf(request)
        try:
            parsed = uuid.UUID(conversation_id)
        except ValueError:
            return _safe_error("conversation_not_found", "Conversation not found.", status_code=404)
        deleter = getattr(_service(request).repository, "delete_conversation", None)
        if deleter is None:
            return _safe_error(
                "conversation_delete_unavailable",
                "Conversation deletion is unavailable for this storage backend.",
                status_code=503,
                retryable=True,
            )
        try:
            deleted = await deleter(parsed)
        except ActiveRunExists:
            return _safe_error(
                "conversation_active_run",
                "Stop the active run before deleting this conversation.",
                status_code=409,
                retryable=True,
            )
        if not deleted:
            return _safe_error("conversation_not_found", "Conversation not found.", status_code=404)
        return {"conversation_id": conversation_id, "deleted": True}

    @router.get("/api/conversations/{conversation_id}/messages")
    async def conversation_messages(request: Request, conversation_id: str):
        require_auth(request)
        try:
            parsed = uuid.UUID(conversation_id)
        except ValueError:
            return _safe_error("conversation_not_found", "Conversation not found.", status_code=404)
        service = _service(request)
        conversation = await service.repository.get_conversation(parsed)
        if conversation is None or conversation.get("archived_at") is not None:
            return _safe_error("conversation_not_found", "Conversation not found.", status_code=404)
        return {"conversation_id": conversation_id, "messages": [_message(row) for row in await service.repository.list_messages(parsed)]}

    @router.get("/api/conversations/{conversation_id}/active-run")
    async def conversation_active_run(request: Request, conversation_id: str):
        """Discover durable work after a reload or conversation switch."""

        require_auth(request)
        try:
            parsed = uuid.UUID(conversation_id)
        except ValueError:
            return _safe_error("conversation_not_found", "Conversation not found.", status_code=404)
        service = _service(request)
        conversation = await service.repository.get_conversation(parsed)
        if conversation is None or conversation.get("archived_at") is not None:
            return _safe_error("conversation_not_found", "Conversation not found.", status_code=404)
        getter = getattr(service.repository, "get_active_run", None)
        run = await getter(parsed) if getter is not None else None
        return {"conversation_id": conversation_id, "run": _run(run) if run is not None else None}

    @router.get("/api/conversations/{conversation_id}/draft")
    async def conversation_draft(request: Request, conversation_id: str):
        """Return the conversation's active artifact after a reload."""

        require_auth(request)
        try:
            parsed = uuid.UUID(conversation_id)
        except ValueError:
            return _safe_error("conversation_not_found", "Conversation not found.", status_code=404)
        service = _service(request)
        conversation = await service.repository.get_conversation(parsed)
        if conversation is None or conversation.get("archived_at") is not None:
            return _safe_error("conversation_not_found", "Conversation not found.", status_code=404)
        getter = getattr(service.repository, "get_current_draft", None)
        draft = await getter(conversation_id=parsed, channel_id=int(conversation["channel_id"])) if getter is not None else None
        bundle = await service.research_service.get_bundle(
            workspace_id=service.repository.workspace_id,
            conversation_id=parsed,
            channel_id=int(conversation["channel_id"]),
        )
        referenced = set((_draft(draft) or {}).get("source_ids", []))
        sources = [
            {"id": source.source_id, "title": source.title, "url": source.url}
            for source in (bundle.sources if bundle else [])
            if source.source_id in referenced
        ]
        return {"conversation_id": conversation_id, "draft": _draft(draft), "sources": sources}

    @router.get("/api/drafts/{draft_id}")
    async def get_draft(request: Request, draft_id: str):
        require_auth(request)
        try:
            parsed = uuid.UUID(draft_id)
        except ValueError:
            return _safe_error("draft_not_found", "Draft not found.", status_code=404)
        row = await _service(request).repository.get_draft(parsed)
        if row is None:
            return _safe_error("draft_not_found", "Draft not found.", status_code=404)
        return {"draft": _draft(row)}

    @router.patch("/api/drafts/{draft_id}")
    async def patch_draft(request: Request, draft_id: str):
        require_auth(request)
        require_csrf(request)
        try:
            parsed = uuid.UUID(draft_id)
        except ValueError:
            return _safe_error("draft_not_found", "Draft not found.", status_code=404)
        try:
            payload = DraftPatchRequest.model_validate(await request.json())
        except (ValidationError, ValueError, TypeError):
            return _safe_error("invalid_draft", "Draft details are invalid.", status_code=422)
        service = _service(request)
        try:
            chosen = payload.choose_version if payload.choose_version is not None else payload.restore_version
            if chosen is not None:
                row = await service.repository.choose_draft_version(
                    draft_id=parsed,
                    version=chosen,
                    expected_revision=payload.expected_revision,
                )
            else:
                values = payload.model_dump(exclude={"expected_revision", "restore_version", "choose_version", "save_as_new_version"}, exclude_none=True)
                row = await service.repository.save_draft(
                    draft_id=parsed,
                    payload=values,
                    expected_revision=payload.expected_revision,
                    new_version=True,
                )
        except DraftConflictError as exc:
            local = {key: value for key, value in payload.model_dump(exclude={"expected_revision", "restore_version", "choose_version", "save_as_new_version"}, exclude_none=True).items()}
            return JSONResponse(
                {
                    "error": {"code": exc.code, "message": str(exc), "retryable": True},
                    "server_draft": _draft(exc.current),
                    "local_draft": local,
                    "current_revision": int(exc.current.get("revision", 1)),
                },
                status_code=409,
            )
        except DraftValidationError as exc:
            return _safe_error(exc.code, exc.message, status_code=422)
        except DraftNotFound:
            return _safe_error("draft_not_found", "Draft not found.", status_code=404)
        return {"draft": _draft(row), "saved": True}

    @router.post("/api/drafts/{draft_id}/copied")
    async def copied_draft(request: Request, draft_id: str):
        require_auth(request)
        require_csrf(request)
        try:
            parsed = uuid.UUID(draft_id)
        except ValueError:
            return _safe_error("draft_not_found", "Draft not found.", status_code=404)
        try:
            row = await _service(request).repository.mark_draft_copied(draft_id=parsed)
        except DraftValidationError as exc:
            return _safe_error(exc.code, exc.message, status_code=409)
        except DraftNotFound:
            return _safe_error("draft_not_found", "Draft not found.", status_code=404)
        copied_at = _iso(row.get("copied_at")) or ""
        body = str(row.get("body") or "")
        plain = render_markdown_plain(body)
        html_body = render_markdown_html(body)
        return {"draft": _draft(row), "copied_text": plain, "copied_html": html_body, "copied_at": copied_at}

    @router.get("/api/drafts/{draft_id}/versions")
    async def draft_versions(request: Request, draft_id: str):
        require_auth(request)
        try:
            parsed = uuid.UUID(draft_id)
        except ValueError:
            return _safe_error("draft_not_found", "Draft not found.", status_code=404)
        try:
            versions = await _service(request).repository.list_draft_versions(draft_id=parsed)
        except DraftNotFound:
            return _safe_error("draft_not_found", "Draft not found.", status_code=404)
        return {"draft_id": draft_id, "versions": [_draft_version(row) for row in versions]}

    @router.post("/api/agent")
    async def studio_agent(request: Request):
        context = require_auth(request)
        require_csrf(request)
        setup = _ensure_ready(request)
        if not setup["ready"]:
            return _safe_error("studio_not_ready", "Finish Studio setup before starting an agent run.", status_code=409)
        provider_consent = await _consent(request, context)
        if provider_consent["required"] and not provider_consent["granted"]:
            return _safe_error("provider_consent_required", "Review and confirm the provider disclosure before using Studio.", status_code=409)
        return await _service(request).stream_request(request, await request.body())

    @router.get("/api/runs/{run_id}/events")
    async def run_events(request: Request, run_id: str, after: int = 0):
        require_auth(request)
        service = _service(request)
        try:
            parsed = resolve_run_id(run_id, workspace_id=service.repository.workspace_id)
        except ValueError:
            return _safe_error("run_not_found", "Run not found.", status_code=404)
        try:
            run = await service.repository.get_run(parsed)
            if run is None:
                raise RunNotFound("run not found")
            events = await service.repository.get_events(parsed, after=after)
        except RunNotFound:
            return _safe_error("run_not_found", "Run not found.", status_code=404)
        return {
            "run": _run(run),
            "events": [
                {"id": int(event["id"]), "sequence": int(event["sequence"]), "event_type": event["event_type"], "safe_payload": _public_event_payload(event.get("safe_payload", {}) or {}), "created_at": _iso(event.get("created_at"))}
                for event in events
            ],
        }

    @router.get("/api/runs/{run_id}/details")
    async def run_details(request: Request, run_id: str):
        """Return a quiet, privacy-safe operational summary for one run."""

        require_auth(request)
        service = _service(request)
        try:
            parsed = resolve_run_id(run_id, workspace_id=service.repository.workspace_id)
        except ValueError:
            return _safe_error("run_not_found", "Run not found.", status_code=404)
        try:
            run = await service.repository.get_run(parsed)
            if run is None:
                raise RunNotFound("run not found")
            events = await service.repository.get_events(parsed, after=0)
        except RunNotFound:
            return _safe_error("run_not_found", "Run not found.", status_code=404)
        public_run = _run(run)
        error = None
        if public_run["error_code"]:
            code, message, retryable = safe_error_for_code(public_run["error_code"], public_run["error_message"])
            error = {"code": code, "message": message, "retryable": retryable}
        return {
            "run": public_run,
            "details": {
                "provider": public_run["provider"],
                "requested_model": public_run["requested_model"],
                "actual_model": public_run["actual_model"],
                "prompt_version": PROMPT_VERSION,
                "status": public_run["status"],
                "stage": public_run["stage"],
                "duration_ms": public_run["duration_ms"],
                "usage": public_run["usage"],
                "activity": _run_activity(events),
                "error": error,
                "diagnostics": public_run.get("diagnostics"),
            },
            "events": [
                {
                    "id": int(event["id"]),
                    "sequence": int(event["sequence"]),
                    "event_type": event["event_type"],
                    "safe_payload": _public_event_payload(event.get("safe_payload", {}) or {}),
                    "created_at": _iso(event.get("created_at")),
                }
                for event in events[-100:]
            ],
        }

    @router.post("/api/runs/{run_id}/cancel")
    async def cancel_run(request: Request, run_id: str):
        require_auth(request)
        require_csrf(request)
        service = _service(request)
        try:
            parsed = resolve_run_id(run_id, workspace_id=service.repository.workspace_id)
        except ValueError:
            return _safe_error("run_not_found", "Run not found.", status_code=404)
        try:
            run = await service.cancel(parsed)
        except RunNotFound:
            return _safe_error("run_not_found", "Run not found.", status_code=404)
        return {"run": _run(run), "run_id": run_id, "status": "cancel_requested" if run["status"] in {"queued", "running"} else run["status"]}

    return router
