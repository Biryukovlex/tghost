"""Workspace-scoped persistence for the M2 Studio slice."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .drafts import (
    DraftConflictError,
    DraftInput,
    DraftValidationError,
    MAX_DRAFT_CHARS,
    input_from_row,
    telegram_character_count,
    validate_draft_input,
)


class SystemPromptConflict(ValueError):
    """Another editor changed the channel instructions."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _datetime(value: Any) -> datetime | None:
    """Convert JSON-facing ISO timestamps back to timezone-aware datetimes."""

    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _json_value(value: Any, default: Any) -> Any:
    """Decode JSONB values consistently for asyncpg and in-memory rows."""

    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return value


def _draft_public(row: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe draft row with server-derived metadata."""

    from .markdown import render_markdown_html, render_markdown_plain

    data = dict(row)
    list_defaults: tuple[tuple[str, list[Any]], ...] = (
        ("media_ids", []),
        ("source_links", []),
        ("source_ids", []),
        ("claim_support", []),
        ("assumptions", []),
        ("warnings", []),
        ("channel_evidence", []),
        ("web_evidence", []),
    )
    for key, default in list_defaults:
        data[key] = _json_value(data.get(key), default)
    data["id"] = str(data["id"])
    if data.get("workspace_id") is not None:
        data["workspace_id"] = str(data["workspace_id"])
    if data.get("conversation_id") is not None:
        data["conversation_id"] = str(data["conversation_id"])
    if data.get("analysis_id") is not None:
        data["analysis_id"] = str(data["analysis_id"])
    if data.get("story_cluster_id") is not None:
        data["story_cluster_id"] = str(data["story_cluster_id"])
    for key in ("created_at", "updated_at", "copied_at"):
        if data.get(key) is not None:
            data[key] = _iso(data[key])
    body = str(data.get("body") or "")
    data["body"] = body
    data["title"] = str(data.get("working_title") or "Untitled draft")
    plain = render_markdown_plain(body)
    data["body_plain"] = plain
    data["body_html"] = render_markdown_html(body)
    count = telegram_character_count(plain)
    data["plain_character_count"] = count
    data["character_count"] = count
    data["over_limit"] = count > MAX_DRAFT_CHARS
    data["warning_threshold"] = count >= 3800
    data["revision"] = int(data.get("revision") or 1)
    data["current_version"] = int(data.get("current_version") or 1)
    data["current_version_origin"] = str(data.get("current_version_origin") or "generated")
    data["channel_id"] = int(data["channel_id"])
    return data


def _version_public(row: dict[str, Any]) -> dict[str, Any]:
    from .markdown import render_markdown_plain

    data = dict(row)
    data["id"] = int(data["id"])
    data["draft_id"] = str(data["draft_id"])
    data["title"] = data.get("working_title")
    data["source_links"] = _json_value(data.get("source_links"), [])
    data["media_ids"] = _json_value(data.get("media_ids"), [])
    data["version"] = int(data["version"])
    data["body"] = str(data.get("body") or "")
    # New rows persist the authoritative UTF-16 count.  Recompute only for
    # legacy rows that predate that field.
    if data.get("character_count") is None:
        data["character_count"] = telegram_character_count(render_markdown_plain(data["body"]))
    else:
        data["character_count"] = int(data["character_count"])
    if data.get("created_at") is not None:
        data["created_at"] = _iso(data["created_at"])
    return data


def _draft_values(payload: dict[str, Any] | DraftInput, *, known_source_ids: set[str] | frozenset[str], require_sources: bool = True) -> dict[str, Any]:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "media_ids"}
    validated = validate_draft_input(payload, known_source_ids=known_source_ids, require_sources=False)
    values = validated.model_dump(mode="json")
    values["claim_support"] = [item.model_dump(mode="json") for item in validated.claim_support]
    return values


def _preserve_server_verification_on_user_edit(current: dict[str, Any], values: dict[str, Any]) -> None:
    """Never accept a verified flag supplied by an editable draft payload.

    A no-op edit may retain server-verified mappings, but any change to the
    post, source selection, or mapping substance invalidates them together.
    """

    prior = current.get("claim_support") or []
    incoming = values.get("claim_support") or []
    def substance(item: dict[str, Any]) -> tuple[Any, ...]:
        return (
            item.get("claim"), item.get("source_ids"),
            item.get("passage"), item.get("passages"),
        )

    unchanged = (
        values.get("body") == current.get("body")
        and values.get("source_ids") == current.get("source_ids")
        and len(incoming) == len(prior)
        and all(
            isinstance(new, dict) and isinstance(old, dict)
            and substance(new) == substance(old)
            for new, old in zip(incoming, prior)
        )
    )
    values["claim_support"] = [
        {**item, "verified": bool(prior[index].get("verified")) if unchanged else False}
        for index, item in enumerate(incoming)
    ]


def _diagnostic_int(value: Any) -> int | None:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if 100 <= number <= 599 else None


def _draft_uuid(value: Any) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise DraftNotFound("draft id is invalid") from exc


class StudioRepositoryError(RuntimeError):
    code = "studio_repository_error"


class ConversationNotFound(StudioRepositoryError):
    code = "conversation_not_found"


class RunNotFound(StudioRepositoryError):
    code = "run_not_found"


class DraftNotFound(StudioRepositoryError):
    code = "draft_not_found"


class ActiveRunExists(StudioRepositoryError):
    code = "run_already_active"


class RunClaimLost(StudioRepositoryError):
    code = "run_claim_lost"


class StudioRepositoryProtocol(Protocol):
    @property
    def workspace_id(self) -> Any: ...

    async def list_channels(self) -> list[dict[str, Any]]: ...
    async def channel_context(self, channel_id: int) -> dict[str, Any]: ...
    async def performance_rows(self, channel_id: int, limit: int = 2000) -> list[dict[str, Any]]: ...
    async def profile_evidence_ids(self, channel_id: int, post_ids: list[int]) -> set[int]: ...
    async def get_system_prompt(self, channel_id: int) -> str: ...
    async def set_system_prompt(self, channel_id: int, value: str, *, expected_prompt: str | None = None) -> str: ...
    async def get_profile(self, channel_id: int) -> dict[str, Any] | None: ...
    async def get_analysis(self, analysis_id: uuid.UUID) -> dict[str, Any] | None: ...
    async def create_analysis(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    async def upsert_profile(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    async def upsert_profile_text(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    async def create_profile_change(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    async def get_profile_change(self, change_id: uuid.UUID, *, channel_id: int | None = None) -> dict[str, Any] | None: ...
    async def confirm_profile_change(self, change_id: uuid.UUID) -> dict[str, Any] | None: ...
    async def apply_profile_change(self, change_id: uuid.UUID) -> dict[str, Any]: ...
    async def list_conversations(self, *, channel_id: int, include_archived: bool = False) -> list[dict[str, Any]]: ...
    async def get_conversation(self, conversation_id: uuid.UUID) -> dict[str, Any] | None: ...
    async def create_conversation(self, *, channel_id: int, title: str | None = None) -> dict[str, Any]: ...
    async def rename_conversation(self, conversation_id: uuid.UUID, *, title: str, only_default: bool = False) -> Any: ...
    async def delete_conversation(self, conversation_id: uuid.UUID) -> bool: ...
    async def list_messages(self, conversation_id: uuid.UUID) -> list[dict[str, Any]]: ...
    async def append_message(self, *, conversation_id: uuid.UUID, role: str, content: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]: ...
    async def create_draft(self, *, conversation_id: uuid.UUID, channel_id: int, payload: dict[str, Any], origin: str = "generated", instruction: str = "") -> dict[str, Any]: ...
    async def get_draft(self, draft_id: uuid.UUID, *, conversation_id: uuid.UUID | None = None, channel_id: int | None = None) -> dict[str, Any] | None: ...
    async def get_current_draft(self, *, conversation_id: uuid.UUID, channel_id: int) -> dict[str, Any] | None: ...
    async def update_draft(self, *, draft_id: uuid.UUID, payload: dict[str, Any], expected_revision: int | None = None, origin: str = "user_edit", instruction: str = "", new_version: bool = True) -> dict[str, Any]: ...
    async def save_draft(self, *, draft_id: uuid.UUID, payload: dict[str, Any], expected_revision: int, instruction: str = "", new_version: bool = False) -> dict[str, Any]: ...
    async def revise_draft(self, *, draft_id: uuid.UUID, payload: dict[str, Any], instruction: str = "") -> dict[str, Any]: ...
    async def list_draft_versions(
        self,
        *,
        draft_id: uuid.UUID,
        conversation_id: uuid.UUID | None = None,
        channel_id: int | None = None,
    ) -> list[dict[str, Any]]: ...
    async def restore_draft_version(self, *, draft_id: uuid.UUID, version: int, expected_revision: int, instruction: str = "") -> dict[str, Any]: ...
    async def choose_draft_version(self, *, draft_id: uuid.UUID, version: int, expected_revision: int) -> dict[str, Any]: ...
    async def mark_draft_copied(self, *, draft_id: uuid.UUID) -> dict[str, Any]: ...
    async def persist_research_bundle(self, *, conversation_id: uuid.UUID, channel_id: int, bundle: dict[str, Any]) -> dict[str, int]: ...
    async def get_research_bundle(self, *, conversation_id: uuid.UUID, channel_id: int) -> dict[str, Any] | None: ...
    async def get_research_sources(self, *, conversation_id: uuid.UUID, channel_id: int, source_ids: list[str]) -> list[dict[str, Any]]: ...
    async def record_research_event(self, *, conversation_id: uuid.UUID, channel_id: int, event: dict[str, Any]) -> dict[str, Any]: ...
    async def list_research_events(self, *, conversation_id: uuid.UUID, channel_id: int, limit: int = 50) -> list[dict[str, Any]]: ...
    async def create_run(self, *, conversation_id: uuid.UUID, user_message_id: int, requested_model: str, provider: str = "openrouter", run_id: uuid.UUID | None = None) -> dict[str, Any]: ...
    async def claim_run(self, run_id: uuid.UUID, *, worker_id: str, lease_seconds: int = 120) -> dict[str, Any] | None: ...
    async def renew_run_lease(self, run_id: uuid.UUID, *, worker_id: str, lease_seconds: int = 120) -> dict[str, Any] | None: ...
    async def get_run(self, run_id: uuid.UUID) -> dict[str, Any] | None: ...
    async def get_active_run(self, conversation_id: uuid.UUID) -> dict[str, Any] | None: ...
    async def get_events(self, run_id: uuid.UUID, *, after: int = 0) -> list[dict[str, Any]]: ...
    async def set_run_status(self, run_id: uuid.UUID, *, status: str, stage: str | None = None, error_code: str | None = None, error_message: str | None = None, actual_model: str | None = None, usage: dict[str, Any] | None = None, worker_id: str | None = None, diagnostics: dict[str, Any] | None = None) -> dict[str, Any]: ...
    async def append_event(self, run_id: uuid.UUID, *, event_type: str, safe_payload: dict[str, Any] | None = None, result_content: str | None = None) -> dict[str, Any]: ...
    async def list_tool_result_logs(self, *, limit: int = 50, offset: int = 0, query: str | None = None, channel_id: int | None = None, status: str | None = None, include_runs: bool = False) -> list[dict[str, Any]]: ...
    async def count_tool_result_logs(self, *, query: str | None = None, channel_id: int | None = None, status: str | None = None, include_runs: bool = False) -> int: ...
    async def request_cancel(self, run_id: uuid.UUID) -> dict[str, Any]: ...
    async def mark_stale_runs_interrupted(self, *, queued_grace_seconds: int = 60) -> int: ...


class StudioRepository:
    """PostgreSQL repository; every query is scoped to ``db.workspace_id``."""

    def __init__(self, db) -> None:
        self.db = db

    @property
    def workspace_id(self):
        return self.db._workspace()

    async def list_channels(self) -> list[dict[str, Any]]:
        return await self.db.get_channels()

    async def get_system_prompt(self, channel_id: int) -> str:
        result = await self.db._execute(
            """SELECT studio_system_prompt FROM channels
                WHERE workspace_id=:workspace_id AND id=:channel_id AND active=true""",
            {"channel_id": int(channel_id)},
        )
        return str(result.scalar_one_or_none() or "")

    async def set_system_prompt(self, channel_id: int, value: str, *, expected_prompt: str | None = None) -> str:
        if len(value) > 12000:
            raise ValueError("Studio instructions exceed 12000 characters")
        result = await self.db._execute(
            """UPDATE channels SET studio_system_prompt=:prompt
                WHERE workspace_id=:workspace_id AND id=:channel_id AND active=true
                  AND (CAST(:expected_prompt AS text) IS NULL OR COALESCE(studio_system_prompt, '')=:expected_prompt)
                RETURNING id""",
            {"channel_id": int(channel_id), "prompt": value, "expected_prompt": expected_prompt},
        )
        if result.scalar_one_or_none() is None:
            if expected_prompt is not None:
                raise SystemPromptConflict("System Prompt changed in another tab")
            raise ConversationNotFound("channel is not part of the active workspace")
        return value

    async def channel_context(self, channel_id: int) -> dict[str, Any]:
        workspace_id = self.workspace_id
        async with self.db.sessions.session() as session:
            summary = (
                await session.execute(
                    text(
                        """SELECT c.id AS channel_id, c.identifier, c.title,
                                  COUNT(p.id) AS tracked_posts,
                                  MIN(p.posted_at) AS oldest_post,
                                  MAX(p.posted_at) AS newest_post
                             FROM channels c LEFT JOIN posts p
                               ON p.workspace_id=c.workspace_id AND p.channel_id=c.id
                            WHERE c.workspace_id=:workspace_id AND c.id=:channel_id
                            GROUP BY c.id, c.identifier, c.title"""
                    ),
                    {"workspace_id": workspace_id, "channel_id": channel_id},
                )
            ).mappings().first()
            if not summary:
                raise ConversationNotFound("channel is not part of the active workspace")
            recent = (
                await session.execute(
                    text(
                        """SELECT message_id, posted_at, LEFT(text, 2000) AS text
                             FROM posts
                            WHERE workspace_id=:workspace_id AND channel_id=:channel_id
                            ORDER BY posted_at DESC, id DESC LIMIT 6"""
                    ),
                    {"workspace_id": workspace_id, "channel_id": channel_id},
                )
            ).mappings().all()
        return {
            "channel_id": int(summary["channel_id"]),
            "identifier": summary["identifier"],
            "title": summary["title"] or "",
            "tracked_posts": int(summary["tracked_posts"] or 0),
            "oldest_post": summary["oldest_post"],
            "newest_post": summary["newest_post"],
            "recent_posts": [
                {
                    "message_id": int(row["message_id"]),
                    "posted_at": row["posted_at"],
                    "text": row["text"] or "",
                }
                for row in recent
            ],
            "note": "Read-only channel context; discussion comment bodies are excluded.",
        }

    async def performance_rows(self, channel_id: int, limit: int = 2000) -> list[dict[str, Any]]:
        """Return latest snapshot metrics without joining comment bodies."""

        workspace_id = self.workspace_id
        bounded = max(1, min(int(limit), 10_000))
        async with self.db.sessions.session() as session:
            rows = (
                await session.execute(
                    text(
                        """WITH latest AS (
                               SELECT DISTINCT ON (workspace_id, post_id)
                                      workspace_id, post_id, taken_at, views, comments, reactions, shares
                                 FROM snapshots
                                WHERE workspace_id=:workspace_id
                                ORDER BY workspace_id, post_id, id DESC
                           )
                           SELECT p.id AS post_id, p.message_id, p.channel_id, p.posted_at,
                                  p.text, p.formatting_entities, p.is_deleted, l.taken_at AS snapshot_at,
                                  l.views, l.comments, l.reactions, l.shares,
                                  true AS has_snapshot
                             FROM posts p JOIN channels c
                               ON c.workspace_id=p.workspace_id AND c.id=p.channel_id
                             JOIN latest l
                               ON l.workspace_id=p.workspace_id AND l.post_id=p.id
                            WHERE p.workspace_id=:workspace_id AND p.channel_id=:channel_id
                            ORDER BY p.posted_at DESC, p.id DESC
                            LIMIT :limit"""
                    ),
                    {"workspace_id": workspace_id, "channel_id": channel_id, "limit": bounded},
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def profile_evidence_ids(self, channel_id: int, post_ids: list[int]) -> set[int]:
        """Validate the small supplied sample against the full scoped archive."""
        if not post_ids:
            return set()
        result = await self.db._execute(
            """SELECT p.id FROM posts p
                 WHERE p.workspace_id=:workspace_id AND p.channel_id=:channel_id
                   AND p.id = ANY(CAST(:post_ids AS bigint[]))
                   AND EXISTS (SELECT 1 FROM snapshots s
                                WHERE s.workspace_id=p.workspace_id AND s.post_id=p.id)""",
            {"channel_id": channel_id, "post_ids": list(set(post_ids))},
        )
        return {int(row["id"]) for row in result.mappings().all()}

    # ---------- M3 analyses / profiles / consent ----------

    async def get_analysis_by_hash(self, channel_id: int, input_hash: str) -> dict[str, Any] | None:
        result = await self.db._execute(
            """SELECT * FROM studio_analyses
                WHERE workspace_id=:workspace_id AND channel_id=:channel_id AND input_hash=:input_hash
                ORDER BY created_at DESC LIMIT 1""",
            {"channel_id": channel_id, "input_hash": input_hash},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def get_analysis(self, analysis_id: uuid.UUID) -> dict[str, Any] | None:
        result = await self.db._execute(
            "SELECT * FROM studio_analyses WHERE workspace_id=:workspace_id AND id=:analysis_id",
            {"analysis_id": analysis_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def create_analysis(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace_id = self.workspace_id
        analysis_id = payload.get("id") or uuid.uuid4()
        values = {
            "id": analysis_id,
            "workspace_id": workspace_id,
            "channel_id": int(payload["channel_id"]),
            "analysis_start": payload.get("analysis_start"),
            "analysis_end": payload.get("analysis_end"),
            "eligible_post_count": int(payload.get("eligible_post_count", 0)),
            "style_eligible_post_count": int(payload.get("style_eligible_post_count", 0)),
            "evidence_post_ids": json.dumps(payload.get("evidence_post_ids", [])),
            "scoring_version": payload.get("scoring_version", "m3.v1"),
            "scoring_weights": json.dumps(payload.get("scoring_weights", {})),
            "topic_insights": json.dumps(payload.get("topic_insights", [])),
            "style_insights": json.dumps(payload.get("style_insights", {})),
            "limitations": json.dumps(payload.get("limitations", [])),
            "confidence": payload.get("confidence", "low"),
            "confidence_score": float(payload.get("confidence_score", 0.25)),
            "input_hash": payload["input_hash"],
            "provider": payload.get("provider", "local"),
            "model": payload.get("model", ""),
            "prompt_version": payload.get("prompt_version", "m3.profile.v1"),
        }
        async with self.db.sessions.session() as session:
            await session.execute(
                text(
                    """INSERT INTO studio_analyses(
                           id, workspace_id, channel_id, analysis_start, analysis_end,
                           eligible_post_count, style_eligible_post_count, evidence_post_ids,
                           scoring_version, scoring_weights, topic_insights, style_insights,
                           limitations, confidence, confidence_score, input_hash, provider,
                           model, prompt_version
                       ) SELECT :id, :workspace_id, id, :analysis_start, :analysis_end,
                           :eligible_post_count, :style_eligible_post_count, CAST(:evidence_post_ids AS jsonb),
                           :scoring_version, CAST(:scoring_weights AS jsonb), CAST(:topic_insights AS jsonb),
                           CAST(:style_insights AS jsonb), CAST(:limitations AS jsonb), :confidence,
                           :confidence_score, :input_hash, :provider, :model, :prompt_version
                       FROM channels WHERE workspace_id=:workspace_id AND id=:channel_id
                       ON CONFLICT (workspace_id, channel_id, input_hash) DO NOTHING"""
                ),
                values,
            )
            row = (
                await session.execute(
                    text(
                        """SELECT * FROM studio_analyses
                            WHERE workspace_id=:workspace_id AND channel_id=:channel_id AND input_hash=:input_hash
                            ORDER BY created_at DESC LIMIT 1"""
                    ),
                    values,
                )
            ).mappings().first()
            if row is None:
                await session.rollback()
                raise ConversationNotFound("channel is not part of the active workspace")
            await session.commit()
        return dict(row)

    async def get_profile(self, channel_id: int) -> dict[str, Any] | None:
        result = await self.db._execute(
            """SELECT p.*, COALESCE(p.style_profile->'evidence_post_ids', a.evidence_post_ids) AS evidence_post_ids FROM studio_profiles p
                LEFT JOIN studio_analyses a ON a.id=p.current_analysis_id
                  AND a.workspace_id=p.workspace_id AND a.channel_id=p.channel_id
                WHERE p.workspace_id=:workspace_id AND p.channel_id=:channel_id""",
            {"channel_id": channel_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def upsert_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        values = {
            "id": payload.get("id") or uuid.uuid4(),
            "workspace_id": self.workspace_id,
            "channel_id": int(payload["channel_id"]),
            "topics": json.dumps(payload.get("topics", [])),
            "style_profile": json.dumps(payload.get("style_profile", {})),
            "editorial_rules": json.dumps(payload.get("editorial_rules", {})),
            "confidence": payload.get("confidence", "low"),
            "current_analysis_id": payload.get("current_analysis_id"),
            "version": int(payload.get("version", 1)),
        }
        result = await self.db._execute(
            """INSERT INTO studio_profiles(
                       id, workspace_id, channel_id, topics, style_profile,
                       editorial_rules, confidence, current_analysis_id, version
                   ) VALUES (:id, :workspace_id, :channel_id, CAST(:topics AS jsonb),
                       CAST(:style_profile AS jsonb), CAST(:editorial_rules AS jsonb),
                       :confidence, :current_analysis_id, :version)
               ON CONFLICT (workspace_id, channel_id) DO UPDATE SET
                   topics=EXCLUDED.topics, style_profile=EXCLUDED.style_profile,
                   editorial_rules=EXCLUDED.editorial_rules, confidence=EXCLUDED.confidence,
                   current_analysis_id=EXCLUDED.current_analysis_id,
                   version=studio_profiles.version + 1, updated_at=now()
               RETURNING *""",
            values,
        )
        row = result.mappings().first()
        if row is None:
            raise ConversationNotFound("channel is not part of the active workspace")
        return dict(row)

    async def upsert_profile_text(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Validate and clean text fields
        def clean(v: str) -> str:
            lines = [line.rstrip() for line in str(v or "").splitlines()]
            # Drop empty lines
            return "\n".join(line for line in lines if line.strip() != "")
        topics_text = clean(payload.get("topics_text", ""))
        editorial_text = clean(payload.get("editorial_text", ""))
        style_text = clean(payload.get("style_text", ""))
        for name, txt in [("topics_text", topics_text), ("editorial_text", editorial_text), ("style_text", style_text)]:
            if len(txt) > 2000:
                raise ValueError(f"{name} exceeds 2000 characters")
            if len(txt.splitlines()) > 60:
                raise ValueError(f"{name} exceeds 60 lines")
        # Check version
        channel_id = int(payload["channel_id"])
        expected = payload.get("expected_version")
        # For new profile, expected 0
        current = await self.get_profile(channel_id)
        current_version = int(current["version"]) if current else 0
        if expected is not None and int(expected) != current_version:
            # Return conflict via exception to be handled by routes
            raise DraftConflictError(current or {}, expected_revision=int(expected))
        built_from = int(payload.get("built_from_posts", 0) or 0)
        evidence = payload.get("evidence_post_ids")
        style_profile = dict((current or {}).get("style_profile") or {})
        if evidence is not None:
            style_profile["evidence_post_ids"] = evidence
        # Determine version
        new_version = current_version + 1
        values = {
            # ``id`` has no server default; the INSERT tuple must carry one even
            # when ON CONFLICT ends up updating the existing row.
            "id": uuid.uuid4(),
            "workspace_id": self.workspace_id,
            "channel_id": channel_id,
            "topics_text": topics_text,
            "editorial_text": editorial_text,
            "style_text": style_text,
            "built_from_posts": built_from,
            "version": new_version,
            "style_profile": json.dumps(style_profile),
            "expected_version": current_version,
        }
        # A manual edit (built_from_posts == 0) keeps the previous build
        # provenance; only a save that carries build output refreshes it.
        result = await self.db._execute(
            """INSERT INTO studio_profiles(
                       id, workspace_id, channel_id, topics_text, editorial_text, style_text,
                       built_from_posts, built_at, version, style_profile
                   ) VALUES (
                       :id, :workspace_id, :channel_id, :topics_text, :editorial_text, :style_text,
                       :built_from_posts, CASE WHEN :built_from_posts > 0 THEN now() ELSE NULL END, :version, CAST(:style_profile AS jsonb)
                   )
               ON CONFLICT (workspace_id, channel_id) DO UPDATE SET
                   topics_text=EXCLUDED.topics_text, editorial_text=EXCLUDED.editorial_text, style_text=EXCLUDED.style_text,
                   built_from_posts=CASE WHEN EXCLUDED.built_from_posts > 0 THEN EXCLUDED.built_from_posts ELSE studio_profiles.built_from_posts END,
                   built_at=CASE WHEN EXCLUDED.built_from_posts > 0 THEN now() ELSE studio_profiles.built_at END,
                   version=EXCLUDED.version, updated_at=now(), style_profile=EXCLUDED.style_profile
               WHERE studio_profiles.version=:expected_version
               RETURNING *""",
            values,
        )
        row = result.mappings().first()
        if row is None:
            raise DraftConflictError(await self.get_profile(channel_id) or {}, expected_revision=current_version)
        return dict(row)

    async def create_profile_change(self, payload: dict[str, Any]) -> dict[str, Any]:
        change_id = payload.get("id") or uuid.uuid4()
        result = await self.db._execute(
            """INSERT INTO studio_profile_changes(
                       id, workspace_id, channel_id, base_profile_version,
                       proposed_topics, style_diff, editorial_rules, reason,
                       status, requested_by
                   ) SELECT :id, :workspace_id, id, :base_profile_version,
                       CAST(:proposed_topics AS jsonb), CAST(:style_diff AS jsonb),
                       CAST(:editorial_rules AS jsonb), :reason, :status, :requested_by
                   FROM channels WHERE workspace_id=:workspace_id AND id=:channel_id
                   RETURNING *""",
            {
                "id": change_id,
                "workspace_id": self.workspace_id,
                "channel_id": int(payload["channel_id"]),
                "base_profile_version": int(payload.get("base_profile_version", 1)),
                "proposed_topics": json.dumps(payload.get("proposed_topics", [])),
                "style_diff": json.dumps(payload.get("style_diff", {})),
                "editorial_rules": json.dumps(payload.get("editorial_rules", {})),
                "reason": str(payload.get("reason", ""))[:4000],
                "status": payload.get("status", "proposed"),
                "requested_by": payload.get("requested_by"),
            },
        )
        row = result.mappings().first()
        if row is None:
            raise ConversationNotFound("channel is not part of the active workspace")
        return dict(row)

    async def get_profile_change(
        self, change_id: uuid.UUID, *, channel_id: int | None = None
    ) -> dict[str, Any] | None:
        filters = ["workspace_id=:workspace_id", "id=:change_id"]
        params: dict[str, Any] = {"change_id": change_id}
        if channel_id is not None:
            filters.append("channel_id=:channel_id")
            params["channel_id"] = int(channel_id)
        result = await self.db._execute(
            f"SELECT * FROM studio_profile_changes WHERE {' AND '.join(filters)}",
            params,
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def confirm_profile_change(self, change_id: uuid.UUID) -> dict[str, Any] | None:
        result = await self.db._execute(
            """UPDATE studio_profile_changes SET status='confirmed', confirmed_at=COALESCE(confirmed_at, now())
                WHERE workspace_id=:workspace_id AND id=:change_id AND status IN ('proposed', 'confirmed')
                RETURNING *""",
            {"change_id": change_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def apply_profile_change(self, change_id: uuid.UUID) -> dict[str, Any]:
        workspace_id = self.workspace_id
        async with self.db.sessions.session() as session:
            change = (
                await session.execute(
                    text("SELECT * FROM studio_profile_changes WHERE workspace_id=:workspace_id AND id=:change_id FOR UPDATE"),
                    {"workspace_id": workspace_id, "change_id": change_id},
                )
            ).mappings().first()
            if change is None or change["status"] != "confirmed":
                raise StudioRepositoryError("profile change requires explicit confirmation")
            profile = (
                await session.execute(
                    text("SELECT * FROM studio_profiles WHERE workspace_id=:workspace_id AND channel_id=:channel_id FOR UPDATE"),
                    {"workspace_id": workspace_id, "channel_id": change["channel_id"]},
                )
            ).mappings().first()
            if profile is None:
                raise ConversationNotFound("profile is not part of the active workspace")
            if int(profile["version"]) != int(change["base_profile_version"]):
                raise StudioRepositoryError("profile change is based on an outdated version")
            proposed_topics = change["proposed_topics"] or []
            if proposed_topics and isinstance(proposed_topics[0], str):
                proposed_topics = [{"name": topic, "claim": "Added or confirmed by the channel owner."} for topic in proposed_topics]
            row = (
                await session.execute(
                    text(
                        """UPDATE studio_profiles SET topics=:topics, style_profile=:style_profile,
                               editorial_rules=:editorial_rules, version=version+1, updated_at=now()
                            WHERE workspace_id=:workspace_id AND channel_id=:channel_id RETURNING *"""
                    ),
                    {
                        "workspace_id": workspace_id,
                        "channel_id": change["channel_id"],
                        "topics": json.dumps(proposed_topics),
                        "style_profile": json.dumps(change["style_diff"] or profile["style_profile"]),
                        "editorial_rules": json.dumps(change["editorial_rules"] or profile["editorial_rules"]),
                    },
                )
            ).mappings().one()
            await session.execute(
                text("UPDATE studio_profile_changes SET status='applied', applied_at=now() WHERE workspace_id=:workspace_id AND id=:change_id"),
                {"workspace_id": workspace_id, "change_id": change_id},
            )
            await session.commit()
        return dict(row)

    async def get_provider_consent(self, *, provider: str, configuration_fingerprint: str, user_id: Any | None = None) -> dict[str, Any] | None:
        filters = " AND user_id=:user_id" if user_id is not None else ""
        result = await self.db._execute(
            f"""SELECT * FROM provider_consents
                  WHERE workspace_id=:workspace_id AND provider=:provider
                    AND configuration_fingerprint=:configuration_fingerprint{filters}
                  ORDER BY granted_at DESC NULLS LAST LIMIT 1""",
            {"provider": provider, "configuration_fingerprint": configuration_fingerprint, "user_id": user_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def grant_provider_consent(self, *, user_id: Any, provider: str, configuration_fingerprint: str) -> dict[str, Any]:
        result = await self.db._execute(
            """INSERT INTO provider_consents(
                       workspace_id, user_id, provider, configuration_fingerprint,
                       allowed, granted_at, revoked_at
                   ) VALUES (:workspace_id, :user_id, :provider, :configuration_fingerprint,
                       true, now(), NULL)
               ON CONFLICT (workspace_id, user_id, provider) DO UPDATE SET
                   configuration_fingerprint=EXCLUDED.configuration_fingerprint,
                   allowed=true, granted_at=now(), revoked_at=NULL
               RETURNING *""",
            {"user_id": user_id, "provider": provider, "configuration_fingerprint": configuration_fingerprint},
        )
        return dict(result.mappings().one())

    async def revoke_provider_consent(self, *, user_id: Any, provider: str) -> dict[str, Any] | None:
        result = await self.db._execute(
            """UPDATE provider_consents SET allowed=false, revoked_at=now()
                WHERE workspace_id=:workspace_id AND user_id=:user_id AND provider=:provider
                RETURNING *""",
            {"user_id": user_id, "provider": provider},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def create_conversation(self, *, channel_id: int, title: str | None = None) -> dict[str, Any]:
        workspace_id = self.workspace_id
        conversation_id = uuid.uuid4()
        safe_title = (title or "New conversation").strip()[:160] or "New conversation"
        async with self.db.sessions.session() as session:
            try:
                await session.execute(
                    text(
                        """INSERT INTO studio_conversations(id, workspace_id, channel_id, title)
                           SELECT :id, :workspace_id, id, :title FROM channels
                            WHERE workspace_id=:workspace_id AND id=:channel_id AND active=true"""
                    ),
                    {
                        "id": conversation_id,
                        "workspace_id": workspace_id,
                        "channel_id": channel_id,
                        "title": safe_title,
                    },
                )
                row = (
                    await session.execute(
                        text(
                            """SELECT c.*, ch.identifier AS channel_identifier,
                                      ch.title AS channel_title
                                 FROM studio_conversations c JOIN channels ch
                                   ON ch.workspace_id=c.workspace_id AND ch.id=c.channel_id
                                WHERE c.workspace_id=:workspace_id AND c.id=:id"""
                        ),
                        {"workspace_id": workspace_id, "id": conversation_id},
                    )
                ).mappings().first()
                if row is None:
                    raise ConversationNotFound("channel is not part of the active workspace")
                await session.commit()
            except Exception:
                await session.rollback()
                raise
        return dict(row)

    async def list_conversations(self, *, channel_id: int, include_archived: bool = False) -> list[dict[str, Any]]:
        result = await self.db._execute(
            """SELECT c.*, ch.identifier AS channel_identifier, ch.title AS channel_title
                 FROM studio_conversations c JOIN channels ch
                   ON ch.workspace_id=c.workspace_id AND ch.id=c.channel_id
                WHERE c.workspace_id=:workspace_id AND c.channel_id=:channel_id
                  AND (:include_archived OR c.archived_at IS NULL)
                ORDER BY c.updated_at DESC, c.created_at DESC""",
            {"channel_id": int(channel_id), "include_archived": bool(include_archived)},
        )
        return [dict(row) for row in result.mappings().all()]

    async def get_conversation(self, conversation_id: uuid.UUID) -> dict[str, Any] | None:
        result = await self.db._execute(
            """SELECT c.*, ch.identifier AS channel_identifier, ch.title AS channel_title
                 FROM studio_conversations c JOIN channels ch
                   ON ch.workspace_id=c.workspace_id AND ch.id=c.channel_id
                WHERE c.workspace_id=:workspace_id AND c.id=:conversation_id""",
            {"conversation_id": conversation_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def rename_conversation(self, conversation_id: uuid.UUID, *, title: str, only_default: bool = False):
        async with self.db.sessions.session() as session:
            await session.execute(text("""UPDATE studio_conversations SET title=:title, updated_at=now()
                WHERE workspace_id=:workspace_id AND id=:id AND archived_at IS NULL
                  AND (NOT :only_default OR title='New conversation')"""),
                {"workspace_id": self.workspace_id, "id": conversation_id, "title": title,
                 "only_default": only_default})
            await session.commit()
        return await self.get_conversation(conversation_id)

    # ---------- M5 drafts and immutable versions ----------

    @staticmethod
    def _draft_origin(origin: str) -> str:
        value = str(origin or "generated")
        if value not in {"generated", "regenerated", "user_edit"}:
            raise DraftValidationError("invalid_draft_origin", "Draft version origin is invalid.", field="origin")
        return value

    async def _draft_source_ids(self, session, *, conversation_id: uuid.UUID) -> set[str]:
        rows = (
            await session.execute(
                text(
                    """SELECT id FROM studio_sources
                        WHERE workspace_id=:workspace_id AND conversation_id=:conversation_id"""
                ),
                {"workspace_id": self.workspace_id, "conversation_id": conversation_id},
            )
        ).scalars().all()
        return {str(value) for value in rows}

    async def _validate_draft_scope(self, session, *, conversation_id: uuid.UUID, channel_id: int) -> dict[str, Any]:
        row = (
            await session.execute(
                text(
                    """SELECT id, channel_id, archived_at FROM studio_conversations
                        WHERE workspace_id=:workspace_id AND id=:conversation_id
                        FOR UPDATE"""
                ),
                {"workspace_id": self.workspace_id, "conversation_id": conversation_id},
            )
        ).mappings().first()
        if row is None or row["archived_at"] is not None or int(row["channel_id"]) != int(channel_id):
            raise ConversationNotFound("conversation is not part of the active workspace/channel")
        return dict(row)

    async def _validate_draft_references(self, session, *, conversation_id: uuid.UUID, payload: dict[str, Any]) -> set[str]:
        known = await self._draft_source_ids(session, conversation_id=conversation_id)
        validated = validate_draft_input({key: value for key, value in payload.items() if key != "media_ids"}, known_source_ids=known, require_sources=False)
        if validated.story_cluster_id:
            story = (
                await session.execute(
                    text(
                        """SELECT 1 FROM studio_story_clusters
                            WHERE workspace_id=:workspace_id AND conversation_id=:conversation_id AND id=:story_cluster_id"""
                    ),
                    {
                        "workspace_id": self.workspace_id,
                        "conversation_id": conversation_id,
                        "story_cluster_id": validated.story_cluster_id,
                    },
                )
            ).first()
            if story is None:
                raise DraftValidationError("unknown_story", "The selected story is not part of this conversation.", field="story_cluster_id")
        if validated.analysis_id:
            try:
                analysis_id = uuid.UUID(str(validated.analysis_id))
            except (TypeError, ValueError) as exc:
                raise DraftValidationError("unknown_analysis", "The selected channel analysis is invalid.", field="analysis_id") from exc
            analysis = (
                await session.execute(
                    text(
                        """SELECT 1 FROM studio_analyses a
                            WHERE a.workspace_id=:workspace_id AND a.id=:analysis_id
                              AND a.channel_id=(
                                  SELECT channel_id FROM studio_conversations
                                   WHERE workspace_id=:workspace_id AND id=:conversation_id
                              )"""
                    ),
                    {"workspace_id": self.workspace_id, "analysis_id": analysis_id, "conversation_id": conversation_id},
                )
            ).first()
            if analysis is None:
                raise DraftValidationError("unknown_analysis", "The selected channel analysis is not part of this conversation.", field="analysis_id")
        return known

    @staticmethod
    def _draft_input_payload(current: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        base = input_from_row(current).model_dump(mode="json")
        # ``None`` means “leave the current field unchanged” for PATCH-like
        # callers; an empty list is an intentional clear for user metadata.
        for key, value in payload.items():
            if value is not None and key in base:
                base[key] = value
        return base

    async def create_draft(
        self,
        *,
        conversation_id: uuid.UUID,
        channel_id: int,
        payload: dict[str, Any],
        origin: str = "generated",
        instruction: str = "",
    ) -> dict[str, Any]:
        origin = self._draft_origin(origin)
        workspace_id = self.workspace_id
        async with self.db.sessions.session() as session:
            await self._validate_draft_scope(session, conversation_id=conversation_id, channel_id=channel_id)
            active = (await session.execute(text("SELECT active_draft_id FROM studio_conversations WHERE workspace_id=:workspace_id AND id=:conversation_id FOR UPDATE"), {"workspace_id": workspace_id, "conversation_id": conversation_id})).scalar_one()
            if active is not None:
                raise DraftValidationError("draft_exists", "This chat already has a draft. Edit the existing draft.")
            if payload.get("media_ids"):
                raise DraftValidationError("invalid_media", "Create the draft before uploading its images.")
            known = await self._validate_draft_references(session, conversation_id=conversation_id, payload=payload)
            values = _draft_values(payload, known_source_ids=known, require_sources=not bool(payload.get("creative", False)))
            if values.get("analysis_id"):
                values["analysis_id"] = uuid.UUID(str(values["analysis_id"]))
            draft_id = uuid.uuid4()
            params = {
                "id": draft_id,
                "workspace_id": workspace_id,
                "conversation_id": conversation_id,
                "channel_id": int(channel_id),
                "story_cluster_id": values.get("story_cluster_id"),
                "analysis_id": values.get("analysis_id"),
                "working_title": values["working_title"],
                "source_links": json.dumps(values["source_links"]),
                "body": values["body"],
                "status": "working",
                "source_ids": json.dumps(values["source_ids"]),
                "claim_support": json.dumps(values["claim_support"]),
                "assumptions": json.dumps(values["assumptions"]),
                "warnings": json.dumps(values["warnings"]),
                "channel_evidence": json.dumps(values["channel_evidence"]),
                "web_evidence": json.dumps(values["web_evidence"]),
                "confidence": values["confidence"],
                "creative": bool(values["creative"]),
                "provider": values["provider"],
                "model": values["model"],
                "prompt_version": values["prompt_version"],
                "character_count": values["character_count"],
                "origin": origin,
                "instruction": str(instruction or "")[:4_000],
            }
            row = (
                await session.execute(
                    text(
                        """INSERT INTO studio_drafts(
                               id, workspace_id, conversation_id, channel_id,
                               story_cluster_id, analysis_id, working_title, source_links, body,
                               status, source_ids, claim_support, assumptions, warnings,
                               channel_evidence, web_evidence, confidence, creative,
                               provider, model, prompt_version, revision, current_version,
                               current_version_origin
                           ) VALUES (
                               :id, :workspace_id, :conversation_id, :channel_id,
                               :story_cluster_id, :analysis_id, :working_title, CAST(:source_links AS jsonb), :body,
                               :status, CAST(:source_ids AS jsonb), CAST(:claim_support AS jsonb),
                               CAST(:assumptions AS jsonb), CAST(:warnings AS jsonb),
                               CAST(:channel_evidence AS jsonb), CAST(:web_evidence AS jsonb),
                               :confidence, :creative, :provider, :model, :prompt_version,
                               1, 1, :origin
                           ) RETURNING *"""
                    ),
                    params,
                )
            ).mappings().one()
            await session.execute(
                text(
                    """INSERT INTO studio_draft_versions(
                               workspace_id, draft_id, version, body, origin, instruction, character_count, working_title, source_links
                           ) VALUES (:workspace_id, :draft_id, 1, :body, :origin, :instruction, :character_count, :working_title, CAST(:source_links AS jsonb))"""
                ),
                {
                    "workspace_id": workspace_id,
                    "draft_id": draft_id,
                    "body": values["body"],
                    "origin": origin,
                    "instruction": str(instruction or "")[:4_000],
                    "character_count": values["character_count"],
                    "working_title": values["working_title"],
                    "source_links": json.dumps(values["source_links"]),
                },
            )
            await session.execute(
                text(
                    """UPDATE studio_conversations SET active_draft_id=:draft_id, updated_at=now()
                        WHERE workspace_id=:workspace_id AND id=:conversation_id"""
                ),
                {"workspace_id": workspace_id, "conversation_id": conversation_id, "draft_id": draft_id},
            )
            await session.commit()
        return _draft_public(dict(row))

    async def get_draft(
        self,
        draft_id: uuid.UUID,
        *,
        conversation_id: uuid.UUID | None = None,
        channel_id: int | None = None,
    ) -> dict[str, Any] | None:
        draft_id = _draft_uuid(draft_id)
        filters = ["workspace_id=:workspace_id", "id=:draft_id"]
        params: dict[str, Any] = {"draft_id": draft_id}
        if conversation_id is not None:
            filters.append("conversation_id=:conversation_id")
            params["conversation_id"] = conversation_id
        if channel_id is not None:
            filters.append("channel_id=:channel_id")
            params["channel_id"] = int(channel_id)
        result = await self.db._execute(f"SELECT * FROM studio_drafts WHERE {' AND '.join(filters)}", params)
        row = result.mappings().first()
        return _draft_public(dict(row)) if row else None

    async def get_current_draft(self, *, conversation_id: uuid.UUID, channel_id: int) -> dict[str, Any] | None:
        result = await self.db._execute(
            """SELECT d.* FROM studio_drafts d
                 JOIN studio_conversations c ON c.workspace_id=d.workspace_id
                    AND c.id=d.conversation_id AND c.active_draft_id=d.id
                WHERE d.workspace_id=:workspace_id AND d.conversation_id=:conversation_id AND d.channel_id=:channel_id
                LIMIT 1""",
            {"conversation_id": conversation_id, "channel_id": int(channel_id)},
        )
        row = result.mappings().first()
        if row is None:
            result = await self.db._execute(
                """SELECT * FROM studio_drafts
                    WHERE workspace_id=:workspace_id AND conversation_id=:conversation_id AND channel_id=:channel_id
                    ORDER BY updated_at DESC, created_at DESC LIMIT 1""",
                {"conversation_id": conversation_id, "channel_id": int(channel_id)},
            )
            row = result.mappings().first()
        return _draft_public(dict(row)) if row else None

    async def update_draft(
        self,
        *,
        draft_id: uuid.UUID,
        payload: dict[str, Any],
        expected_revision: int | None = None,
        origin: str = "user_edit",
        instruction: str = "",
        new_version: bool = True,
    ) -> dict[str, Any]:
        """Write a draft.

        ``new_version=True`` appends an immutable version and makes it current
        (agent output, or the owner's "Save as new version"). ``False``
        overwrites the body of the current version in place (the owner's plain
        "Save"); no version row is created.
        """
        draft_id = _draft_uuid(draft_id)
        origin = self._draft_origin(origin)
        workspace_id = self.workspace_id
        async with self.db.sessions.session() as session:
            current_row = (
                await session.execute(
                    text("SELECT * FROM studio_drafts WHERE workspace_id=:workspace_id AND id=:draft_id FOR UPDATE"),
                    {"workspace_id": workspace_id, "draft_id": draft_id},
                )
            ).mappings().first()
            if current_row is None:
                raise DraftNotFound("draft is not part of the active workspace")
            current = _draft_public(dict(current_row))
            if expected_revision is not None and int(current["revision"]) != int(expected_revision):
                raise DraftConflictError(current, expected_revision=expected_revision)
            media_ids = payload.get("media_ids", current.get("media_ids", []))
            try:
                media_ids = [str(uuid.UUID(str(item))) for item in media_ids]
            except (ValueError, TypeError):
                raise DraftValidationError("invalid_media", "Image identifiers are invalid.") from None
            if len(media_ids) > 10 or len(set(media_ids)) != len(media_ids):
                raise DraftValidationError("invalid_media", "Choose up to ten different images.")
            if media_ids:
                found = (await session.execute(text("SELECT id FROM studio_media WHERE workspace_id=:workspace_id AND draft_id=:draft_id AND id=ANY(:ids)"),
                    {"workspace_id": workspace_id, "draft_id": draft_id, "ids": [uuid.UUID(item) for item in media_ids]})).scalars().all()
                if len(found) != len(media_ids):
                    raise DraftValidationError("invalid_media", "An image is unavailable in this draft.")
            merged = self._draft_input_payload(current, payload)
            known = await self._validate_draft_references(
                session,
                conversation_id=uuid.UUID(str(current["conversation_id"])),
                payload=merged,
            )
            values = _draft_values(merged, known_source_ids=known, require_sources=not bool(merged.get("creative", False)))
            if origin == "user_edit":
                _preserve_server_verification_on_user_edit(current, values)
            current_version = int(current.get("current_version") or 1)
            if new_version:
                max_version = (
                    await session.execute(
                        text("SELECT COALESCE(MAX(version), 0) FROM studio_draft_versions WHERE workspace_id=:workspace_id AND draft_id=:draft_id"),
                        {"workspace_id": workspace_id, "draft_id": draft_id},
                    )
                ).scalar_one()
                next_version = max(current_version, int(max_version or 0)) + 1
                await session.execute(
                    text(
                        """INSERT INTO studio_draft_versions(
                                   workspace_id, draft_id, version, body, origin, instruction, character_count, media_ids, working_title, source_links
                               ) VALUES (:workspace_id, :draft_id, :version, :body, :origin, :instruction, :character_count, CAST(:media_ids AS jsonb), :working_title, CAST(:source_links AS jsonb))"""
                    ),
                    {
                        "workspace_id": workspace_id,
                        "draft_id": draft_id,
                        "version": next_version,
                        "body": values["body"],
                        "origin": origin,
                        "instruction": str(instruction or "")[:4_000],
                        "character_count": values["character_count"],
                        "media_ids": json.dumps(media_ids),
                        "working_title": values["working_title"],
                        "source_links": json.dumps(values["source_links"]),
                    },
                )
            else:
                next_version = current_version
                await session.execute(
                    text(
                        """UPDATE studio_draft_versions
                              SET body=:body, origin=:origin, instruction=:instruction, character_count=:character_count, media_ids=CAST(:media_ids AS jsonb), working_title=:working_title, source_links=CAST(:source_links AS jsonb)
                            WHERE workspace_id=:workspace_id AND draft_id=:draft_id AND version=:version"""
                    ),
                    {
                        "workspace_id": workspace_id,
                        "draft_id": draft_id,
                        "version": next_version,
                        "body": values["body"],
                        "origin": origin,
                        "instruction": str(instruction or "")[:4_000],
                        "character_count": values["character_count"],
                        "media_ids": json.dumps(media_ids),
                        "working_title": values["working_title"],
                        "source_links": json.dumps(values["source_links"]),
                    },
                )
            update_params = {
                "workspace_id": workspace_id,
                "draft_id": draft_id,
                "story_cluster_id": values.get("story_cluster_id"),
                "analysis_id": uuid.UUID(str(values["analysis_id"])) if values.get("analysis_id") else None,
                "working_title": values["working_title"],
                "source_links": json.dumps(values["source_links"]),
                "media_ids": json.dumps(media_ids),
                "body": values["body"],
                "status": "working",
                "source_ids": json.dumps(values["source_ids"]),
                "claim_support": json.dumps(values["claim_support"]),
                "assumptions": json.dumps(values["assumptions"]),
                "warnings": json.dumps(values["warnings"]),
                "channel_evidence": json.dumps(values["channel_evidence"]),
                "web_evidence": json.dumps(values["web_evidence"]),
                "confidence": values["confidence"],
                "creative": bool(values["creative"]),
                "provider": values["provider"],
                "model": values["model"],
                "prompt_version": values["prompt_version"],
                "revision": int(current["revision"]) + 1,
                "current_version": next_version,
                "current_version_origin": origin,
            }
            row = (
                await session.execute(
                    text(
                        """UPDATE studio_drafts SET
                               story_cluster_id=:story_cluster_id, analysis_id=:analysis_id,
                               working_title=:working_title, source_links=CAST(:source_links AS jsonb), body=:body, status=:status, media_ids=CAST(:media_ids AS jsonb),
                               source_ids=CAST(:source_ids AS jsonb), claim_support=CAST(:claim_support AS jsonb),
                               assumptions=CAST(:assumptions AS jsonb), warnings=CAST(:warnings AS jsonb),
                               channel_evidence=CAST(:channel_evidence AS jsonb), web_evidence=CAST(:web_evidence AS jsonb),
                               confidence=:confidence, creative=:creative, provider=:provider, model=:model,
                               prompt_version=:prompt_version, revision=:revision, current_version=:current_version,
                               current_version_origin=:current_version_origin, updated_at=now()
                           WHERE workspace_id=:workspace_id AND id=:draft_id RETURNING *"""
                    ),
                    update_params,
                )
            ).mappings().one()
            await session.commit()
        return _draft_public(dict(row))

    async def save_draft(
        self,
        *,
        draft_id: uuid.UUID,
        payload: dict[str, Any],
        expected_revision: int,
        instruction: str = "",
        new_version: bool = False,
    ) -> dict[str, Any]:
        return await self.update_draft(
            draft_id=draft_id,
            payload=payload,
            expected_revision=expected_revision,
            origin="user_edit",
            instruction=instruction,
            new_version=new_version,
        )

    async def revise_draft(self, *, draft_id: uuid.UUID, payload: dict[str, Any], instruction: str = "") -> dict[str, Any]:
        return await self.update_draft(
            draft_id=draft_id,
            payload=payload,
            expected_revision=None,
            origin="regenerated",
            instruction=instruction,
        )

    async def list_draft_versions(
        self,
        *,
        draft_id: uuid.UUID,
        conversation_id: uuid.UUID | None = None,
        channel_id: int | None = None,
    ) -> list[dict[str, Any]]:
        draft_id = _draft_uuid(draft_id)
        filters = ["d.workspace_id=:workspace_id", "d.id=:draft_id"]
        params: dict[str, Any] = {"draft_id": draft_id}
        if conversation_id is not None:
            filters.append("d.conversation_id=:conversation_id")
            params["conversation_id"] = conversation_id
        if channel_id is not None:
            filters.append("d.channel_id=:channel_id")
            params["channel_id"] = int(channel_id)
        result = await self.db._execute(
            f"""SELECT v.id, v.draft_id, v.version, v.body, v.origin, v.instruction, v.character_count, v.created_at, v.media_ids, v.working_title, v.source_links
                 FROM studio_draft_versions v
                 JOIN studio_drafts d ON d.workspace_id=v.workspace_id AND d.id=v.draft_id
                WHERE {' AND '.join(filters)}
                 ORDER BY v.version ASC""",
            params,
        )
        return [_version_public(dict(row)) for row in result.mappings().all()]

    async def choose_draft_version(self, *, draft_id: uuid.UUID, version: int, expected_revision: int) -> dict[str, Any]:
        """Make an existing version current without creating a new one."""
        draft_id = _draft_uuid(draft_id)
        workspace_id = self.workspace_id
        async with self.db.sessions.session() as session:
            current_row = (
                await session.execute(
                    text("SELECT * FROM studio_drafts WHERE workspace_id=:workspace_id AND id=:draft_id FOR UPDATE"),
                    {"workspace_id": workspace_id, "draft_id": draft_id},
                )
            ).mappings().first()
            if current_row is None:
                raise DraftNotFound("draft is not part of the active workspace")
            current = _draft_public(dict(current_row))
            if int(current["revision"]) != int(expected_revision):
                raise DraftConflictError(current, expected_revision=expected_revision)
            selected = (
                await session.execute(
                    text("SELECT * FROM studio_draft_versions WHERE workspace_id=:workspace_id AND draft_id=:draft_id AND version=:version"),
                    {"workspace_id": workspace_id, "draft_id": draft_id, "version": int(version)},
                )
            ).mappings().first()
            if selected is None:
                raise DraftNotFound("draft version not found")
            if int(selected["version"]) == int(current["current_version"]):
                return current
            row = (
                await session.execute(
                    text(
                        """UPDATE studio_drafts SET body=:body, working_title=COALESCE(:working_title, working_title), source_links=COALESCE(CAST(:source_links AS jsonb), source_links), media_ids=CAST(:media_ids AS jsonb), current_version=:version,
                                  current_version_origin=:origin, revision=:revision, updated_at=now()
                            WHERE workspace_id=:workspace_id AND id=:draft_id RETURNING *"""
                    ),
                    {
                        "workspace_id": workspace_id,
                        "draft_id": draft_id,
                        "body": selected["body"],
                        "working_title": selected.get("working_title"),
                        "source_links": json.dumps(selected["source_links"]) if selected.get("source_links") is not None else None,
                        "media_ids": json.dumps(selected.get("media_ids", [])),
                        "version": int(selected["version"]),
                        "origin": selected["origin"],
                        "revision": int(current["revision"]) + 1,
                    },
                )
            ).mappings().one()
            await session.commit()
        return _draft_public(dict(row))

    async def restore_draft_version(self, *, draft_id: uuid.UUID, version: int, expected_revision: int, instruction: str = "") -> dict[str, Any]:
        """Compatibility alias: choosing a version never creates a new one."""
        return await self.choose_draft_version(draft_id=draft_id, version=version, expected_revision=expected_revision)

    async def mark_draft_copied(self, *, draft_id: uuid.UUID) -> dict[str, Any]:
        draft_id = _draft_uuid(draft_id)
        current = await self.get_draft(draft_id)
        if current is None:
            raise DraftNotFound("draft is not part of the active workspace")
        if int(current.get("character_count", len(current.get("body", "")))) > MAX_DRAFT_CHARS:
            raise DraftValidationError("draft_too_long_to_copy", "Shorten the draft below 4,096 characters before copying.", field="body")
        result = await self.db._execute(
            """UPDATE studio_drafts SET copied_at=now()
                WHERE workspace_id=:workspace_id AND id=:draft_id RETURNING *""",
            {"draft_id": draft_id},
        )
        row = result.mappings().first()
        if row is None:
            raise DraftNotFound("draft is not part of the active workspace")
        return _draft_public(dict(row))

    async def persist_research_bundle(
        self,
        *,
        conversation_id: uuid.UUID,
        channel_id: int,
        bundle: dict[str, Any],
    ) -> dict[str, int]:
        """Upsert normalized sources/clusters within the active tenant scope.

        The service only passes bounded, already-sanitized records.  The
        conversation/channel check is repeated here so a future caller cannot
        persist a bundle into a different channel by changing tool input.
        """

        workspace_id = self.workspace_id
        sources = list(bundle.get("sources") or [])
        stories = list(bundle.get("stories") or [])
        async with self.db.sessions.session() as session:
            owned = (
                await session.execute(
                    text(
                        """SELECT 1 FROM studio_conversations
                            WHERE workspace_id=:workspace_id AND id=:conversation_id AND channel_id=:channel_id
                              AND archived_at IS NULL"""
                    ),
                    {"workspace_id": workspace_id, "conversation_id": conversation_id, "channel_id": int(channel_id)},
                )
            ).first()
            if owned is None:
                raise ConversationNotFound("conversation is not part of the active workspace/channel")
            source_sql = text(
                """INSERT INTO studio_sources(
                       workspace_id, id, conversation_id, channel_id, url, canonical_url,
                       title, publisher, domain, published_at, retrieved_at, excerpt,
                       content, content_hash, provider, query, accessible, status, warnings,
                       injection_flags, metadata_json, quality_score, quality_notes
                   ) VALUES (
                       :workspace_id, :id, :conversation_id, :channel_id, :url, :canonical_url,
                       :title, :publisher, :domain, :published_at, :retrieved_at, :excerpt,
                       :content, :content_hash, :provider, :query, :accessible, :status,
                       CAST(:warnings AS jsonb), CAST(:injection_flags AS jsonb),
                       CAST(:metadata_json AS jsonb), :quality_score, CAST(:quality_notes AS jsonb)
                   ) ON CONFLICT (workspace_id, conversation_id, id) DO UPDATE SET
                       channel_id=EXCLUDED.channel_id,
                       url=EXCLUDED.url, canonical_url=EXCLUDED.canonical_url, title=EXCLUDED.title,
                       publisher=EXCLUDED.publisher, domain=EXCLUDED.domain,
                       published_at=EXCLUDED.published_at, retrieved_at=EXCLUDED.retrieved_at,
                       excerpt=EXCLUDED.excerpt, content=EXCLUDED.content, content_hash=EXCLUDED.content_hash,
                       provider=EXCLUDED.provider, query=EXCLUDED.query, accessible=EXCLUDED.accessible,
                       status=EXCLUDED.status, warnings=EXCLUDED.warnings,
                       injection_flags=EXCLUDED.injection_flags, metadata_json=EXCLUDED.metadata_json,
                       quality_score=EXCLUDED.quality_score, quality_notes=EXCLUDED.quality_notes,
                       updated_at=now()"""
            )
            for source in sources:
                await session.execute(
                    source_sql,
                    {
                        "workspace_id": workspace_id,
                        "id": str(source.get("source_id") or "")[:128],
                        "conversation_id": conversation_id,
                        "channel_id": int(channel_id),
                        "url": str(source.get("url") or "")[:2_000],
                        "canonical_url": str(source.get("canonical_url") or source.get("url") or "")[:2_000],
                        "title": str(source.get("title") or "")[:500],
                        "publisher": str(source.get("publisher") or "")[:160],
                        "domain": str(source.get("domain") or "")[:255],
                        "published_at": _datetime(source.get("published_at")),
                        "retrieved_at": _datetime(source.get("retrieved_at")) or utcnow(),
                        "excerpt": str(source.get("excerpt") or "")[:2_000],
                        "content": str(source.get("content") or "")[:12_000],
                        "content_hash": str(source.get("content_hash") or "")[:128],
                        "provider": str(source.get("provider") or "")[:80],
                        "query": str(source.get("query") or "")[:500],
                        "accessible": bool(source.get("accessible", True)),
                        "status": str(source.get("status") or "ok")[:80],
                        "warnings": json.dumps(list(source.get("warnings") or [])[:20]),
                        "injection_flags": json.dumps(list(source.get("injection_flags") or [])[:20]),
                        "metadata_json": json.dumps(dict(source.get("metadata") or {})),
                        "quality_score": float(source.get("quality_score") or 0.0),
                        "quality_notes": json.dumps(list(source.get("quality_notes") or [])[:20]),
                    },
                )
            story_sql = text(
                """INSERT INTO studio_story_clusters(
                       workspace_id, id, conversation_id, channel_id, status, topic_ids,
                       headline, summary, source_ids, primary_source_id, supporting_source_ids,
                       score, topic_relevance, freshness, source_quality, novelty,
                       score_breakdown, matched_topics, relevance_features, novelty_features,
                       conflict_flags, warnings, channel_evidence, content_fingerprint,
                       published_at, retrieved_at
                   ) VALUES (
                       :workspace_id, :id, :conversation_id, :channel_id, :status,
                       CAST(:topic_ids AS jsonb), :headline, :summary, CAST(:source_ids AS jsonb),
                       :primary_source_id, CAST(:supporting_source_ids AS jsonb), :score,
                       :topic_relevance, :freshness, :source_quality, :novelty,
                       CAST(:score_breakdown AS jsonb), CAST(:matched_topics AS jsonb),
                       CAST(:relevance_features AS jsonb), CAST(:novelty_features AS jsonb),
                       CAST(:conflict_flags AS jsonb), CAST(:warnings AS jsonb),
                       CAST(:channel_evidence AS jsonb), :content_fingerprint,
                       :published_at, :retrieved_at
                   ) ON CONFLICT (workspace_id, conversation_id, id) DO UPDATE SET
                       channel_id=EXCLUDED.channel_id,
                       topic_ids=EXCLUDED.topic_ids, headline=EXCLUDED.headline, summary=EXCLUDED.summary,
                       source_ids=EXCLUDED.source_ids, primary_source_id=EXCLUDED.primary_source_id,
                       supporting_source_ids=EXCLUDED.supporting_source_ids, score=EXCLUDED.score,
                       topic_relevance=EXCLUDED.topic_relevance, freshness=EXCLUDED.freshness,
                       source_quality=EXCLUDED.source_quality, novelty=EXCLUDED.novelty,
                       score_breakdown=EXCLUDED.score_breakdown, matched_topics=EXCLUDED.matched_topics,
                       relevance_features=EXCLUDED.relevance_features, novelty_features=EXCLUDED.novelty_features,
                       conflict_flags=EXCLUDED.conflict_flags, warnings=EXCLUDED.warnings,
                       channel_evidence=EXCLUDED.channel_evidence, content_fingerprint=EXCLUDED.content_fingerprint,
                       published_at=EXCLUDED.published_at, retrieved_at=EXCLUDED.retrieved_at,
                       updated_at=now()"""
            )
            for story in stories:
                await session.execute(
                    story_sql,
                    {
                        "workspace_id": workspace_id,
                        "id": str(story.get("cluster_id") or "")[:128],
                        "conversation_id": conversation_id,
                        "channel_id": int(channel_id),
                        "status": str(story.get("status") or "new") if str(story.get("status") or "new") in {"new", "saved", "dismissed", "used"} else "new",
                        "topic_ids": json.dumps(list(story.get("matched_topics") or [])[:20]),
                        "headline": str(story.get("headline") or "")[:1_000],
                        "summary": str(story.get("summary") or "")[:2_000],
                        "source_ids": json.dumps(list(story.get("source_ids") or [])[:12]),
                        "primary_source_id": str(story.get("primary_source_id") or "")[:128] or None,
                        "supporting_source_ids": json.dumps(list(story.get("supporting_source_ids") or [])[:12]),
                        "score": float(story.get("score") or 0.0),
                        "topic_relevance": float(story.get("topic_relevance") or 0.0),
                        "freshness": float(story.get("freshness") or 0.0),
                        "source_quality": float(story.get("source_quality") or 0.0),
                        "novelty": float(story.get("novelty") or 0.0),
                        "score_breakdown": json.dumps(dict(story.get("score_breakdown") or {})),
                        "matched_topics": json.dumps(list(story.get("matched_topics") or [])[:20]),
                        "relevance_features": json.dumps(dict(story.get("relevance_features") or {})),
                        "novelty_features": json.dumps(dict(story.get("novelty_features") or {})),
                        "conflict_flags": json.dumps(list(story.get("conflict_flags") or [])[:20]),
                        "warnings": json.dumps(list(story.get("warnings") or [])[:20]),
                        "channel_evidence": json.dumps(list(story.get("channel_evidence") or [])[:20]),
                        "content_fingerprint": str(story.get("cluster_id") or "")[:128],
                        "published_at": _datetime(story.get("published_at")),
                        "retrieved_at": _datetime(story.get("retrieved_at")) or utcnow(),
                    },
                )
            await session.commit()
        return {"sources": len(sources), "stories": len(stories)}

    async def get_research_sources(self, *, conversation_id: uuid.UUID, channel_id: int, source_ids: list[str]) -> list[dict[str, Any]]:
        """Read a bounded page even after its records leave the process cache."""
        result = await self.db._execute(
            """SELECT s.* FROM studio_sources s
               WHERE s.workspace_id=:workspace_id AND s.conversation_id=:conversation_id
                 AND s.channel_id=:channel_id AND s.id=ANY(CAST(:source_ids AS text[]))
                 AND EXISTS (SELECT 1 FROM studio_conversations c
                     WHERE c.workspace_id=s.workspace_id AND c.id=s.conversation_id
                       AND c.channel_id=s.channel_id AND c.archived_at IS NULL)""",
            {"workspace_id": self.workspace_id, "conversation_id": conversation_id,
             "channel_id": int(channel_id), "source_ids": source_ids[:20]},
        )
        rows = []
        for item in result.mappings().all():
            row = dict(item)
            row["source_id"] = row.pop("id")
            row["metadata"] = row.pop("metadata_json", {}) or {}
            rows.append(row)
        return rows

    async def get_research_bundle(self, *, conversation_id: uuid.UUID, channel_id: int) -> dict[str, Any] | None:
        """Reload the latest normalized research bundle after process restart."""

        workspace_id = self.workspace_id
        result = await self.db._execute(
            """SELECT 1 FROM studio_conversations
                WHERE workspace_id=:workspace_id AND id=:conversation_id AND channel_id=:channel_id""",
            {"workspace_id": workspace_id, "conversation_id": conversation_id, "channel_id": int(channel_id)},
        )
        if result.first() is None:
            return None
        source_result = await self.db._execute(
            """SELECT * FROM studio_sources
                WHERE workspace_id=:workspace_id AND conversation_id=:conversation_id AND channel_id=:channel_id
                ORDER BY retrieved_at DESC, id""",
            {"workspace_id": workspace_id, "conversation_id": conversation_id, "channel_id": int(channel_id)},
        )
        story_result = await self.db._execute(
            """SELECT * FROM studio_story_clusters
                WHERE workspace_id=:workspace_id AND conversation_id=:conversation_id AND channel_id=:channel_id
                ORDER BY score DESC, retrieved_at DESC, id""",
            {"workspace_id": workspace_id, "conversation_id": conversation_id, "channel_id": int(channel_id)},
        )
        sources = []
        for row in source_result.mappings().all():
            source = dict(row)
            source["source_id"] = source.pop("id")
            source["metadata"] = source.pop("metadata_json", {}) or {}
            sources.append(source)
        stories = []
        for row in story_result.mappings().all():
            story = dict(row)
            story["cluster_id"] = story.pop("id")
            story["selected_source_ids"] = list(story.get("source_ids") or [])
            story["topic_ids"] = list(story.get("topic_ids") or [])
            stories.append(story)
        if not sources and not stories:
            return None
        latest_query = next((str(row.get("query") or "") for row in sources if row.get("query")), "")
        retrieved_values = [
            value for value in (
                *[_datetime(row.get("retrieved_at")) for row in sources],
                *[_datetime(row.get("retrieved_at")) for row in stories],
            ) if value is not None
        ]
        return {
            "query": latest_query,
            "sources": sources,
            "stories": stories,
            "warnings": [],
            "retrieved_at": _iso(max(retrieved_values, default=None)),
            "selected_source_ids": list(stories[0].get("source_ids") or []) if stories else [],
        }

    async def record_research_event(self, *, conversation_id: uuid.UUID, channel_id: int, event: dict[str, Any]) -> dict[str, Any]:
        workspace_id = self.workspace_id
        result = await self.db._execute(
            """INSERT INTO studio_research_events(
                       workspace_id, conversation_id, channel_id, event_type, provider,
                       query_count, result_count, cache_hit, degraded, trace_id, metadata_json
                   ) SELECT :workspace_id, :conversation_id, :channel_id, :event_type, :provider,
                       :query_count, :result_count, :cache_hit, :degraded, :trace_id, CAST(:metadata_json AS jsonb)
                     WHERE EXISTS (
                       SELECT 1 FROM studio_conversations
                        WHERE workspace_id=:workspace_id AND id=:conversation_id AND channel_id=:channel_id
                     )
                   RETURNING *""",
            {
                "workspace_id": workspace_id,
                "conversation_id": conversation_id,
                "channel_id": int(channel_id),
                "event_type": str(event.get("event_type") or "search")[:80],
                "provider": str(event.get("provider") or "unknown")[:80],
                "query_count": max(1, min(int(event.get("query_count") or 1), 8)),
                "result_count": max(0, min(int(event.get("result_count") or 0), 100)),
                "cache_hit": bool(event.get("cache_hit", False)),
                "degraded": bool(event.get("degraded", False)),
                "trace_id": str(event.get("trace_id") or uuid.uuid4())[:128],
                "metadata_json": json.dumps(dict(event.get("metadata") or {})),
            },
        )
        row = result.mappings().first()
        if row is None:
            raise ConversationNotFound("conversation is not part of the active workspace/channel")
        return dict(row)

    async def list_research_events(self, *, conversation_id: uuid.UUID, channel_id: int, limit: int = 50) -> list[dict[str, Any]]:
        result = await self.db._execute(
            """SELECT * FROM studio_research_events
                WHERE workspace_id=:workspace_id AND conversation_id=:conversation_id AND channel_id=:channel_id
                ORDER BY created_at DESC, id DESC LIMIT :limit""",
            {"conversation_id": conversation_id, "channel_id": int(channel_id), "limit": max(1, min(int(limit), 200))},
        )
        return [dict(row) for row in result.mappings().all()]

    async def archive_conversation(self, conversation_id: uuid.UUID) -> bool:
        result = await self.db._execute(
            """UPDATE studio_conversations SET archived_at=COALESCE(archived_at, now()), updated_at=now()
                WHERE workspace_id=:workspace_id AND id=:conversation_id""",
            {"conversation_id": conversation_id},
        )
        return bool(result.rowcount)

    async def delete_conversation(self, conversation_id: uuid.UUID) -> bool:
        """Permanently delete one conversation and its scoped Studio records.

        PostgreSQL cascades the child messages, runs/events, research bundle,
        drafts, and draft versions.  Locking the conversation first makes the
        active-run check race-safe: a run that already owns the conversation
        blocks deletion, while a new run cannot be created after the row is
        removed.
        """

        workspace_id = self.workspace_id
        async with self.db.sessions.session() as session:
            row = (
                await session.execute(
                    text(
                        """SELECT id FROM studio_conversations
                            WHERE workspace_id=:workspace_id AND id=:conversation_id
                            FOR UPDATE"""
                    ),
                    {"workspace_id": workspace_id, "conversation_id": conversation_id},
                )
            ).first()
            if row is None:
                await session.rollback()
                return False
            active = (
                await session.execute(
                    text(
                        """SELECT 1 FROM studio_agent_runs
                            WHERE workspace_id=:workspace_id AND conversation_id=:conversation_id
                              AND status IN ('queued', 'running')
                            LIMIT 1"""
                    ),
                    {"workspace_id": workspace_id, "conversation_id": conversation_id},
                )
            ).first()
            if active is not None:
                await session.rollback()
                raise ActiveRunExists("conversation has an active run")
            deleted = await session.execute(
                text(
                    """DELETE FROM studio_conversations
                        WHERE workspace_id=:workspace_id AND id=:conversation_id"""
                ),
                {"workspace_id": workspace_id, "conversation_id": conversation_id},
            )
            await session.commit()
        return bool(deleted.rowcount)

    async def list_messages(self, conversation_id: uuid.UUID) -> list[dict[str, Any]]:
        result = await self.db._execute(
            """SELECT id, role, content, metadata_json, created_at
                 FROM studio_messages
                WHERE workspace_id=:workspace_id AND conversation_id=:conversation_id
                ORDER BY created_at ASC, id ASC""",
            {"conversation_id": conversation_id},
        )
        return [dict(row) for row in result.mappings().all()]

    async def append_message(
        self,
        *,
        conversation_id: uuid.UUID,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if role not in {"user", "assistant", "system_summary"}:
            raise StudioRepositoryError("unsupported message role")
        if not isinstance(content, str) or not content.strip():
            raise StudioRepositoryError("message content is required")
        if len(content) > 32_000:
            raise StudioRepositoryError("message content is too large")
        workspace_id = self.workspace_id
        async with self.db.sessions.session() as session:
            conversation = (
                await session.execute(
                    text(
                        "SELECT id FROM studio_conversations WHERE workspace_id=:workspace_id AND id=:conversation_id AND archived_at IS NULL FOR UPDATE"
                    ),
                    {"workspace_id": workspace_id, "conversation_id": conversation_id},
                )
            ).first()
            if conversation is None:
                raise ConversationNotFound("conversation is not part of the active workspace")
            row = (
                await session.execute(
                    text(
                        """INSERT INTO studio_messages(workspace_id, conversation_id, role, content, metadata_json)
                           VALUES (:workspace_id, :conversation_id, :role, :content, CAST(:metadata AS jsonb))
                        RETURNING id, role, content, metadata_json, created_at"""
                    ),
                    {
                        "workspace_id": workspace_id,
                        "conversation_id": conversation_id,
                        "role": role,
                        "content": content,
                        "metadata": json.dumps(metadata or {}),
                    },
                )
            ).mappings().one()
            await session.execute(
                text("UPDATE studio_conversations SET updated_at=now() WHERE workspace_id=:workspace_id AND id=:conversation_id"),
                {"workspace_id": workspace_id, "conversation_id": conversation_id},
            )
            await session.commit()
        return dict(row)

    async def delete_message(self, *, conversation_id: uuid.UUID, message_id: int) -> bool:
        """Remove one message; used to clean up an orphaned user message."""
        workspace_id = self.workspace_id
        async with self.db.sessions.session() as session:
            deleted = await session.execute(
                text(
                    """DELETE FROM studio_messages
                        WHERE workspace_id=:workspace_id AND conversation_id=:conversation_id AND id=:message_id"""
                ),
                {"workspace_id": workspace_id, "conversation_id": conversation_id, "message_id": message_id},
            )
            await session.commit()
        return bool(deleted.rowcount)

    async def append_user_message_and_create_run(
        self,
        *,
        conversation_id: uuid.UUID,
        content: str,
        requested_model: str,
        provider: str = "openrouter",
        run_id: uuid.UUID | None = None,
        retry_run_id: uuid.UUID | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Append the user message and queue its run in one transaction.

        Only unique violations on the active-run index become
        ``ActiveRunExists``; every other integrity error propagates and the
        transaction rolls back, so a failed run creation never orphans the
        user message.
        """
        if not isinstance(content, str) or not content.strip():
            raise StudioRepositoryError("message content is required")
        if len(content) > 32_000:
            raise StudioRepositoryError("message content is too large")
        workspace_id = self.workspace_id
        run_id = run_id or uuid.uuid4()
        async with self.db.sessions.session() as session:
            try:
                conversation = (
                    await session.execute(
                        text(
                            "SELECT id FROM studio_conversations WHERE workspace_id=:workspace_id AND id=:conversation_id AND archived_at IS NULL FOR UPDATE"
                        ),
                        {"workspace_id": workspace_id, "conversation_id": conversation_id},
                    )
                ).first()
                if conversation is None:
                    raise ConversationNotFound("conversation is not part of the active workspace")
                active = (
                    await session.execute(
                        text(
                            "SELECT id FROM studio_agent_runs WHERE workspace_id=:workspace_id AND conversation_id=:conversation_id AND status IN ('queued', 'running') LIMIT 1"
                        ),
                        {"workspace_id": workspace_id, "conversation_id": conversation_id},
                    )
                ).first()
                if active is not None:
                    raise ActiveRunExists("conversation already has an active run")
                if retry_run_id is not None:
                    latest = (await session.execute(text(
                        "SELECT * FROM studio_agent_runs WHERE workspace_id=:workspace_id AND conversation_id=:conversation_id ORDER BY created_at DESC, id DESC LIMIT 1"
                    ), {"workspace_id": workspace_id, "conversation_id": conversation_id})).mappings().first()
                    message = (await session.execute(text(
                        "SELECT id, role, content, metadata_json, created_at FROM studio_messages WHERE workspace_id=:workspace_id AND conversation_id=:conversation_id AND role='user' ORDER BY id DESC LIMIT 1"
                    ), {"workspace_id": workspace_id, "conversation_id": conversation_id})).mappings().first()
                    if (latest is None or latest["id"] != retry_run_id or latest["status"] != "failed"
                            or message is None or latest["user_message_id"] != message["id"]):
                        raise StudioRepositoryError("Only the latest failed request can be retried")
                else:
                    message = (
                        await session.execute(
                            text(
                                """INSERT INTO studio_messages(workspace_id, conversation_id, role, content, metadata_json)
                                   VALUES (:workspace_id, :conversation_id, 'user', :content, '{}'::jsonb)
                                RETURNING id, role, content, metadata_json, created_at"""
                            ),
                            {"workspace_id": workspace_id, "conversation_id": conversation_id, "content": content},
                        )
                    ).mappings().one()
                try:
                    run = (
                        await session.execute(
                            text(
                                """INSERT INTO studio_agent_runs(
                                       id, workspace_id, conversation_id, user_message_id,
                                       status, stage, provider, requested_model
                                   ) VALUES (:id, :workspace_id, :conversation_id, :user_message_id,
                                             'queued', 'queued', :provider, :requested_model)
                                RETURNING *"""
                            ),
                            {
                                "id": run_id,
                                "workspace_id": workspace_id,
                                "conversation_id": conversation_id,
                                "user_message_id": message["id"],
                                "provider": provider,
                                "requested_model": requested_model,
                            },
                        )
                    ).mappings().one()
                except IntegrityError as exc:
                    orig = getattr(exc, "orig", None)
                    pgcode = getattr(orig, "pgcode", "") or ""
                    text_ = str(orig) if orig is not None else str(exc)
                    if pgcode == "23505" or "uq_studio_runs_active_conversation" in text_:
                        raise ActiveRunExists("conversation already has an active run") from exc
                    raise
                await session.execute(
                    text("UPDATE studio_conversations SET updated_at=now() WHERE workspace_id=:workspace_id AND id=:conversation_id"),
                    {"workspace_id": workspace_id, "conversation_id": conversation_id},
                )
                await session.commit()
            except Exception:
                try:
                    await session.rollback()
                except Exception:
                    pass
                raise
        return dict(message), dict(run)

    async def create_run(
        self,
        *,
        conversation_id: uuid.UUID,
        user_message_id: int,
        requested_model: str,
        provider: str = "openrouter",
        run_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        workspace_id = self.workspace_id
        run_id = run_id or uuid.uuid4()
        async with self.db.sessions.session() as session:
            conversation = (
                await session.execute(
                    text(
                        "SELECT id FROM studio_conversations WHERE workspace_id=:workspace_id AND id=:conversation_id AND archived_at IS NULL FOR UPDATE"
                    ),
                    {"workspace_id": workspace_id, "conversation_id": conversation_id},
                )
            ).first()
            if conversation is None:
                raise ConversationNotFound("conversation is not part of the active workspace")
            message = (
                await session.execute(
                    text(
                        "SELECT id FROM studio_messages WHERE workspace_id=:workspace_id AND id=:message_id AND conversation_id=:conversation_id AND role='user'"
                    ),
                    {"workspace_id": workspace_id, "message_id": user_message_id, "conversation_id": conversation_id},
                )
            ).first()
            if message is None:
                raise StudioRepositoryError("user message is not part of the conversation")
            active = (
                await session.execute(
                    text(
                        "SELECT id FROM studio_agent_runs WHERE workspace_id=:workspace_id AND conversation_id=:conversation_id AND status IN ('queued', 'running') LIMIT 1"
                    ),
                    {"workspace_id": workspace_id, "conversation_id": conversation_id},
                )
            ).first()
            if active is not None:
                raise ActiveRunExists("conversation already has an active run")
            try:
                row = (
                    await session.execute(
                        text(
                            """INSERT INTO studio_agent_runs(
                                   id, workspace_id, conversation_id, user_message_id,
                                   status, stage, provider, requested_model
                               ) VALUES (:id, :workspace_id, :conversation_id, :user_message_id,
                                         'queued', 'queued', :provider, :requested_model)
                            RETURNING *"""
                        ),
                        {
                            "id": run_id,
                            "workspace_id": workspace_id,
                            "conversation_id": conversation_id,
                            "user_message_id": user_message_id,
                            "provider": provider,
                            "requested_model": requested_model,
                        },
                    )
                ).mappings().one()
            except IntegrityError as exc:
                await session.rollback()
                orig = getattr(exc, "orig", None)
                pgcode = getattr(orig, "pgcode", "") or ""
                message = str(orig) if orig is not None else str(exc)
                if pgcode == "23505" or "uq_studio_runs_active_conversation" in message:
                    raise ActiveRunExists("conversation already has an active run") from exc
                raise
            await session.commit()
        return dict(row)

    async def claim_run(self, run_id: uuid.UUID, *, worker_id: str, lease_seconds: int = 120) -> dict[str, Any] | None:
        """Atomically claim one queued run for one worker.

        A competing process receives ``None``. PostgreSQL is authoritative;
        the in-process registry is only a cancellation accelerator.
        """

        row = (
            await self.db._execute(
                """UPDATE studio_agent_runs
                      SET status='running', stage='thinking', started_at=COALESCE(started_at, now()),
                          worker_id=:worker_id, lease_expires_at=now() + (:lease_seconds * interval '1 second')
                    WHERE workspace_id=:workspace_id AND id=:run_id AND status='queued'
                      AND cancel_requested=false
                RETURNING *""",
                {"run_id": run_id, "worker_id": worker_id, "lease_seconds": max(30, lease_seconds)},
            )
        ).mappings().first()
        return dict(row) if row else None

    async def mark_run_started(self, run_id: uuid.UUID, *, worker_id: str, lease_seconds: int = 120) -> dict[str, Any]:
        """Compatibility wrapper retained for the M2 repository contract."""

        claimed = await self.claim_run(run_id, worker_id=worker_id, lease_seconds=lease_seconds)
        if claimed is not None:
            return claimed
        current = await self.get_run(run_id)
        if current is None:
            raise RunNotFound("run is not part of the active workspace")
        return current

    async def renew_run_lease(self, run_id: uuid.UUID, *, worker_id: str, lease_seconds: int = 120) -> dict[str, Any] | None:
        row = (
            await self.db._execute(
                """UPDATE studio_agent_runs
                      SET lease_expires_at=now() + (:lease_seconds * interval '1 second')
                    WHERE workspace_id=:workspace_id AND id=:run_id
                      AND status='running' AND worker_id=:worker_id
                      AND lease_expires_at >= now()
                RETURNING *""",
                {"run_id": run_id, "worker_id": worker_id, "lease_seconds": max(30, lease_seconds)},
            )
        ).mappings().first()
        return dict(row) if row else None

    async def set_run_status(
        self,
        run_id: uuid.UUID,
        *,
        status: str,
        stage: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        actual_model: str | None = None,
        usage: dict[str, Any] | None = None,
        worker_id: str | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {"queued", "running", "succeeded", "failed", "cancelled", "interrupted"}:
            raise StudioRepositoryError("unsupported run status")
        terminal = status in {"succeeded", "failed", "cancelled", "interrupted"}
        row = (
            await self.db._execute(
                """UPDATE studio_agent_runs
                      SET status=:status, stage=COALESCE(:stage, stage),
                          error_code=:error_code, error_message=:error_message,
                          actual_model=COALESCE(:actual_model, actual_model),
                          usage=CASE WHEN CAST(:usage AS jsonb) IS NULL THEN usage ELSE CAST(:usage AS jsonb) END,
                          error_phase=COALESCE(:error_phase, error_phase),
                          error_class=COALESCE(:error_class, error_class),
                          error_status=COALESCE(:error_status, error_status),
                          error_provider_code=COALESCE(:error_provider_code, error_provider_code),
                          finished_at=CASE WHEN :terminal THEN COALESCE(finished_at, now()) ELSE finished_at END,
                          lease_expires_at=CASE WHEN :terminal THEN NULL ELSE lease_expires_at END
                    WHERE workspace_id=:workspace_id AND id=:run_id
                      AND (CAST(:worker_id AS text) IS NULL OR worker_id=:worker_id)
                RETURNING *""",
                {
                    "run_id": run_id,
                    "status": status,
                    "stage": stage,
                    "error_code": error_code,
                    "error_message": error_message,
                    "actual_model": actual_model,
                    "usage": json.dumps(usage) if usage is not None else None,
                    "terminal": terminal,
                    "worker_id": worker_id,
                    "error_phase": str((diagnostics or {}).get("phase") or "")[:32] or None,
                    "error_class": str((diagnostics or {}).get("exception_class") or "")[:80] or None,
                    "error_status": _diagnostic_int((diagnostics or {}).get("status_code")),
                    "error_provider_code": _diagnostic_int((diagnostics or {}).get("provider_code")),
                },
            )
        ).mappings().first()
        if row is None:
            current = await self.get_run(run_id)
            if current is None:
                raise RunNotFound("run is not part of the active workspace")
            raise RunClaimLost("run is owned by a different worker")
        return dict(row)

    async def append_event(self, run_id: uuid.UUID, *, event_type: str, safe_payload: dict[str, Any] | None = None, result_content: str | None = None) -> dict[str, Any]:
        workspace_id = self.workspace_id
        async with self.db.sessions.session() as session:
            run = (
                await session.execute(
                    text("SELECT id FROM studio_agent_runs WHERE workspace_id=:workspace_id AND id=:run_id FOR UPDATE"),
                    {"workspace_id": workspace_id, "run_id": run_id},
                )
            ).first()
            if run is None:
                raise RunNotFound("run is not part of the active workspace")
            sequence = (
                await session.execute(
                    text("SELECT COALESCE(MAX(sequence), 0) + 1 FROM studio_run_events WHERE workspace_id=:workspace_id AND run_id=:run_id"),
                    {"workspace_id": workspace_id, "run_id": run_id},
                )
            ).scalar_one()
            row = (
                await session.execute(
                    text(
                        """INSERT INTO studio_run_events(workspace_id, run_id, sequence, event_type, safe_payload, result_content)
                           VALUES (:workspace_id, :run_id, :sequence, :event_type, CAST(:safe_payload AS jsonb), :result_content)
                        RETURNING id, sequence, event_type, safe_payload, result_content, created_at"""
                    ),
                    {
                        "workspace_id": workspace_id,
                        "run_id": run_id,
                        "sequence": sequence,
                        "event_type": event_type,
                        "safe_payload": json.dumps(safe_payload or {}),
                        "result_content": result_content,
                    },
                )
            ).mappings().one()
            await session.commit()
        return dict(row)

    async def get_events(self, run_id: uuid.UUID, *, after: int = 0) -> list[dict[str, Any]]:
        result = await self.db._execute(
            """SELECT id, sequence, event_type, safe_payload, created_at
                 FROM studio_run_events
                WHERE workspace_id=:workspace_id AND run_id=:run_id AND sequence > :after
                ORDER BY sequence ASC""",
            {"run_id": run_id, "after": max(0, int(after))},
        )
        return [dict(row) for row in result.mappings().all()]

    _LOG_STATUSES = {"queued", "running", "succeeded", "failed", "cancelled", "interrupted"}

    @staticmethod
    def _validate_log_filters(
        query: Any | None, channel_id: Any | None, status: Any | None
    ) -> tuple[str | None, int | None, str | None]:
        trimmed: str | None = None
        if query is not None:
            candidate = str(query).strip()
            if candidate:
                if len(candidate) > 200:
                    raise ValueError("Invalid query: must be at most 200 characters.")
                trimmed = candidate
        parsed_channel: int | None = None
        if channel_id is not None and (not isinstance(channel_id, str) or str(channel_id).strip() != ""):
            try:
                parsed_channel = int(str(channel_id).strip())  # type: ignore[arg-type]
            except (TypeError, ValueError):
                raise ValueError("Invalid channel: expected a positive integer.") from None
            if parsed_channel <= 0:
                raise ValueError("Invalid channel: expected a positive integer.")
        normalized_status: str | None = None
        if status is not None and str(status).strip() != "":
            normalized_status = str(status).strip().lower()
            if normalized_status not in StudioRepository._LOG_STATUSES:
                raise ValueError(
                    "Invalid status: expected one of queued, running, succeeded, failed, cancelled, interrupted."
                )
        return trimmed, parsed_channel, normalized_status

    def _log_source_sql(self, include_runs: bool) -> str:
        if include_runs:
            return """FROM studio_agent_runs r
                      JOIN studio_conversations c
                        ON c.workspace_id=r.workspace_id AND c.id=r.conversation_id
                      LEFT JOIN studio_run_events e
                        ON e.workspace_id=r.workspace_id AND e.run_id=r.id
                       AND e.event_type='TOOL_CALL_RESULT' AND e.result_content IS NOT NULL
                     WHERE r.workspace_id=:workspace_id"""
        return """FROM studio_run_events e
                  JOIN studio_agent_runs r ON r.workspace_id=e.workspace_id AND r.id=e.run_id
                  JOIN studio_conversations c ON c.workspace_id=r.workspace_id AND c.id=r.conversation_id
                 WHERE e.workspace_id=:workspace_id AND e.event_type='TOOL_CALL_RESULT'
                   AND e.result_content IS NOT NULL"""

    def _log_filter_sql(self) -> str:
        return (
            "AND (CAST(:query AS text) IS NULL OR ("
            "POSITION(LOWER(CAST(:query AS text)) IN LOWER(COALESCE(e.safe_payload->>'tool_name',CASE WHEN e.id IS NULL THEN 'Agent run' ELSE '' END))) > 0 "
            "OR POSITION(LOWER(CAST(:query AS text)) IN LOWER(COALESCE(c.title,''))) > 0 "
            "OR POSITION(LOWER(CAST(:query AS text)) IN LOWER(COALESCE(e.result_content,''))) > 0)) "
            "AND (CAST(:channel_id AS bigint) IS NULL OR c.channel_id=CAST(:channel_id AS bigint)) "
            "AND (CAST(:status AS text) IS NULL OR r.status=CAST(:status AS text))"
        )

    async def list_tool_result_logs(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        query: str | None = None,
        channel_id: int | None = None,
        status: str | None = None,
        include_runs: bool = False,
    ) -> list[dict[str, Any]]:
        trimmed, parsed_channel, normalized_status = self._validate_log_filters(query, channel_id, status)
        result = await self.db._execute(
            f"""SELECT COALESCE(e.id, 0) AS id, r.id AS run_id, e.sequence, e.safe_payload, e.result_content,
                      COALESCE(e.created_at, r.created_at) AS created_at,
                      COALESCE(
                          e.safe_payload->>'tool_name',
                          (SELECT matched.safe_payload->>'tool_name'
                             FROM studio_run_events matched
                            WHERE matched.workspace_id=e.workspace_id AND matched.run_id=e.run_id
                              AND matched.event_type='TOOL_CALL_START'
                              AND matched.safe_payload->>'tool_call_id'=e.safe_payload->>'tool_call_id'
                            ORDER BY matched.sequence DESC LIMIT 1),
                          (SELECT prior.safe_payload->>'tool_name'
                             FROM studio_run_events prior
                            WHERE prior.workspace_id=e.workspace_id AND prior.run_id=e.run_id
                              AND prior.sequence < e.sequence AND prior.event_type='TOOL_CALL_START'
                            ORDER BY prior.sequence DESC LIMIT 1),
                          CASE WHEN e.id IS NULL THEN 'Agent run' END
                      ) AS tool_name,
                      r.requested_model, r.actual_model, r.status AS run_status,
                      r.error_phase, r.error_class, r.error_status, r.error_provider_code,
                      r.error_code, c.channel_id,
                      c.id AS conversation_id, c.title AS conversation_title
                 {self._log_source_sql(include_runs)}
                  {self._log_filter_sql()}
                ORDER BY COALESCE(e.created_at, r.created_at) DESC, COALESCE(e.id, 0) DESC, r.id DESC
                LIMIT :limit OFFSET :offset""",
            {
                "limit": max(1, min(int(limit), 100)),
                "offset": max(0, int(offset)),
                "query": trimmed,
                "channel_id": parsed_channel,
                "status": normalized_status,
            },
        )
        rows = [dict(row) for row in result.mappings().all()]
        for row in rows:
            row["diagnostics"] = {
                "phase": (str(row.get("error_phase") or "")[:32] or None),
                "exception_class": (str(row.get("error_class") or "")[:80] or None),
                "status_code": int(row["error_status"]) if row.get("error_status") is not None and 100 <= int(row["error_status"]) <= 599 else None,
                "provider_code": int(row["error_provider_code"]) if row.get("error_provider_code") is not None and 100 <= int(row["error_provider_code"]) <= 599 else None,
            }
            if all(value is None for value in row["diagnostics"].values()):
                row["diagnostics"] = None
        return rows

    async def count_tool_result_logs(
        self,
        *,
        query: str | None = None,
        channel_id: int | None = None,
        status: str | None = None,
        include_runs: bool = False,
    ) -> int:
        trimmed, parsed_channel, normalized_status = self._validate_log_filters(query, channel_id, status)
        result = await self.db._execute(
            f"""SELECT COUNT(*) AS total
                  {self._log_source_sql(include_runs)}
                   {self._log_filter_sql()}""",
            {"query": trimmed, "channel_id": parsed_channel, "status": normalized_status},
        )
        row = result.mappings().first()
        return int(row["total"] or 0) if row else 0

    async def get_run(self, run_id: uuid.UUID) -> dict[str, Any] | None:
        result = await self.db._execute(
            "SELECT * FROM studio_agent_runs WHERE workspace_id=:workspace_id AND id=:run_id",
            {"run_id": run_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def get_active_run(self, conversation_id: uuid.UUID) -> dict[str, Any] | None:
        """Return the durable queued/running run for one scoped conversation."""

        result = await self.db._execute(
            """SELECT r.* FROM studio_agent_runs r
                 JOIN studio_conversations c
                   ON c.workspace_id=r.workspace_id AND c.id=r.conversation_id
                WHERE r.workspace_id=:workspace_id AND r.conversation_id=:conversation_id
                  AND c.archived_at IS NULL AND r.status IN ('queued', 'running')
                ORDER BY r.created_at DESC LIMIT 1""",
            {"conversation_id": conversation_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def request_cancel(self, run_id: uuid.UUID) -> dict[str, Any]:
        row = (
            await self.db._execute(
                """UPDATE studio_agent_runs SET cancel_requested=true
                    WHERE workspace_id=:workspace_id AND id=:run_id AND status IN ('queued', 'running')
                RETURNING *""",
                {"run_id": run_id},
            )
        ).mappings().first()
        if row is None:
            current = await self.get_run(run_id)
            if current is None:
                raise RunNotFound("run is not part of the active workspace")
            return current
        return dict(row)

    async def mark_stale_runs_interrupted(self, *, queued_grace_seconds: int = 60) -> int:
        """Interrupt orphaned work and append a durable, reload-visible event."""

        workspace_id = self.workspace_id
        async with self.db.sessions.session() as session:
            rows = (
                await session.execute(
                    text(
                        """UPDATE studio_agent_runs
                              SET status='interrupted', stage='interrupted',
                                  finished_at=COALESCE(finished_at, now()), lease_expires_at=NULL,
                                  error_code='run_interrupted',
                                  error_message='The previous worker stopped before completion.'
                            WHERE workspace_id=:workspace_id
                              AND (
                                (status='running' AND (lease_expires_at IS NULL OR lease_expires_at < now()))
                                OR
                                (status='queued' AND created_at < now() - (:queued_grace_seconds * interval '1 second'))
                              )
                        RETURNING id"""
                    ),
                    {
                        "workspace_id": workspace_id,
                        "queued_grace_seconds": max(1, int(queued_grace_seconds)),
                    },
                )
            ).scalars().all()
            for run_id in rows:
                sequence = (
                    await session.execute(
                        text(
                            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM studio_run_events "
                            "WHERE workspace_id=:workspace_id AND run_id=:run_id"
                        ),
                        {"workspace_id": workspace_id, "run_id": run_id},
                    )
                ).scalar_one()
                await session.execute(
                    text(
                        """INSERT INTO studio_run_events(workspace_id, run_id, sequence, event_type, safe_payload)
                           VALUES (:workspace_id, :run_id, :sequence, 'RUN_INTERRUPTED', '{}'::jsonb)"""
                    ),
                    {
                        "workspace_id": workspace_id,
                        "run_id": run_id,
                        "sequence": sequence,
                    },
                )
            await session.commit()
        return len(rows)


class MemoryStudioRepository:
    """Deterministic repository used by tests; production requires PostgreSQL."""

    def __init__(self, *, workspace_id: str = "community-test") -> None:
        self.workspace_id = workspace_id
        self.system_prompts: dict[int, str] = {}
        now = utcnow()
        self.channels = [{"id": 1, "identifier": "@sample_channel", "title": "Sample channel", "active": True}]
        self.conversations: dict[uuid.UUID, dict[str, Any]] = {}
        self.messages: dict[uuid.UUID, list[dict[str, Any]]] = {}
        self.runs: dict[uuid.UUID, dict[str, Any]] = {}
        self.events: dict[uuid.UUID, list[dict[str, Any]]] = {}
        self.analyses: dict[uuid.UUID, dict[str, Any]] = {}
        self.profiles: dict[int, dict[str, Any]] = {}
        self.profile_changes: dict[uuid.UUID, dict[str, Any]] = {}
        self.consents: dict[tuple[str, str, str | None], dict[str, Any]] = {}
        self.research: dict[uuid.UUID, dict[str, Any]] = {}
        self.drafts: dict[uuid.UUID, dict[str, Any]] = {}
        self.draft_versions: dict[uuid.UUID, list[dict[str, Any]]] = {}
        self._draft_version_id = 0
        self._message_id = 0
        self._lock = asyncio.Lock()
        self._now = now

    async def list_channels(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.channels]

    async def get_system_prompt(self, channel_id: int) -> str:
        if not any(row["id"] == int(channel_id) and row["active"] for row in self.channels):
            return ""
        return self.system_prompts.get(int(channel_id), "")

    async def set_system_prompt(self, channel_id: int, value: str, *, expected_prompt: str | None = None) -> str:
        if len(value) > 12000:
            raise ValueError("Studio instructions exceed 12000 characters")
        if not any(row["id"] == int(channel_id) and row["active"] for row in self.channels):
            raise ConversationNotFound("channel is not part of the active workspace")
        if expected_prompt is not None and self.system_prompts.get(int(channel_id), "") != expected_prompt:
            raise SystemPromptConflict("System Prompt changed in another tab")
        self.system_prompts[int(channel_id)] = value
        return value

    async def channel_context(self, channel_id: int) -> dict[str, Any]:
        channel = next((row for row in self.channels if row["id"] == channel_id and row["active"]), None)
        if channel is None:
            raise ConversationNotFound("channel is not part of the active workspace")
        return {
            "channel_id": channel_id,
            "identifier": channel["identifier"],
            "title": channel["title"],
            "tracked_posts": 0,
            "oldest_post": None,
            "newest_post": None,
            "recent_posts": [],
            "note": "Synthetic context for the local Studio test model; no channel data was read.",
        }

    async def performance_rows(self, channel_id: int, limit: int = 2000) -> list[dict[str, Any]]:
        if not any(row["id"] == channel_id and row["active"] for row in self.channels):
            raise ConversationNotFound("channel is not part of the active workspace")
        return []

    async def profile_evidence_ids(self, channel_id: int, post_ids: list[int]) -> set[int]:
        requested = set(post_ids)
        rows = await self.performance_rows(channel_id, limit=10_000)
        return {int(row.get("post_id", row.get("id", 0))) for row in rows
                if int(row.get("post_id", row.get("id", 0))) in requested}

    async def get_analysis_by_hash(self, channel_id: int, input_hash: str) -> dict[str, Any] | None:
        for row in self.analyses.values():
            if (
                row["channel_id"] == channel_id
                and row["input_hash"] == input_hash
                and row.get("workspace_id") == self.workspace_id
            ):
                return dict(row)
        return None

    async def get_analysis(self, analysis_id: uuid.UUID) -> dict[str, Any] | None:
        row = self.analyses.get(analysis_id)
        if not row or row.get("workspace_id") != self.workspace_id:
            return None
        return dict(row)

    async def create_analysis(self, payload: dict[str, Any]) -> dict[str, Any]:
        existing = await self.get_analysis_by_hash(int(payload["channel_id"]), payload["input_hash"])
        if existing:
            return existing
        row = {
            "id": payload.get("id") or uuid.uuid4(),
            "workspace_id": self.workspace_id,
            "channel_id": int(payload["channel_id"]),
            "analysis_start": payload.get("analysis_start"),
            "analysis_end": payload.get("analysis_end"),
            "eligible_post_count": int(payload.get("eligible_post_count", 0)),
            "style_eligible_post_count": int(payload.get("style_eligible_post_count", 0)),
            "evidence_post_ids": payload.get("evidence_post_ids", []),
            "scoring_version": payload.get("scoring_version", "m3.v1"),
            "scoring_weights": payload.get("scoring_weights", {}),
            "topic_insights": payload.get("topic_insights", []),
            "style_insights": payload.get("style_insights", {}),
            "limitations": payload.get("limitations", []),
            "confidence": payload.get("confidence", "low"),
            "confidence_score": float(payload.get("confidence_score", 0.25)),
            "input_hash": payload["input_hash"],
            "provider": payload.get("provider", "local"),
            "model": payload.get("model", ""),
            "prompt_version": payload.get("prompt_version", "m3.profile.v1"),
            "created_at": utcnow(),
        }
        if not any(channel["id"] == row["channel_id"] and channel["active"] for channel in self.channels):
            raise ConversationNotFound("channel is not part of the active workspace")
        self.analyses[row["id"]] = row
        return dict(row)

    async def get_profile(self, channel_id: int) -> dict[str, Any] | None:
        row = self.profiles.get(int(channel_id))
        if row is None:
            return None
        # Ensure text fields exist for legacy rows
        result = dict(row)
        analysis_id = row.get("current_analysis_id")
        analysis = self.analyses.get(analysis_id, {}) if analysis_id is not None else {}
        result["evidence_post_ids"] = row.get("style_profile", {}).get("evidence_post_ids", analysis.get("evidence_post_ids", []) if analysis.get("channel_id") == int(channel_id) else [])
        for k in ["topics_text", "editorial_text", "style_text"]:
            if k not in result:
                result[k] = ""
        if "built_at" not in result:
            result["built_at"] = None
        if "built_from_posts" not in result:
            result["built_from_posts"] = 0
        return result

    async def upsert_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        channel_id = int(payload["channel_id"])
        if not any(channel["id"] == channel_id and channel["active"] for channel in self.channels):
            raise ConversationNotFound("channel is not part of the active workspace")
        previous = self.profiles.get(channel_id)
        row = {
            "id": payload.get("id") or (previous or {}).get("id") or uuid.uuid4(),
            "workspace_id": self.workspace_id,
            "channel_id": channel_id,
            "topics": payload.get("topics", []),
            "style_profile": payload.get("style_profile", {}),
            "editorial_rules": payload.get("editorial_rules", {}),
            "confidence": payload.get("confidence", "low"),
            "current_analysis_id": payload.get("current_analysis_id"),
            "version": (int(previous["version"]) + 1) if previous else int(payload.get("version", 1)),
            "topics_text": payload.get("topics_text", (previous or {}).get("topics_text", "")),
            "editorial_text": payload.get("editorial_text", (previous or {}).get("editorial_text", "")),
            "style_text": payload.get("style_text", (previous or {}).get("style_text", "")),
            "built_at": payload.get("built_at", (previous or {}).get("built_at")),
            "built_from_posts": int(payload.get("built_from_posts", (previous or {}).get("built_from_posts", 0)) or 0),
            "created_at": (previous or {}).get("created_at", utcnow()),
            "updated_at": utcnow(),
        }
        self.profiles[channel_id] = row
        return dict(row)

    async def upsert_profile_text(self, payload: dict[str, Any]) -> dict[str, Any]:
        channel_id = int(payload["channel_id"])
        if not any(channel["id"] == channel_id and channel["active"] for channel in self.channels):
            raise ConversationNotFound("channel is not part of the active workspace")
        def clean(v: str) -> str:
            lines = [line.rstrip() for line in str(v or "").splitlines()]
            return "\n".join(line for line in lines if line.strip() != "")
        topics_text = clean(payload.get("topics_text", ""))
        editorial_text = clean(payload.get("editorial_text", ""))
        style_text = clean(payload.get("style_text", ""))
        for name, txt in [("topics_text", topics_text), ("editorial_text", editorial_text), ("style_text", style_text)]:
            if len(txt) > 2000:
                raise ValueError(f"{name} exceeds 2000 characters")
            if len(txt.splitlines()) > 60:
                raise ValueError(f"{name} exceeds 60 lines")
        expected = payload.get("expected_version")
        previous = self.profiles.get(channel_id)
        current_version = int(previous["version"]) if previous else 0
        if expected is not None and int(expected) != current_version:
            raise DraftConflictError(previous or {}, expected_revision=int(expected))
        new_version = current_version + 1
        style_profile = dict((previous or {}).get("style_profile") or {})
        if payload.get("evidence_post_ids") is not None:
            style_profile["evidence_post_ids"] = payload["evidence_post_ids"]
        row = {
            "id": (previous or {}).get("id") or uuid.uuid4(),
            "workspace_id": self.workspace_id,
            "channel_id": channel_id,
            "topics": (previous or {}).get("topics", []),
            "style_profile": style_profile,
            "editorial_rules": (previous or {}).get("editorial_rules", {}),
            "confidence": (previous or {}).get("confidence", "low"),
            "current_analysis_id": (previous or {}).get("current_analysis_id"),
            "version": new_version,
            "topics_text": topics_text,
            "editorial_text": editorial_text,
            "style_text": style_text,
            "built_at": utcnow() if payload.get("built_from_posts") else (previous or {}).get("built_at"),
            "built_from_posts": int(payload.get("built_from_posts") or (previous or {}).get("built_from_posts", 0)),
            "created_at": (previous or {}).get("created_at", utcnow()),
            "updated_at": utcnow(),
        }
        self.profiles[channel_id] = row
        return dict(row)

    async def create_profile_change(self, payload: dict[str, Any]) -> dict[str, Any]:
        channel_id = int(payload["channel_id"])
        if channel_id not in self.profiles:
            raise ConversationNotFound("profile is not part of the active workspace")
        row = {
            "id": payload.get("id") or uuid.uuid4(),
            "workspace_id": self.workspace_id,
            "channel_id": channel_id,
            "base_profile_version": int(payload.get("base_profile_version", self.profiles[channel_id]["version"])),
            "proposed_topics": payload.get("proposed_topics", []),
            "style_diff": payload.get("style_diff", {}),
            "editorial_rules": payload.get("editorial_rules", {}),
            "reason": str(payload.get("reason", "")),
            "status": payload.get("status", "proposed"),
            "requested_by": payload.get("requested_by"),
            "created_at": utcnow(),
            "confirmed_at": None,
            "applied_at": None,
        }
        self.profile_changes[row["id"]] = row
        return dict(row)

    async def get_profile_change(
        self, change_id: uuid.UUID, *, channel_id: int | None = None
    ) -> dict[str, Any] | None:
        row = self.profile_changes.get(change_id)
        if row is None or str(row.get("workspace_id")) != str(self.workspace_id):
            return None
        if channel_id is not None and int(row["channel_id"]) != int(channel_id):
            return None
        return dict(row)

    async def confirm_profile_change(self, change_id: uuid.UUID) -> dict[str, Any] | None:
        row = self.profile_changes.get(change_id)
        if row is None or row["status"] not in {"proposed", "confirmed"}:
            return None
        row["status"] = "confirmed"
        row["confirmed_at"] = utcnow()
        return dict(row)

    async def apply_profile_change(self, change_id: uuid.UUID) -> dict[str, Any]:
        change = self.profile_changes.get(change_id)
        if change is None or change["status"] != "confirmed":
            raise StudioRepositoryError("profile change requires explicit confirmation")
        profile = self.profiles.get(int(change["channel_id"]))
        if profile is None:
            raise ConversationNotFound("profile is not part of the active workspace")
        if int(profile["version"]) != int(change["base_profile_version"]):
            raise StudioRepositoryError("profile change is based on an outdated version")
        proposed_topics = list(change["proposed_topics"])
        if proposed_topics and isinstance(proposed_topics[0], str):
            proposed_topics = [{"name": topic, "claim": "Added or confirmed by the channel owner."} for topic in proposed_topics]
        profile.update({
            "topics": proposed_topics,
            "style_profile": dict(change["style_diff"] or profile["style_profile"]),
            "editorial_rules": dict(change["editorial_rules"] or profile["editorial_rules"]),
            "version": int(profile["version"]) + 1,
            "updated_at": utcnow(),
        })
        change["status"] = "applied"
        change["applied_at"] = utcnow()
        return dict(profile)

    async def get_provider_consent(self, *, provider: str, configuration_fingerprint: str, user_id: Any | None = None) -> dict[str, Any] | None:
        row = self.consents.get((provider, configuration_fingerprint, str(user_id) if user_id is not None else None))
        if row is None and user_id is not None:
            row = self.consents.get((provider, configuration_fingerprint, None))
        return dict(row) if row else None

    async def grant_provider_consent(self, *, user_id: Any, provider: str, configuration_fingerprint: str) -> dict[str, Any]:
        row = {
            "id": len(self.consents) + 1,
            "workspace_id": self.workspace_id,
            "user_id": user_id,
            "provider": provider,
            "configuration_fingerprint": configuration_fingerprint,
            "allowed": True,
            "granted_at": utcnow(),
            "revoked_at": None,
        }
        self.consents[(provider, configuration_fingerprint, str(user_id) if user_id is not None else None)] = row
        return dict(row)

    async def revoke_provider_consent(self, *, user_id: Any, provider: str) -> dict[str, Any] | None:
        found = None
        for key, row in self.consents.items():
            if row["provider"] == provider and row.get("user_id") == user_id:
                row["allowed"] = False
                row["revoked_at"] = utcnow()
                found = row
        return dict(found) if found else None

    async def create_conversation(self, *, channel_id: int, title: str | None = None) -> dict[str, Any]:
        channel = next(
            (row for row in self.channels if row["id"] == channel_id and row["active"]),
            None,
        )
        if channel is None:
            raise ConversationNotFound("channel is not part of the active workspace")
        now = utcnow()
        conversation_id = uuid.uuid4()
        row = {
            "id": conversation_id,
            "workspace_id": self.workspace_id,
            "channel_id": channel_id,
            "channel_identifier": channel["identifier"],
            "channel_title": channel["title"],
            "title": (title or "New conversation").strip()[:160] or "New conversation",
            "summary": "",
            "active_draft_id": None,
            "created_at": now,
            "updated_at": now,
            "archived_at": None,
        }
        async with self._lock:
            self.conversations[conversation_id] = row
            self.messages[conversation_id] = []
        return dict(row)

    async def list_conversations(self, *, channel_id: int, include_archived: bool = False) -> list[dict[str, Any]]:
        rows = [
            row for row in self.conversations.values()
            if int(row["channel_id"]) == int(channel_id) and (include_archived or row["archived_at"] is None)
        ]
        return [dict(row) for row in sorted(rows, key=lambda row: row["updated_at"], reverse=True)]

    async def get_conversation(self, conversation_id: uuid.UUID) -> dict[str, Any] | None:
        row = self.conversations.get(conversation_id)
        return dict(row) if row else None

    async def rename_conversation(self, conversation_id: uuid.UUID, *, title: str, only_default: bool = False):
        row = self.conversations.get(conversation_id)
        if row is None or row.get("archived_at") is not None:
            return None
        if not only_default or row["title"] == "New conversation":
            row["title"] = title
        return dict(row)

    # ---------- M5 drafts and immutable versions ----------

    @staticmethod
    def _draft_origin(origin: str) -> str:
        value = str(origin or "generated")
        if value not in {"generated", "regenerated", "user_edit"}:
            raise DraftValidationError("invalid_draft_origin", "Draft version origin is invalid.", field="origin")
        return value

    def _known_draft_sources(self, conversation_id: uuid.UUID) -> set[str]:
        state = self.research.get(conversation_id) or {}
        return {str(key) for key in (state.get("sources") or {}).keys()}

    def _draft_payload(self, current: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        base = input_from_row(current).model_dump(mode="json")
        for key, value in payload.items():
            if value is not None and key in base:
                base[key] = value
        return base

    def _validate_memory_draft(self, conversation_id: uuid.UUID, payload: dict[str, Any]):
        known = self._known_draft_sources(conversation_id)
        values = _draft_values(payload, known_source_ids=known, require_sources=not bool(payload.get("creative", False)))
        story_id = values.get("story_cluster_id")
        state = self.research.get(conversation_id) or {}
        if story_id and str(story_id) not in (state.get("stories") or {}):
            raise DraftValidationError("unknown_story", "The selected story is not part of this conversation.", field="story_cluster_id")
        if values.get("analysis_id"):
            try:
                parsed = uuid.UUID(str(values["analysis_id"]))
            except (TypeError, ValueError) as exc:
                raise DraftValidationError("unknown_analysis", "The selected channel analysis is invalid.", field="analysis_id") from exc
            analysis = self.analyses.get(parsed)
            conversation = self.conversations.get(conversation_id)
            if analysis is None or conversation is None or int(analysis.get("channel_id", -1)) != int(conversation.get("channel_id", -2)):
                raise DraftValidationError("unknown_analysis", "The selected channel analysis is not part of this conversation.", field="analysis_id")
        return values, known

    async def create_draft(
        self,
        *,
        conversation_id: uuid.UUID,
        channel_id: int,
        payload: dict[str, Any],
        origin: str = "generated",
        instruction: str = "",
    ) -> dict[str, Any]:
        origin = self._draft_origin(origin)
        conversation = self.conversations.get(conversation_id)
        if conversation is None or conversation.get("archived_at") is not None or int(conversation["channel_id"]) != int(channel_id):
            raise ConversationNotFound("conversation is not part of the active workspace/channel")
        values, _ = self._validate_memory_draft(conversation_id, payload)
        now = utcnow()
        draft_id = uuid.uuid4()
        row = {
            "id": draft_id,
            "workspace_id": self.workspace_id,
            "conversation_id": conversation_id,
            "channel_id": int(channel_id),
            "story_cluster_id": values.get("story_cluster_id"),
            "analysis_id": values.get("analysis_id"),
            "working_title": values["working_title"],
                    "source_links": list(values["source_links"]),
            "body": values["body"],
            "status": "working",
            "source_ids": list(values["source_ids"]),
            "claim_support": list(values["claim_support"]),
            "assumptions": list(values["assumptions"]),
            "warnings": list(values["warnings"]),
            "channel_evidence": list(values["channel_evidence"]),
            "web_evidence": list(values["web_evidence"]),
            "confidence": values["confidence"],
            "creative": bool(values["creative"]),
            "provider": values["provider"],
            "model": values["model"],
            "prompt_version": values["prompt_version"],
            "revision": 1,
            "current_version": 1,
            "current_version_origin": origin,
            "copied_at": None,
            "created_at": now,
            "updated_at": now,
        }
        version = {
            "id": 1,
            "workspace_id": self.workspace_id,
            "draft_id": draft_id,
            "version": 1,
            "body": values["body"],
            "working_title": values["working_title"],
            "source_links": list(values["source_links"]),
            "media_ids": [],
            "origin": origin,
            "instruction": str(instruction or "")[:4_000],
            "character_count": values["character_count"],
            "created_at": now,
        }
        async with self._lock:
            if conversation.get("active_draft_id"):
                raise DraftValidationError("draft_exists", "This chat already has a draft. Edit the existing draft.")
            if payload.get("media_ids"):
                raise DraftValidationError("invalid_media", "Create the draft before uploading its images.")
            self.drafts[draft_id] = row
            self.draft_versions[draft_id] = [version]
            self._draft_version_id = max(self._draft_version_id, 1)
            conversation["active_draft_id"] = draft_id
            conversation["updated_at"] = now
        return _draft_public(row)

    async def get_draft(
        self,
        draft_id: uuid.UUID,
        *,
        conversation_id: uuid.UUID | None = None,
        channel_id: int | None = None,
    ) -> dict[str, Any] | None:
        draft_id = _draft_uuid(draft_id)
        row = self.drafts.get(draft_id)
        if row is None or str(row.get("workspace_id")) != str(self.workspace_id):
            return None
        if conversation_id is not None and row["conversation_id"] != conversation_id:
            return None
        if channel_id is not None and int(row["channel_id"]) != int(channel_id):
            return None
        return _draft_public(row)

    async def get_current_draft(self, *, conversation_id: uuid.UUID, channel_id: int) -> dict[str, Any] | None:
        conversation = self.conversations.get(conversation_id)
        if conversation is None or int(conversation["channel_id"]) != int(channel_id):
            return None
        active = conversation.get("active_draft_id")
        if active:
            row = await self.get_draft(active, conversation_id=conversation_id, channel_id=channel_id)
            if row is not None:
                return row
        rows = [row for row in self.drafts.values() if row["conversation_id"] == conversation_id and int(row["channel_id"]) == int(channel_id)]
        if not rows:
            return None
        return _draft_public(max(rows, key=lambda item: item["updated_at"]))

    async def update_draft(
        self,
        *,
        draft_id: uuid.UUID,
        payload: dict[str, Any],
        expected_revision: int | None = None,
        origin: str = "user_edit",
        instruction: str = "",
        new_version: bool = True,
    ) -> dict[str, Any]:
        draft_id = _draft_uuid(draft_id)
        origin = self._draft_origin(origin)
        async with self._lock:
            current = self.drafts.get(draft_id)
            if current is None:
                raise DraftNotFound("draft is not part of the active workspace")
            public = _draft_public(current)
            if expected_revision is not None and int(public["revision"]) != int(expected_revision):
                raise DraftConflictError(public, expected_revision=expected_revision)
            merged = self._draft_payload(public, payload)
            values, _ = self._validate_memory_draft(current["conversation_id"], merged)
            if origin == "user_edit":
                _preserve_server_verification_on_user_edit(public, values)
            media_ids = payload.get("media_ids", current.get("media_ids", []))
            if media_ids:
                raise DraftValidationError("invalid_media", "Image attachments require PostgreSQL media storage.")
            versions = self.draft_versions.setdefault(draft_id, [])
            now = utcnow()
            current_version = int(public.get("current_version") or 1)
            if new_version:
                next_version = max([int(item["version"]) for item in versions] or [0]) + 1
                self._draft_version_id += 1
                versions.append(
                    {
                        "id": self._draft_version_id,
                        "workspace_id": self.workspace_id,
                        "draft_id": draft_id,
                        "version": next_version,
                        "body": values["body"],
                        "working_title": values["working_title"],
                        "source_links": list(values["source_links"]),
                        "media_ids": list(media_ids),
                        "origin": origin,
                        "instruction": str(instruction or "")[:4_000],
                        "character_count": values["character_count"],
                        "created_at": now,
                    }
                )
            else:
                next_version = current_version
                for item in versions:
                    if int(item["version"]) == current_version:
                        item.update({"working_title": values["working_title"], "source_links": list(values["source_links"]), "media_ids": list(media_ids), "body": values["body"], "origin": origin, "instruction": str(instruction or "")[:4_000], "character_count": values["character_count"]})
            current.update(
                {
                    "story_cluster_id": values.get("story_cluster_id"),
                    "analysis_id": values.get("analysis_id"),
                    "working_title": values["working_title"],
                    "source_links": list(values["source_links"]),
                    "body": values["body"],
                    "status": "working",
                    "source_ids": list(values["source_ids"]),
                    "claim_support": list(values["claim_support"]),
                    "assumptions": list(values["assumptions"]),
                    "warnings": list(values["warnings"]),
                    "channel_evidence": list(values["channel_evidence"]),
                    "web_evidence": list(values["web_evidence"]),
                    "confidence": values["confidence"],
                    "creative": bool(values["creative"]),
                    "provider": values["provider"],
                    "model": values["model"],
                    "prompt_version": values["prompt_version"],
                    "revision": int(public["revision"]) + 1,
                    "current_version": next_version,
                    "current_version_origin": origin,
                    "updated_at": now,
                }
            )
            return _draft_public(current)

    async def save_draft(self, *, draft_id: uuid.UUID, payload: dict[str, Any], expected_revision: int, instruction: str = "", new_version: bool = False) -> dict[str, Any]:
        return await self.update_draft(draft_id=draft_id, payload=payload, expected_revision=expected_revision, origin="user_edit", instruction=instruction, new_version=new_version)

    async def revise_draft(self, *, draft_id: uuid.UUID, payload: dict[str, Any], instruction: str = "") -> dict[str, Any]:
        return await self.update_draft(draft_id=draft_id, payload=payload, origin="regenerated", instruction=instruction)

    async def list_draft_versions(
        self,
        *,
        draft_id: uuid.UUID,
        conversation_id: uuid.UUID | None = None,
        channel_id: int | None = None,
    ) -> list[dict[str, Any]]:
        draft_id = _draft_uuid(draft_id)
        row = self.drafts.get(draft_id)
        if row is None or str(row.get("workspace_id")) != str(self.workspace_id):
            raise DraftNotFound("draft is not part of the active workspace")
        if conversation_id is not None and row["conversation_id"] != conversation_id:
            raise DraftNotFound("draft is not part of the active conversation")
        if channel_id is not None and int(row["channel_id"]) != int(channel_id):
            raise DraftNotFound("draft is not part of the active channel")
        return [_version_public(row) for row in sorted(self.draft_versions.get(draft_id, []), key=lambda item: item["version"])]

    async def choose_draft_version(self, *, draft_id: uuid.UUID, version: int, expected_revision: int) -> dict[str, Any]:
        draft_id = _draft_uuid(draft_id)
        async with self._lock:
            current = self.drafts.get(draft_id)
            if current is None:
                raise DraftNotFound("draft is not part of the active workspace")
            public = _draft_public(current)
            if int(public["revision"]) != int(expected_revision):
                raise DraftConflictError(public, expected_revision=expected_revision)
            selected = next((item for item in self.draft_versions.get(draft_id, []) if int(item["version"]) == int(version)), None)
            if selected is None:
                raise DraftNotFound("draft version not found")
            if int(selected["version"]) == int(public["current_version"]):
                return public
            current.update({
                "body": selected["body"],
                "working_title": selected.get("working_title") or public["working_title"],
                "source_links": selected.get("source_links") if selected.get("source_links") is not None else public["source_links"],
                "media_ids": selected.get("media_ids", []),
                "current_version": int(selected["version"]),
                "current_version_origin": selected["origin"],
                "revision": int(public["revision"]) + 1,
                "updated_at": utcnow(),
            })
            return _draft_public(current)

    async def restore_draft_version(self, *, draft_id: uuid.UUID, version: int, expected_revision: int, instruction: str = "") -> dict[str, Any]:
        return await self.choose_draft_version(draft_id=draft_id, version=version, expected_revision=expected_revision)

    async def mark_draft_copied(self, *, draft_id: uuid.UUID) -> dict[str, Any]:
        draft_id = _draft_uuid(draft_id)
        async with self._lock:
            row = self.drafts.get(draft_id)
            if row is None:
                raise DraftNotFound("draft is not part of the active workspace")
            public = _draft_public(row)
            if int(public["character_count"]) > MAX_DRAFT_CHARS:
                raise DraftValidationError("draft_too_long_to_copy", "Shorten the draft below 4,096 characters before copying.", field="body")
            row["copied_at"] = utcnow()
            return _draft_public(row)

    async def persist_research_bundle(
        self,
        *,
        conversation_id: uuid.UUID,
        channel_id: int,
        bundle: dict[str, Any],
    ) -> dict[str, int]:
        conversation = self.conversations.get(conversation_id)
        if conversation is None or int(conversation["channel_id"]) != int(channel_id):
            raise ConversationNotFound("conversation is not part of the active workspace/channel")
        state = self.research.setdefault(conversation_id, {"sources": {}, "stories": {}, "events": []})
        for source in bundle.get("sources") or []:
            item = dict(source)
            source_id = str(item.get("source_id") or "")
            if source_id:
                state["sources"][source_id] = item
        for story in bundle.get("stories") or []:
            item = dict(story)
            cluster_id = str(item.get("cluster_id") or "")
            if cluster_id:
                state["stories"][cluster_id] = item
        state["query"] = str(bundle.get("query") or state.get("query") or "")
        state["warnings"] = list(bundle.get("warnings") or [])
        state["retrieved_at"] = bundle.get("retrieved_at") or utcnow()
        state["selected_source_ids"] = list(bundle.get("selected_source_ids") or [])
        return {"sources": len(bundle.get("sources") or []), "stories": len(bundle.get("stories") or [])}

    async def get_research_sources(self, *, conversation_id: uuid.UUID, channel_id: int, source_ids: list[str]) -> list[dict[str, Any]]:
        bundle = await self.get_research_bundle(conversation_id=conversation_id, channel_id=channel_id)
        wanted = set(source_ids[:20])
        return [row for row in (bundle or {}).get("sources", []) if row.get("source_id") in wanted]

    async def get_research_bundle(self, *, conversation_id: uuid.UUID, channel_id: int) -> dict[str, Any] | None:
        conversation = self.conversations.get(conversation_id)
        if conversation is None or int(conversation["channel_id"]) != int(channel_id):
            return None
        state = self.research.get(conversation_id)
        if not state:
            return None
        return {
            "query": state.get("query", ""),
            "sources": [dict(value) for value in state.get("sources", {}).values()],
            "stories": [dict(value) for value in state.get("stories", {}).values()],
            "warnings": list(state.get("warnings", [])),
            "retrieved_at": state.get("retrieved_at"),
            "selected_source_ids": list(state.get("selected_source_ids", [])),
        }

    async def record_research_event(self, *, conversation_id: uuid.UUID, channel_id: int, event: dict[str, Any]) -> dict[str, Any]:
        conversation = self.conversations.get(conversation_id)
        if conversation is None or int(conversation["channel_id"]) != int(channel_id):
            raise ConversationNotFound("conversation is not part of the active workspace/channel")
        state = self.research.setdefault(conversation_id, {"sources": {}, "stories": {}, "events": []})
        row = {
            "id": len(state["events"]) + 1,
            "workspace_id": self.workspace_id,
            "conversation_id": conversation_id,
            "channel_id": int(channel_id),
            "event_type": str(event.get("event_type") or "search"),
            "provider": str(event.get("provider") or "unknown"),
            "query_count": max(1, min(int(event.get("query_count") or 1), 8)),
            "result_count": max(0, min(int(event.get("result_count") or 0), 100)),
            "cache_hit": bool(event.get("cache_hit", False)),
            "degraded": bool(event.get("degraded", False)),
            "trace_id": str(event.get("trace_id") or uuid.uuid4()),
            "metadata_json": dict(event.get("metadata") or {}),
            "created_at": utcnow(),
        }
        state["events"].append(row)
        return dict(row)

    async def list_research_events(self, *, conversation_id: uuid.UUID, channel_id: int, limit: int = 50) -> list[dict[str, Any]]:
        conversation = self.conversations.get(conversation_id)
        if conversation is None or int(conversation["channel_id"]) != int(channel_id):
            return []
        state = self.research.get(conversation_id) or {}
        return [dict(row) for row in list(reversed(state.get("events", [])))[: max(1, min(int(limit), 200))]]

    async def archive_conversation(self, conversation_id: uuid.UUID) -> bool:
        row = self.conversations.get(conversation_id)
        if row is None:
            return False
        row["archived_at"] = utcnow()
        row["updated_at"] = utcnow()
        return True

    async def delete_conversation(self, conversation_id: uuid.UUID) -> bool:
        """Permanently remove a conversation and all of its in-memory records."""

        async with self._lock:
            row = self.conversations.get(conversation_id)
            if row is None:
                return False
            if any(
                run["conversation_id"] == conversation_id
                and run["status"] in {"queued", "running"}
                for run in self.runs.values()
            ):
                raise ActiveRunExists("conversation has an active run")
            del self.conversations[conversation_id]
            self.messages.pop(conversation_id, None)
            self.research.pop(conversation_id, None)
            draft_ids = [
                draft_id
                for draft_id, draft in self.drafts.items()
                if draft.get("conversation_id") == conversation_id
            ]
            for draft_id in draft_ids:
                self.drafts.pop(draft_id, None)
                self.draft_versions.pop(draft_id, None)
            run_ids = [
                run_id
                for run_id, run in self.runs.items()
                if run.get("conversation_id") == conversation_id
            ]
            for run_id in run_ids:
                self.runs.pop(run_id, None)
                self.events.pop(run_id, None)
        return True

    async def list_messages(self, conversation_id: uuid.UUID) -> list[dict[str, Any]]:
        if conversation_id not in self.conversations:
            raise ConversationNotFound("conversation is not part of the active workspace")
        return [dict(row) for row in self.messages[conversation_id]]

    async def append_message(self, *, conversation_id: uuid.UUID, role: str, content: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        if role not in {"user", "assistant", "system_summary"} or not content.strip():
            raise StudioRepositoryError("invalid message")
        row = self.conversations.get(conversation_id)
        if row is None or row["archived_at"] is not None:
            raise ConversationNotFound("conversation is not part of the active workspace")
        async with self._lock:
            self._message_id += 1
            message = {"id": self._message_id, "role": role, "content": content, "metadata_json": metadata or {}, "created_at": utcnow()}
            self.messages[conversation_id].append(message)
            row["updated_at"] = message["created_at"]
        return dict(message)

    async def delete_message(self, *, conversation_id: uuid.UUID, message_id: int) -> bool:
        """Remove one message; used to clean up an orphaned user message."""
        async with self._lock:
            rows = self.messages.get(conversation_id)
            if not rows:
                return False
            kept = [row for row in rows if int(row["id"]) != int(message_id)]
            if len(kept) == len(rows):
                return False
            self.messages[conversation_id] = kept
            return True

    async def append_user_message_and_create_run(
        self,
        *,
        conversation_id: uuid.UUID,
        content: str,
        requested_model: str,
        provider: str = "openrouter",
        run_id: uuid.UUID | None = None,
        retry_run_id: uuid.UUID | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Append the user message and queue its run atomically (in-memory)."""
        if not isinstance(content, str) or not content.strip():
            raise StudioRepositoryError("invalid message")
        if len(content) > 32_000:
            raise StudioRepositoryError("message content is too large")
        row = self.conversations.get(conversation_id)
        if row is None or row["archived_at"] is not None:
            raise ConversationNotFound("conversation is not part of the active workspace")
        async with self._lock:
            if any(
                item["conversation_id"] == conversation_id and item["status"] in {"queued", "running"}
                for item in self.runs.values()
            ):
                raise ActiveRunExists("conversation already has an active run")
            if retry_run_id is not None:
                candidates = [item for item in self.runs.values() if item["conversation_id"] == conversation_id]
                latest = max(candidates, key=lambda item: (item["created_at"], str(item["id"]))) if candidates else None
                message = next((item for item in reversed(self.messages[conversation_id]) if item["role"] == "user"), None)
                if (latest is None or latest["id"] != retry_run_id or latest["status"] != "failed"
                        or message is None or latest["user_message_id"] != message["id"]):
                    raise StudioRepositoryError("Only the latest failed request can be retried")
            else:
                self._message_id += 1
                message = {"id": self._message_id, "role": "user", "content": content, "metadata_json": {}, "created_at": utcnow()}
                self.messages[conversation_id].append(message)
            row["updated_at"] = message["created_at"]
            run_id = run_id or uuid.uuid4()
            run = {
                "id": run_id,
                "workspace_id": self.workspace_id,
                "conversation_id": conversation_id,
                "user_message_id": message["id"],
                "status": "queued",
                "stage": "queued",
                "provider": provider,
                "requested_model": requested_model,
                "actual_model": None,
                "usage": {},
                "error_code": None,
                "error_message": None,
                "worker_id": None,
                "lease_expires_at": None,
                "cancel_requested": False,
                "created_at": utcnow(),
                "started_at": None,
                "finished_at": None,
            }
            self.runs[run_id] = run
            self.events[run_id] = []
            return dict(message), dict(run)

    async def create_run(self, *, conversation_id: uuid.UUID, user_message_id: int, requested_model: str, provider: str = "openrouter", run_id: uuid.UUID | None = None) -> dict[str, Any]:
        conversation = self.conversations.get(conversation_id)
        if conversation is None or conversation.get("archived_at") is not None:
            raise ConversationNotFound("conversation is not part of the active workspace")
        if any(row["conversation_id"] == conversation_id and row["status"] in {"queued", "running"} for row in self.runs.values()):
            raise ActiveRunExists("conversation already has an active run")
        run_id = run_id or uuid.uuid4()
        row = {
            "id": run_id,
            "workspace_id": self.workspace_id,
            "conversation_id": conversation_id,
            "user_message_id": user_message_id,
            "status": "queued",
            "stage": "queued",
            "provider": provider,
            "requested_model": requested_model,
            "actual_model": None,
            "usage": {},
            "error_code": None,
            "error_message": None,
            "worker_id": None,
            "lease_expires_at": None,
            "cancel_requested": False,
            "created_at": utcnow(),
            "started_at": None,
            "finished_at": None,
        }
        self.runs[run_id] = row
        self.events[run_id] = []
        return dict(row)

    async def claim_run(self, run_id: uuid.UUID, *, worker_id: str, lease_seconds: int = 120) -> dict[str, Any] | None:
        async with self._lock:
            row = self.runs.get(run_id)
            if row is None:
                raise RunNotFound("run is not part of the active workspace")
            if row["status"] != "queued" or row["cancel_requested"]:
                return None
            row.update(
                {
                    "status": "running",
                    "stage": "thinking",
                    "worker_id": worker_id,
                    "started_at": row["started_at"] or utcnow(),
                    "lease_expires_at": utcnow() + timedelta(seconds=max(1, lease_seconds)),
                }
            )
            return dict(row)

    async def mark_run_started(self, run_id: uuid.UUID, *, worker_id: str, lease_seconds: int = 120) -> dict[str, Any]:
        claimed = await self.claim_run(run_id, worker_id=worker_id, lease_seconds=lease_seconds)
        if claimed is not None:
            return claimed
        row = self.runs.get(run_id)
        if row is None:
            raise RunNotFound("run is not part of the active workspace")
        return dict(row)

    async def renew_run_lease(self, run_id: uuid.UUID, *, worker_id: str, lease_seconds: int = 120) -> dict[str, Any] | None:
        async with self._lock:
            row = self.runs.get(run_id)
            if row is None:
                raise RunNotFound("run is not part of the active workspace")
            now = utcnow()
            if (
                row["status"] != "running"
                or row["worker_id"] != worker_id
                or row["lease_expires_at"] is None
                or row["lease_expires_at"] < now
            ):
                return None
            row["lease_expires_at"] = now + timedelta(seconds=max(1, lease_seconds))
            return dict(row)

    async def set_run_status(self, run_id: uuid.UUID, *, status: str, stage: str | None = None, error_code: str | None = None, error_message: str | None = None, actual_model: str | None = None, usage: dict[str, Any] | None = None, worker_id: str | None = None, diagnostics: dict[str, Any] | None = None) -> dict[str, Any]:
        row = self.runs.get(run_id)
        if row is None:
            raise RunNotFound("run is not part of the active workspace")
        if worker_id is not None and row.get("worker_id") != worker_id:
            raise RunClaimLost("run is owned by a different worker")
        row.update({"status": status, "stage": stage or row["stage"], "error_code": error_code, "error_message": error_message, "actual_model": actual_model or row["actual_model"], "usage": usage if usage is not None else row["usage"]})
        if diagnostics:
            if diagnostics.get("phase"):
                row["error_phase"] = str(diagnostics["phase"])[:32]
            if diagnostics.get("exception_class"):
                row["error_class"] = str(diagnostics["exception_class"])[:80]
            if _diagnostic_int(diagnostics.get("status_code")) is not None:
                row["error_status"] = _diagnostic_int(diagnostics.get("status_code"))
            if _diagnostic_int(diagnostics.get("provider_code")) is not None:
                row["error_provider_code"] = _diagnostic_int(diagnostics.get("provider_code"))
        if status in {"succeeded", "failed", "cancelled", "interrupted"}:
            row["finished_at"] = row["finished_at"] or utcnow()
            row["lease_expires_at"] = None
        return dict(row)

    async def append_event(self, run_id: uuid.UUID, *, event_type: str, safe_payload: dict[str, Any] | None = None, result_content: str | None = None) -> dict[str, Any]:
        if run_id not in self.runs:
            raise RunNotFound("run is not part of the active workspace")
        row = {"id": len(self.events[run_id]) + 1, "sequence": len(self.events[run_id]) + 1, "event_type": event_type, "safe_payload": safe_payload or {}, "result_content": result_content, "created_at": utcnow()}
        self.events[run_id].append(row)
        return dict(row)

    async def get_events(self, run_id: uuid.UUID, *, after: int = 0) -> list[dict[str, Any]]:
        row = self.runs.get(run_id)
        if row is None or row.get("workspace_id") != self.workspace_id:
            raise RunNotFound("run is not part of the active workspace")
        return [dict(row) for row in self.events[run_id] if row["sequence"] > after]

    def _filtered_log_rows(
        self,
        *,
        query: str | None = None,
        channel_id: int | None = None,
        status: str | None = None,
        include_runs: bool = False,
    ) -> list[dict[str, Any]]:
        trimmed, parsed_channel, normalized_status = StudioRepository._validate_log_filters(query, channel_id, status)
        rows: list[dict[str, Any]] = []
        for run_id, run in self.runs.items():
            if run.get("workspace_id") != self.workspace_id:
                continue
            events = self.events.get(run_id, [])
            if normalized_status is not None and str(run.get("status")) != normalized_status:
                continue
            conversation = self.conversations.get(run["conversation_id"])
            if parsed_channel is not None and (conversation is None or int(conversation.get("channel_id", -1)) != int(parsed_channel)):
                continue
            latest_tool_name: str | None = None
            tool_names_by_id: dict[str, str] = {}
            for event in events:
                if event["event_type"] == "TOOL_CALL_START":
                    candidate = event.get("safe_payload", {}).get("tool_name")
                    latest_tool_name = str(candidate) if candidate else None
                    tool_call_id = event.get("safe_payload", {}).get("tool_call_id")
                    if tool_call_id and latest_tool_name:
                        tool_names_by_id[str(tool_call_id)] = latest_tool_name
                if event["event_type"] != "TOOL_CALL_RESULT" or event.get("result_content") is None:
                    continue
                tool_call_id = event.get("safe_payload", {}).get("tool_call_id")
                tool_name = (
                    event.get("safe_payload", {}).get("tool_name")
                    or tool_names_by_id.get(str(tool_call_id))
                    or latest_tool_name
                )
                title = conversation.get("title") if conversation else None
                content = str(event.get("result_content") or "")
                if trimmed:
                    haystacks = [str(tool_name or "").lower(), str(title or "").lower(), content.lower()]
                    if not any(trimmed.lower() in hay for hay in haystacks):
                        continue
                rows.append(
                    {
                        **event,
                        "run_id": run_id,
                        "tool_name": tool_name,
                        "requested_model": run.get("requested_model"),
                        "actual_model": run.get("actual_model"),
                        "run_status": run.get("status"),
                        "channel_id": conversation.get("channel_id") if conversation else None,
                        "conversation_id": run.get("conversation_id"),
                        "conversation_title": title,
                        "diagnostics": None,
                    }
                )
            if include_runs and not any(event["event_type"] == "TOOL_CALL_RESULT" and event.get("result_content") is not None for event in events):
                title = conversation.get("title") if conversation else None
                if trimmed and not any(trimmed.lower() in text.lower() for text in ("Agent run", str(title or ""))):
                    continue
                rows.append({
                    "id": 0, "run_id": run_id, "sequence": None, "result_content": None,
                    "created_at": run["created_at"], "tool_name": "Agent run",
                    "requested_model": run.get("requested_model"), "actual_model": run.get("actual_model"),
                    "run_status": run.get("status"), "channel_id": conversation.get("channel_id") if conversation else None,
                    "conversation_id": run.get("conversation_id"), "conversation_title": title, "diagnostics": None,
                })
        rows.sort(key=lambda row: (row["created_at"], row["id"], str(row["run_id"])), reverse=True)
        return rows

    async def list_tool_result_logs(
        self, *, limit: int = 50, offset: int = 0, query: str | None = None,
        channel_id: int | None = None, status: str | None = None, include_runs: bool = False,
    ) -> list[dict[str, Any]]:
        rows = self._filtered_log_rows(query=query, channel_id=channel_id, status=status, include_runs=include_runs)
        start = max(0, int(offset))
        return rows[start : start + max(1, min(int(limit), 100))]

    async def count_tool_result_logs(
        self, *, query: str | None = None, channel_id: int | None = None,
        status: str | None = None, include_runs: bool = False,
    ) -> int:
        return len(self._filtered_log_rows(query=query, channel_id=channel_id, status=status, include_runs=include_runs))

    async def get_run(self, run_id: uuid.UUID) -> dict[str, Any] | None:
        row = self.runs.get(run_id)
        if not row or row.get("workspace_id") != self.workspace_id:
            return None
        return dict(row)

    async def get_active_run(self, conversation_id: uuid.UUID) -> dict[str, Any] | None:
        conversation = self.conversations.get(conversation_id)
        if conversation is None or conversation.get("archived_at") is not None:
            return None
        active = [
            row
            for row in self.runs.values()
            if row["conversation_id"] == conversation_id and row["status"] in {"queued", "running"}
        ]
        if not active:
            return None
        return dict(max(active, key=lambda row: row["created_at"]))

    async def request_cancel(self, run_id: uuid.UUID) -> dict[str, Any]:
        row = self.runs.get(run_id)
        if row is None:
            raise RunNotFound("run is not part of the active workspace")
        if row["status"] in {"queued", "running"}:
            row["cancel_requested"] = True
        return dict(row)

    async def mark_stale_runs_interrupted(self, *, queued_grace_seconds: int = 60) -> int:
        now = utcnow()
        count = 0
        async with self._lock:
            for run_id, row in self.runs.items():
                running_stale = row["status"] == "running" and (
                    row["lease_expires_at"] is None or row["lease_expires_at"] < now
                )
                queued_stale = row["status"] == "queued" and row["created_at"] < (
                    now - timedelta(seconds=max(1, queued_grace_seconds))
                )
                if not (running_stale or queued_stale):
                    continue
                row.update(
                    {
                        "status": "interrupted",
                        "stage": "interrupted",
                        "finished_at": row["finished_at"] or now,
                        "lease_expires_at": None,
                        "error_code": "run_interrupted",
                        "error_message": "The previous worker stopped before completion.",
                    }
                )
                self.events[run_id].append(
                    {
                        "id": len(self.events[run_id]) + 1,
                        "sequence": len(self.events[run_id]) + 1,
                        "event_type": "RUN_INTERRUPTED",
                        "safe_payload": {},
                        "created_at": now,
                    }
                )
                count += 1
        return count
