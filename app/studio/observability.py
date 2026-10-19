"""Privacy-safe run observability helpers.

The Studio deliberately keeps the provider boundary small.  This module is the
single place where provider usage and exception objects become data that may be
persisted or emitted to logs.  Callers should pass the returned projections,
never the original provider response or exception string.
"""

from __future__ import annotations

import asyncio
import logging
import json
import math
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from openai import APIError, APIStatusError
from pydantic import ValidationError
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UsageLimitExceeded

from .model import StudioConfigurationError, StudioModelStreamError, ModelStreamFailure


_MAX_COUNT = 1_000_000_000
_MAX_COST_USD = Decimal("1000000000")
_SAFE_DETAIL = re.compile(r"^[a-z][a-z0-9_]{0,48}_tokens$")

# The message is intentionally independent of the exception text.  A provider
# can include request headers, URLs, prompts, or response bodies in an error.
_ERRORS: dict[str, tuple[str, bool]] = {
    "studio_not_configured": ("Studio is not configured for an agent run.", False),
    "provider_consent_required": ("Review and confirm the provider disclosure before using Studio.", False),
    "provider_timeout": ("The agent took too long to respond. Try again.", True),
    "provider_rate_limited": ("The model provider is busy. Try again shortly.", True),
    "provider_unavailable": ("The model provider is unavailable. Try again shortly.", True),
    "provider_auth_failed": ("The model provider rejected the connection. Check or reconnect it in Settings → Studio.", False),
    "provider_payment_required": ("The model provider requires credits or plan access.", False),
    "provider_model_not_found": ("The configured model is not available on OpenRouter. Choose another model in Settings → Studio.", False),
    "provider_bad_request": ("The model provider rejected the request. Check the model and Studio settings.", True),
    "provider_invalid_response": ("The model provider returned an invalid response. Try again.", True),
    "invalid_agent_output": ("The agent returned an invalid response. Try again.", True),
    "run_cancelled": ("Run cancelled by the user.", False),
    "run_interrupted": ("The worker stopped before completion.", True),
    "run_budget_exceeded": ("The agent reached its context or run budget. Narrow the request or start a new conversation.", False),
    "agent_failed": ("The agent could not complete this run. Try again.", True),
}


def _bounded_int(value: Any, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, min(number, _MAX_COUNT))


def _bounded_money(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite() or amount < 0 or amount > _MAX_COST_USD:
        return None
    return float(round(amount, 8))


def _value(source: Any, *names: str) -> Any:
    if isinstance(source, Mapping):
        for name in names:
            if name in source:
                return source[name]
        return None
    for name in names:
        value = getattr(source, name, None)
        if value is not None:
            return value
    return None


def normalize_usage(value: Any = None, *, latency_ms: int | None = None) -> dict[str, Any]:
    """Return a bounded, JSON-safe usage projection.

    PydanticAI exposes ``RunUsage`` while test seams and future providers may
    return dictionaries.  Only token/request counters and a best-effort USD
    amount cross this boundary; arbitrary provider metadata is discarded.
    """

    source = value if value is not None else {}
    input_tokens = _bounded_int(_value(source, "input_tokens", "prompt_tokens", "request_tokens"))
    output_tokens = _bounded_int(_value(source, "output_tokens", "completion_tokens", "response_tokens"))
    explicit_total = _value(source, "total_tokens")
    total_tokens = _bounded_int(explicit_total, default=input_tokens + output_tokens) if explicit_total is not None else min(input_tokens + output_tokens, _MAX_COUNT)
    if total_tokens < input_tokens + output_tokens:
        total_tokens = min(input_tokens + output_tokens, _MAX_COUNT)

    usage: dict[str, Any] = {
        "requests": _bounded_int(_value(source, "requests", "request_count")),
        "tool_calls": _bounded_int(_value(source, "tool_calls")),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    for key in (
        "cache_read_tokens",
        "cache_write_tokens",
        "input_audio_tokens",
        "cache_audio_read_tokens",
        "output_audio_tokens",
    ):
        count = _bounded_int(_value(source, key))
        if count:
            usage[key] = count

    details = _value(source, "details")
    if isinstance(details, Mapping):
        safe_details: dict[str, int] = {}
        for key, item in details.items():
            name = str(key)
            if _SAFE_DETAIL.fullmatch(name):
                count = _bounded_int(item)
                if count:
                    safe_details[name] = count
        if safe_details:
            usage["details"] = dict(sorted(safe_details.items())[:16])

    cost = _bounded_money(_value(source, "estimated_cost_usd", "cost", "estimated_cost"))
    if cost is not None:
        usage["estimated_cost_usd"] = cost

    if latency_ms is not None:
        usage["latency_ms"] = _bounded_int(latency_ms)
    return usage


def duration_ms(started_at: Any, finished_at: Any = None) -> int | None:
    """Calculate a bounded duration from persisted timestamps."""

    if started_at is None:
        return None
    end = finished_at
    if end is None:
        from datetime import datetime, timezone

        end = datetime.now(timezone.utc)
    try:
        seconds = (end - started_at).total_seconds()
    except (AttributeError, TypeError):
        return None
    if not math.isfinite(seconds):
        return None
    return max(0, min(int(seconds * 1000), _MAX_COUNT))


def streaming_error_category(exc: BaseException) -> str | None:
    """Project OpenAI SSE errors without retaining their message or payload."""
    # Provider adapters wrap SDK errors in ModelAPIError and preserve their
    # typed cause. Inspect only that cause's structured error code, not prose.
    if isinstance(exc, ModelAPIError):
        cause = exc.__cause__
        if not isinstance(cause, APIError):
            return None
        exc = cause
    if not isinstance(exc, APIError) or isinstance(exc, APIStatusError):
        return None
    body = getattr(exc, "body", None)
    if not isinstance(body, Mapping):
        return None
    if isinstance(body.get("error"), Mapping):
        body = body["error"]
    categories = {
        "server_error": "provider_unavailable",
        "internal_server_error": "provider_unavailable",
        "rate_limit_exceeded": "provider_rate_limited",
        "rate_limit_error": "provider_rate_limited",
        "authentication_error": "provider_auth_failed",
        "invalid_api_key": "provider_auth_failed",
        "insufficient_quota": "provider_payment_required",
        "invalid_request_error": "provider_bad_request",
        "context_length_exceeded": "provider_bad_request",
    }
    for field in ("code", "type"):
        value = body.get(field)
        if isinstance(value, str) and value in categories:
            return categories[value]
    return None


def safe_error(exc: BaseException) -> tuple[str, str, bool]:
    """Classify an exception without retaining its message or payload."""

    if isinstance(exc, UsageLimitExceeded):
        message, retryable = _ERRORS["run_budget_exceeded"]
        return "run_budget_exceeded", message, retryable
    if isinstance(exc, StudioConfigurationError):
        return "studio_not_configured", _ERRORS["studio_not_configured"][0], False
    if isinstance(exc, asyncio.CancelledError):
        return "run_cancelled", _ERRORS["run_cancelled"][0], False
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "provider_timeout", _ERRORS["provider_timeout"][0], True
    category = streaming_error_category(exc)
    if category:
        message, retryable = _ERRORS[category]
        return category, message, retryable
    if isinstance(exc, StudioModelStreamError):
        message, retryable = _ERRORS["provider_invalid_response"]
        return "provider_invalid_response", message, retryable
    name = exc.__class__.__name__.lower()
    text = name + " " + str(exc).lower()
    status_code = _status_code(exc, text)
    if status_code in {401, 403} or any(token in text for token in ("invalid api key", "unauthorized", "authentication failed")):
        code = "provider_auth_failed"
    elif status_code == 402 or any(token in text for token in ("insufficient credits", "payment required", "payment")):
        code = "provider_payment_required"
    elif status_code == 404 or any(token in text for token in ("no endpoints found", "model not found", "is not a valid model")):
        code = "provider_model_not_found"
    elif status_code == 400:
        code = "provider_bad_request"
    elif status_code is not None and 500 <= status_code <= 599:
        code = "provider_unavailable"
    elif any(token in text for token in ("rate", "quota", "429")):
        code = "provider_rate_limited"
    elif any(token in text for token in ("timeout", "timed out")):
        code = "provider_timeout"
    elif any(token in text for token in ("connect", "connection", "unavailable", "503", "502", "bad gateway")):
        code = "provider_unavailable"
    elif isinstance(exc, ValidationError):
        code = "invalid_agent_output"
    elif any(token in name for token in ("unexpectedmodelbehavior", "usagelimit", "toolretry")):
        code = "invalid_agent_output"
    elif isinstance(exc, ValueError) or "invalid" in name:
        # Preserve the existing Studio contract for validation/parsing errors;
        # the message remains generic and never includes the exception text.
        code = "invalid_agent_output"
    else:
        code = "agent_failed"
    message, retryable = _ERRORS[code]
    if code == "provider_model_not_found":
        configured_model = _model_name(exc, text)
        if configured_model:
            message = f"The model '{configured_model}' is not available on OpenRouter. Choose another model in Settings → Studio."
    return code, message, retryable


def _status_code(exc: BaseException, text: str | None = None) -> int | None:
    value = exc.status_code if isinstance(exc, ModelHTTPError) else getattr(exc, "status_code", None)
    if isinstance(value, int) and 100 <= value <= 599:
        return value
    match = re.search(r"status[_ ]?code\s*[:=]\s*(\d{3})", text or str(exc), re.IGNORECASE)
    return int(match.group(1)) if match else None


def safe_status_code(exc: BaseException) -> int | None:
    """Return only an exception's numeric provider status code."""

    return _status_code(exc, str(exc))


def _model_name(exc: BaseException, text: str | None = None) -> str | None:
    value = getattr(exc, "model_name", None)
    if value is None:
        match = re.search(r"model[_ ]?name\s*[:=]\s*([^,;\s]+)", text or str(exc), re.IGNORECASE)
        value = match.group(1) if match else None
    if value is None:
        return None
    candidate = str(value).strip()
    # Model IDs are configuration, but bound the projection to a conservative
    # identifier alphabet so an exception cannot smuggle provider text through.
    return candidate[:160] if re.fullmatch(r"[A-Za-z0-9_.:/-]+", candidate[:160]) else None


def safe_error_for_code(code: Any, fallback: str | None = None) -> tuple[str, str, bool]:
    """Normalize a persisted error code and return its public message."""

    normalized = str(code or "").strip().lower()
    if normalized not in _ERRORS:
        normalized = "agent_failed" if normalized else ""
    if not normalized:
        return "", "", False
    message, retryable = _ERRORS[normalized]
    if normalized == "provider_model_not_found" and isinstance(fallback, str):
        if re.fullmatch(
            r"The model '[A-Za-z0-9_.:/-]{1,160}' is not available on OpenRouter\. "
            r"Choose another model in Settings → Studio\.",
            fallback,
        ):
            message = fallback
    return normalized, message, retryable


def emit_observation(logger: Any, kind: str, **fields: Any) -> None:
    """Emit a structured record containing only pre-projected metadata."""

    level = fields.pop("_level", logging.INFO)

    allowed = {
        "conversation_id",
        "run_id",
        "tool_name",
        "event_type",
        "status",
        "stage",
        "duration_ms",
        "result_count",
        "source_count",
        "story_count",
        "cache_hit",
        "degraded",
        "provider",
        "requested_model",
        "actual_model",
        "prompt_version",
        "analysis_id",
        "story_cluster_id",
        "draft_id",
        "error_code",
        "exception_class",
        "upstream_class",
        "failure_kind",
        "status_code",
        "provider_code",
        "phase",
        "usage",
        "estimated_input_tokens",
        "remaining_input_tokens",
        "input_limit_tokens",
        "compacted_results",
        "finalizing",
    }
    safe: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in allowed or value is None:
            continue
        if key == "usage":
            safe[key] = normalize_usage(value)
        elif key == "failure_kind":
            if isinstance(value, str) and value in {*(reason.value for reason in ModelStreamFailure), "unexpected_model_behavior"}:
                safe[key] = value
        elif key == "exception_class":
            safe[key] = str(value)[:120]
        elif key == "status_code":
            safe[key] = _bounded_int(value)
        elif key.endswith("_id") or key in {"provider", "requested_model", "actual_model", "prompt_version", "tool_name", "event_type", "stage", "status", "error_code", "phase", "upstream_class"}:
            safe[key] = str(value)[:160]
        elif key in {"cache_hit", "degraded", "finalizing"}:
            safe[key] = bool(value)
        else:
            safe[key] = _bounded_int(value)
    # ``extra`` is visible to JSON-capable logging handlers without forcing a
    # particular production logging formatter. Emit the same safe projection
    # in plain text too, so the default runtime logger preserves the counters.
    logger.log(level, "studio.%s %s", str(kind)[:40], json.dumps(safe, ensure_ascii=True, sort_keys=True), extra={"studio_observation": safe})
