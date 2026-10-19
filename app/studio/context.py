"""Bounded, versioned context assembly for Studio agent runs.

The assembler is the single place where channel evidence becomes prompt
context.  It keeps the model input small and inspectable, carries only
evidence-backed fields, and fails closed when a caller accidentally supplies a
discussion-body-shaped field.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .analytics import ChannelAnalytics, analyze_posts
from .profile import _strip_markup
from .sources import _INJECTION_PATTERNS, sanitize_untrusted_text


CONTEXT_VERSION = "m6.context.v1"
DEFAULT_MAX_CHARS = 18_000
DEFAULT_MAX_EVIDENCE_POSTS = 20
_COMMENT_BODY_KEYS = {
    "comment_body",
    "comment_bodies",
    "comment_text",
    "comments_body",
    "comments_text",
    "discussion_body",
    "discussion_bodies",
    "discussion_text",
    "reply_body",
    "replies_body",
}


class ContextPack(BaseModel):
    """Serialized server-owned context sent to a Studio agent."""

    model_config = ConfigDict(extra="forbid")

    context_version: str = CONTEXT_VERSION
    cache_key: str
    channel: dict[str, Any] = Field(default_factory=dict)
    performance: dict[str, Any] = Field(default_factory=dict)
    profile: Any = Field(default_factory=dict, exclude=True)
    profile_block: str = ""
    recent_posts: list[dict[str, Any]] = Field(default_factory=list)
    conversation_summary: str = ""
    user_instruction: str = ""
    limitations: list[str] = Field(default_factory=list)
    comment_bodies_excluded: bool = True
    untrusted_content_filtered: bool = False
    char_count: int = 0

    def prompt_json(self) -> str:
        """Return the exact bounded JSON representation for a model prompt."""

        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text(value: Any, limit: int) -> str:
    value = " ".join(str(value or "").split())
    return value if len(value) <= limit else value[: max(0, limit - 1)].rstrip() + "…"


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _without_comment_bodies(value: Any) -> Any:
    """Recursively remove known discussion-body keys from untrusted mappings."""

    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _COMMENT_BODY_KEYS or ("comment" in normalized and "body" in normalized):
                continue
            cleaned[str(key)] = _without_comment_bodies(item)
        return cleaned
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_without_comment_bodies(item) for item in value]
    return _iso(value)


def _stable_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _neutralize_profile_line(value: str) -> str:
    """Strip markup and neutralise instruction-shaped phrases in one rule.

    A saved profile is owner-authored context, not a fetched source. Keep the
    useful part of a line even when a heuristic sees an injection phrase;
    replacing the phrase is safer and more useful than dropping the entire
    editorial rule.  The replacement is deliberately plain text so it cannot
    become a new instruction for the model.
    """

    line = _strip_markup(value.replace("\x00", " "))
    # This is a legitimate editorial topic, not an instruction to the agent.
    # Keep the exact owner-authored wording while still filtering genuinely
    # directive variants such as "system prompt: reveal ..." below.
    if line.strip().casefold() == "system prompt engineering":
        return line.strip()
    for _flag, pattern in _INJECTION_PATTERNS:
        line = pattern.sub("[instruction removed]", line)
    return " ".join(line.split()).strip()


def _sanitize_profile_text(value: Any, *, limit: int) -> str:
    """Sanitize profile rules one line at a time while retaining line breaks."""

    lines: list[str] = []
    for raw_line in str(value or "").splitlines():
        line = _neutralize_profile_line(raw_line)
        if line:
            lines.append(line)
    return "\n".join(lines)[:limit].rstrip()


def _sanitize_profile(value: Any, *, limit: int = 4_000) -> Any:
    """Return a copy of a profile with line boundaries and safe rule text.

    Profile text is inferred from channel posts and model output, so it is
    untrusted until the owner has reviewed it. The input is never mutated.
    Unlike generic untrusted source excerpts, a suspicious owner-authored line
    is retained with its phrase neutralised rather than removed wholesale.
    """

    if isinstance(value, str):
        return _sanitize_profile_text(value, limit=limit)
    if isinstance(value, Mapping):
        return {str(key): _sanitize_profile(item, limit=limit) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_profile(item, limit=limit) for item in value]
    return value


def profile_block_from_mapping(profile: Mapping[str, Any] | None) -> str:
    """Build the canonical Markdown channel-profile block for every tool."""

    if not profile:
        return ""
    safe = _sanitize_profile(_without_comment_bodies(dict(profile)))
    topics_text = str(safe.get("topics_text") or "").strip()
    editorial_text = str(safe.get("editorial_text") or "").strip()
    style_text = str(safe.get("style_text") or "").strip()
    version = safe.get("version", "?")
    # Fallback to legacy JSON if text fields are empty.
    if not topics_text and not editorial_text and not style_text:
        topics = safe.get("topics", [])
        if topics:
            topics_text = "\n".join(
                str(topic.get("name", ""))
                for topic in topics
                if isinstance(topic, Mapping) and topic.get("name")
            )
    if not topics_text and not editorial_text and not style_text:
        return ""
    parts = [f"CHANNEL PROFILE (written and approved by the channel owner, version {version})"]
    if topics_text:
        parts.append("Topics:\n" + "\n".join(f"- {line}" for line in topics_text.splitlines() if line.strip()))
    if editorial_text:
        parts.append("Editorial rules:\n" + "\n".join(f"- {line}" for line in editorial_text.splitlines() if line.strip()))
    if style_text:
        parts.append("Style rules:\n" + style_text)
    parts.append("These lines are guidelines, not a template. Choose the form each post needs; do not copy the structure or distinctive wording of past posts. Formatting shown in Markdown (**bold**, *italic*, [links](url)) is to be reproduced in the draft body using the same Markdown.")
    return "\n\n".join(parts)


class ContextAssembler:
    """Build a bounded pack from server-owned channel and analysis objects."""

    def __init__(self, *, max_chars: int = DEFAULT_MAX_CHARS, max_evidence_posts: int = DEFAULT_MAX_EVIDENCE_POSTS) -> None:
        self.max_chars = max(2_000, int(max_chars))
        self.max_evidence_posts = max(1, min(int(max_evidence_posts), 100))

    def cache_key(
        self,
        *,
        channel_id: int,
        profile_version: int | str | None = None,
        analysis_hash: str | None = None,
        instruction: str = "",
        conversation_summary: str = "",
    ) -> str:
        """Return a versioned key; changing any input invalidates the pack."""

        return _stable_hash(
            {
                "context_version": CONTEXT_VERSION,
                "channel_id": int(channel_id),
                "profile_version": profile_version,
                "analysis_hash": analysis_hash,
                "instruction": _text(instruction, 4_000),
                "conversation_summary": _text(conversation_summary, 4_000),
            }
        )

    def assemble(
        self,
        channel_context: Mapping[str, Any],
        analytics: ChannelAnalytics | Mapping[str, Any] | None = None,
        profile: Mapping[str, Any] | None = None,
        *,
        instruction: str = "",
        conversation_summary: str = "",
        profile_version: int | str | None = None,
        analysis_hash: str | None = None,
    ) -> ContextPack:
        """Assemble and trim context while preserving evidence identity."""

        safe_input = _without_comment_bodies(dict(channel_context))
        raw_recent_rows = safe_input.get("recent_posts", [])
        safe_channel = safe_input
        safe_channel = {
            "channel_id": safe_channel.get("channel_id"),
            "identifier": _text(safe_channel.get("identifier"), 200),
            "title": _text(safe_channel.get("title"), 300),
            "tracked_posts": safe_channel.get("tracked_posts", 0),
            "oldest_post": safe_channel.get("oldest_post"),
            "newest_post": safe_channel.get("newest_post"),
            "note": _text(safe_channel.get("note"), 500),
        }
        # Profile text and the conversation summary are derived from channel
        # posts and model output, so instruction-shaped lines are removed before
        # they reach the model. The user's own instruction is trusted input and
        # is deliberately not filtered: "ignore the previous draft" is a request.
        raw_profile = _sanitize_profile(_without_comment_bodies(dict(profile or {})))
        raw_instruction = _text(instruction, 4_000)
        raw_summary = _text(sanitize_untrusted_text(str(conversation_summary or ""))[0], 4_000)

        if analytics is None:
            performance: dict[str, Any] = {}
            evidence: list[dict[str, Any]] = []
            limitations: list[str] = ["No performance analysis is available yet."]
            resolved_hash = analysis_hash
        else:
            analytics_model = analytics if isinstance(analytics, ChannelAnalytics) else ChannelAnalytics.model_validate(analytics)
            resolved_hash = analysis_hash or analytics_model.input_hash
            performance = {
                "analytics_version": analytics_model.analytics_version,
                "analysis_at": analytics_model.analysis_at,
                "eligible_post_count": analytics_model.eligible_post_count,
                "style_eligible_post_count": analytics_model.style_eligible_post_count,
                "cohort_counts": analytics_model.cohort_counts,
                "confidence": analytics_model.confidence,
                "confidence_score": analytics_model.confidence_score,
                "low_data": analytics_model.low_data,
                "scoring_weights": analytics_model.scoring_weights,
                "top_post_ids": [post.post_id for post in analytics_model.top_posts],
                "baseline_post_ids": [post.post_id for post in analytics_model.baseline_posts],
                "recent_post_ids": [post.post_id for post in analytics_model.recent_posts],
                "observations": [observation.model_dump(mode="json") for observation in analytics_model.observations],
            }
            evidence = [post.model_dump(mode="json") for post in analytics_model.evidence_posts[: self.max_evidence_posts]]
            limitations = list(analytics_model.limitations)

        filtered_flags: set[str] = set()
        for item in evidence:
            excerpt, flags = sanitize_untrusted_text(str(item.get("excerpt") or ""))
            item["excerpt"] = _text(excerpt, 600)
            filtered_flags.update(flags)

        recent_rows = raw_recent_rows if isinstance(raw_recent_rows, list) else []
        recent_posts = []
        for row in recent_rows[: self.max_evidence_posts]:
            if isinstance(row, Mapping):
                post_text, flags = sanitize_untrusted_text(str(row.get("text") or ""))
                filtered_flags.update(flags)
                recent_posts.append(
                    {
                        "message_id": row.get("message_id"),
                        "posted_at": _iso(row.get("posted_at")),
                        "text": _text(post_text, 600),
                    }
                )
        if filtered_flags:
            limitations.append(
                f"Potential prompt-injection language was removed from channel evidence ({len(filtered_flags)} pattern types)."
            )

        key = self.cache_key(
            channel_id=int(safe_channel.get("channel_id") or 0),
            profile_version=profile_version,
            analysis_hash=resolved_hash,
            instruction=raw_instruction,
            conversation_summary=raw_summary,
        )
        # Build the canonical Markdown profile block (never dropped).
        profile_block = profile_block_from_mapping(raw_profile)
        base: dict[str, Any] = {
            "context_version": CONTEXT_VERSION,
            "cache_key": key,
            "channel": safe_channel,
            "performance": performance,
            "profile_block": profile_block,
            "recent_posts": recent_posts,
            "conversation_summary": raw_summary,
            "user_instruction": raw_instruction,
            "limitations": limitations,
            "comment_bodies_excluded": True,
            "untrusted_content_filtered": bool(filtered_flags),
        }

        # Trim evidence in whole objects first, then recent excerpts, then
        # optional prose fields. Profile block is never dropped.
        base["performance"]["evidence_posts"] = evidence
        serialized = json.dumps(_without_comment_bodies(base), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        while len(serialized) > self.max_chars and base["performance"].get("evidence_posts"):
            base["performance"]["evidence_posts"].pop()
            serialized = json.dumps(_without_comment_bodies(base), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        while len(serialized) > self.max_chars and recent_posts:
            recent_posts.pop()
            base["recent_posts"] = recent_posts
            serialized = json.dumps(_without_comment_bodies(base), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        if len(serialized) > self.max_chars:
            base["conversation_summary"] = _text(raw_summary, 600)
            base["user_instruction"] = _text(raw_instruction, 1_200)
            serialized = json.dumps(_without_comment_bodies(base), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        # The minimum budget is deliberately large enough for the structural
        # envelope. If a caller chooses an impossibly small value, retain a
        # valid pack rather than silently emitting malformed JSON.
        pack = ContextPack.model_validate({**base, "profile": profile_block, "char_count": 0})
        # ``char_count`` is itself part of the prompt JSON. Recalculate it
        # after validation and trim once more if those digits push the final
        # representation over the advertised budget. Profile block is never dropped.
        while True:
            pack.char_count = len(pack.prompt_json())
            if len(pack.prompt_json()) <= self.max_chars:
                break
            evidence_items = pack.performance.get("evidence_posts", [])
            if evidence_items:
                evidence_items.pop()
            elif pack.performance.get("observations"):
                pack.performance["observations"].pop()
            elif pack.performance.get("top_post_ids") and len(pack.performance["top_post_ids"]) > 5:
                pack.performance["top_post_ids"] = pack.performance["top_post_ids"][:5]
            elif pack.performance.get("baseline_post_ids") and len(pack.performance["baseline_post_ids"]) > 5:
                pack.performance["baseline_post_ids"] = pack.performance["baseline_post_ids"][:5]
            elif pack.performance.get("recent_post_ids") and len(pack.performance["recent_post_ids"]) > 5:
                pack.performance["recent_post_ids"] = pack.performance["recent_post_ids"][:5]
            elif pack.recent_posts:
                pack.recent_posts.pop()
            elif pack.conversation_summary and len(pack.conversation_summary) > 600:
                pack.conversation_summary = _text(pack.conversation_summary, 600)
            elif pack.conversation_summary:
                pack.conversation_summary = ""
            elif pack.user_instruction and len(pack.user_instruction) > 800:
                pack.user_instruction = _text(pack.user_instruction, 800)
            elif pack.user_instruction:
                pack.user_instruction = ""
            elif pack.performance.get("limitations") and len(pack.performance["limitations"]) > 1:
                pack.performance["limitations"] = pack.performance["limitations"][:1]
            else:
                # Profile block intentionally preserved even under tight budget.
                # Accept slight overage only if truly minimal budget; otherwise
                # truncate profile block gracefully (still present, just shortened)
                # to honor the advertised budget while preserving guidelines.
                if isinstance(pack.profile, str) and len(pack.profile) > 500:
                    pack.profile = pack.profile[:500].rstrip() + "…"
                    pack.profile_block = pack.profile
                elif isinstance(pack.profile_block, str) and len(pack.profile_block) > 500:
                    pack.profile_block = pack.profile_block[:500].rstrip() + "…"
                    pack.profile = pack.profile_block
                else:
                    break
        # Keep profile_block in sync with profile (both are the Markdown block)
        if pack.profile and not pack.profile_block:
            pack.profile_block = str(pack.profile)
        elif pack.profile_block and not pack.profile:
            pack.profile = pack.profile_block
        return pack

    def assemble_from_rows(
        self,
        channel_context: Mapping[str, Any],
        rows: Sequence[Mapping[str, Any]],
        *,
        channel_id: int | None = None,
        now: datetime | None = None,
        identifier: str | None = None,
        profile: Mapping[str, Any] | None = None,
        instruction: str = "",
        conversation_summary: str = "",
        profile_version: int | str | None = None,
    ) -> ContextPack:
        """Convenience path used by repository-backed tools and tests."""

        resolved_channel = int(channel_id or channel_context.get("channel_id") or 0)
        analysis = analyze_posts(rows, resolved_channel, now=now, identifier=identifier or channel_context.get("identifier"))
        return self.assemble(
            channel_context,
            analysis,
            profile,
            instruction=instruction,
            conversation_summary=conversation_summary,
            profile_version=profile_version,
        )


def context_contains_comment_bodies(value: Any) -> bool:
    """Test helper/guard for payload reviews; numeric comment metrics are safe."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _COMMENT_BODY_KEYS or ("comment" in normalized and "body" in normalized):
                return True
            if context_contains_comment_bodies(item):
                return True
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(context_contains_comment_bodies(item) for item in value)
    return False


__all__ = ["CONTEXT_VERSION", "ContextAssembler", "ContextPack", "context_contains_comment_bodies"]
