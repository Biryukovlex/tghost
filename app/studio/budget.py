"""Provider-independent input budgeting without replaying a model request."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from functools import lru_cache
import json
from typing import Any

from pydantic_ai import RunContext, UsageLimitExceeded
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessagesTypeAdapter, ToolReturnPart
from pydantic_ai.models import ModelRequestContext

from .. import limits


@lru_cache(maxsize=1)
def _encoding():
    try:
        import tiktoken
        return tiktoken.get_encoding("o200k_base")
    except Exception:
        # Cache an unavailable tokenizer too, rather than attempting network
        # initialization again for every estimate in an offline installation.
        return None


def estimate_tokens(text: str) -> int:
    encoding = _encoding()
    if encoding is None:
        return len(text.encode("utf-8"))
    return len(encoding.encode(text, disallowed_special=()))


def _payload(value: Any) -> dict[str, Any] | None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return deepcopy(value) if isinstance(value, dict) else None


def _compact(payload: dict[str, Any]) -> dict[str, Any]:
    keys = ("source_id", "url", "title", "published_at", "accessible", "status", "has_read_content", "warnings")
    return {
        "status": "compacted",
        "continuation": "Use get_research_sources with selected_source_ids and include_content=true for stored details; read_sources can fetch unread pages.",
        "selected_source_ids": payload.get("selected_source_ids", []) or [s.get("source_id") for s in payload.get("sources", [])],
        "sources": [{k: s[k] for k in keys if k in s} for s in payload.get("sources", [])],
        "stories": [{k: s[k] for k in ("cluster_id", "headline", "source_ids", "channel_evidence_ids", "channel_evidence_ref") if k in s} for s in payload.get("stories", [])],
        "channel_evidence_sets": payload.get("channel_evidence_sets", {}),
        "search_outcome": payload.get("search_outcome"),
        "warnings": payload.get("warnings", []),
    }


class StudioInputBudget(AbstractCapability):
    async def after_model_request(self, ctx: RunContext[Any], *, request_context: ModelRequestContext, response):
        callback = getattr(ctx.deps, "on_model_response", None)
        if callback is not None:
            # PydanticAI adds this response to ctx.usage after this hook.
            await callback(ctx.usage + response.usage)
        return response

    async def before_model_request(self, ctx: RunContext[Any], request_context: ModelRequestContext) -> ModelRequestContext:
        # Copy parts as well as messages: compaction must not mutate the event
        # archive or the original tool output inspected by the owner.
        messages = deepcopy(request_context.messages)
        seen: set[str] = set()
        research: list[tuple[ToolReturnPart, dict[str, Any]]] = []
        for message in messages:
            for part in message.parts:
                if not isinstance(part, ToolReturnPart):
                    continue
                payload = _payload(part.content)
                if payload is None:
                    continue
                evidence = payload.get("channel_evidence_sets")
                if isinstance(evidence, dict):
                    payload["channel_evidence_sets"] = {key: value for key, value in evidence.items() if key not in seen}
                    seen.update(evidence)
                part.content = payload
                if part.tool_name in {"search_web", "read_sources", "compare_sources", "find_novel_topics", "get_research_sources"}:
                    research.append((part, payload))
        params = request_context.model_request_parameters
        overhead = estimate_tokens(json.dumps({
            "tools": [asdict(tool) for tool in params.function_tools],
            "instructions": [asdict(item) for item in (params.instruction_parts or [])],
        }, default=str, ensure_ascii=False)) + 1024

        def count() -> int:
            return overhead + estimate_tokens(ModelMessagesTypeAdapter.dump_json(messages).decode())

        target = min(limits.MAX_INPUT_TOKENS, limits.MAX_RUN_INPUT_TOKENS - ctx.usage.input_tokens)
        # Keep the latest research result intact. Older evidence stays linkable
        # and can be re-opened from the scoped server archive without a search.
        for part, payload in research[:-1]:
            if count() <= target:
                break
            part.content = _compact(payload)
        if count() > target:
            raise UsageLimitExceeded("Studio input budget reached; narrow the request or start a new conversation.")
        request_context.messages = messages
        return request_context
