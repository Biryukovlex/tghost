"""Domain contracts and validation for Telegram Studio draft artifacts.

The model and browser are deliberately not trusted to calculate draft
metadata.  This module is the small, provider-neutral boundary used by both
agent tools and HTTP persistence: it normalizes line endings, validates
provenance, and calculates Unicode character counts on the server.
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlsplit
import uuid

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from .markdown import render_markdown_plain, validate_markdown_body


MAX_DRAFT_CHARS = 4096
DRAFT_WARNING_CHARS = 3800
MAX_DRAFT_BODY_INPUT = 32_000
MAX_TITLE_CHARS = 240
MAX_LIST_ITEMS = 40
_HTTP_URL = TypeAdapter(AnyHttpUrl)

DraftOrigin = Literal["generated", "regenerated", "user_edit"]
DraftConfidence = Literal["high", "medium", "low"]


def telegram_character_count(value: str) -> int:
    """Count Telegram-visible text as UTF-16 code units."""

    return len(str(value or "").encode("utf-16-le")) // 2


class DraftValidationError(ValueError):
    """A safe, user-actionable draft contract error."""

    def __init__(self, code: str, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


class DraftConflictError(RuntimeError):
    """Raised when an optimistic draft save is based on an old revision."""

    code = "draft_conflict"

    def __init__(self, current: dict[str, Any], *, expected_revision: int | None = None) -> None:
        self.current = current
        self.expected_revision = expected_revision
        super().__init__("The draft changed in another tab. Review both versions before saving.")


def validate_source_links(values: list[str]) -> list[str]:
    """Store links without fetching them. Retrieval still uses SSRF defenses."""
    links = []
    for value in values:
        link = str(value).strip()
        if not link:
            continue
        try:
            _HTTP_URL.validate_python(link)
            parsed = urlsplit(link)
            valid = parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password
        except ValueError:
            valid = False
        if not valid or len(link) > 2048 or any(character.isspace() for character in link):
            raise DraftValidationError("invalid_source_link", "Source links must be HTTP(S) URLs without credentials.")
        if link not in links:
            links.append(link)
    return links


class DraftContent(BaseModel):
    """The four editable content fields exposed to the language model."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    body: str = Field(max_length=MAX_DRAFT_BODY_INPUT)
    source_links: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    media_content: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("source_links")
    @classmethod
    def _links(cls, values: list[str]) -> list[str]:
        return validate_source_links(values)

    @field_validator("media_content")
    @classmethod
    def _media(cls, values: list[str]) -> list[str]:
        try:
            ids = [str(uuid.UUID(value)) for value in values]
        except ValueError:
            raise DraftValidationError("invalid_media", "Media content must contain uploaded image IDs.") from None
        if len(set(ids)) != len(ids):
            raise DraftValidationError("invalid_media", "Choose distinct images.")
        return ids

    def storage_payload(self) -> dict[str, Any]:
        # Existing rows keep legacy research metadata for compatibility, but
        # that metadata is not part of the new draft contract or a save gate.
        return {"working_title": self.title, "body": self.body, "source_links": self.source_links, "media_ids": self.media_content,
                "source_ids": [], "claim_support": []}


class ClaimSupport(BaseModel):
    """A concise factual claim mapped to conversation-scoped source IDs.

    `passage` (or per-source `passages`) carries the exact supporting quote
    from fetched article text; the server confirms it occurs in the stored
    read text before the mapping is accepted. `verified` is server-managed:
    agent tools set it only for mappings that passed verification, and any
    owner body edit clears it.
    """

    model_config = ConfigDict(extra="forbid")

    claim: str = Field(min_length=1, max_length=500)
    source_ids: list[str] = Field(default_factory=list, max_length=12)
    passage: str = Field(default="", max_length=2_000)
    passages: dict[str, str] = Field(default_factory=dict)
    verified: bool = False

    @field_validator("source_ids", mode="before")
    @classmethod
    def _coerce_source_ids(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, (list, tuple, set)):
            raise ValueError("source_ids must be a list")
        return [str(item).strip() for item in value if str(item).strip()][:12]

    @field_validator("passage", mode="before")
    @classmethod
    def _coerce_passage(cls, value: Any) -> str:
        return str(value or "").strip()[:2_000]

    @field_validator("passages", mode="before")
    @classmethod
    def _coerce_passages(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        return {
            str(key).strip(): str(item or "").strip()[:2_000]
            for key, item in list(value.items())[:12]
            if str(key).strip() and str(item or "").strip()
        }


class DraftInput(BaseModel):
    """Storage input, including legacy fields retained for existing artifacts.

    LLM tools use DraftContent, never this compatibility contract.
    """

    model_config = ConfigDict(extra="forbid")

    working_title: str = Field(default="Untitled draft", max_length=MAX_TITLE_CHARS)
    body: str = Field(default="", max_length=MAX_DRAFT_BODY_INPUT)
    source_links: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)

    _links = field_validator("source_links")(validate_source_links)
    source_ids: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    claim_support: list[ClaimSupport] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    assumptions: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    warnings: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    channel_evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    web_evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    confidence: DraftConfidence = "low"
    creative: bool = False
    story_cluster_id: str | None = Field(default=None, max_length=160)
    analysis_id: str | None = Field(default=None, max_length=80)
    provider: str = Field(default="openrouter", max_length=80)
    model: str = Field(default="", max_length=240)
    prompt_version: str = Field(default="m5.draft.v1", max_length=120)

    @field_validator("working_title", mode="before")
    @classmethod
    def _title(cls, value: Any) -> str:
        return str(value or "Untitled draft").strip()[:MAX_TITLE_CHARS] or "Untitled draft"

    @field_validator("source_ids", mode="before")
    @classmethod
    def _source_ids(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, (list, tuple, set)):
            raise ValueError("source_ids must be a list")
        result: list[str] = []
        for item in value:
            source_id = str(item).strip()
            if source_id and source_id not in result:
                result.append(source_id)
        return result[:MAX_LIST_ITEMS]

    @field_validator("assumptions", "warnings", mode="before")
    @classmethod
    def _short_lists(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, (list, tuple, set)):
            raise ValueError("value must be a list")
        return [str(item).strip()[:500] for item in value if str(item).strip()][:MAX_LIST_ITEMS]

    @field_validator("body", mode="before")
    @classmethod
    def _body(cls, value: Any) -> str:
        if value is None:
            return ""
        # Telegram treats CRLF and LF as the same visible line break.  Keep
        # every intentional blank line and do not trim user-owned whitespace.
        return str(value).replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")


class DraftPayload(DraftInput):
    """Validated, server-calculated artifact payload."""

    character_count: int = 0
    over_limit: bool = False
    warning_threshold: bool = False


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def validate_draft_input(
    value: DraftInput | dict[str, Any],
    *,
    known_source_ids: set[str] | frozenset[str] = frozenset(),
    require_sources: bool = True,
) -> DraftPayload:
    """Validate provenance and calculate the authoritative character count.

    Over-limit text remains saveable so a user can fix it in the editor, but
    copying is blocked by :func:`copy_allowed`.  Inputs larger than the hard
    request bound are rejected to keep an accidental paste from becoming a
    persistence or provider-cost issue.
    """

    try:
        parsed = value if isinstance(value, DraftInput) else DraftInput.model_validate(value)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_url=False)
        details = "; ".join(f"{'.'.join(map(str, item['loc']))}: {item['msg']}" for item in errors[:5])
        raise DraftValidationError("invalid_draft", f"Correct draft fields: {details}") from exc
    if len(parsed.body) > MAX_DRAFT_BODY_INPUT:
        raise DraftValidationError("draft_too_large", "Draft text is too large to save.", field="body")
    # Validate markdown dialect: headings, images, HTML are not allowed
    try:
        validate_markdown_body(parsed.body)
    except DraftValidationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DraftValidationError("invalid_markdown", str(exc), field="body") from exc

    source_ids = _unique([str(item).strip() for item in parsed.source_ids if str(item).strip()])
    claims: list[ClaimSupport] = []
    claim_source_ids: list[str] = []
    for claim in parsed.claim_support:
        ids = _unique([str(item).strip() for item in claim.source_ids if str(item).strip()])
        if not ids:
            raise DraftValidationError(
                "claim_source_required",
                "Each factual claim must reference at least one source.",
                field="claim_support",
            )
        claims.append(claim.model_copy(update={"source_ids": ids}))
        claim_source_ids.extend(ids)
    referenced = _unique(source_ids + claim_source_ids)
    if known_source_ids:
        unknown = [source_id for source_id in referenced if source_id not in known_source_ids]
        if unknown:
            raise DraftValidationError(
                "unknown_source",
                "The draft references a source that is not part of this conversation.",
                field="source_ids",
            )
    elif referenced:
        raise DraftValidationError(
            "unknown_source",
            "The draft references a source that is not available in this conversation.",
            field="source_ids",
        )
    # A factual draft may be grounded in the channel archive when no web
    # source was selected.  The agent adds a structured ``channel_evidence``
    # record for this case; web-backed runs still require conversation-scoped
    # source IDs at the agent boundary.
    if require_sources and not parsed.creative and not referenced and not parsed.channel_evidence:
        raise DraftValidationError(
            "source_evidence_required",
            "Factual drafts need at least one source or channel evidence, or mark the request as creative.",
            field="source_ids",
        )

    warnings = _unique([str(item).strip()[:500] for item in parsed.warnings if str(item).strip()])
    plain = render_markdown_plain(parsed.body)
    count = telegram_character_count(plain)
    over_limit = count > MAX_DRAFT_CHARS
    warning_threshold = count >= DRAFT_WARNING_CHARS
    if warning_threshold and "Approaching Telegram's 4,096-character limit." not in warnings and not over_limit:
        warnings.append("Approaching Telegram's 4,096-character limit.")
    if over_limit and "Over Telegram's 4,096-character limit; copying is blocked." not in warnings:
        warnings.append("Over Telegram's 4,096-character limit; copying is blocked.")
    return DraftPayload(
        **parsed.model_dump(exclude={"source_ids", "claim_support", "warnings"}),
        source_ids=referenced,
        claim_support=claims,
        warnings=warnings,
        character_count=count,
        over_limit=over_limit,
        warning_threshold=warning_threshold,
    )


def copy_allowed(payload: DraftPayload | dict[str, Any]) -> bool:
    """Return whether the exact visible plain-text body may be copied."""

    if not isinstance(payload, DraftPayload):
        payload = validate_draft_input(payload, require_sources=False)
    return payload.character_count <= MAX_DRAFT_CHARS


def diff_summary(previous: str, current: str, *, max_chars: int = 600) -> str:
    """Return a compact, non-sensitive edit summary for the next model turn."""

    if previous == current:
        return "No body edits."
    # Deliberately expose only lengths and a bounded line-change count.  The
    # full draft remains in the authorized context, while event logs never get
    # a copy of the text.
    old_lines = previous.splitlines()
    new_lines = current.splitlines()
    changed = sum(1 for old, new in zip(old_lines, new_lines) if old != new)
    changed += abs(len(old_lines) - len(new_lines))
    summary = f"User edited the draft: {changed} line changes, {len(previous)}→{len(current)} Unicode characters."
    return summary[:max_chars]


def input_from_row(row: dict[str, Any]) -> DraftInput:
    """Extract model-facing draft fields from a database/memory row."""

    return DraftInput.model_validate(
        {
            "working_title": row.get("working_title", row.get("title", "Untitled draft")),
            "body": row.get("body", ""),
            "source_ids": row.get("source_ids", []) or [],
            "claim_support": row.get("claim_support", []) or [],
            "assumptions": row.get("assumptions", []) or [],
            "warnings": row.get("warnings", []) or [],
            "channel_evidence": row.get("channel_evidence", []) or [],
            "web_evidence": row.get("web_evidence", []) or [],
            "confidence": row.get("confidence", "low") or "low",
            "creative": bool(row.get("creative", False)),
            "story_cluster_id": row.get("story_cluster_id"),
            "analysis_id": row.get("analysis_id"),
            "provider": row.get("provider", "openrouter") or "openrouter",
            "model": row.get("model", "") or "",
            "prompt_version": row.get("prompt_version", "m5.draft.v1") or "m5.draft.v1",
        }
    )
