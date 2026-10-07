"""Workspace-scoped PostgreSQL repository used by the collector and web app.

PostgreSQL is the only TGhost runtime.  Methods are asynchronous and
return mapping-like rows for the templates and command formatter.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from . import limits
from .channel_metrics_repository import ChannelMetricsRepository
from .db_session import DatabaseSessionManager, normalize_database_url
from .telegram_formatting import normalize_entities
from .web.links import normalize_channel_identifier


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    value = _aware(value)
    return value.isoformat(timespec="microseconds") if value else None


_UTC_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def parse_utc_date(value: Any) -> datetime | None:
    """Parse an inclusive UTC publication date (``YYYY-MM-DD``).

    Empty/None means open-ended.  Raises ``ValueError`` for malformed or
    out-of-range input so callers return 422 instead of partially applying.
    """

    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = _aware(value)
        assert parsed is not None
        return parsed.replace(hour=0, minute=0, second=0, microsecond=0)
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    raw = str(value or "").strip()
    if not raw:
        return None
    match = _UTC_DATE_RE.match(raw)
    if not match:
        raise ValueError(f"Invalid date '{raw}': expected YYYY-MM-DD (UTC).")
    year, month, day = (int(part) for part in match.groups())
    if not (1970 <= year <= 2100):
        raise ValueError(f"Invalid date '{raw}': year must be 1970-2100.")
    try:
        return datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        raise ValueError(f"Invalid date '{raw}': not a calendar date.") from None


def cohort_bounds(from_date: Any, to_date: Any) -> tuple[datetime | None, datetime | None]:
    """Return (from_inclusive, to_exclusive) for an inclusive From/To cohort.

    The upper bound is the half-open next-day boundary, so a post published
    at ``To 23:59:59.999 UTC`` is included while ``To+1 00:00 UTC`` is not.
    """

    start = parse_utc_date(from_date)
    end_inclusive = parse_utc_date(to_date)
    end_exclusive = (end_inclusive + timedelta(days=1)) if end_inclusive is not None else None
    if start is not None and end_exclusive is not None and start >= end_exclusive:
        raise ValueError("Invalid date range: From must not be after To.")
    return start, end_exclusive


class PostgresDatabase(ChannelMetricsRepository):
    """Async PostgreSQL repository with an explicit workspace boundary."""

    def __init__(
        self,
        database_url: str,
        *,
        workspace_slug: str = "community",
        pool_size: int = 5,
        max_overflow: int = 5,
        pool_timeout: float = 30.0,
        pool_recycle: int = 1800,
    ) -> None:
        self.database_url = normalize_database_url(database_url)
        self.workspace_slug = workspace_slug.strip() or "community"
        self.workspace_id: uuid.UUID | None = None
        self.active_channel_count: int | None = None
        self.user_id: uuid.UUID | None = None
        self.sessions = DatabaseSessionManager(
            self.database_url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            pool_recycle=pool_recycle,
        )

    @classmethod
    def from_settings(cls, settings) -> "PostgresDatabase":
        return cls(
            settings.database_url,
            workspace_slug=limits.WORKSPACE_SLUG,
            pool_size=limits.DATABASE_POOL_SIZE,
            max_overflow=limits.DATABASE_MAX_OVERFLOW,
            pool_timeout=limits.DATABASE_POOL_TIMEOUT,
            pool_recycle=limits.DATABASE_POOL_RECYCLE,
        )

    async def init_db(self, *, admin_username: str = "admin", workspace_name: str = "Community") -> None:
        """Check migration readiness and seed the local workspace/admin.

        Schema creation intentionally stays in Alembic.  Starting against an
        empty database therefore fails with a precise migration instruction
        instead of silently creating a divergent production schema.
        """
        await self.sessions.healthcheck()
        async with self.sessions.session() as session:
            result = await session.execute(text("SELECT to_regclass('public.workspaces') AS table_name"))
            if result.scalar_one_or_none() is None:
                raise RuntimeError(
                    "PostgreSQL schema is not migrated; run `alembic upgrade head` before starting the app"
                )
            workspace_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"telegram-stats:workspace:{self.workspace_slug}")
            user_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"telegram-stats:user:{admin_username}")
            await session.execute(
                text(
                    """INSERT INTO workspaces(id, slug, name) VALUES (:id, :slug, :name)
                       ON CONFLICT (slug) DO UPDATE SET name=EXCLUDED.name"""
                ),
                {"id": workspace_uuid, "slug": self.workspace_slug, "name": workspace_name},
            )
            workspace_uuid = (
                await session.execute(
                    text("SELECT id FROM workspaces WHERE slug=:slug"), {"slug": self.workspace_slug}
                )
            ).scalar_one()
            await session.execute(
                text(
                    """INSERT INTO users(id, username, display_name)
                       VALUES (:id, :username, :display_name)
                       ON CONFLICT (username) DO UPDATE SET display_name=EXCLUDED.display_name,
                                                             is_active=true"""
                ),
                {"id": user_uuid, "username": admin_username, "display_name": admin_username},
            )
            user_uuid = (
                await session.execute(
                    text("SELECT id FROM users WHERE username=:username"), {"username": admin_username}
                )
            ).scalar_one()
            await session.execute(
                text(
                    """INSERT INTO memberships(workspace_id, user_id, role)
                       VALUES (:workspace_id, :user_id, 'owner')
                       ON CONFLICT (workspace_id, user_id) DO UPDATE SET role='owner'"""
                ),
                {"workspace_id": workspace_uuid, "user_id": user_uuid},
            )
            await session.commit()
            self.workspace_id = workspace_uuid
            self.user_id = user_uuid

    async def ensure_workspace(self, *, admin_username: str = "admin") -> uuid.UUID:
        """Seed/read the community workspace after a test-created schema."""
        if self.workspace_id is None:
            await self.init_db(admin_username=admin_username)
        assert self.workspace_id is not None
        return self.workspace_id

    def _workspace(self) -> uuid.UUID:
        if self.workspace_id is None:
            raise RuntimeError("PostgresDatabase.init_db() must run before repository access")
        return self.workspace_id

    async def close(self) -> None:
        await self.sessions.dispose()

    async def healthcheck(self) -> None:
        await self.sessions.healthcheck()

    async def workspace_context(self, *, username: str | None = None) -> dict[str, Any]:
        """Return the seeded workspace/user/membership tuple for auth wiring."""
        params: dict[str, Any] = {"slug": self.workspace_slug}
        username_filter = ""
        if username:
            username_filter = " AND u.username=:username"
            params["username"] = username
        result = await self._execute(
            f"""SELECT w.id AS workspace_id, w.slug AS workspace_slug,
                       u.id AS user_id, u.username, m.role
                  FROM workspaces w JOIN memberships m ON m.workspace_id=w.id
                  JOIN users u ON u.id=m.user_id
                 WHERE w.slug=:slug{username_filter}
                 ORDER BY u.username LIMIT 1""",
            params,
        )
        row = result.mappings().first()
        if not row:
            raise LookupError("workspace membership is not seeded")
        return dict(row)

    async def list_workspaces_for_user(self, user_id: uuid.UUID) -> list[dict[str, Any]]:
        result = await self._execute(
            """SELECT w.id AS workspace_id, w.slug AS workspace_slug, m.role
                 FROM memberships m JOIN workspaces w ON w.id=m.workspace_id
                WHERE m.user_id=:user_id ORDER BY w.slug""",
            {"user_id": user_id},
        )
        return [dict(row) for row in result.mappings().all()]

    # ---------- Telegram connection/session persistence ----------

    async def persist_telegram_session(
        self,
        *,
        label: str,
        api_id: int,
        api_hash: str,
        session_string: str,
        cipher,
    ) -> uuid.UUID:
        if cipher is None:
            raise ValueError("an external TELEGRAM_SESSION_ENCRYPTION_KEY is required for persistence")
        workspace_id = self._workspace()
        encrypted = cipher.encrypt(session_string)
        encrypted_api_hash = cipher.encrypt(api_hash) if api_hash else None
        fingerprint = hashlib.sha256(session_string.encode("utf-8")).hexdigest()[:16]
        async with self.sessions.session() as session:
            row = (
                await session.execute(
                    text(
                        """INSERT INTO telegram_connections(
                               id, workspace_id, label, api_id, api_hash, encrypted_api_hash,
                               encrypted_session, session_key_version,
                               session_fingerprint, status, updated_at
                           ) VALUES (:id, :workspace_id, :label, :api_id, NULL, :encrypted_api_hash,
                                     :encrypted_session, :key_version,
                                     :fingerprint, 'active', now())
                           ON CONFLICT (workspace_id, label) DO UPDATE SET
                               api_id=EXCLUDED.api_id, api_hash=NULL,
                               encrypted_api_hash=EXCLUDED.encrypted_api_hash,
                               encrypted_session=EXCLUDED.encrypted_session,
                               session_key_version=EXCLUDED.session_key_version,
                               session_fingerprint=EXCLUDED.session_fingerprint,
                               status='active', updated_at=now()
                           RETURNING id"""
                    ),
                    {
                        "id": uuid.uuid4(),
                        "workspace_id": workspace_id,
                        "label": label,
                        "api_id": api_id,
                        "encrypted_api_hash": encrypted_api_hash,
                        "encrypted_session": encrypted,
                        "key_version": cipher.key_version,
                        "fingerprint": fingerprint,
                    },
                )
            ).scalar_one()
            await session.commit()
            return row

    async def load_telegram_connection(self, *, label: str, cipher) -> dict[str, Any] | None:
        """Load the complete active connection for collector startup."""
        if cipher is None:
            return None
        row = (
            await self._execute(
                """SELECT api_id, api_hash, encrypted_api_hash, encrypted_session, session_key_version
                     FROM telegram_connections
                   WHERE workspace_id=:workspace_id AND label=:label AND status='active'""",
                {"label": label},
            )
        ).mappings().first()
        if not row or row["encrypted_session"] is None:
            return None
        session_string, session_from_previous = cipher.decrypt_with_previous_flag(bytes(row["encrypted_session"]))
        stored_hash = row.get("encrypted_api_hash") if hasattr(row, "get") else row["encrypted_api_hash"]
        if stored_hash is not None:
            api_hash, hash_from_previous = cipher.decrypt_with_previous_flag(bytes(stored_hash))
        else:
            # Legacy row written before api_hash encryption; upgrade on read.
            legacy_hash = row.get("api_hash") if hasattr(row, "get") else row["api_hash"]
            api_hash, hash_from_previous = str(legacy_hash or ""), True
        stored_version = row.get("session_key_version", "v1") if hasattr(row, "get") else row["session_key_version"]
        if session_from_previous or hash_from_previous or stored_version != cipher.key_version:
            await self._execute(
                """UPDATE telegram_connections
                      SET encrypted_session=:encrypted_session, encrypted_api_hash=:encrypted_api_hash,
                          api_hash=NULL, session_key_version=:key_version, updated_at=now()
                    WHERE workspace_id=:workspace_id AND label=:label AND status='active'""",
                {
                    "label": label,
                    "encrypted_session": cipher.encrypt(session_string),
                    "encrypted_api_hash": cipher.encrypt(api_hash) if api_hash else None,
                    "key_version": cipher.key_version,
                },
            )
        return {
            "api_id": int(row["api_id"]),
            "api_hash": str(api_hash),
            "session_string": session_string,
        }

    async def load_telegram_session(self, *, label: str, cipher) -> str | None:
        """Backward-compatible session-only accessor."""
        connection = await self.load_telegram_connection(label=label, cipher=cipher)
        return str(connection["session_string"]) if connection else None

    async def _execute(self, statement: str, params: dict[str, Any] | None = None):
        async with self.sessions.session() as session:
            result = await session.execute(text(statement), {"workspace_id": self._workspace(), **(params or {})})
            # Repository writes are short, explicit transactions.  Read-only
            # results are fully buffered by SQLAlchemy before the session is
            # closed, so callers can safely consume their mappings.
            keyword = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
            if keyword not in {"SELECT", "WITH", "SHOW", "EXPLAIN"}:
                await session.commit()
            return result

    # ---------- channels ----------

    async def upsert_channel(
        self,
        identifier: str,
        title: str = "",
        chat_id: int | None = None,
        connection_id: uuid.UUID | None = None,
    ) -> int:
        row = (
            await self._execute(
                """INSERT INTO channels(workspace_id, identifier, title, chat_id, telegram_connection_id, created_at)
                   VALUES (:workspace_id, :identifier, :title, :chat_id, :connection_id, now())
                   ON CONFLICT (workspace_id, identifier) DO UPDATE SET
                       title=CASE WHEN EXCLUDED.title <> '' THEN EXCLUDED.title ELSE channels.title END,
                       chat_id=COALESCE(EXCLUDED.chat_id, channels.chat_id),
                       telegram_connection_id=COALESCE(EXCLUDED.telegram_connection_id, channels.telegram_connection_id)
                   RETURNING id""",
                {"identifier": identifier, "title": title or "", "chat_id": chat_id, "connection_id": connection_id},
            )
        ).first()
        assert row is not None
        return int(row[0])

    async def get_channels(self) -> list[dict[str, Any]]:
        result = await self._execute(
            """SELECT id, workspace_id, identifier, title, chat_id, active
               FROM channels WHERE workspace_id=:workspace_id AND active=true ORDER BY id"""
        )
        rows = [dict(row) for row in result.mappings().all()]
        # Studio readiness reads this count synchronously; the collector and the
        # dashboard refresh it on every call, so it never goes stale for long.
        self.active_channel_count = len(rows)
        return rows

    async def get_channels_for_settings(self) -> list[dict[str, Any]]:
        """List active and deactivated channels so either can be purged."""

        result = await self._execute(
            """SELECT id, workspace_id, identifier, title, chat_id, active
                 FROM channels WHERE workspace_id=:workspace_id
                 ORDER BY active DESC, id"""
        )
        return [dict(row) for row in result.mappings().all()]

    async def add_channel(self, identifier: str) -> int:
        # Normalize identifier: strip, ensure non-empty, basic validation
        ident = normalize_channel_identifier(identifier)
        if not ident:
            raise ValueError("Channel identifier must not be empty")
        # Basic validation matching T23 spec: @name, t.me/name, or -100...
        # We keep it permissive but reject strings with spaces
        if " " in ident:
            raise ValueError("Channel identifier must not contain spaces")
        # Insert with chat_id NULL so next poll resolves it; reactivate if inactive
        row = (
            await self._execute(
                """INSERT INTO channels(workspace_id, identifier, title, chat_id, active, created_at)
                   VALUES (:workspace_id, :identifier, '', NULL, true, now())
                   ON CONFLICT (workspace_id, identifier) DO UPDATE SET active=true
                   RETURNING id""",
                {"identifier": ident},
            )
        ).first()
        assert row is not None
        await self.get_channels()
        return int(row[0])

    async def deactivate_channel(self, channel_id: int) -> bool:
        result = await self._execute(
            """UPDATE channels SET active=false
               WHERE workspace_id=:workspace_id AND id=:channel_id AND active=true""",
            {"channel_id": channel_id},
        )
        await self.get_channels()
        return bool(result.rowcount and result.rowcount > 0)

    async def channel_data_summaries(self) -> dict[int, dict[str, int]]:
        """Return deletion-impact counts for every workspace channel."""

        result = await self._execute(
            """SELECT c.id,
                      (SELECT COUNT(*) FROM posts p
                        WHERE p.workspace_id=c.workspace_id AND p.channel_id=c.id) AS posts,
                      (SELECT COUNT(*) FROM comments cm
                        JOIN posts p ON p.workspace_id=cm.workspace_id AND p.id=cm.post_id
                       WHERE p.workspace_id=c.workspace_id AND p.channel_id=c.id) AS comments,
                      (SELECT COUNT(*) FROM studio_conversations sc
                        WHERE sc.workspace_id=c.workspace_id AND sc.channel_id=c.id) AS conversations,
                      (SELECT COUNT(*) FROM studio_drafts sd
                        WHERE sd.workspace_id=c.workspace_id AND sd.channel_id=c.id) AS drafts
                 FROM channels c
                WHERE c.workspace_id=:workspace_id
                ORDER BY c.id"""
        )
        return {
            int(row["id"]): {
                "posts": int(row["posts"] or 0),
                "comments": int(row["comments"] or 0),
                "conversations": int(row["conversations"] or 0),
                "drafts": int(row["drafts"] or 0),
            }
            for row in result.mappings().all()
        }

    async def delete_channel(self, channel_id: int, *, confirmation: str) -> dict[str, Any] | None:
        """Permanently purge a channel and all dependent archive/Studio data.

        The composite foreign keys below ``channels`` use ``ON DELETE
        CASCADE`` (with nullable cross-links using ``SET NULL``), so one
        workspace-scoped delete is both complete and atomic.  We lock the
        parent row and validate the exact identifier inside the transaction.
        """

        async with self.sessions.session() as session:
            channel = (
                await session.execute(
                    text(
                        """SELECT id, identifier
                             FROM channels
                            WHERE workspace_id=:workspace_id AND id=:channel_id
                            FOR UPDATE"""
                    ),
                    {"workspace_id": self._workspace(), "channel_id": channel_id},
                )
            ).mappings().first()
            if channel is None:
                return None
            identifier = str(channel["identifier"])
            if not secrets.compare_digest(confirmation.strip(), identifier):
                raise ValueError(f"Type {identifier} exactly to confirm deletion.")

            active_schedules = (await session.execute(text("SELECT count(*) FROM scheduled_posts WHERE workspace_id=:workspace_id AND channel_id=:channel_id AND status NOT IN ('published','cancelled','failed')"),
                {"workspace_id": self._workspace(), "channel_id": channel_id})).scalar_one()
            if active_schedules:
                raise ValueError("Cancel or resolve scheduled posts in Calendar before deleting this channel. Telegram may still publish them.")
            await session.execute(text("DELETE FROM scheduled_posts WHERE workspace_id=:workspace_id AND channel_id=:channel_id"), {"workspace_id": self._workspace(), "channel_id": channel_id})
            counts = (
                await session.execute(
                    text(
                        """SELECT
                              (SELECT COUNT(*) FROM posts p
                                WHERE p.workspace_id=:workspace_id AND p.channel_id=:channel_id) AS posts,
                              (SELECT COUNT(*) FROM comments cm
                                JOIN posts p ON p.workspace_id=cm.workspace_id AND p.id=cm.post_id
                               WHERE p.workspace_id=:workspace_id AND p.channel_id=:channel_id) AS comments,
                              (SELECT COUNT(*) FROM studio_conversations sc
                                WHERE sc.workspace_id=:workspace_id AND sc.channel_id=:channel_id) AS conversations,
                              (SELECT COUNT(*) FROM studio_drafts sd
                                WHERE sd.workspace_id=:workspace_id AND sd.channel_id=:channel_id) AS drafts"""
                    ),
                    {"workspace_id": self._workspace(), "channel_id": channel_id},
                )
            ).mappings().one()
            await session.execute(
                text("DELETE FROM channels WHERE workspace_id=:workspace_id AND id=:channel_id"),
                {"workspace_id": self._workspace(), "channel_id": channel_id},
            )
            await session.commit()

        await self.get_channels()
        return {
            "identifier": identifier,
            "posts": int(counts["posts"] or 0),
            "comments": int(counts["comments"] or 0),
            "conversations": int(counts["conversations"] or 0),
            "drafts": int(counts["drafts"] or 0),
        }

    async def telegram_connection_secrets(self, label: str, *, cipher) -> dict[str, Any] | None:
        """Server-side read of the stored connection so a partial edit can be merged.

        Never expose the result to a template or an API response.
        """
        try:
            return await self.load_telegram_connection(label=label, cipher=cipher)
        except ValueError:  # rotated encryption key: do not expose partial credentials
            return None

    async def telegram_connection_status(self, label: str) -> dict[str, Any]:
        row = (
            await self._execute(
                """SELECT api_id, encrypted_session, updated_at, status FROM telegram_connections
                   WHERE workspace_id=:workspace_id AND label=:label""",
                {"label": label},
            )
        ).mappings().first()
        if not row:
            return {"configured": False, "api_id": None, "has_session": False, "updated_at": None}
        d = dict(row)
        has_session = bool(d.get("encrypted_session"))
        api_id = d.get("api_id")
        updated_at = d.get("updated_at")
        configured = bool(api_id and has_session)
        return {"configured": configured, "api_id": api_id, "has_session": has_session, "updated_at": updated_at}

    async def active_connection_id(self) -> uuid.UUID | None:
        """Return the active Telegram connection id for channel attribution."""
        row = (
            await self._execute(
                """SELECT id FROM telegram_connections
                    WHERE workspace_id=:workspace_id AND status='active'
                    ORDER BY updated_at DESC NULLS LAST LIMIT 1""",
                {},
            )
        ).mappings().first()
        return row["id"] if row else None

    # ---------- posts / snapshots ----------

    async def upsert_post(
        self,
        channel_db_id: int,
        message_id: int,
        posted_at: datetime,
        text: str,
        formatting_entities: Any = None,
    ) -> int:
        serialized_entities = json.dumps(
            normalize_entities(formatting_entities),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        row = (
            await self._execute(
                """INSERT INTO posts(
                           workspace_id, channel_id, message_id, posted_at, text,
                           formatting_entities, is_deleted, created_at
                       )
                   VALUES (
                           :workspace_id, :channel_id, :message_id, :posted_at, :text,
                           CAST(:formatting_entities AS jsonb), false, now()
                       )
                    ON CONFLICT (workspace_id, channel_id, message_id) DO UPDATE SET
                        posted_at=EXCLUDED.posted_at,
                        text=EXCLUDED.text,
                        formatting_entities=EXCLUDED.formatting_entities,
                        is_deleted=false,
                        deleted_at=NULL
                    RETURNING id""",
                {
                    "channel_id": channel_db_id,
                    "message_id": message_id,
                    "posted_at": _aware(posted_at),
                    "text": text or "",
                    "formatting_entities": serialized_entities,
                },
            )
        ).first()
        assert row is not None
        return int(row[0])

    async def mark_unseen_posts_deleted(
        self,
        channel_id: int,
        seen_message_ids: list[int] | set[int],
        since: datetime | None = None,
        min_message_id: int | None = None,
    ) -> int:
        """Retire posts absent from a successful scan, bounded by the scan window.

        Only posts inside the proven window are retired: ``posted_at >= since``
        when a cutoff is given and ``message_id >= min_message_id`` when the
        scan may have stopped early at a message cap.  Posts seen in this scan
        have any stale deleted flag cleared (the per-post upsert already does
        this; the explicit update keeps repositories without that behaviour
        consistent).
        """
        seen = list(seen_message_ids or [])
        await self._execute(
            """UPDATE posts SET is_deleted=false, deleted_at=NULL
                WHERE workspace_id=:workspace_id AND channel_id=:channel_id
                  AND is_deleted=true AND message_id = ANY(:seen)""",
            {"channel_id": channel_id, "seen": seen},
        )
        clauses = [
            "workspace_id=:workspace_id",
            "channel_id=:channel_id",
            "is_deleted=false",
            "NOT (message_id = ANY(:seen))",
        ]
        params: dict[str, Any] = {"channel_id": channel_id, "seen": seen}
        if since is not None:
            clauses.append("posted_at >= :since")
            params["since"] = _aware(since)
        if min_message_id is not None:
            clauses.append("message_id >= :min_message_id")
            params["min_message_id"] = int(min_message_id)
        result = await self._execute(
            "UPDATE posts SET is_deleted=true, deleted_at=now() WHERE " + " AND ".join(clauses),
            params,
        )
        return int(result.rowcount or 0)

    async def last_snapshot(self, post_id: int) -> dict[str, Any] | None:
        result = await self._execute(
            """SELECT * FROM snapshots
               WHERE workspace_id=:workspace_id AND post_id=:post_id
               ORDER BY id DESC LIMIT 1""",
            {"post_id": post_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def add_snapshot_if_changed(
        self, post_id: int, views: int, comments: int, reactions: int, shares: int
    ) -> bool:
        async with self.sessions.session() as session:
            params = {
                "workspace_id": self._workspace(),
                "post_id": post_id,
                "views": int(views or 0),
                "comments": int(comments or 0),
                "reactions": int(reactions or 0),
                "shares": int(shares or 0),
            }
            latest = (
                await session.execute(
                    text(
                        """SELECT views, comments, reactions, shares FROM snapshots
                           WHERE workspace_id=:workspace_id AND post_id=:post_id
                           ORDER BY id DESC LIMIT 1 FOR UPDATE"""
                    ),
                    params,
                )
            ).mappings().first()
            if latest and all(latest[key] == params[key] for key in ("views", "comments", "reactions", "shares")):
                await session.rollback()
                return False
            await session.execute(
                text(
                    """INSERT INTO snapshots(workspace_id, post_id, taken_at, views, comments, reactions, shares)
                       VALUES (:workspace_id, :post_id, now(), :views, :comments, :reactions, :shares)"""
                ),
                params,
            )
            await session.commit()
            return True

    # ---------- comments ----------

    async def upsert_comment(self, **kwargs: Any) -> bool:
        workspace_id = self._workspace()
        values = {
            "workspace_id": workspace_id,
            "post_id": kwargs["post_id"],
            "telegram_message_id": kwargs["telegram_message_id"],
            "discussion_chat_id": kwargs.get("discussion_chat_id", 0),
            "discussion_username": kwargs.get("discussion_username", "") or "",
            "sender_id": kwargs.get("sender_id"),
            "sender_name": kwargs.get("sender_name", "") or "",
            "sender_username": kwargs.get("sender_username", "") or "",
            "posted_at": _aware(kwargs["posted_at"]),
            "edited_at": _aware(kwargs.get("edited_at")),
            "text": kwargs.get("text", "") or "",
            "media_type": kwargs.get("media_type", "") or "",
            "reactions": kwargs.get("reactions", 0) or 0,
            "reply_to_message_id": kwargs.get("reply_to_message_id"),
            "sync_token": kwargs["sync_token"],
        }
        async with self.sessions.session() as session:
            existing = (
                await session.execute(
                    text(
                        """SELECT discussion_chat_id, discussion_username, sender_id,
                                  sender_name, sender_username, posted_at, edited_at,
                                  text, media_type, reactions, reply_to_message_id, is_deleted
                           FROM comments
                           WHERE workspace_id=:workspace_id AND post_id=:post_id
                             AND telegram_message_id=:telegram_message_id"""
                    ),
                    values,
                )
            ).mappings().first()
            changed = existing is None or existing["is_deleted"] or any(
                existing[key] != values[key]
                for key in (
                    "discussion_chat_id", "discussion_username", "sender_id", "sender_name",
                    "sender_username", "posted_at", "edited_at", "text", "media_type",
                    "reactions", "reply_to_message_id",
                )
            )
            await session.execute(
                text(
                    """INSERT INTO comments(
                           workspace_id, post_id, telegram_message_id, discussion_chat_id,
                           discussion_username, sender_id, sender_name, sender_username,
                           posted_at, edited_at, text, media_type, reactions,
                           reply_to_message_id, is_deleted, first_collected_at,
                           last_collected_at, last_seen_sync
                       ) VALUES (:workspace_id, :post_id, :telegram_message_id, :discussion_chat_id,
                                 :discussion_username, :sender_id, :sender_name, :sender_username,
                                 :posted_at, :edited_at, :text, :media_type, :reactions,
                                 :reply_to_message_id, false, now(), now(), :sync_token)
                       ON CONFLICT (workspace_id, post_id, telegram_message_id) DO UPDATE SET
                           discussion_chat_id=EXCLUDED.discussion_chat_id,
                           discussion_username=EXCLUDED.discussion_username,
                           sender_id=EXCLUDED.sender_id, sender_name=EXCLUDED.sender_name,
                           sender_username=EXCLUDED.sender_username, posted_at=EXCLUDED.posted_at,
                           edited_at=EXCLUDED.edited_at, text=EXCLUDED.text,
                           media_type=EXCLUDED.media_type, reactions=EXCLUDED.reactions,
                           reply_to_message_id=EXCLUDED.reply_to_message_id,
                           is_deleted=false, last_collected_at=now(), last_seen_sync=EXCLUDED.last_seen_sync"""
                ),
                values,
            )
            await session.commit()
            return bool(changed)

    async def mark_unseen_comments_deleted(self, post_id: int, sync_token: str) -> int:
        result = await self._execute(
            """UPDATE comments SET is_deleted=true, last_collected_at=now()
               WHERE workspace_id=:workspace_id AND post_id=:post_id AND is_deleted=false
                 AND last_seen_sync <> :sync_token""",
            {"post_id": post_id, "sync_token": sync_token},
        )
        return int(result.rowcount or 0)

    async def has_comments(self, post_id: int) -> bool:
        result = await self._execute(
            "SELECT 1 FROM comments WHERE workspace_id=:workspace_id AND post_id=:post_id LIMIT 1",
            {"post_id": post_id},
        )
        return result.first() is not None

    async def comments_for_post(self, post_id: int) -> list[dict[str, Any]]:
        result = await self._execute(
            """SELECT * FROM comments WHERE workspace_id=:workspace_id AND post_id=:post_id
               ORDER BY posted_at ASC, telegram_message_id ASC""",
            {"post_id": post_id},
        )
        return [dict(row) for row in result.mappings().all()]

    async def all_comments(self, channel_id: int | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 100000)) if limit is not None else 100000
        result = await self._execute(
            """SELECT cm.*, p.message_id AS post_message_id, p.posted_at AS post_posted_at,
                      ch.identifier AS channel_identifier
               FROM comments cm JOIN posts p ON p.id=cm.post_id AND p.workspace_id=cm.workspace_id
               JOIN channels ch ON ch.id=p.channel_id AND ch.workspace_id=p.workspace_id
                WHERE cm.workspace_id=:workspace_id
                  AND (CAST(:channel_id AS bigint) IS NULL OR p.channel_id=CAST(:channel_id AS bigint))
                  AND (CAST(:channel_id AS bigint) IS NOT NULL OR ch.active=true)
                  AND cm.is_deleted=false AND p.is_deleted=false
                ORDER BY cm.posted_at DESC, cm.id DESC LIMIT :limit""",
            {"channel_id": channel_id, "limit": bounded},
        )
        return [dict(row) for row in result.mappings().all()]

    # ---------- dashboard/command queries ----------

    _ORDER_SQL = {
        "date": "p.posted_at DESC, p.id DESC",
        "views": "l.views DESC NULLS LAST, p.posted_at DESC, p.id DESC",
        "reactions": "l.reactions DESC NULLS LAST, p.posted_at DESC, p.id DESC",
        "comments": "l.comments DESC NULLS LAST, p.posted_at DESC, p.id DESC",
        "shares": "l.shares DESC NULLS LAST, p.posted_at DESC, p.id DESC",
    }

    _LATEST_CTE = """
    WITH latest AS (
        SELECT DISTINCT ON (workspace_id, post_id) * FROM snapshots
        WHERE workspace_id=:workspace_id ORDER BY workspace_id, post_id, id DESC
    ), dayago AS (
        SELECT DISTINCT ON (workspace_id, post_id) post_id, views FROM snapshots
        WHERE workspace_id=:workspace_id AND taken_at <= now() - interval '24 hours'
        ORDER BY workspace_id, post_id, id DESC
    ), firstsnap AS (
        SELECT DISTINCT ON (workspace_id, post_id) post_id, views FROM snapshots
        WHERE workspace_id=:workspace_id ORDER BY workspace_id, post_id, id ASC
    )
    """

    async def latest_stats(
        self,
        channel_id: int | None = None,
        limit: int = 500,
        order: str = "date",
        offset: int = 0,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        sql = self._LATEST_CTE + f"""
        SELECT p.id, p.message_id, p.posted_at, p.text, p.channel_id,
               c.identifier, c.title AS channel_title, c.chat_id,
               COALESCE(l.views,0) AS views, COALESCE(l.comments,0) AS comments,
               COALESCE(l.reactions,0) AS reactions, COALESCE(l.shares,0) AS shares,
               (SELECT COUNT(*) FROM comments cm
                  WHERE cm.workspace_id=p.workspace_id AND cm.post_id=p.id AND cm.is_deleted=false) AS collected_comments,
               l.taken_at AS updated_at,
               COALESCE(l.views,0) - COALESCE(d.views, f.views, COALESCE(l.views,0)) AS views_delta
          FROM posts p JOIN channels c ON c.id=p.channel_id AND c.workspace_id=p.workspace_id
          LEFT JOIN latest l ON l.workspace_id=p.workspace_id AND l.post_id=p.id
          LEFT JOIN dayago d ON d.post_id=p.id LEFT JOIN firstsnap f ON f.post_id=p.id
         WHERE p.workspace_id=:workspace_id
           AND (CAST(:channel_id AS bigint) IS NULL OR p.channel_id=CAST(:channel_id AS bigint))
           AND (CAST(:channel_id AS bigint) IS NOT NULL OR c.active=true)
           AND (CAST(:include_deleted AS boolean) OR p.is_deleted=false)
         ORDER BY {self._ORDER_SQL.get(order, 'p.posted_at DESC')} LIMIT :limit OFFSET :offset"""
        result = await self._execute(
            sql,
            {
                "channel_id": channel_id,
                "limit": max(1, min(int(limit), 100000)),
                "offset": max(0, int(offset)),
                "include_deleted": bool(include_deleted),
            },
        )
        return [dict(row) for row in result.mappings().all()]

    @staticmethod
    def _validate_metric_bound(name: str, value: Any) -> int | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            raise ValueError(f"Invalid {name}: expected a non-negative integer.") from None
        if parsed < 0 or parsed > 2_147_483_647:
            raise ValueError(f"Invalid {name}: expected 0..2147483647.")
        return parsed

    async def explorer_posts(
        self,
        channel_id: int | None = None,
        channel_ids: list[int] | tuple[int, ...] | None = None,
        search: str | None = None,
        sort: str = "date",
        min_views: Any = None,
        max_views: Any = None,
        min_reactions: Any = None,
        max_reactions: Any = None,
        min_comments: Any = None,
        max_comments: Any = None,
        min_shares: Any = None,
        max_shares: Any = None,
        limit: int = 50,
        offset: int = 0,
        include_deleted: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        """Server-side Post Explorer query with stable pagination.

        Search is a bounded case-insensitive substring over post text.
        ``sort`` is one field (date/views/reactions/comments/shares, newest
        or highest first) with stable ``posted_at DESC, id DESC`` tie-breaks.
        Metric min/max pairs are AND-combined.  Filters never expand the
        workspace scope; ``None`` means All active (paused excluded), while
        explicitly listed stored channels remain readable.  The Explorer
        history is independent of chart dates (no date narrowing here).
        Returns ``(rows, total)`` with a bounded page.
        """

        normalized_sort = str(sort or "date").strip().lower()
        if normalized_sort not in self._ORDER_SQL:
            raise ValueError(f"Invalid sort '{sort}': expected one of date, views, reactions, comments, shares.")
        try:
            bounded_limit = int(limit)
        except (TypeError, ValueError):
            raise ValueError("Invalid page_size: expected 1..100.") from None
        if not 1 <= bounded_limit <= 100:
            raise ValueError("Invalid page_size: expected 1..100.")
        try:
            bounded_offset = int(offset)
        except (TypeError, ValueError):
            raise ValueError("Invalid page: expected offset >= 0.") from None
        if bounded_offset < 0 or bounded_offset > 100000:
            raise ValueError("Invalid page: offset must be 0..100000.")
        bounds = {
            "views": (self._validate_metric_bound("min_views", min_views), self._validate_metric_bound("max_views", max_views)),
            "reactions": (self._validate_metric_bound("min_reactions", min_reactions), self._validate_metric_bound("max_reactions", max_reactions)),
            "comments": (self._validate_metric_bound("min_comments", min_comments), self._validate_metric_bound("max_comments", max_comments)),
            "shares": (self._validate_metric_bound("min_shares", min_shares), self._validate_metric_bound("max_shares", max_shares)),
        }
        for metric, (low, high) in bounds.items():
            if low is not None and high is not None and low > high:
                raise ValueError(f"Invalid {metric} range: min must not exceed max.")
        trimmed_search: str | None = None
        if search is not None:
            candidate = str(search).strip()
            if candidate:
                if len(candidate) > 200:
                    raise ValueError("Invalid search: must be at most 200 characters.")
                trimmed_search = candidate
        explicit_ids: list[int] | None = None
        if channel_ids is not None:
            if channel_id is not None:
                raise ValueError("Invalid channel filter: specify channel or channels, not both.")
            explicit_ids = []
            for raw in list(channel_ids):
                try:
                    parsed = int(raw)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    raise ValueError("Invalid channel filter: expected positive integers.") from None
                if parsed <= 0:
                    raise ValueError("Invalid channel filter: expected positive integers.")
                explicit_ids.append(parsed)
            if len(explicit_ids) > 50:
                raise ValueError("Invalid channel filter: at most 50 channels.")
            # Deduplicate preserving order for stable caching.
            explicit_ids = list(dict.fromkeys(explicit_ids))
            if not explicit_ids:
                explicit_ids = None
        order_sql = self._ORDER_SQL[normalized_sort]
        metric_clauses: list[str] = []
        params: dict[str, Any] = {
            "channel_id": channel_id,
            "include_deleted": bool(include_deleted),
            "search": trimmed_search,
        }
        column_map = {"views": "l.views", "reactions": "l.reactions", "comments": "l.comments", "shares": "l.shares"}
        for metric, (low, high) in bounds.items():
            column = column_map[metric]
            if low is not None:
                metric_clauses.append(f"COALESCE({column},0) >= :min_{metric}")
                params[f"min_{metric}"] = low
            if high is not None:
                metric_clauses.append(f"COALESCE({column},0) <= :max_{metric}")
                params[f"max_{metric}"] = high
        metric_sql = (" AND " + " AND ".join(metric_clauses)) if metric_clauses else ""
        if explicit_ids is not None:
            channel_sql = "AND p.channel_id = ANY(CAST(:channel_ids AS bigint[]))"
            params["channel_ids"] = explicit_ids
            # Explicit subset remains readable even when paused; no active filter.
            active_sql = ""
        elif channel_id is not None:
            channel_sql = "AND p.channel_id = CAST(:channel_id AS bigint)"
            active_sql = ""
        else:
            channel_sql = ""
            active_sql = "AND c.active=true"
        search_sql = "AND (POSITION(LOWER(CAST(:search AS text)) IN LOWER(p.text)) > 0)" if trimmed_search else ""
        base = self._LATEST_CTE + f"""
        SELECT p.id, p.message_id, p.posted_at, p.text, p.channel_id,
               c.identifier, c.title AS channel_title, c.chat_id,
               COALESCE(l.views,0) AS views, COALESCE(l.comments,0) AS comments,
               COALESCE(l.reactions,0) AS reactions, COALESCE(l.shares,0) AS shares,
               (SELECT COUNT(*) FROM comments cm
                  WHERE cm.workspace_id=p.workspace_id AND cm.post_id=p.id AND cm.is_deleted=false) AS collected_comments,
               l.taken_at AS updated_at,
               COALESCE(l.views,0) - COALESCE(d.views, f.views, COALESCE(l.views,0)) AS views_delta
          FROM posts p JOIN channels c ON c.id=p.channel_id AND c.workspace_id=p.workspace_id
          LEFT JOIN latest l ON l.workspace_id=p.workspace_id AND l.post_id=p.id
          LEFT JOIN dayago d ON d.post_id=p.id LEFT JOIN firstsnap f ON f.post_id=p.id
         WHERE p.workspace_id=:workspace_id {channel_sql} {active_sql}
           AND (CAST(:include_deleted AS boolean) OR p.is_deleted=false)
           {search_sql}{metric_sql}"""
        count_result = await self._execute(
            f"SELECT COUNT(*) AS total FROM ({base}) AS scoped",
            params,
        )
        total = int(count_result.mappings().first()["total"] or 0)  # type: ignore[index]
        page_result = await self._execute(
            base + f" ORDER BY {order_sql} LIMIT :limit OFFSET :offset",
            {**params, "limit": bounded_limit, "offset": bounded_offset},
        )
        return [dict(row) for row in page_result.mappings().all()], total

    async def kpis(
        self,
        channel_id: int | None = None,
        include_deleted: bool = False,
        from_date: Any = None,
        to_date: Any = None,
    ) -> dict[str, Any]:
        """Latest known totals for the UTC publication-date cohort.

        Inclusive From/To via a half-open next-day boundary.  ``All active``
        (``channel_id=None``) excludes paused channels; an explicitly
        selected stored channel remains readable even when paused.
        """

        start, end_exclusive = cohort_bounds(from_date, to_date)
        sql = self._LATEST_CTE + """
        SELECT COUNT(*) AS posts,
               COALESCE(SUM(l.views),0) AS views, COALESCE(SUM(l.comments),0) AS comments,
               COALESCE(SUM(l.reactions),0) AS reactions, COALESCE(SUM(l.shares),0) AS shares,
               (SELECT COUNT(*) FROM comments cm JOIN posts cp ON cp.id=cm.post_id AND cp.workspace_id=cm.workspace_id
                 JOIN channels ccp ON ccp.id=cp.channel_id AND ccp.workspace_id=cp.workspace_id
                 WHERE cm.workspace_id=:workspace_id AND cm.is_deleted=false AND cp.is_deleted=false
                   AND (CAST(:channel_id AS bigint) IS NULL OR cp.channel_id=CAST(:channel_id AS bigint))
                   AND (CAST(:channel_id AS bigint) IS NOT NULL OR ccp.active=true)
                   AND (CAST(:from_dt AS timestamptz) IS NULL OR cp.posted_at >= CAST(:from_dt AS timestamptz))
                   AND (CAST(:to_exclusive AS timestamptz) IS NULL OR cp.posted_at < CAST(:to_exclusive AS timestamptz))) AS collected_comments,
               (SELECT MAX(s.taken_at) FROM snapshots s JOIN posts pp ON pp.id=s.post_id AND pp.workspace_id=s.workspace_id
                 JOIN channels cpp ON cpp.id=pp.channel_id AND cpp.workspace_id=pp.workspace_id
                 WHERE s.workspace_id=:workspace_id AND pp.is_deleted=false
                   AND (CAST(:channel_id AS bigint) IS NULL OR pp.channel_id=CAST(:channel_id AS bigint))
                   AND (CAST(:channel_id AS bigint) IS NOT NULL OR cpp.active=true)) AS last_poll
          FROM posts p JOIN channels c ON c.id=p.channel_id AND c.workspace_id=p.workspace_id
          LEFT JOIN latest l ON l.workspace_id=p.workspace_id AND l.post_id=p.id
         WHERE p.workspace_id=:workspace_id
           AND (CAST(:channel_id AS bigint) IS NULL OR p.channel_id=CAST(:channel_id AS bigint))
           AND (CAST(:channel_id AS bigint) IS NOT NULL OR c.active=true)
           AND (CAST(:include_deleted AS boolean) OR p.is_deleted=false)
           AND (CAST(:from_dt AS timestamptz) IS NULL OR p.posted_at >= CAST(:from_dt AS timestamptz))
           AND (CAST(:to_exclusive AS timestamptz) IS NULL OR p.posted_at < CAST(:to_exclusive AS timestamptz))"""
        result = await self._execute(
            sql,
            {
                "channel_id": channel_id,
                "include_deleted": bool(include_deleted),
                "from_dt": start,
                "to_exclusive": end_exclusive,
            },
        )
        row = result.mappings().first()
        return dict(row) if row else {}

    async def post_row(self, post_id: int) -> dict[str, Any] | None:
        sql = self._LATEST_CTE + """
        SELECT p.*, c.identifier, c.title AS channel_title, c.chat_id,
               COALESCE(l.views,0) AS views, COALESCE(l.comments,0) AS comments,
               COALESCE(l.reactions,0) AS reactions, COALESCE(l.shares,0) AS shares,
               (SELECT COUNT(*) FROM comments cm WHERE cm.workspace_id=p.workspace_id
                  AND cm.post_id=p.id AND cm.is_deleted=false) AS collected_comments
          FROM posts p JOIN channels c ON c.id=p.channel_id AND c.workspace_id=p.workspace_id
          LEFT JOIN latest l ON l.workspace_id=p.workspace_id AND l.post_id=p.id
         WHERE p.workspace_id=:workspace_id AND p.id=:post_id"""
        result = await self._execute(sql, {"post_id": post_id})
        row = result.mappings().first()
        return dict(row) if row else None

    async def post_history(self, post_id: int) -> list[dict[str, Any]]:
        result = await self._execute(
            """SELECT * FROM snapshots WHERE workspace_id=:workspace_id AND post_id=:post_id
               ORDER BY taken_at ASC, id ASC""",
            {"post_id": post_id},
        )
        return [dict(row) for row in result.mappings().all()]

    async def timeseries_totals(
        self,
        days: int | None,
        channel_id: int | None = None,
        from_date: Any = None,
        to_date: Any = None,
    ) -> dict[str, list]:
        """Latest post metrics grouped by UTC publication day, never accumulated.

        The browser owns Day/Week/Month grouping and cumulative display. Empty
        days are zero; earlier posts never carry into a selected date window.
        """

        start, end_exclusive = cohort_bounds(from_date, to_date)
        explicit = start is not None or end_exclusive is not None
        workspace_id = self._workspace()
        now = datetime.now(timezone.utc)
        async with self.sessions.session() as session:
            rows = (
                await session.execute(
                    text(
                        """WITH latest AS (
                               SELECT DISTINCT ON (workspace_id, post_id)
                                      workspace_id, post_id, views, comments, reactions, shares
                                 FROM snapshots
                                WHERE workspace_id=:workspace_id
                                ORDER BY workspace_id, post_id, id DESC
                           )
                           SELECT (p.posted_at AT TIME ZONE 'UTC')::date AS day,
                                  COUNT(*) AS posts,
                                  COALESCE(SUM(latest.views), 0) AS views,
                                  COALESCE(SUM(latest.comments), 0) AS comments,
                                  COALESCE(SUM(latest.reactions), 0) AS reactions,
                                  COALESCE(SUM(latest.shares), 0) AS shares
                             FROM posts p
                             JOIN channels c ON c.id=p.channel_id AND c.workspace_id=p.workspace_id
                             LEFT JOIN latest ON latest.workspace_id=p.workspace_id AND latest.post_id=p.id
                             WHERE p.workspace_id=:workspace_id
                             AND (CAST(:channel_id AS bigint) IS NULL OR p.channel_id=CAST(:channel_id AS bigint))
                             AND (CAST(:channel_id AS bigint) IS NOT NULL OR c.active=true)
                             AND p.is_deleted=false
                             AND (CAST(:from_dt AS timestamptz) IS NULL OR p.posted_at >= CAST(:from_dt AS timestamptz))
                             AND (CAST(:to_exclusive AS timestamptz) IS NULL OR p.posted_at < CAST(:to_exclusive AS timestamptz))
                             GROUP BY (p.posted_at AT TIME ZONE 'UTC')::date
                             ORDER BY (p.posted_at AT TIME ZONE 'UTC')::date"""
                    ),
                    {
                        "workspace_id": workspace_id,
                        "channel_id": channel_id,
                        "from_dt": start,
                        "to_exclusive": end_exclusive,
                    },
                )
            ).mappings().all()
        if explicit:
            today = now.date()
            if start is not None and end_exclusive is not None:
                range_start = start.date()
                range_end = (end_exclusive - timedelta(days=1)).date()
            elif start is not None:
                range_start = start.date()
                range_end = today
            else:
                assert end_exclusive is not None
                range_end = (end_exclusive - timedelta(days=1)).date()
                earliest = min((row["day"] for row in rows), default=range_end)
                range_start = min(earliest, range_end)
            span = max(1, (range_end - range_start).days + 1)
            # Bound an absurd single request while keeping every valid
            # Overview range usable (collection itself caps at 3650 days).
            span = min(span, 4000)
            days_list = [(datetime.combine(range_start, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(span)]
            by_day = {str(row["day"]): dict(row) for row in rows}
            output = {key: [int(by_day.get(day, {}).get(key) or 0) for day in days_list]
                      for key in ("views", "comments", "reactions", "shares")}
            output["posts_per_day"] = [int(by_day.get(day, {}).get("posts") or 0) for day in days_list]
            return {"days": days_list, **output}
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        first_day = min((row["day"] for row in rows), default=now.date())
        if days is None:
            start_date = datetime.combine(first_day, datetime.min.time(), tzinfo=timezone.utc)
        else:
            start_date = today - timedelta(days=max(1, days) - 1)
        span = max(1, (now.date() - start_date.date()).days + 1)
        days_list = [(start_date + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(span)]
        by_day = {str(row["day"]): dict(row) for row in rows}
        output = {key: [int(by_day.get(day, {}).get(key) or 0) for day in days_list]
                  for key in ("views", "comments", "reactions", "shares")}
        output["posts_per_day"] = [int(by_day.get(day, {}).get("posts") or 0) for day in days_list]
        return {"days": days_list, **output}

    # ---------- history diagnostics and overlap-safe jobs ----------

    async def history_diagnostic(self, channel_id: int | None = None) -> dict[str, Any]:
        result = await self._execute(
            """SELECT COUNT(*) AS total_posts,
                      COUNT(*) FILTER (WHERE char_length(p.text) > 500) AS posts_over_500,
                      COUNT(*) FILTER (WHERE char_length(p.text) = 500) AS posts_at_500,
                      COUNT(*) FILTER (WHERE p.posted_at >= now() - interval '30 days') AS eligible_posts,
                      MIN(p.posted_at) AS oldest_post, MAX(p.posted_at) AS newest_post,
                      COUNT(DISTINCT p.channel_id) AS channels,
                      COALESCE(jsonb_agg(DISTINCT jsonb_build_object('channel_id', p.channel_id))
                               FILTER (WHERE char_length(p.text) = 500), '[]'::jsonb) AS channels_requiring_recollection
                 FROM posts p JOIN channels c ON c.id=p.channel_id AND c.workspace_id=p.workspace_id
                WHERE p.workspace_id=:workspace_id
                  AND (CAST(:channel_id AS bigint) IS NULL OR p.channel_id=CAST(:channel_id AS bigint))
                  AND (CAST(:channel_id AS bigint) IS NOT NULL OR c.active=true)""",
            {"channel_id": channel_id},
        )
        row = result.mappings().first()
        return dict(row) if row else {}

    async def claim_collection_job(self, channel_id: int, lease_seconds: int = 180) -> uuid.UUID | None:
        workspace_id = self._workspace()
        job_id = uuid.uuid4()
        lease_until = datetime.now(timezone.utc) + timedelta(seconds=max(30, lease_seconds))
        try:
            async with self.sessions.session() as session:
                await session.execute(
                    text(
                        """UPDATE collection_jobs SET status='expired', finished_at=now(), error_code='lease_expired'
                           WHERE workspace_id=:workspace_id AND channel_id=:channel_id AND status='running'
                             AND lease_until < now()"""
                    ),
                    {"workspace_id": workspace_id, "channel_id": channel_id},
                )
                created = await session.execute(
                    text(
                        """INSERT INTO collection_jobs(id, workspace_id, channel_id, status, lease_until)
                           SELECT :id, :workspace_id, :channel_id, 'running', :lease_until
                             FROM channels WHERE workspace_id=:workspace_id AND id=:channel_id"""
                    ),
                    {"id": job_id, "workspace_id": workspace_id, "channel_id": channel_id, "lease_until": lease_until},
                )
                if not getattr(created, "rowcount", 0):
                    return None
                await session.commit()
                return job_id
        except IntegrityError as exc:
            orig = getattr(exc, "orig", None)
            pgcode = getattr(orig, "pgcode", "") or ""
            message = str(exc.orig) if orig is not None else str(exc)
            if pgcode == "23505" or "uq_collection_jobs_active_channel" in message:
                return None
            raise

    async def finish_collection_job(self, job_id: uuid.UUID, *, status: str = "succeeded", error: str | None = None) -> None:
        await self._execute(
            """UPDATE collection_jobs SET status=:status, finished_at=now(), lease_until=now(),
                      error_code=CASE WHEN CAST(:error AS text) IS NULL THEN NULL ELSE 'collector_error' END,
                      error_detail=:error
               WHERE workspace_id=:workspace_id AND id=:job_id""",
            {"job_id": job_id, "status": status, "error": (error or "")[:1000] if error else None},
        )

    async def expire_stale_collection_jobs(self) -> int:
        """Mark running jobs with expired leases as failed (startup recovery)."""

        result = await self._execute(
            """UPDATE collection_jobs SET status='failed', finished_at=now(),
                      error_code='stale_on_startup', error_detail='stale_on_startup'
               WHERE workspace_id=:workspace_id AND status='running' AND lease_until < now()""",
        )
        return int(result.rowcount or 0)

    async def prune_collection_jobs(self, older_than_days: int = 7) -> int:
        """Delete finished jobs older than the cutoff; running jobs are kept."""

        result = await self._execute(
            """DELETE FROM collection_jobs
                WHERE workspace_id=:workspace_id AND status <> 'running'
                  AND finished_at IS NOT NULL
                  AND finished_at < now() - (CAST(:days AS integer) * interval '1 day')""",
            {"days": max(0, int(older_than_days))},
        )
        return int(result.rowcount or 0)

    async def renew_collection_job(self, job_id: uuid.UUID, lease_seconds: int = 180) -> None:
        """Refresh lease for a long-running collection job."""

        lease_until = datetime.now(timezone.utc) + timedelta(seconds=max(30, lease_seconds))
        await self._execute(
            """UPDATE collection_jobs SET lease_until=:lease_until
               WHERE workspace_id=:workspace_id AND id=:job_id AND status='running'""",
            {"job_id": job_id, "lease_until": lease_until},
        )

    async def post_ids_with_comments(self, channel_id: int) -> list[int]:
        """Return distinct post IDs that have at least one non-deleted comment."""

        result = await self._execute(
            """SELECT DISTINCT post_id FROM comments
               WHERE workspace_id=:workspace_id AND post_id IN (
                   SELECT id FROM posts WHERE workspace_id=:workspace_id AND channel_id=:channel_id
               ) AND is_deleted=false""",
            {"channel_id": channel_id},
        )
        return [int(row[0]) for row in result.all()]

    async def last_successful_cycle_at(self) -> datetime | None:
        """Return the newest succeeded collection job finish time, if any."""
        row = (
            await self._execute(
                """SELECT MAX(finished_at) AS value FROM collection_jobs
                    WHERE workspace_id=:workspace_id AND status='succeeded'""",
                {},
            )
        ).mappings().first()
        value = row["value"] if row else None
        return _aware(value) if value is not None else None
