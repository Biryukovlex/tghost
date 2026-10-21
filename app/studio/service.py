"""Conversation, streaming, and scoped Studio research service."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator, Callable

from pydantic import ValidationError
from ag_ui.core import (
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedSuccessOutcome,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
)
from pydantic_ai import UsageLimits
from pydantic_ai.usage import RunUsage
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.ui import SSE_CONTENT_TYPE
from starlette.responses import JSONResponse, StreamingResponse

from .adapter import StudioAGUIAdapter as AGUIAdapter
from .agent import StudioDeps, build_agent
from .. import limits
from .analytics import analyze_posts_async
from .context import profile_instructions
from .profile import build_profile
from .semantic_profile import build_semantic_profile
from .research import ResearchService
from .prompts import PROMPT_VERSION
from .model import model_name, provider_name, run_settings
from .observability import emit_observation, normalize_usage, safe_error, safe_status_code
from .run_ids import resolve_run_id
from .repository import (
    ActiveRunExists,
    ConversationNotFound,
    RunNotFound,
    RunClaimLost,
    StudioRepositoryError,
    StudioRepositoryProtocol,
)


log = logging.getLogger("studio")


class _UpstreamRunError(RuntimeError):
    """Internal sentinel for an error already emitted by the UI adapter.

    ``diagnostics`` carries the upstream classification captured before
    AG-UI wrapping so the failure path persists it instead of re-deriving
    everything from the flattened message text.
    """

    def __init__(self, message: str = "", *, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostics: dict[str, Any] = dict(diagnostics or {})
        self.safe_failure: tuple[str, str, bool] | None = None


@dataclass(slots=True)
class RunHandle:
    run_id: uuid.UUID
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[Any] | None = None
    cancelled: bool = False
    cancel_reason: str | None = None
    active: bool = True

    def attach(self, task: asyncio.Task[Any]) -> None:
        self.task = task
        if self.cancelled and not task.done():
            task.cancel()

    def cancel(self, *, reason: str = "user") -> bool:
        if not self.active:
            return False
        self.cancelled = True
        self.cancel_reason = reason
        self.cancel_event.set()
        if self.task is not None and not self.task.done():
            self.task.cancel()
        return True

    def finish(self) -> None:
        self.active = False
        self.task = None


@dataclass(slots=True)
class RunRegistry:
    """Local cancellation accelerator; PostgreSQL remains authoritative."""

    max_runs: int = 64
    _runs: dict[uuid.UUID, RunHandle] = field(default_factory=dict)

    def begin(self, run_id: uuid.UUID) -> RunHandle:
        existing = self._runs.get(run_id)
        if existing is not None and existing.active:
            raise ActiveRunExists("run already active")
        handle = RunHandle(run_id=run_id)
        self._runs[run_id] = handle
        while len(self._runs) > self.max_runs:
            oldest = next(iter(self._runs))
            if self._runs[oldest].active:
                oldest_inactive = next((key for key, item in self._runs.items() if not item.active), None)
                if oldest_inactive is None:
                    break
                del self._runs[oldest_inactive]
            else:
                del self._runs[oldest]
        return handle

    def attach(self, run_id: uuid.UUID, task: asyncio.Task[Any]) -> None:
        handle = self._runs.get(run_id)
        if handle is not None:
            handle.attach(task)

    def cancel(self, run_id: uuid.UUID, *, reason: str = "user") -> bool:
        handle = self._runs.get(run_id)
        return handle.cancel(reason=reason) if handle is not None else False

    def finish(self, run_id: uuid.UUID) -> None:
        handle = self._runs.get(run_id)
        if handle is not None:
            handle.finish()


@dataclass(slots=True)
class RunCoordinator:
    """Bound provider execution within one process.

    PostgreSQL leases prevent duplicate ownership across processes; this
    semaphore enforces the configured local provider-call ceiling.
    """

    max_concurrency: int = 2
    _slots: asyncio.Semaphore = field(init=False, repr=False)
    active: int = 0

    def __post_init__(self) -> None:
        self.max_concurrency = max(1, int(self.max_concurrency))
        self._slots = asyncio.Semaphore(self.max_concurrency)

    @asynccontextmanager
    async def slot(self, heartbeat=None, interval: float = 20.0):
        """Acquire a local execution slot, optionally heartbeating while queued.

        The heartbeat runs each time the wait exceeds the interval so an
        already-claimed run can renew its durable lease while waiting.
        """
        while True:
            try:
                if heartbeat is None:
                    await self._slots.acquire()
                else:
                    await asyncio.wait_for(self._slots.acquire(), timeout=max(0.05, float(interval)))
                break
            except asyncio.TimeoutError:
                try:
                    await heartbeat()
                except Exception:  # noqa: BLE001 - heartbeat must not fail the wait
                    pass
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1
            self._slots.release()


def _event_type(event: Any) -> str:
    value = getattr(event, "type", event.__class__.__name__)
    return str(getattr(value, "value", value))


def _safe_event_payload(event: Any) -> dict[str, Any]:
    """Project an AG-UI event without storing text, prompts, or raw payloads."""

    payload: dict[str, Any] = {}
    tool_name = getattr(event, "tool_call_name", None)
    if tool_name:
        payload["tool_name"] = str(tool_name)
    tool_call_id = getattr(event, "tool_call_id", None)
    if tool_call_id:
        payload["tool_call_id"] = str(tool_call_id)[:160]
    delta = getattr(event, "delta", None)
    if delta:
        payload["delta_length"] = len(str(delta))
    content = getattr(event, "content", None)
    if content and _event_type(event) == "TOOL_CALL_RESULT":
        payload["result_length"] = len(str(content))
        # AG-UI serializes tool results to JSON strings. Decode only for this
        # bounded projection so operational source/story counts survive the
        # protocol boundary; raw URLs and excerpts are still never persisted.
        if isinstance(content, str) and len(content) <= 2_000_000:
            try:
                decoded = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                decoded = None
            if isinstance(decoded, dict):
                content = decoded
        # Research tools return structured activity metadata. Keep only the
        # bounded operational fields required by the collapsed activity row;
        # URLs, excerpts, prompts, and provider responses never enter events.
        candidate = content.get("activity") if isinstance(content, dict) else None
        if candidate is None and isinstance(content, dict):
            candidate = content
        if isinstance(candidate, list):
            candidate = candidate[0] if candidate else None
        if isinstance(candidate, dict):
            for key in ("provider", "degraded", "cache_hit", "persisted", "query_count", "result_count"):
                if key in candidate:
                    value = candidate[key]
                    if key == "provider":
                        payload[key] = str(value)[:80]
                    elif key in {"degraded", "cache_hit", "persisted"}:
                        payload[key] = bool(value)
                    else:
                        try:
                            payload[key] = max(0, min(int(value), 100))
                        except (TypeError, ValueError):
                            continue
            outcome = candidate.get("search_outcome")
            if isinstance(outcome, str) and outcome in ("healthy", "partial", "empty", "unavailable"):
                payload["search_outcome"] = outcome
            failed = candidate.get("failed_engines")
            if isinstance(failed, list):
                payload["failed_engines"] = [str(name)[:80] for name in failed[:8]]
        if isinstance(content, dict):
            if "sources" in content and isinstance(content["sources"], list):
                payload["source_count"] = min(len(content["sources"]), 100)
            if "stories" in content and isinstance(content["stories"], list):
                payload["story_count"] = min(len(content["stories"]), 100)
            providers = content.get("providers")
            if isinstance(providers, list):
                payload["providers"] = [str(item)[:80] for item in providers[:4]]
            # Resource IDs are operational references, not private content.
            # Keep them available for quiet run details and structured logs,
            # while deliberately ignoring URLs, excerpts, headlines, and
            # arbitrary provider fields.
            for key in ("analysis_id", "story_cluster_id", "draft_id"):
                candidate_value = content.get(key)
                if candidate_value is None and isinstance(candidate, dict):
                    candidate_value = candidate.get(key)
                if candidate_value is not None and not isinstance(candidate_value, (dict, list)):
                    payload[key] = str(candidate_value)[:160]
            for nested_key in ("analysis", "draft", "proposal", "change"):
                nested = content.get(nested_key)
                if not isinstance(nested, dict):
                    continue
                for key in ("analysis_id", "story_cluster_id", "draft_id", "id"):
                    if key not in nested or nested[key] is None or isinstance(nested[key], (dict, list)):
                        continue
                    if nested_key == "proposal" and key == "id":
                        mapped = "story_cluster_id"
                    elif nested_key == "draft" and key == "id":
                        mapped = "draft_id"
                    else:
                        mapped = key
                    payload.setdefault(mapped, str(nested[key])[:160])
    return payload


def _tool_result_content(event: Any) -> str | None:
    """Return the complete tool result for the private owner log.

    This value is deliberately kept separate from ``safe_payload`` so it never
    crosses the ordinary Studio event API or structured process logs.
    """

    if _event_type(event) != "TOOL_CALL_RESULT":
        return None
    content = getattr(event, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(content)


def _text_from_user_message(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    parts: list[str] = []
    for item in content or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(str(text))
    return "".join(parts).strip()


async def _conversation_evidence_state(repository, *, conversation_id, channel_id: int) -> dict[str, Any]:
    """Supply the same scoped retrieval metadata for every message and language."""
    research: dict[str, Any] = {"status": "unavailable", "catalog_tool": "get_research_sources"}
    getter = getattr(repository, "get_research_summary", None)
    if getter is None:
        return {"research": research}
    try:
        raw = await getter(conversation_id=conversation_id, channel_id=channel_id)
        if raw is None:
            return {"research": research}
        counts = {key: raw[key] for key in ("source_count", "read_count", "failed_count")}
        if any(type(value) is not int or value < 0 for value in counts.values()):
            raise ValueError("Invalid retrieval counts")
        if counts["read_count"] + counts["failed_count"] > counts["source_count"]:
            raise ValueError("Inconsistent retrieval counts")
        research = {**research, "status": "available", **counts}
        latest = raw.get("latest_retrieved_at")
        if isinstance(latest, datetime):
            research["latest_retrieved_at"] = latest.isoformat()
    except Exception as exc:  # noqa: BLE001 - metadata failure must not stop the agent or invent zero counts
        log.warning("Conversation evidence state unavailable (%s)", type(exc).__name__)
    return {"research": research}


def _model_history(rows: list[dict[str, Any]]) -> list[Any]:
    history: list[Any] = []
    bounded: list[dict[str, Any]] = []
    remaining = limits.HISTORY_MAX_CHARS
    for row in reversed(rows):
        metadata = row.get("metadata_json", row.get("metadata")) or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except ValueError:
                metadata = {}
        if row.get("role") == "assistant" and isinstance(metadata, dict) and metadata.get("run_failed") is True:
            continue
        if len(bounded) >= 40:
            break
        content = str(row.get("content") or "")[:6000]
        if len(content) > remaining:
            break
        bounded.append({**row, "content": content})
        remaining -= len(content)
    for row in reversed(bounded):
        content = str(row.get("content") or "")
        if not content:
            continue
        role = row.get("role")
        if role == "user":
            history.append(ModelRequest(parts=[UserPromptPart(content=content)]))
        elif role == "assistant":
            history.append(ModelResponse(parts=[TextPart(content=content)]))
    return history


def _safe_error(exc: BaseException) -> tuple[str, str, bool]:
    captured = getattr(exc, "safe_failure", None)
    return captured if captured else safe_error(exc)


class StudioService:
    """Orchestrates validated persistence, PydanticAI, and safe event output."""

    def __init__(
        self,
        repository: StudioRepositoryProtocol,
        settings,
        *,
        agent_factory: Callable[..., Any] = build_agent,
        research_service: ResearchService | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.agent_factory = agent_factory
        self.registry = RunRegistry()
        self.coordinator = RunCoordinator(limits.RUN_CONCURRENCY)
        self.research_service = research_service or ResearchService(settings, repository=repository)

    async def create_conversation(self, *, channel_id: int, title: str | None = None) -> dict[str, Any]:
        return await self.repository.create_conversation(channel_id=channel_id, title=title)

    async def recover_stale_runs(self) -> int:
        return await self.repository.mark_stale_runs_interrupted(
            queued_grace_seconds=max(
                1, int(limits.QUEUED_RUN_GRACE_SECONDS), int(limits.RUN_TIMEOUT_SECONDS)
            )
        )

    async def _watch_run_control(self, run_id: uuid.UUID, worker_id: str, handle: RunHandle) -> None:
        """Renew the durable lease and observe cancellation from any process."""

        lease_seconds = max(30, int(limits.RUN_LEASE_SECONDS))
        configured = float(limits.RUN_HEARTBEAT_SECONDS)
        interval = max(0.05, min(configured, lease_seconds / 3))
        consecutive_failures = 0
        while handle.active:
            await asyncio.sleep(interval)
            try:
                state = await self.repository.renew_run_lease(
                    run_id,
                    worker_id=worker_id,
                    lease_seconds=lease_seconds,
                )
            except Exception:
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    handle.cancel(reason="lease_lost")
                    return
                continue
            consecutive_failures = 0
            if state is None:
                handle.cancel(reason="lease_lost")
                return
            if state.get("cancel_requested"):
                handle.cancel(reason="user")
                return

    async def ensure_profile(self, channel_id: int, *, force: bool = False, semantic: bool = False) -> dict[str, Any] | None:
        """Reuse a profile; semantic=True permits a consent-gated model refresh."""

        getter = getattr(self.repository, "get_profile", None)
        existing = await getter(channel_id) if getter is not None else None
        if existing is not None and not force:
            return existing
        reader = getattr(self.repository, "performance_rows", None)
        if reader is None:
            return existing
        channel = await self.repository.channel_context(channel_id)
        rows = await reader(channel_id)
        analytics = await analyze_posts_async(rows, channel_id, identifier=channel.get("identifier"))
        profile, analysis = build_profile(
            analytics,
            provider="local",
            model="",
        )
        if semantic:
            profile, analysis = await build_semantic_profile(analytics, rows, self.settings)
        analysis_row = None
        create_analysis = getattr(self.repository, "create_analysis", None)
        if create_analysis is not None:
            analysis_row = await create_analysis(
                {
                    "channel_id": channel_id,
                    "analysis_start": analytics.analysis_start,
                    "analysis_end": analytics.analysis_end,
                    "eligible_post_count": analytics.eligible_post_count,
                    "style_eligible_post_count": analytics.style_eligible_post_count,
                    "evidence_post_ids": analysis.evidence_post_ids,
                    "scoring_version": analytics.analytics_version,
                    "scoring_weights": analytics.scoring_weights,
                    "topic_insights": [topic.model_dump(mode="json") for topic in analysis.topic_insights],
                    "style_insights": analysis.style_insights,
                    "limitations": analysis.limitations,
                    "confidence": analysis.confidence,
                    "confidence_score": analysis.confidence_score,
                    "input_hash": analysis.input_hash,
                    "provider": analysis.provider,
                    "model": analysis.model,
                    "prompt_version": analysis.prompt_version,
                }
            )
        upsert = getattr(self.repository, "upsert_profile", None)
        if upsert is None:
            return existing
        return await upsert(
            {
                "channel_id": channel_id,
                "topics": [topic.model_dump(mode="json") for topic in profile.topics],
                "style_profile": profile.style_profile.model_dump(mode="json"),
                "editorial_rules": profile.editorial_rules,
                "confidence": profile.confidence,
                "current_analysis_id": analysis_row["id"] if analysis_row else None,
            }
        )

    async def build_profile_draft(self, channel_id: int) -> dict[str, Any]:
        """Build a draft profile text from channel posts (no save)."""
        reader = getattr(self.repository, "performance_rows", None)
        if reader is None:
            raise ValueError("performance rows unavailable")
        channel = await self.repository.channel_context(channel_id)
        rows = await reader(channel_id, limit=10_000)
        if not rows:
            raise ValueError("too few posts")
        min_posts = int(limits.MIN_PROFILE_POSTS)
        if len(rows) < min_posts:
            raise ValueError(f"too few posts: {len(rows)} < {min_posts}")
        analytics = await analyze_posts_async(rows, channel_id, identifier=channel.get("identifier"), max_evidence_posts=30)
        from .semantic_profile import build_profile_text_draft
        current = None
        getter = getattr(self.repository, "get_profile", None)
        if getter is not None:
            try:
                current = await getter(channel_id)
            except Exception:  # noqa: BLE001 - existing text only refines the prompt
                current = None
        draft = await build_profile_text_draft(analytics, rows, self.settings, current=current)
        from .profile import fit_field_lines
        topics, dropped_topics = fit_field_lines(draft.topics)
        editorial, dropped_editorial = fit_field_lines(draft.editorial_rules)
        style, dropped_style = fit_field_lines(draft.style_rules)
        limitations = list(draft.limitations)
        dropped = dropped_topics + dropped_editorial + dropped_style
        if dropped:
            limitations.append(f"{dropped} lines omitted to fit the 2,000-character field limit")
        return {
            "topics_text": "\n".join(topics),
            "editorial_text": "\n".join(editorial),
            "style_text": "\n".join(style),
            "built_from_posts": draft.built_from_posts,
            "limitations": limitations,
            "formatting_facts": draft.formatting_facts,
            "evidence_post_ids": draft.evidence_post_ids,
        }

    async def stream_request(self, request, body: bytes) -> StreamingResponse | JSONResponse:
        settings = run_settings(self.settings)
        if len(body) > 512 * 1024:
            return JSONResponse({"error": {"code": "request_too_large", "message": "Studio request is too large.", "retryable": False}}, status_code=413)
        try:
            run_input = AGUIAdapter.build_run_input(body)
        except (ValidationError, ValueError, TypeError):
            return JSONResponse({"error": {"code": "invalid_agent_request", "message": "Invalid Studio agent request.", "retryable": False}}, status_code=422)

        try:
            conversation_id = uuid.UUID(str(run_input.thread_id))
            run_id = resolve_run_id(run_input.run_id, workspace_id=self.repository.workspace_id)
        except (TypeError, ValueError):
            return JSONResponse({"error": {"code": "invalid_identifier", "message": "Conversation or run identifier is invalid.", "retryable": False}}, status_code=422)
        conversation = await self.repository.get_conversation(conversation_id)
        if conversation is None or conversation.get("archived_at") is not None:
            return JSONResponse({"error": {"code": "conversation_not_found", "message": "Conversation not found.", "retryable": False}}, status_code=404)
        user_message = next((message for message in reversed(run_input.messages) if getattr(message, "role", "") == "user"), None)
        content = _text_from_user_message(user_message) if user_message is not None else ""
        if user_message is None or not content:
            return JSONResponse({"error": {"code": "message_required", "message": "Send a text message to start the run.", "retryable": False}}, status_code=422)
        if len(content) > 32_000:
            return JSONResponse({"error": {"code": "message_too_large", "message": "Message is too large.", "retryable": False}}, status_code=413)

        custom = (run_input.forwarded_props or {}).get("runConfig", {})
        retry_value = custom.get("retryRunId") if isinstance(custom, dict) else None
        try:
            retry_run_id = uuid.UUID(str(retry_value)) if retry_value is not None else None
        except (TypeError, ValueError):
            return JSONResponse({"error": {"code": "invalid_identifier", "message": "Retry identifier is invalid.", "retryable": False}}, status_code=422)
        history_rows = await self.repository.list_messages(conversation_id)
        user_row: dict[str, Any] | None = None
        try:
            combined = getattr(self.repository, "append_user_message_and_create_run", None)
            if combined is not None:
                user_row, _ = await combined(
                    conversation_id=conversation_id,
                    content=content,
                    requested_model=model_name(settings),
                    provider=provider_name(settings),
                    run_id=run_id,
                    **({"retry_run_id": retry_run_id} if retry_run_id else {}),
                )
            else:
                if retry_run_id:
                    raise StudioRepositoryError("Repository does not support retry")
                user_row = await self.repository.append_message(conversation_id=conversation_id, role="user", content=content)
                await self.repository.create_run(
                    conversation_id=conversation_id,
                    user_message_id=int(user_row["id"]),
                    requested_model=model_name(settings),
                    provider=provider_name(settings),
                    run_id=run_id,
                )
            handle = self.registry.begin(run_id)
            renamer = getattr(self.repository, "rename_conversation", None)
            if renamer is not None:
                title = " ".join(content.split())
                title = title[:77].rsplit(" ", 1)[0] + "…" if len(title) > 80 else title
                await renamer(conversation_id, title=title, only_default=True)
        except ActiveRunExists:
            if user_row is not None and retry_run_id is None:
                remover = getattr(self.repository, "delete_message", None)
                if remover is not None:
                    try:
                        await remover(conversation_id=conversation_id, message_id=int(user_row["id"]))
                    except Exception:  # noqa: BLE001 - compensation must not fail the 409
                        pass
            return JSONResponse({"error": {"code": "run_already_active", "message": "This conversation already has an active run.", "retryable": False}}, status_code=409)
        except ConversationNotFound:
            return JSONResponse({"error": {"code": "conversation_not_found", "message": "Conversation not found.", "retryable": False}}, status_code=404)
        except StudioRepositoryError:
            return JSONResponse({"error": {"code": "conversation_write_failed", "message": "The conversation could not be updated.", "retryable": True}}, status_code=409)

        if retry_run_id:
            # Reuse the server-owned request. Failed output after it is not
            # evidence or conversation context for the replacement attempt.
            content = str(user_row["content"])
            user_message = user_message.model_copy(update={"content": content})
            history_rows = [row for row in history_rows if int(row["id"]) < int(user_row["id"])]

        # Strip client-supplied history, frontend tools, and context. Only the
        # newly accepted user message is sent through AG-UI; persisted history
        # is supplied separately by the server below.
        run_input = run_input.model_copy(
            update={
                "run_id": str(run_id),
                "messages": [user_message],
                "tools": [],
                "context": [],
            }
        )
        try:
            from .connections import prepare_model
            await prepare_model(settings)
            agent = self.agent_factory(settings)
        except Exception as exc:  # noqa: BLE001 - convert setup failures to a safe response
            code, message, retryable = _safe_error(exc)
            from .diagnostics import build_run_diagnostics

            setup_diagnostics = build_run_diagnostics(
                exc, phase="startup", requested_model=model_name(settings),
                elapsed_ms=0, requests=0, tool_calls=0,
            )
            await self.repository.set_run_status(
                run_id,
                status="failed",
                stage="failed",
                error_code=code,
                error_message=message,
                usage=normalize_usage(None, latency_ms=0),
                diagnostics=setup_diagnostics,
            )
            emit_observation(
                log,
                "run",
                conversation_id=conversation_id,
                run_id=run_id,
                status="failed",
                stage="failed",
                duration_ms=0,
                provider=provider_name(settings),
                requested_model=model_name(settings),
                prompt_version=PROMPT_VERSION,
                error_code=code,
                exception_class=exc.__class__.__name__,
                upstream_class=setup_diagnostics.get("exception_class"),
                phase="startup",
                status_code=safe_status_code(exc),
                provider_code=setup_diagnostics.get("provider_code"),
                _level=logging.WARNING,
            )
            self.registry.finish(run_id)
            return JSONResponse({"error": {"code": code, "message": message, "retryable": retryable}}, status_code=409)
        adapter = AGUIAdapter(agent=agent, run_input=run_input, accept=SSE_CONTENT_TYPE, manage_system_prompt="server")
        server_history = _model_history(history_rows)
        evidence_state = await _conversation_evidence_state(
            self.repository, conversation_id=conversation_id, channel_id=int(conversation["channel_id"]),
        )
        run_instructions = (
            "Server-owned conversation retrieval state (counts describe stored evidence, not factual verification):\n"
            "<conversation_evidence_state>\n" + json.dumps(evidence_state, ensure_ascii=False) +
            "\n</conversation_evidence_state>\n"
            "Unavailable state is unknown, not an empty archive. The source catalog can discover stored IDs and reopen read text. "
            "Choose actions from the latest user request and available evidence. No fixed tool sequence is required."
        )
        instruction_getter = getattr(self.repository, "get_system_prompt", None)
        channel_instructions = (
            await instruction_getter(int(conversation["channel_id"])) if instruction_getter else ""
        )
        run_instructions = (run_instructions or "") + (
            "\nAnswer the latest user message. Earlier turns are completed context, not a backlog. "
            "Do not repeat completed research or drafts unless asked. Previous assistant prose is not source evidence."
        )
        if channel_instructions:
            run_instructions += (
                "\nChannel owner's standing editorial instructions (apply across this channel's conversations only; "
                "the latest user request may refine them; security and evidence rules still apply):\n"
                + channel_instructions
            )
        profile_getter = getattr(self.repository, "get_profile", None)
        channel_profile = await profile_getter(int(conversation["channel_id"])) if profile_getter is not None else None
        run_instructions += profile_instructions(channel_profile)

        queue: asyncio.Queue[Any] = asyncio.Queue()
        complete = object()
        worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        run_started_monotonic = time.monotonic()
        usage_holder: dict[str, Any] = {}
        run_usage = RunUsage()
        actual_model_holder: dict[str, str] = {}
        final_output_holder: dict[str, str] = {}
        observed_tool_calls = 0
        saw_model_output = False

        def _run_elapsed_ms() -> int:
            return int(max(0, (time.monotonic() - run_started_monotonic) * 1000))

        def _run_requests() -> int:
            try:
                return max(0, int(normalize_usage(usage_holder or run_usage).get("requests", 0)))
            except Exception:  # noqa: BLE001 - diagnostics must never crash the run
                return 0

        def _failure_phase() -> str:
            from .diagnostics import phase_for_failure

            return phase_for_failure(
                tool_calls_started=observed_tool_calls, model_output_seen=saw_model_output
            )

        def _failure_diagnostics(exc: BaseException) -> dict[str, Any]:
            from .diagnostics import build_run_diagnostics

            stored = dict(getattr(exc, "diagnostics", None) or ())
            if stored:
                return stored
            return build_run_diagnostics(
                exc,
                phase=_failure_phase(),
                requested_model=model_name(settings),
                elapsed_ms=_run_elapsed_ms(),
                requests=_run_requests(),
                tool_calls=observed_tool_calls,
            )
        async def persist_model_usage(usage: RunUsage) -> None:
            snapshot = normalize_usage(usage, latency_ms=_run_elapsed_ms())
            await self.repository.set_run_status(
                run_id, status="running", stage="running", worker_id=worker_id, usage=snapshot,
            )
            emit_observation(log, "model", conversation_id=conversation_id, run_id=run_id,
                             requested_model=model_name(settings), usage=snapshot)

        async def observe_input_budget(snapshot: dict[str, Any]) -> None:
            emit_observation(log, "input_budget", conversation_id=conversation_id, run_id=run_id, **snapshot)

        deps = StudioDeps(
            repository=self.repository,
            workspace_id=self.repository.workspace_id,
            conversation_id=conversation_id,
            channel_id=int(conversation["channel_id"]),
            cancel_event=handle.cancel_event,
            research=self.research_service,
            on_model_response=persist_model_usage,
            on_input_budget=observe_input_budget,
            owner_messages=tuple(
                [str(row.get("content") or "") for row in history_rows if row.get("role") == "user"]
                + [_text_from_user_message(user_message)]
            ),
        )

        async def emit_terminal_notice(current: dict[str, Any] | None) -> bool:
            """Finish a run that was terminalized by another worker or sweep."""

            if not current or current.get("status") not in {"cancelled", "interrupted"}:
                return False
            user_cancelled = current.get("status") == "cancelled"
            code = "run_cancelled" if user_cancelled else "run_interrupted"
            message = "Run cancelled by the user." if user_cancelled else "The worker stopped before completion."
            usage = normalize_usage(
                usage_holder or run_usage,
                latency_ms=int(max(0, (time.monotonic() - run_started_monotonic) * 1000)),
            )
            usage["tool_calls"] = max(int(usage.get("tool_calls", 0)), observed_tool_calls)
            events = await self.repository.get_events(run_id)
            if not any(event.get("event_type") in {"RUN_CANCELLED", "RUN_INTERRUPTED", "RUN_ERROR"} for event in events):
                await self.repository.append_event(run_id, event_type="RUN_CANCELLED" if user_cancelled else "RUN_INTERRUPTED", safe_payload={"usage": usage})
            await self.repository.append_message(
                conversation_id=conversation_id,
                role="assistant",
                content=message,
                metadata={"run_id": str(run_id), "prompt_version": PROMPT_VERSION, "run_failed": not user_cancelled},
            )
            terminal_message_id = f"studio-terminal-{run_id}"
            await queue.put(TextMessageStartEvent(message_id=terminal_message_id, role="assistant"))
            await queue.put(TextMessageContentEvent(message_id=terminal_message_id, delta=message))
            await queue.put(TextMessageEndEvent(message_id=terminal_message_id))
            await queue.put(RunErrorEvent(message=message, code=code))
            emit_observation(
                log,
                "run",
                conversation_id=conversation_id,
                run_id=run_id,
                status=current.get("status"),
                stage=current.get("stage") or current.get("status"),
                duration_ms=usage.get("latency_ms"),
                provider=provider_name(settings),
                requested_model=model_name(settings),
                actual_model=actual_model_holder.get("value"),
                prompt_version=PROMPT_VERSION,
                usage=usage,
                error_code=code,
            )
            return True

        async def capture_completion(result: Any) -> None:
            """Capture the final reply and bounded usage, excluding retry prose."""

            output = getattr(result, "output", None)
            if isinstance(output, str):
                final_output_holder["value"] = output
            try:
                usage_holder.clear()
                usage_holder.update(normalize_usage(getattr(result, "usage", None)))
            except Exception:  # noqa: BLE001 - observability must not fail a run
                usage_holder.clear()
            try:
                response = getattr(result, "response", None)
                model = getattr(response, "model_name", None)
                if model:
                    actual_model_holder["value"] = str(model)[:160]
            except Exception:  # noqa: BLE001 - observability must not fail a run
                return

        async def execute_run() -> None:
            nonlocal observed_tool_calls, saw_model_output
            output_parts: list[str] = []
            finished_event = None
            watchdog: asyncio.Task[Any] | None = None
            run_finished = False
            lease_seconds = max(30, int(limits.RUN_LEASE_SECONDS))
            # Claim before waiting for a local slot so a queued run is owned
            # while it waits; the heartbeat below renews that ownership.
            claimed = await self.repository.claim_run(
                run_id,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            )
            if claimed is None:
                await emit_terminal_notice(await self.repository.get_run(run_id))
                self.registry.finish(run_id)
                await queue.put(complete)
                return

            async def _queued_heartbeat() -> None:
                await self.repository.renew_run_lease(
                    run_id, worker_id=worker_id, lease_seconds=lease_seconds
                )
                await self.repository.append_event(
                    run_id, event_type="RUN_QUEUED", safe_payload={"lease_seconds": lease_seconds}
                )

            heartbeat_interval = max(0.05, min(float(limits.RUN_HEARTBEAT_SECONDS), lease_seconds / 3))
            try:
                async with self.coordinator.slot(heartbeat=_queued_heartbeat, interval=heartbeat_interval):
                    watchdog = asyncio.create_task(
                        self._watch_run_control(run_id, worker_id, handle),
                        name=f"studio-run-watchdog-{run_id}",
                    )
                    async with asyncio.timeout(max(1.0, float(limits.RUN_TIMEOUT_SECONDS))):
                        async for event in adapter.run_stream(
                            message_history=server_history,
                            instructions=run_instructions,
                            conversation_id=str(conversation_id),
                            run_id=str(run_id),
                            deps=deps,
                            usage=run_usage,
                            usage_limits=UsageLimits(
                                # A post pipeline job adds its own model requests and a
                                # finalizing turn may still save the draft.
                                request_limit=max(2, int(limits.MAX_TOOL_CALLS) + 2 + int(limits.PIPELINE_MAX_REQUESTS)),
                                # Safety net: soft caps and finalization end research earlier, and calls
                                # answered with "answer now" while finishing still count here.
                                tool_calls_limit=max(1, int(limits.HARD_TOOL_CALLS)),
                                output_tokens_limit=max(64, int(limits.MAX_RUN_OUTPUT_TOKENS)),
                                input_tokens_limit=int(limits.MAX_RUN_INPUT_TOKENS) + int(limits.PIPELINE_MAX_INPUT_TOKENS),
                                per_request_input_tokens_limit=int(limits.MAX_INPUT_TOKENS),
                            ),
                            on_complete=capture_completion,
                        ):
                            event_type = _event_type(event)
                            if event_type == "TOOL_CALL_START":
                                observed_tool_calls += 1
                            delta = getattr(event, "delta", None)
                            if event_type == "TEXT_MESSAGE_START":
                                output_parts.clear()
                            if event_type == "TEXT_MESSAGE_CONTENT" and delta:
                                output_parts.append(str(delta))
                            # Tool activity remains live. Only the accepted
                            # final reply is shown, not provisional narration.
                            if event_type.startswith(("TEXT_MESSAGE_", "THINKING_", "REASONING_")):
                                saw_model_output = True
                                continue
                            event_payload = _safe_event_payload(event)
                            if event_type == "RUN_FINISHED":
                                run_finished = True
                                event_payload["usage"] = normalize_usage(
                                    usage_holder or run_usage,
                                    latency_ms=int(max(0, (time.monotonic() - run_started_monotonic) * 1000)),
                                )
                            elif event_type == "RUN_ERROR":
                                # The adapter exposes provider/model failures as
                                # terminal protocol events. Classify the upstream
                                # message immediately, before AG-UI wrapping
                                # erases it, and carry the result on the
                                # sentinel. Let the recovery block decide whether
                                # this is a failed run or a successfully saved
                                # artifact with only its optional chat
                                # acknowledgement missing.
                                from .diagnostics import describe_upstream_failure

                                upstream_message = (
                                    getattr(event, "message", "")
                                    or "The provider ended the AG-UI run with an error."
                                )
                                diagnostics = getattr(adapter, "failure_diagnostics", None)
                                if not diagnostics:
                                    diagnostics = describe_upstream_failure(upstream_message)
                                diagnostics = {
                                    **diagnostics,
                                    "phase": _failure_phase(),
                                    "elapsed_ms": _run_elapsed_ms(),
                                    "requested_model": model_name(settings),
                                    "requests": _run_requests(),
                                    "tool_calls": observed_tool_calls,
                                }
                                failure = _UpstreamRunError(upstream_message, diagnostics=diagnostics)
                                failure.safe_failure = getattr(adapter, "safe_failure", None)
                                raise failure
                            await self.repository.append_event(
                                run_id,
                                event_type=event_type,
                                safe_payload=event_payload,
                                result_content=_tool_result_content(event),
                            )
                            if event_type in {"TOOL_CALL_START", "TOOL_CALL_RESULT"}:
                                emit_observation(
                                    log,
                                    "tool",
                                    conversation_id=conversation_id,
                                    run_id=run_id,
                                    tool_name=event_payload.get("tool_name"),
                                    event_type=event_type,
                                    status="completed" if event_type == "TOOL_CALL_RESULT" else "started",
                                    duration_ms=int(max(0, (time.monotonic() - run_started_monotonic) * 1000)),
                                    result_count=event_payload.get("result_count"),
                                    source_count=event_payload.get("source_count"),
                                    story_count=event_payload.get("story_count"),
                                    cache_hit=event_payload.get("cache_hit"),
                                    degraded=event_payload.get("degraded"),
                                    provider=event_payload.get("provider"),
                                    analysis_id=event_payload.get("analysis_id"),
                                    story_cluster_id=event_payload.get("story_cluster_id"),
                                    draft_id=event_payload.get("draft_id"),
                                )
                            if handle.cancelled:
                                raise asyncio.CancelledError
                            if event_type == "RUN_FINISHED":
                                finished_event = event
                            else:
                                await queue.put(event)
                    if not run_finished:
                        ended = _UpstreamRunError("The AG-UI run ended without a completion event.")
                        ended.diagnostics = _failure_diagnostics(ended)
                        raise ended
                    if handle.cancelled:
                        raise asyncio.CancelledError
                assistant_text = (final_output_holder["value"] if "value" in final_output_holder else "".join(output_parts)).strip()
                if not assistant_text:
                    assistant_text = "Работа завершена. Результаты инструментов сохранены; текстовый ответ модели не поступил."
                if assistant_text:
                    await self.repository.append_message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=assistant_text,
                        metadata={"run_id": str(run_id), "prompt_version": PROMPT_VERSION},
                    )
                usage = normalize_usage(
                    usage_holder or run_usage,
                    latency_ms=int(max(0, (time.monotonic() - run_started_monotonic) * 1000)),
                )
                usage["tool_calls"] = max(int(usage.get("tool_calls", 0)), observed_tool_calls)
                actual_model = actual_model_holder.get("value") or model_name(settings)
                await self.repository.set_run_status(
                    run_id,
                    status="succeeded",
                    stage="complete",
                    actual_model=actual_model,
                    usage=usage,
                    worker_id=worker_id,
                )
                final_message_id = f"studio-final-{run_id}"
                await queue.put(TextMessageStartEvent(message_id=final_message_id, role="assistant"))
                await queue.put(TextMessageContentEvent(message_id=final_message_id, delta=assistant_text))
                await queue.put(TextMessageEndEvent(message_id=final_message_id))
                if finished_event is not None:
                    await queue.put(finished_event)
                emit_observation(
                    log,
                    "run",
                    conversation_id=conversation_id,
                    run_id=run_id,
                    status="succeeded",
                    stage="complete",
                    duration_ms=usage.get("latency_ms"),
                    provider=provider_name(settings),
                    requested_model=model_name(settings),
                    actual_model=actual_model,
                    prompt_version=PROMPT_VERSION,
                    usage=usage,
                )
            except asyncio.CancelledError:
                current = await self.repository.get_run(run_id)
                if await emit_terminal_notice(current):
                    return
                user_cancelled = bool(current and current.get("cancel_requested")) or handle.cancel_reason == "user"
                event_type = "RUN_CANCELLED" if user_cancelled else "RUN_INTERRUPTED"
                status = "cancelled" if user_cancelled else "interrupted"
                code = "run_cancelled" if user_cancelled else "run_interrupted"
                message = "Run cancelled by the user." if user_cancelled else "The worker stopped before completion."
                cancel_diagnostics = {
                    "phase": _failure_phase(),
                    "error_code": code,
                    "exception_class": "CancelledError" if user_cancelled else "InterruptedError",
                    "status_code": None,
                    "provider_code": None,
                    "requested_model": model_name(settings),
                    "elapsed_ms": _run_elapsed_ms(),
                    "requests": _run_requests(),
                    "tool_calls": observed_tool_calls,
                }
                if current and current.get("status") in {"queued", "running"}:
                    usage = normalize_usage(
                        usage_holder or run_usage,
                        latency_ms=int(max(0, (time.monotonic() - run_started_monotonic) * 1000)),
                    )
                    await self.repository.append_event(run_id, event_type=event_type, safe_payload={"usage": usage, "diagnostics": cancel_diagnostics})
                    with suppress(RunClaimLost):
                        await self.repository.set_run_status(
                            run_id,
                            status=status,
                            stage=status,
                            error_code=code,
                            error_message=message,
                            usage=usage,
                            worker_id=worker_id,
                            diagnostics=cancel_diagnostics,
                        )
                    emit_observation(
                        log,
                        "run",
                        conversation_id=conversation_id,
                        run_id=run_id,
                        status=status,
                        stage=status,
                        duration_ms=usage.get("latency_ms"),
                        provider=provider_name(settings),
                        requested_model=model_name(settings),
                        actual_model=actual_model_holder.get("value"),
                        prompt_version=PROMPT_VERSION,
                        usage=usage,
                        error_code=code,
                        phase=cancel_diagnostics["phase"],
                        _level=logging.WARNING,
                    )
            except Exception as exc:  # noqa: BLE001 - persist only a safe classification
                code, message, _ = _safe_error(exc)
                diagnostics = _failure_diagnostics(exc)
                current = await self.repository.get_run(run_id)
                if await emit_terminal_notice(current):
                    return
                if current and current.get("status") in {"queued", "running"}:
                    usage = normalize_usage(
                        usage_holder or run_usage,
                        latency_ms=int(max(0, (time.monotonic() - run_started_monotonic) * 1000)),
                    )
                    usage["tool_calls"] = max(int(usage.get("tool_calls", 0)), observed_tool_calls)
                    artifact_ready = bool(
                        {"create_draft", "edit_draft"}.intersection(deps.completed_tools)
                    )
                    if artifact_ready:
                        failure_text = (
                            "The draft artifact is ready. The model's separate chat summary was unavailable, "
                            "so the saved artifact is the result of this run."
                        )
                    elif "search_web" in deps.completed_tools:
                        failure_text = (
                            "Web results were saved, but the model did not produce a final answer. "
                            "Retry to continue from the saved research."
                        )
                    else:
                        failure_text = message
                    try:
                        await self.repository.append_message(
                            conversation_id=conversation_id,
                            role="assistant",
                            content=failure_text,
                            metadata={
                                "run_id": str(run_id),
                                "prompt_version": PROMPT_VERSION,
                                "run_failed": not artifact_ready,
                                "run_recovered": artifact_ready,
                            },
                        )
                    except Exception as append_exc:  # noqa: BLE001 - a failed acknowledgement must still finish the run
                        log.warning("failure acknowledgement could not be saved: %s", type(append_exc).__name__)
                        with suppress(RunClaimLost):
                            await self.repository.set_run_status(
                                run_id,
                                status="failed",
                                stage="failed",
                                error_code=code,
                                error_message=message,
                                usage=usage,
                                worker_id=worker_id,
                            )
                        return
                    failure_message_id = f"studio-failure-{run_id}"
                    await queue.put(TextMessageStartEvent(message_id=failure_message_id, role="assistant"))
                    await queue.put(TextMessageContentEvent(message_id=failure_message_id, delta=failure_text))
                    await queue.put(TextMessageEndEvent(message_id=failure_message_id))
                    if artifact_ready:
                        await self.repository.append_event(
                            run_id,
                            event_type="RUN_FINISHED",
                            safe_payload={"recovered": True, "usage": usage},
                        )
                        with suppress(RunClaimLost):
                            await self.repository.set_run_status(
                                run_id,
                                status="succeeded",
                                stage="complete",
                                actual_model=actual_model_holder.get("value") or model_name(settings),
                                usage=usage,
                                worker_id=worker_id,
                            )
                        await queue.put(
                            RunFinishedEvent(
                                thread_id=str(conversation_id),
                                run_id=str(run_id),
                                outcome=RunFinishedSuccessOutcome(),
                            )
                        )
                        emit_observation(
                            log,
                            "run",
                            conversation_id=conversation_id,
                            run_id=run_id,
                            status="succeeded",
                            stage="complete",
                            duration_ms=usage.get("latency_ms"),
                            provider=provider_name(settings),
                            requested_model=model_name(settings),
                            actual_model=actual_model_holder.get("value"),
                            prompt_version=PROMPT_VERSION,
                            usage=usage,
                            error_code="final_reply_recovered",
                        )
                        return
                    await queue.put(RunErrorEvent(message=message, code=code))
                    await self.repository.append_event(
                        run_id,
                        event_type="RUN_ERROR",
                        safe_payload={"code": code, "usage": usage, "diagnostics": diagnostics},
                    )
                    with suppress(RunClaimLost):
                        await self.repository.set_run_status(
                            run_id,
                            status="failed",
                            stage="failed",
                            error_code=code,
                            error_message=message,
                            usage=usage,
                            worker_id=worker_id,
                            diagnostics=diagnostics,
                        )
                    emit_observation(
                        log,
                        "run",
                        conversation_id=conversation_id,
                        run_id=run_id,
                        status="failed",
                        stage="failed",
                        duration_ms=usage.get("latency_ms"),
                        provider=provider_name(settings),
                        requested_model=model_name(settings),
                        actual_model=actual_model_holder.get("value"),
                        prompt_version=PROMPT_VERSION,
                        usage=usage,
                        error_code=code,
                        exception_class=exc.__class__.__name__,
                        upstream_class=diagnostics.get("exception_class"),
                        failure_kind=diagnostics.get("failure_kind"),
                        phase=diagnostics.get("phase"),
                        status_code=safe_status_code(exc),
                        provider_code=diagnostics.get("provider_code"),
                        _level=logging.WARNING,
                    )
            finally:
                if watchdog is not None:
                    watchdog.cancel()
                    try:
                        with suppress(asyncio.CancelledError, Exception):
                            await watchdog
                    finally:
                        self.registry.finish(run_id)
                        await queue.put(complete)
                else:
                    self.registry.finish(run_id)
                    await queue.put(complete)

        task = asyncio.create_task(execute_run(), name=f"studio-run-{run_id}")
        self.registry.attach(run_id, task)

        async def event_stream() -> AsyncIterator[Any]:
            # The worker task is intentionally not cancelled when the HTTP
            # stream disconnects. Durable run/event state remains authoritative
            # and the completed message is available after reload.
            while True:
                event = await queue.get()
                if event is complete:
                    break
                yield event

        return StreamingResponse(adapter.encode_stream(event_stream()), media_type=SSE_CONTENT_TYPE)

    async def cancel(self, run_id: uuid.UUID) -> dict[str, Any]:
        run = await self.repository.get_run(run_id)
        if run is None:
            raise RunNotFound("run is not part of the active workspace")
        if run["status"] not in {"queued", "running"}:
            return run
        updated = await self.repository.request_cancel(run_id)
        self.registry.cancel(run_id, reason="user")
        # A queued run may not have attached its HTTP stream yet. Complete its
        # durable cancellation immediately; an attached stream is idempotent.
        if updated.get("status") == "queued":
            updated = await self.repository.set_run_status(
                run_id,
                status="cancelled",
                stage="cancelled",
                error_code="run_cancelled",
                error_message="Run cancelled by the user.",
                diagnostics={
                    "phase": "startup",
                    "error_code": "run_cancelled",
                    "exception_class": "CancelledError",
                    "status_code": None,
                    "provider_code": None,
                    "requested_model": model_name(self.settings),
                    "elapsed_ms": 0,
                    "requests": 0,
                    "tool_calls": 0,
                },
            )
            await self.repository.append_event(run_id, event_type="RUN_CANCELLED", safe_payload={})
        return updated
