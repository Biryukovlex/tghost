"""Typed contracts shared by the Studio agent and HTTP surface."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StudioSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    channel_id: int = Field(gt=0)
    system_prompt: str = Field(max_length=12000)
    expected_prompt: str | None = Field(default=None, max_length=12000)


class ChannelContext(BaseModel):
    """The deliberately compact channel context exposed to the M2 agent."""

    model_config = ConfigDict(extra="forbid")

    channel_id: int
    identifier: str
    title: str = ""
    tracked_posts: int = 0
    oldest_post: datetime | None = None
    newest_post: datetime | None = None
    recent_posts: list[dict[str, Any]] = Field(default_factory=list)
    note: str = ""
    draft_context: dict[str, Any] | None = None
    # M3 adds a bounded, server-built evidence pack while retaining the
    # compact M2 fields for clients that only need channel metadata.
    studio_context: dict[str, Any] | None = None


class DraftPatchRequest(BaseModel):
    """User-owned artifact edit, guarded by an optimistic-lock revision."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    body: str | None = Field(default=None, max_length=32_000)
    working_title: str | None = Field(default=None, max_length=240)
    media_ids: list[str] | None = Field(default=None, max_length=10)
    source_links: list[str] | None = Field(default=None, max_length=40)
    source_ids: list[str] | None = Field(default=None, max_length=40)
    claim_support: list[dict[str, Any]] | None = Field(default=None, max_length=40)
    assumptions: list[str] | None = Field(default=None, max_length=40)
    warnings: list[str] | None = Field(default=None, max_length=40)
    confidence: Literal["high", "medium", "low"] | None = None
    creative: bool | None = None
    # Owner save modes: overwrite the current version (default) or append one.
    save_as_new_version: bool = False
    # Make an existing version current without creating a new one.
    choose_version: int | None = Field(default=None, ge=1)
    # Compatibility alias for choose_version.
    restore_version: int | None = Field(default=None, ge=1)


class DraftCopyResponse(BaseModel):
    """Clipboard payload with plain and Telegram HTML."""

    draft: dict[str, Any]
    copied_text: str
    copied_html: str = ""
    copied_at: str


class DraftPlaceholder(BaseModel):
    """Compatibility response for older callers that still expect a draft key."""

    draft: None = None
    note: str = "No active draft yet."


class StudioError(BaseModel):
    """Stable, safe error shape returned by Studio APIs."""

    code: str
    message: str
    retryable: bool = False


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: int
    title: str | None = Field(default=None, max_length=160)


class ConversationResponse(BaseModel):
    id: str
    workspace_id: str
    channel_id: int
    channel_identifier: str = ""
    channel_title: str = ""
    title: str
    summary: str = ""
    created_at: str
    updated_at: str
    archived_at: str | None = None


class ProfileTextPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel_id: int
    expected_version: int = Field(ge=0)
    evidence_post_ids: list[int] | None = Field(default=None, max_length=30)
    topics_text: str = Field(default="", max_length=2000)
    editorial_text: str = Field(default="", max_length=2000)
    style_text: str = Field(default="", max_length=2000)


class ProfileBuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel_id: int


class ProfileChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: int | None = None
    instruction: str = Field(min_length=1, max_length=4_000)


class ProfileChangeResponse(BaseModel):
    id: str
    channel_id: int
    base_profile_version: int
    proposed_topics: list[Any] = Field(default_factory=list)
    style_diff: dict[str, Any] = Field(default_factory=dict)
    editorial_rules: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    status: str = "proposed"
    created_at: str | None = None
    confirmed_at: str | None = None
    applied_at: str | None = None


class ProviderConsentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: bool = False
    configuration_fingerprint: str | None = None
    channel_id: int | None = None


class MessageResponse(BaseModel):
    id: int
    role: Literal["user", "assistant", "system_summary"]
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class RunResponse(BaseModel):
    id: str
    conversation_id: str
    status: str
    stage: str = ""
    provider: str = "openrouter"
    requested_model: str = ""
    actual_model: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
