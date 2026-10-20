"""Per-step reasoning effort, mapped by provider.

Simple decision steps (reading a request into fields, planning search
queries, picking pages) return a short answer, yet reasoning models spend
thousands of hidden tokens on them. Measured on the real channel copy, low
effort cut page picking from 56 s to 19 s with no quality loss. Effort is a
provider-specific request option, so it is mapped here by endpoint; providers
without a known mapping keep their default behaviour.
"""

from __future__ import annotations

from typing import Any

from .. import limits


def _endpoint(model: Any) -> str:
    return str(getattr(model, "base_url", "") or "")


def light_settings(model: Any) -> dict[str, Any]:
    """Model settings for a short decision step."""
    settings: dict[str, Any] = {"max_tokens": int(limits.MAX_OUTPUT_TOKENS), "timeout": max(1.0, float(limits.PIPELINE_STEP_TIMEOUT_SECONDS))}
    if _endpoint(model).rstrip("/") == limits.OPENROUTER_BASE_URL.rstrip("/"):
        # OpenRouter's unified reasoning option; models without reasoning ignore it.
        settings["extra_body"] = {"reasoning": {"effort": "low"}}
    return settings


def full_settings(model: Any) -> dict[str, Any]:
    """Model settings for steps that need real reasoning (story extraction, writing)."""
    return {"max_tokens": int(limits.MAX_OUTPUT_TOKENS), "timeout": max(1.0, float(limits.PIPELINE_STEP_TIMEOUT_SECONDS))}
