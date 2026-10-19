"""Provider-independent input budgeting without replaying a model request."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from functools import lru_cache
import json
from typing import Any

from pydantic_ai import RunContext, UsageLimitExceeded
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import InstructionPart, ModelMessagesTypeAdapter, ModelRequest, ToolReturnPart
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


def _instructions(request: ModelRequestContext) -> list[InstructionPart]:
    parts = request.model_request_parameters.instruction_parts
    if parts is not None:
        return list(parts)
    # SDK direct-call contract: the latest request, or the preceding request
    # when the latest is a tool-result-only continuation. Normal agent calls
    # always provide structured instruction_parts, including an explicit [].
    recent = [m for m in reversed(request.messages) if isinstance(m, ModelRequest)][:2]
    if recent:
        latest = recent[0]
        if latest.instructions is not None:
            return [InstructionPart(content=latest.instructions)]
        if len(recent) == 2 and all(p.part_kind in {"tool-return", "retry-prompt"} for p in latest.parts):
            if recent[1].instructions is not None:
                return [InstructionPart(content=recent[1].instructions)]
    return []


def estimate_request_tokens(request: ModelRequestContext) -> int:
    """Count semantic input, not the SDK's persisted execution history.

    Every ModelRequest records effective instructions for replay/debugging.
    Adapters send the current instruction parts once. Response usage, run IDs,
    provider details and tool timestamps likewise are not model input.
    """
    params = request.model_request_parameters
    instructions = [part.content for part in _instructions(request)]
    overhead = estimate_tokens(json.dumps({
        "tools": [asdict(tool) for tool in (*params.function_tools, *params.output_tools)],
        "instructions": instructions,
    }, default=str, ensure_ascii=False))
    content = ModelMessagesTypeAdapter.dump_json(request.messages, include={
        "__all__": {"parts": {"__all__": {"part_kind", "content", "tool_name", "tool_call_id", "args"}}},
    }).decode()
    # Conservative allowance for provider framing and tokenization differences.
    return overhead + estimate_tokens(content) + 1024


class StudioInputBudget(AbstractCapability):
    def __init__(self):
        self._finalizing = False

    async def for_run(self, ctx):
        # One agent can serve concurrent runs; lifecycle state is run-local.
        return StudioInputBudget()

    async def before_tool_execute(self, ctx, *, call, tool_def, args):
        if self._finalizing:
            raise UsageLimitExceeded("The remaining run budget is reserved for a final response.")
        return args

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
        request_context.messages = messages
        def count() -> int:
            return estimate_request_tokens(request_context)

        remaining = max(0, limits.MAX_RUN_INPUT_TOKENS - ctx.usage.input_tokens)
        target = min(limits.MAX_INPUT_TOKENS, remaining)
        compacted = 0
        # Prefer older results; the latest can also yield to the hard bound.
        # All detailed records remain in the scoped archive, never overwritten.
        for part, payload in research:
            if count() <= target:
                break
            part.content = _compact(payload)
            compacted += 1
        estimated = count()
        # Fund this request and another similarly sized turn before permitting
        # more tool work. Otherwise use the funded turn for final synthesis.
        finalizing = remaining < 2 * estimated or ctx.usage.tool_calls >= limits.MAX_TOOL_CALLS
        self._finalizing = finalizing
        if finalizing:
            params = request_context.model_request_parameters
            instructions = _instructions(request_context)
            instructions.append(InstructionPart(content=(
                "The remaining run budget is reserved for your final response. Tools are unavailable for this turn. "
                "Answer the latest user request using evidence already present. State material gaps and research "
                "limits; never claim unread sources were read, missing details verified, or unsaved artifacts saved."
            )))
            request_context.model_request_parameters = replace(params, function_tools=[], instruction_parts=instructions)
            request_context.model_settings = {**(request_context.model_settings or {}), "tool_choice": "none"}
            estimated = count()
        callback = getattr(getattr(ctx, "deps", None), "on_input_budget", None)
        if callback is not None:
            await callback({"estimated_input_tokens": estimated, "remaining_input_tokens": remaining,
                            "input_limit_tokens": target, "compacted_results": compacted,
                            "finalizing": finalizing, "status": "rejected" if estimated > target else "ready"})
        if estimated > target:
            raise UsageLimitExceeded("Studio input budget reached; narrow the request or start a new conversation.")
        return request_context
