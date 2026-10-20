"""One boundary for model-supplied tool arguments.

Smaller models routinely add arguments a tool does not declare (for example a
``channel_id`` for a tool whose scope the server supplies) or wrap values in
the wrong JSON shape. Rejecting those calls makes the model retry the same
mistake until the run fails. This capability applies two schema-guided rules
to every tool, with no per-tool or per-model code:

* undeclared arguments are ignored and reported back in the tool result, so
  they can never carry meaning the tool does not understand;
* obvious shape mistakes are repaired only when the declared schema makes the
  intended shape unambiguous (a JSON-encoded list, a single-key wrapper object
  around a list, a numeric or boolean string).

Missing or mistyped declared arguments still fail validation normally.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic_ai.capabilities import AbstractCapability


def _types(schema: Any) -> set[str]:
    if not isinstance(schema, dict):
        return set()
    kinds: set[str] = set()
    declared = schema.get("type")
    if isinstance(declared, str):
        kinds.add(declared)
    elif isinstance(declared, list):
        kinds.update(str(item) for item in declared)
    for alternative in schema.get("anyOf", []) or []:
        kinds |= _types(alternative)
    return kinds


def normalize_value(value: Any, schema: Any) -> Any:
    """Repair a value only when the declared schema leaves a single reading."""

    kinds = _types(schema)
    if not kinds:
        return value
    while "object" not in kinds and isinstance(value, dict) and len(value) == 1:
        value = next(iter(value.values()))
    if "array" in kinds and not isinstance(value, list):
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                except ValueError:
                    parsed = None
                if isinstance(parsed, list):
                    return parsed
            if "string" not in kinds:
                return [value] if stripped else []
    if "integer" in kinds and "string" not in kinds and isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    if "boolean" in kinds and "string" not in kinds and isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    return value


class ToolArgumentBoundary(AbstractCapability):
    """Ignore undeclared arguments and repair unambiguous shapes for all tools."""

    def __init__(self) -> None:
        self._ignored: dict[str, list[str]] = {}

    async def for_run(self, ctx):
        # Ignored-argument notes are per tool call within one run.
        return ToolArgumentBoundary()

    async def before_tool_validate(self, ctx, *, call, tool_def, args):
        parsed: Any = args
        if isinstance(args, str):
            try:
                parsed = json.loads(args or "{}")
            except ValueError:
                return args
        if not isinstance(parsed, dict):
            return args
        schema = tool_def.parameters_json_schema or {}
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return args
        ignored = [key for key in parsed if key not in properties] if schema.get("additionalProperties") is False else []
        fixed = {key: normalize_value(value, properties[key]) if key in properties else value
                 for key, value in parsed.items() if key not in ignored}
        if ignored:
            self._ignored[call.tool_call_id] = ignored
        if not ignored and fixed == parsed:
            return args
        return fixed

    async def after_tool_execute(self, ctx, *, call, tool_def, args, result):
        ignored = self._ignored.pop(call.tool_call_id, None)
        if not ignored:
            return result
        note = {"ignored_arguments": ignored,
                "argument_note": "These arguments are not part of this tool and were ignored. The application supplies channel and chat scope."}
        if isinstance(result, dict):
            return {**result, **note}
        if hasattr(result, "model_dump"):
            return {**result.model_dump(mode="json"), **note}
        return result
