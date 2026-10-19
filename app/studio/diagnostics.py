"""Bounded, non-secret failure diagnostics for Studio runs.

When a run fails, the service persists only allowlisted metadata: the run
phase, the safe error category, the exception class name, a numeric HTTP
status, a numeric provider code, the requested model, elapsed time and
request/tool counts. Exception text, response bodies, URLs, prompts, article
content and credentials never enter the record.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .observability import safe_error, safe_status_code

log = logging.getLogger("studio.diagnostics")

#: Run phases, from before the first provider response to artifact work.
RUN_PHASES = ("startup", "model_response", "tool_execution")

_MAX_STR = 160


def _bounded_str(value: Any) -> str:
    return str(value or "")[:_MAX_STR]


def _bounded_int(value: Any, *, default: int = 0, maximum: int = 1_000_000_000) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(0, min(number, maximum))


def _provider_code(exc: BaseException, text: str) -> int | None:
    """Extract a numeric provider error code without keeping any payload text."""

    body = getattr(exc, "body", None)
    candidates: list[Any] = [body] if body is not None else []
    if isinstance(body, dict):
        candidates.append(body.get("code"))
        error = body.get("error")
        if isinstance(error, dict):
            candidates.append(error.get("code"))
    match = re.search(r'"code"\s*:\s*"?(\d{3})"?', text)
    if match:
        candidates.append(match.group(1))
    for candidate in candidates:
        try:
            number = int(str(candidate).strip().strip('"'))
        except (TypeError, ValueError):
            continue
        if 100 <= number <= 599:
            return number
    return None


def phase_for_failure(*, tool_calls_started: int, model_output_seen: bool) -> str:
    """Distinguish a failure before the first provider response from later ones."""

    if int(tool_calls_started or 0) > 0:
        return "tool_execution"
    if bool(model_output_seen):
        return "model_response"
    return "startup"


def _model_failure_kind(exc: BaseException) -> str | None:
    if type(exc).__name__ != "UnexpectedModelBehavior":
        return None
    message = str(exc)
    known = {
        "ChatGPT returned an unsuccessful response": "provider_response_unsuccessful",
        "ChatGPT stream ended before response.completed": "provider_stream_truncated",
        "Streamed response ended without content or tool calls": "provider_empty_response",
    }
    if message.startswith("Exceeded maximum retries"):
        return "output_retries_exhausted"
    return known.get(message, "unexpected_model_behavior")


def build_run_diagnostics(
    exc: BaseException,
    *,
    phase: str = "startup",
    requested_model: str = "",
    elapsed_ms: int = 0,
    requests: int = 0,
    tool_calls: int = 0,
) -> dict[str, Any]:
    """Project an exception onto the persisted diagnostic record shape."""

    code, _message, _retryable = safe_error(exc)
    text = f"{exc.__class__.__name__} {exc}"
    failure_kind = _model_failure_kind(exc)
    return {
        "phase": phase if phase in RUN_PHASES else "startup",
        "error_code": code,
        "exception_class": type(exc).__name__[:80],
        **({"failure_kind": failure_kind} if failure_kind else {}),
        "status_code": safe_status_code(exc),
        "provider_code": _provider_code(exc, text),
        "requested_model": _bounded_str(requested_model),
        "elapsed_ms": _bounded_int(elapsed_ms),
        "requests": _bounded_int(requests),
        "tool_calls": _bounded_int(tool_calls),
    }


def describe_upstream_failure(
    message: str,
    *,
    phase: str = "startup",
    requested_model: str = "",
    elapsed_ms: int = 0,
    requests: int = 0,
    tool_calls: int = 0,
) -> dict[str, Any]:
    """Classify an AG-UI RUN_ERROR message before the wrapper erases context.

    The adapter only forwards the upstream ``str(error)`` text, so parse the
    structured fields out of that text immediately and keep them alongside
    the wrapper. Only bounded codes, names and numbers are retained.
    """

    text = str(message or "")
    code, _message, _retryable = safe_error(_UpstreamText(text))
    return {
        "phase": phase if phase in RUN_PHASES else "startup",
        "error_code": code,
        "exception_class": _upstream_class(text),
        "status_code": _status_via_text(text),
        "provider_code": _provider_code(_UpstreamText(text), text),
        "requested_model": _bounded_str(requested_model),
        "elapsed_ms": _bounded_int(elapsed_ms),
        "requests": _bounded_int(requests),
        "tool_calls": _bounded_int(tool_calls),
    }


class _UpstreamText(RuntimeError):
    """Text-only stand-in so classifiers run on AG-UI error strings."""


def _upstream_class(text: str) -> str:
    lowered = text.lower()
    if "modelhttperror" in lowered:
        return "ModelHTTPError"
    if "timeout" in lowered or "timed out" in lowered:
        return "TimeoutError"
    if "cancel" in lowered:
        return "CancelledError"
    return "UpstreamProviderError"


def _status_via_text(text: str) -> int | None:
    match = re.search(r"status[_ ]?code\s*[:=]\s*(\d{3})", text, re.IGNORECASE)
    if match and 100 <= int(match.group(1)) <= 599:
        return int(match.group(1))
    return safe_status_code(_UpstreamText(text))


def safe_model_label(settings: Any) -> str:
    """Requested-model label for diagnostics; never a secret or prompt."""

    try:
        from .model import model_name

        return _bounded_str(model_name(settings))
    except Exception:
        return ""
