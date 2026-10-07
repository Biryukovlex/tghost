"""SQLAlchemy models for the PostgreSQL application store.

Every analytics row carries ``workspace_id`` and composite foreign keys keep a
row from being attached to a record belonging to another workspace.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Float,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    studio_system_prompt: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class WorkspaceSetting(Base):
    __tablename__ = "workspace_settings"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    is_secret: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("false"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=sql_text("now()"))
    __table_args__ = (
        Index("ix_workspace_settings_workspace_id", "workspace_id"),
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Membership(Base):
    __tablename__ = "memberships"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default="member")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ProviderConsent(Base):
    __tablename__ = "provider_consents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    configuration_fingerprint: Mapped[str] = mapped_column(Text, nullable=False, server_default="legacy")
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("false"))
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", "provider", name="uq_provider_consent"),
        UniqueConstraint(
            "workspace_id", "provider", "configuration_fingerprint", name="uq_provider_consent_configuration"
        ),
    )


class TelegramConnection(Base):
    __tablename__ = "telegram_connections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    api_id: Mapped[int] = mapped_column(Integer, nullable=False)
    api_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_api_hash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    encrypted_session: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    session_key_version: Mapped[str] = mapped_column(Text, nullable=False, server_default="v1")
    session_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_telegram_connections_workspace_id"),
        UniqueConstraint("workspace_id", "label", name="uq_telegram_connection_label"),
    )


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    telegram_connection_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    identifier: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_channels_workspace_id"),
        UniqueConstraint("workspace_id", "identifier", name="uq_channels_workspace_identifier"),
        ForeignKeyConstraint(
            ["workspace_id", "telegram_connection_id"],
            ["telegram_connections.workspace_id", "telegram_connections.id"],
            ondelete="RESTRICT",
            name="fk_channels_workspace_connection",
        ),
        Index("ix_channels_workspace_active", "workspace_id", "active"),
    )


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    formatting_entities: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=sql_text("'[]'::jsonb")
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("false"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "channel_id"],
            ["channels.workspace_id", "channels.id"],
            ondelete="CASCADE",
            name="fk_posts_workspace_channel",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_posts_workspace_id"),
        UniqueConstraint("workspace_id", "channel_id", "message_id", name="uq_posts_channel_message"),
        Index("ix_posts_workspace_channel_date", "workspace_id", "channel_id", "posted_at"),
    )


class Snapshot(Base):
    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    post_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    views: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    comments: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    reactions: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    shares: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "post_id"],
            ["posts.workspace_id", "posts.id"],
            ondelete="CASCADE",
            name="fk_snapshots_workspace_post",
        ),
        Index("ix_snapshots_workspace_post_time", "workspace_id", "post_id", "taken_at"),
    )


class PostViewHorizon(Base):
    __tablename__ = "post_view_horizons"
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    post_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    horizon_hours: Mapped[int] = mapped_column(Integer, primary_key=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    views: Mapped[int] = mapped_column(BigInteger, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "post_id"], ["posts.workspace_id", "posts.id"], ondelete="CASCADE"),
        CheckConstraint("horizon_hours IN (24,48,72)"), CheckConstraint("views >= 0"),
    )


class ChannelMetricObservation(Base):
    __tablename__ = "channel_metric_observations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    subscribers: Mapped[int | None] = mapped_column(BigInteger)
    subscribers_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    views24: Mapped[float | None] = mapped_column(Float)
    views48: Mapped[float | None] = mapped_column(Float)
    views72: Mapped[float | None] = mapped_column(Float)
    er: Mapped[float | None] = mapped_column(Float)
    err: Mapped[float | None] = mapped_column(Float)
    coverage: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"))
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "channel_id"], ["channels.workspace_id", "channels.id"], ondelete="CASCADE"),
        CheckConstraint("subscribers >= 0"),
        Index("ix_channel_metrics_workspace_channel_time", "workspace_id", "channel_id", "taken_at"),
    )


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    post_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discussion_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    discussion_username: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    sender_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sender_name: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    sender_username: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    media_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    reactions: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("false"))
    first_collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_sync: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "post_id"],
            ["posts.workspace_id", "posts.id"],
            ondelete="CASCADE",
            name="fk_comments_workspace_post",
        ),
        UniqueConstraint("workspace_id", "post_id", "telegram_message_id", name="uq_comments_post_message"),
        Index("ix_comments_workspace_post_date", "workspace_id", "post_id", "posted_at"),
        Index("ix_comments_workspace_sender", "workspace_id", "sender_id"),
    )


class CollectionJob(Base):
    __tablename__ = "collection_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="running")
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"))
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "channel_id"],
            ["channels.workspace_id", "channels.id"],
            ondelete="CASCADE",
            name="fk_collection_jobs_workspace_channel",
        ),
        Index(
            "uq_collection_jobs_active_channel",
            "workspace_id",
            "channel_id",
            unique=True,
            postgresql_where=sql_text("status = 'running'"),
        ),
        Index("ix_collection_jobs_lease", "status", "lease_until"),
    )


class StudioConversation(Base):
    """Workspace-owned conversation thread selected by the Studio UI."""

    __tablename__ = "studio_conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, server_default="New conversation")
    summary: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    reference_channels: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    active_draft_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "channel_id"],
            ["channels.workspace_id", "channels.id"],
            ondelete="CASCADE",
            name="fk_studio_conversations_workspace_channel",
        ),
        # The target table is declared below.  The migration adds this
        # nullable reference after creating drafts to avoid a DDL cycle.  The
        # repository still checks the workspace/channel before setting it.
        ForeignKeyConstraint(
            ["active_draft_id"],
            ["studio_drafts.id"],
            ondelete="SET NULL",
            name="fk_studio_conversations_workspace_active_draft",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_studio_conversations_workspace_id"),
        Index("ix_studio_conversations_workspace_updated", "workspace_id", "updated_at"),
    )


class StudioMessage(Base):
    """Full user/assistant message body, retained for reload and context."""

    __tablename__ = "studio_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "conversation_id"],
            ["studio_conversations.workspace_id", "studio_conversations.id"],
            ondelete="CASCADE",
            name="fk_studio_messages_workspace_conversation",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_studio_messages_workspace_id"),
        Index("ix_studio_messages_workspace_conversation_time", "workspace_id", "conversation_id", "created_at", "id"),
    )


class StudioAgentRun(Base):
    """Durable initial Studio job row and one-active-run lease boundary."""

    __tablename__ = "studio_agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    stage: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    provider: Mapped[str] = mapped_column(Text, nullable=False, server_default="openrouter")
    requested_model: Mapped[str] = mapped_column(Text, nullable=False)
    actual_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"))
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_phase: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_class: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_provider_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "conversation_id"],
            ["studio_conversations.workspace_id", "studio_conversations.id"],
            ondelete="CASCADE",
            name="fk_studio_runs_workspace_conversation",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "user_message_id"],
            ["studio_messages.workspace_id", "studio_messages.id"],
            ondelete="CASCADE",
            name="fk_studio_runs_workspace_message",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_studio_runs_workspace_id"),
        Index(
            "uq_studio_runs_active_conversation",
            "workspace_id",
            "conversation_id",
            unique=True,
            postgresql_where=sql_text("status IN ('queued', 'running')"),
        ),
        Index("ix_studio_runs_workspace_status", "workspace_id", "status", "created_at"),
    )


class StudioRunEvent(Base):
    """Append-only event with a public-safe projection and private tool output."""

    __tablename__ = "studio_run_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    safe_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"))
    result_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["studio_agent_runs.workspace_id", "studio_agent_runs.id"],
            ondelete="CASCADE",
            name="fk_studio_events_workspace_run",
        ),
        UniqueConstraint("workspace_id", "run_id", "sequence", name="uq_studio_events_run_sequence"),
        Index("ix_studio_events_workspace_run_sequence", "workspace_id", "run_id", "sequence"),
        Index(
            "ix_studio_events_workspace_tool_results",
            "workspace_id",
            "created_at",
            postgresql_where=sql_text("event_type = 'TOOL_CALL_RESULT' AND result_content IS NOT NULL"),
        ),
    )


class StudioAnalysis(Base):
    """Append-only deterministic/LLM interpretation of channel evidence."""

    __tablename__ = "studio_analyses"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    analysis_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    analysis_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    eligible_post_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    style_eligible_post_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    evidence_post_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    scoring_version: Mapped[str] = mapped_column(Text, nullable=False)
    scoring_weights: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"))
    topic_insights: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    style_insights: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"))
    limitations: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    confidence: Mapped[str] = mapped_column(Text, nullable=False, server_default="low")
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False, server_default="0.25")
    input_hash: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False, server_default="local")
    model: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False, server_default="m3.profile.v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "channel_id"],
            ["channels.workspace_id", "channels.id"],
            ondelete="CASCADE",
            name="fk_studio_analyses_workspace_channel",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_studio_analyses_workspace_id"),
        UniqueConstraint("workspace_id", "channel_id", "input_hash", name="uq_studio_analyses_input"),
        Index("ix_studio_analyses_workspace_channel_created", "workspace_id", "channel_id", "created_at"),
    )


class StudioProfile(Base):
    """Current versioned topic/style profile for one workspace channel."""

    __tablename__ = "studio_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    topics: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    style_profile: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"))
    editorial_rules: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"))
    confidence: Mapped[str] = mapped_column(Text, nullable=False, server_default="low")
    current_analysis_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    topics_text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    editorial_text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    style_text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    built_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    built_from_posts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "channel_id"],
            ["channels.workspace_id", "channels.id"],
            ondelete="CASCADE",
            name="fk_studio_profiles_workspace_channel",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "current_analysis_id"],
            ["studio_analyses.workspace_id", "studio_analyses.id"],
            ondelete="SET NULL",
            name="fk_studio_profiles_workspace_analysis",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_studio_profiles_workspace_id"),
        UniqueConstraint("workspace_id", "channel_id", name="uq_studio_profiles_channel"),
        Index("ix_studio_profiles_workspace_channel", "workspace_id", "channel_id"),
    )


class StudioProfileChange(Base):
    """Auditable confirmation boundary for conversational profile changes."""

    __tablename__ = "studio_profile_changes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    base_profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    proposed_topics: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    style_diff: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"))
    editorial_rules: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"))
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="proposed")
    requested_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "channel_id"],
            ["channels.workspace_id", "channels.id"],
            ondelete="CASCADE",
            name="fk_studio_profile_changes_workspace_channel",
        ),
        ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="SET NULL", name="fk_studio_profile_changes_user"),
        UniqueConstraint("workspace_id", "id", name="uq_studio_profile_changes_workspace_id"),
        CheckConstraint("status IN ('proposed', 'confirmed', 'applied', 'rejected')", name="ck_studio_profile_change_status"),
        Index("ix_studio_profile_changes_workspace_channel_status", "workspace_id", "channel_id", "status", "created_at"),
    )


class StudioSource(Base):
    """Conversation-scoped, normalized web-source provenance."""

    __tablename__ = "studio_sources"

    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    publisher: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    domain: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    content_hash: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    provider: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    query: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    accessible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("true"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="ok")
    warnings: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    injection_flags: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"))
    quality_score: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    quality_notes: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "conversation_id"],
            ["studio_conversations.workspace_id", "studio_conversations.id"],
            ondelete="CASCADE",
            name="fk_studio_sources_workspace_conversation",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "channel_id"],
            ["channels.workspace_id", "channels.id"],
            ondelete="CASCADE",
            name="fk_studio_sources_workspace_channel",
        ),
        Index("ix_studio_sources_workspace_conversation_retrieved", "workspace_id", "conversation_id", "retrieved_at"),
        Index("ix_studio_sources_workspace_channel", "workspace_id", "channel_id"),
    )


class StudioStoryCluster(Base):
    """Conversation-scoped event cluster with explainable ranking details."""

    __tablename__ = "studio_story_clusters"

    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="new")
    topic_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    headline: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    summary: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    source_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    primary_source_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    supporting_source_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    score: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    topic_relevance: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    freshness: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    source_quality: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    novelty: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    score_breakdown: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"))
    matched_topics: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    relevance_features: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"))
    novelty_features: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"))
    conflict_flags: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    warnings: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    channel_evidence: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    content_fingerprint: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "conversation_id"],
            ["studio_conversations.workspace_id", "studio_conversations.id"],
            ondelete="CASCADE",
            name="fk_studio_story_clusters_workspace_conversation",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "channel_id"],
            ["channels.workspace_id", "channels.id"],
            ondelete="CASCADE",
            name="fk_studio_story_clusters_workspace_channel",
        ),
        CheckConstraint("status IN ('new', 'saved', 'dismissed', 'used')", name="ck_studio_story_cluster_status"),
        UniqueConstraint("workspace_id", "conversation_id", "content_fingerprint", name="uq_studio_story_cluster_fingerprint"),
        Index("ix_studio_story_clusters_workspace_conversation_score", "workspace_id", "conversation_id", "score"),
        Index("ix_studio_story_clusters_workspace_channel", "workspace_id", "channel_id"),
    )


class StudioResearchEvent(Base):
    """Safe, durable activity record for each provider-backed research call."""

    __tablename__ = "studio_research_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="search")
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    query_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("false"))
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("false"))
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "conversation_id"],
            ["studio_conversations.workspace_id", "studio_conversations.id"],
            ondelete="CASCADE",
            name="fk_studio_research_events_workspace_conversation",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "channel_id"],
            ["channels.workspace_id", "channels.id"],
            ondelete="CASCADE",
            name="fk_studio_research_events_workspace_channel",
        ),
        Index("ix_studio_research_events_workspace_conversation_created", "workspace_id", "conversation_id", "created_at"),
    )


class StudioDraft(Base):
    """Current editable Telegram artifact for one conversation."""

    __tablename__ = "studio_drafts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    story_cluster_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    analysis_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    working_title: Mapped[str] = mapped_column(Text, nullable=False, server_default="Untitled draft")
    body: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    media_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="working")
    source_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    claim_support: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    assumptions: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    warnings: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    channel_evidence: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    web_evidence: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    confidence: Mapped[str] = mapped_column(Text, nullable=False, server_default="low")
    creative: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("false"))
    provider: Mapped[str] = mapped_column(Text, nullable=False, server_default="openrouter")
    model: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False, server_default="m5.draft.v1")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    current_version_origin: Mapped[str] = mapped_column(Text, nullable=False, server_default="generated")
    copied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "conversation_id"],
            ["studio_conversations.workspace_id", "studio_conversations.id"],
            ondelete="CASCADE",
            name="fk_studio_drafts_workspace_conversation",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "channel_id"],
            ["channels.workspace_id", "channels.id"],
            ondelete="CASCADE",
            name="fk_studio_drafts_workspace_channel",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "analysis_id"],
            ["studio_analyses.workspace_id", "studio_analyses.id"],
            ondelete="SET NULL",
            name="fk_studio_drafts_workspace_analysis",
        ),
        CheckConstraint("status IN ('working', 'ready', 'archived')", name="ck_studio_draft_status"),
        CheckConstraint("confidence IN ('high', 'medium', 'low')", name="ck_studio_draft_confidence"),
        CheckConstraint("current_version_origin IN ('generated', 'regenerated', 'user_edit')", name="ck_studio_draft_current_origin"),
        UniqueConstraint("workspace_id", "id", name="uq_studio_drafts_workspace_id"),
        Index("ix_studio_drafts_workspace_conversation_updated", "workspace_id", "conversation_id", "updated_at"),
        Index("ix_studio_drafts_workspace_channel", "workspace_id", "channel_id"),
    )


class StudioDraftVersion(Base):
    """Append-only body history for a draft artifact."""

    __tablename__ = "studio_draft_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    draft_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    media_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    character_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "draft_id"],
            ["studio_drafts.workspace_id", "studio_drafts.id"],
            ondelete="CASCADE",
            name="fk_studio_draft_versions_workspace_draft",
        ),
        CheckConstraint("origin IN ('generated', 'regenerated', 'user_edit')", name="ck_studio_draft_version_origin"),
        CheckConstraint("character_count >= 0", name="ck_studio_draft_version_char_count"),
        UniqueConstraint("workspace_id", "draft_id", "version", name="uq_studio_draft_versions_version"),
        Index("ix_studio_draft_versions_workspace_draft_created", "workspace_id", "draft_id", "created_at", "id"),
    )


class StudioMedia(Base):
    """Immutable workspace-private JPEGs; migration sets only draft_id null on draft deletion."""
    __tablename__ = 'studio_media'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey('workspaces.id', ondelete='CASCADE'), nullable=False)
    draft_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False, server_default='image/jpeg')
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (UniqueConstraint('workspace_id', 'id'), CheckConstraint('octet_length(content) <= 10485760'), CheckConstraint('width > 0 AND height > 0'))


class PublishingChannel(Base):
    __tablename__ = 'publishing_channels'
    account_id: Mapped[int | None] = mapped_column(BigInteger)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default='false')
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default='Checking publishing permissions')
    caption_limit: Mapped[int] = mapped_column(Integer, nullable=False, server_default='1024')
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (ForeignKeyConstraint(['workspace_id', 'channel_id'], ['channels.workspace_id', 'channels.id'], ondelete='CASCADE'),)


class ScheduledPost(Base):
    __tablename__ = 'scheduled_posts'
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey('workspaces.id', ondelete='CASCADE'), nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    draft_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    pending_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    parts: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(Text, nullable=False)
    pending_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pending_timezone: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default='queued')
    action: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default='1')
    idempotency_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str] = mapped_column(Text, nullable=False, server_default='')
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (UniqueConstraint('workspace_id', 'id'), UniqueConstraint('workspace_id', 'idempotency_key'),
        ForeignKeyConstraint(['workspace_id', 'channel_id'], ['channels.workspace_id', 'channels.id']),
        CheckConstraint("status IN ('queued','transferring','scheduled','updating','cancelling','published','cancelled','failed','needs_review')"),
        CheckConstraint("action IS NULL OR action IN ('schedule','update','cancel')"), Index('ix_scheduled_posts_calendar', 'workspace_id', 'scheduled_at', 'id'))


class PublishingEvent(Base):
    __tablename__ = 'publishing_events'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    post_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False, server_default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (ForeignKeyConstraint(['workspace_id', 'post_id'], ['scheduled_posts.workspace_id', 'scheduled_posts.id'], ondelete='CASCADE'),)
