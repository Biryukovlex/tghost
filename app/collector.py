"""Stats collection via a Telethon *user* session.

One pass over each channel's configured history yields post metrics plus the
actual comments from linked discussion threads:
    views     -> message.views
    comments  -> message.replies.replies   (requires a linked discussion group)
    reactions -> sum of message.reactions.results[].count
    shares    -> message.forwards          (forward counter shown on the post)
"""

from __future__ import annotations

import asyncio
import logging
import inspect
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from telethon import TelegramClient, errors
from telethon.tl.functions.channels import GetFullChannelRequest

from . import limits
from .config import Settings
from .channel_metrics import eligible_horizons
from .postgres_db import PostgresDatabase
from .workspace_settings import RuntimeSettings
from .telegram_formatting import serialize_entities
from .web.links import normalize_channel_identifier

log = logging.getLogger("collector")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def extract_metrics(msg: Any) -> tuple[int, int, int, int]:
    """Return (views, comments, reactions, shares) for a channel message."""
    views = int(getattr(msg, "views", 0) or 0)
    forwards = int(getattr(msg, "forwards", 0) or 0)

    replies_obj = getattr(msg, "replies", None)
    comments = int(getattr(replies_obj, "replies", 0) or 0) if replies_obj else 0

    reactions = 0
    rx = getattr(msg, "reactions", None)
    for rc in (getattr(rx, "results", None) or []):
        reactions += int(getattr(rc, "count", 0) or 0)

    return views, comments, reactions, forwards


def _sender_details(msg: Any) -> tuple[int | None, str, str]:
    """Return a stable local attribution for a comment author when available."""
    sender = getattr(msg, "sender", None)
    sender_id = getattr(msg, "sender_id", None)
    if sender_id is not None:
        sender_id = int(sender_id)

    username = str(getattr(sender, "username", "") or "") if sender else ""
    if sender is not None:
        title = str(getattr(sender, "title", "") or "").strip()
        first = str(getattr(sender, "first_name", "") or "").strip()
        last = str(getattr(sender, "last_name", "") or "").strip()
        name = title or " ".join(p for p in (first, last) if p) or username
    else:
        name = str(getattr(msg, "post_author", "") or "").strip()

    return sender_id, name or "Unknown / hidden", username


class Collector:
    def __init__(
        self,
        client: TelegramClient | None,
        db: PostgresDatabase,
        settings: Settings | RuntimeSettings,
    ) -> None:
        self.client = client
        self.db = db
        self.settings = settings
        self.workspace_settings: Any = None
        self.on_poll_interval_change: Callable[[float], None] | None = None
        # The interval currently applied to the process scheduler.  This is
        # deliberately separate from ``settings.poll_minutes``: the runtime
        # settings proxy is refreshed from PostgreSQL in-place, so comparing
        # the proxy before and after a refresh cannot detect a web save made
        # by another process.
        self.applied_poll_minutes: float | None = None
        self._entities: dict[str, Any] = {}
        self._lock = asyncio.Lock()  # one cycle at a time (scheduler + manual refresh)
        self._background: asyncio.Task | None = None
        self._pending_polls: dict[int | None, asyncio.Task] = {}
        self._running_poll: asyncio.Task | None = None
        self._running_channel_id: int | None = None
        self._running_reason = ""
        # Successful complete thread reads only. A restart safely causes one
        # fresh reconciliation; partial/failed reads never establish freshness.
        self._comment_checks: dict[int, tuple[int, datetime]] = {}
        self._next_allowed_at: float = 0.0
        self._last_full_scan: dict[int, datetime] = {}

    async def resolve_entity(self, identifier: str) -> Any:
        if identifier in self._entities:
            return self._entities[identifier]
        assert self.client is not None, "Telegram client is required to resolve channels"
        entity = await self.client.get_entity(identifier)
        self._entities[identifier] = entity
        connection_id = None
        connection_getter = getattr(self.db, "active_connection_id", None)
        if connection_getter is not None:
            try:
                connection_id = await (connection_getter())
            except Exception:
                connection_id = None
        title = getattr(entity, "title", "") or ""
        chat_id = int(getattr(entity, "id", 0) or 0) or None
        upsert = getattr(self.db, "upsert_channel")
        try:
            if connection_id is not None:
                await (
                    upsert(identifier, title=title, chat_id=chat_id, connection_id=connection_id)
                )
            else:
                await (upsert(identifier, title=title, chat_id=chat_id))
        except TypeError:
            await (upsert(identifier, title=title, chat_id=chat_id))
        return entity

    async def sync_channels(self) -> list[str]:
        """Resolve configured channels into the DB. Returns identifiers that failed."""
        failed: list[str] = []
        # Union of env channel_list and DB rows with chat_id IS NULL (added via page)
        # Canonicalize environment-seeded entries just like the Settings page:
        # Telethon receives @usernames while numeric channel IDs stay intact.
        idents = [normalize_channel_identifier(value) for value in self.settings.channel_list]
        # Also include DB rows with chat_id IS NULL (not yet resolved)
        try:
            # Fetch all channels and filter those with chat_id is None and not already in idents
            channels = await (self.db.get_channels())
            # get_channels only returns active=true, but we need to include those with chat_id NULL even if active?
            # For Postgres, we can query directly for chat_id IS NULL
            # Use _execute if available to get all with chat_id NULL
            if hasattr(self.db, "_execute"):
                try:
                    result = await self.db._execute(
                        "SELECT identifier FROM channels WHERE workspace_id=:workspace_id AND chat_id IS NULL AND active=true",
                        {},
                    )
                    for row in result.mappings().all():
                        ident = normalize_channel_identifier(row["identifier"])
                        if ident not in idents:
                            idents.append(ident)
                except Exception:
                    pass
            else:
                for ch in channels:
                    ch_dict = ch if isinstance(ch, dict) else dict(ch)
                    ident = normalize_channel_identifier(ch_dict.get("identifier"))
                    if ch_dict.get("chat_id") is None and ident not in idents:
                        idents.append(ident)
        except Exception:
            pass
        for ident in idents:
            try:
                await self.resolve_entity(ident)
                log.info("channel %s resolved", ident)
            except Exception as exc:  # noqa: BLE001 - report and continue
                log.error("cannot resolve channel %s: %s", ident, exc)
                failed.append(ident)
        return failed

    def schedule_channel_poll(self, channel_id: int, reason: str = "manual") -> asyncio.Task:
        return self.schedule_poll(reason, channel_id=channel_id)

    def schedule_poll(self, reason: str = "manual", *, channel_id: int | None = None) -> asyncio.Task:
        """Coalesce refreshes already covered by a running or queued scope."""
        running = self._running_poll
        if running is not None and not running.done() and (self._running_channel_id is None or self._running_channel_id == channel_id) and (reason not in {"web", "manual"} or self._running_reason in {"web", "manual"}):
            return running
        for scope in (None, channel_id):
            pending = self._pending_polls.get(scope)
            if pending is not None and not pending.done():
                return pending
        kwargs: dict[str, Any] = {"reason": reason}
        if channel_id is not None:
            kwargs["channel_id"] = channel_id
        task = asyncio.create_task(self.poll_all(**kwargs))
        self._background = task
        self._pending_polls[channel_id] = task

        def _done(t: asyncio.Task) -> None:
            try:
                t.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                log.exception("background poll (%s) failed: %s", reason, type(exc).__name__)
            finally:
                if self._background is t:
                    self._background = None
                if self._pending_polls.get(channel_id) is t:
                    self._pending_polls.pop(channel_id, None)
        task.add_done_callback(_done)
        return task

    async def sync_comments(self, entity: Any, post_id: int, message_id: int) -> tuple[int, int, int]:
        """Collect every current comment for a channel post's discussion thread."""
        assert self.client is not None, "Telegram client is required to sync comments"
        sync_token = utcnow().isoformat(timespec="microseconds")
        seen = changed = 0

        async for comment in self.client.iter_messages(entity, reply_to=message_id, limit=None):
            if comment.action is not None or comment.date is None:
                continue
            seen += 1
            sender_id, sender_name, sender_username = _sender_details(comment)
            chat = getattr(comment, "chat", None)
            reply = getattr(comment, "reply_to", None)
            media = getattr(comment, "media", None)
            reactions = extract_metrics(comment)[2]

            if await (self.db.upsert_comment(
                post_id=post_id,
                telegram_message_id=int(comment.id),
                discussion_chat_id=int(getattr(comment, "chat_id", 0) or 0),
                discussion_username=str(getattr(chat, "username", "") or ""),
                sender_id=sender_id,
                sender_name=sender_name,
                sender_username=sender_username,
                posted_at=comment.date,
                edited_at=getattr(comment, "edit_date", None),
                text=comment.message or "",
                media_type=type(media).__name__ if media is not None else "",
                reactions=reactions,
                reply_to_message_id=getattr(reply, "reply_to_msg_id", None),
                sync_token=sync_token,
            )):
                changed += 1

        deleted = await (self.db.mark_unseen_comments_deleted(post_id, sync_token))
        return seen, changed, deleted

    async def poll_channel(self, ch: Any, job_id: Any = None, *, force_comments: bool = False) -> tuple[int, int, int, int, int, int]:
        """Scan one channel. Return post, snapshot, and comment collection counts."""

        assert self.client is not None, "Telegram client is required to poll channels"
        entity = await self.resolve_entity(ch["identifier"])
        subscribers = None
        if getattr(self.db, "capture_channel_metrics", None) is not None:
            try:
                full = await self.client(GetFullChannelRequest(entity))
                count = getattr(full.full_chat, "participants_count", None)
                if count is not None and int(count) >= 0:
                    subscribers = int(count)
            except errors.FloodWaitError:
                raise
            except Exception as exc:
                log.warning("subscriber observation unavailable for channel %s: %s", ch["id"], type(exc).__name__)
        cutoff = None
        is_full_scan = False
        if self.settings.track_days > 0:
            cutoff = utcnow() - timedelta(days=self.settings.track_days)
            limit = self.settings.backfill_limit if self.settings.backfill_limit > 0 else None
        else:
            # Whole-history mode with pacing: full scan only every FULL_RESCAN_HOURS
            full_hours = int(limits.FULL_RESCAN_HOURS)
            now = datetime.now(timezone.utc)
            last = self._last_full_scan.get(ch["id"])
            if last is None or (now - last).total_seconds() >= full_hours * 3600:
                cutoff = None
                # A whole-history scan changes the date boundary, not the
                # operator's explicit per-sync message cap.  ``0`` remains
                # the only way to request an unlimited scan.
                limit = self.settings.backfill_limit if self.settings.backfill_limit > 0 else None
                # A capped read cannot prove that older archived posts are
                # absent from Telegram. Retire posts only after an unlimited
                # scan has exhausted the channel history.
                is_full_scan = limit is None
            else:
                recent_minutes = max(float(self.settings.poll_minutes) * 2, 60)
                cutoff = utcnow() - timedelta(minutes=recent_minutes)
                limit = self.settings.backfill_limit if self.settings.backfill_limit > 0 else None

        # Fetch comment-bearing post IDs in one query before scanning.
        commented_ids: set[int] = set()
        has_bulk_comments = False
        getter = getattr(self.db, "post_ids_with_comments", None)
        if getter is not None:
            try:
                commented_ids = set(await (getter(ch["id"])))
                has_bulk_comments = True
            except Exception:
                commented_ids = set()

        seen = written = comments_seen = comments_written = comments_deleted = comment_errors = 0
        seen_message_ids: list[int] = []
        last_renew = time.monotonic()
        # Note: an exception escaping the loop below (e.g. FloodWaitError)
        # skips the retirement step by construction, and an empty scan is
        # skipped by the seen_message_ids guard.
        async for msg in self.client.iter_messages(entity, limit=limit):
            if msg.action is not None:
                continue
            if msg.date is None:
                continue
            msg_date = msg.date if msg.date.tzinfo else msg.date.replace(tzinfo=timezone.utc)
            if cutoff is not None and msg_date < cutoff:
                break
            seen += 1
            seen_message_ids.append(int(msg.id))
            # renew lease every 60s during long scans
            if job_id is not None and time.monotonic() - last_renew >= 60:
                renew = getattr(self.db, "renew_collection_job", None)
                if renew is not None:
                    try:
                        await (renew(job_id))
                    except Exception:
                        pass
                last_renew = time.monotonic()
            post_id = await (self.db.upsert_post(
                ch["id"],
                msg.id,
                msg.date,
                msg.message or "",
                formatting_entities=serialize_entities(getattr(msg, "entities", None)),
            ))
            views, comments, reactions, shares = extract_metrics(msg)
            if await (self.db.add_snapshot_if_changed(post_id, views, comments, reactions, shares)):
                written += 1
            horizon_writer = getattr(self.db, "record_post_horizon_views", None)
            if horizon_writer is not None and eligible_horizons(msg_date, utcnow()):
                try:
                    await horizon_writer(post_id, getattr(msg, "views", None))
                except Exception as exc:
                    log.warning("fixed-age observation failed for post %s: %s", post_id, type(exc).__name__)
            should_fetch = False
            if comments > 0:
                should_fetch = True
            elif has_bulk_comments:
                should_fetch = post_id in commented_ids
            else:
                # fallback per-post check (repositories without a bulk getter)
                try:
                    should_fetch = bool(await (self.db.has_comments(post_id)))
                except Exception:
                    should_fetch = False
            checked = self._comment_checks.get(post_id)
            if should_fetch and not force_comments and checked is not None:
                checked_count, checked_at = checked
                if comments == checked_count and 0 <= (utcnow() - checked_at).total_seconds() < limits.COMMENT_RECHECK_SECONDS:
                    should_fetch = False
            if should_fetch:
                # Invalidate before the first write. A failed/partial pass must
                # retry even when Telegram reports the same comment count.
                self._comment_checks.pop(post_id, None)
                try:
                    c_seen, c_written, c_deleted = await self.sync_comments(entity, post_id, msg.id)
                    comments_seen += c_seen
                    comments_written += c_written
                    comments_deleted += c_deleted
                    self._comment_checks[post_id] = (comments, utcnow())
                    if len(self._comment_checks) > 10_000:
                        self._comment_checks.pop(next(iter(self._comment_checks)))
                    await asyncio.sleep(0.5)
                except errors.FloodWaitError:
                    raise
                except errors.RPCError as exc:
                    comment_errors += 1
                    log.warning("comment sync failed for post %s (%s): %s", post_id, msg.id, type(exc).__name__)
                    continue
                except Exception as exc:  # noqa: BLE001 - keep scanning on unexpected comment errors
                    comment_errors += 1
                    log.warning("comment sync failed for post %s (%s): %s", post_id, msg.id, type(exc).__name__)
                    continue
        # Recent-only scans do not visit 24/48/72-hour-old messages. Fetch due IDs explicitly.
        pending = getattr(self.db, "pending_view_horizons", None)
        if pending is not None:
            try:
                due = await pending(ch["id"])
                for start in range(0, len(due), 100):
                    batch = due[start:start + 100]
                    by_message = {row["message_id"]:row["id"] for row in batch}
                    messages = await self.client.get_messages(entity, ids=list(by_message))
                    for message in messages:
                        if message is None or getattr(message, "id", None) not in by_message:
                            continue
                        post_id = by_message[message.id]
                        await self.db.record_post_horizon_views(post_id, getattr(message, "views", None))
                        await self.db.add_snapshot_if_changed(post_id, *extract_metrics(message))
            except errors.FloodWaitError:
                raise
            except Exception as exc:
                log.warning("due view observations failed for channel %s: %s", ch["id"], type(exc).__name__)
        if is_full_scan:
            self._last_full_scan[ch["id"]] = datetime.now(timezone.utc)
        if seen_message_ids:
            # Reconcile deletions within the proven scan window only: posts at
            # or after the cutoff date and at or above the smallest seen
            # message id.  A capped read therefore never retires an older
            # archived post it could not have reached.
            retire = getattr(self.db, "mark_unseen_posts_deleted", None)
            if retire is not None:
                try:
                    await retire(
                        ch["id"],
                        seen_message_ids,
                        since=cutoff,
                        min_message_id=min(seen_message_ids),
                    )
                except Exception:
                    log.warning("post retirement failed for channel %s", ch["identifier"])
        capture = getattr(self.db, "capture_channel_metrics", None)
        if capture is not None:
            try:
                await capture(ch["id"], subscribers)
            except Exception as exc:
                log.warning("channel metrics observation failed for channel %s: %s", ch["id"], type(exc).__name__)
        return seen, written, comments_seen, comments_written, comments_deleted, comment_errors

    async def poll_all(self, reason: str = "scheduled", *, channel_id: int | None = None) -> dict[str, Any]:
        # Cross-process refresh: reload workspace settings and reschedule if poll interval changed
        if getattr(self, "workspace_settings", None) is not None:
            try:
                await self.workspace_settings.load()  # type: ignore[attr-defined]
                new = float(self.settings.poll_minutes)
                applied = self.applied_poll_minutes
                interval_callback = getattr(self, "on_poll_interval_change", None)
                if applied is not None and new != applied and callable(interval_callback):
                    try:
                        changed = interval_callback(new)
                        if inspect.isawaitable(changed):
                            await changed
                    except Exception:
                        log.warning("on_poll_interval_change failed")
            except Exception:
                log.warning("workspace_settings load failed in poll_all")
        async with self._lock:
            self._running_poll = asyncio.current_task()
            self._running_channel_id = channel_id
            self._running_reason = reason
            try:
                started = utcnow()
                total_seen = total_new = 0
                total_comments = total_comments_changed = total_comments_deleted = total_comment_errors = 0
                err_count = 0
                channels = await (self.db.get_channels())
                if channel_id is not None:
                    channels = [ch for ch in channels if int(ch["id"]) == channel_id]
                for ch in channels:
                    if time.monotonic() < self._next_allowed_at:
                        log.info("skipping channel %s due to active FloodWait", ch["identifier"])
                        continue
                    job_id = None
                    channel_failed = False
                    failure_code = None
                    claim = getattr(self.db, "claim_collection_job", None)
                    if claim is not None:
                        job_id = await (claim(ch["id"]))
                        if job_id is None:
                            log.info("collection already running for channel %s; skipping overlap", ch["identifier"])
                            continue
                    try:
                        seen, written, comments, comments_changed, comments_deleted, comment_errors = (
                            await self.poll_channel(ch, job_id=job_id, **({"force_comments": True} if reason in {"web", "manual"} else {}))
                        )
                        total_seen += seen
                        total_new += written
                        total_comments += comments
                        total_comments_changed += comments_changed
                        total_comments_deleted += comments_deleted
                        total_comment_errors += comment_errors
                    except errors.FloodWaitError as exc:
                        log.warning("flood wait %ss while polling %s", exc.seconds, ch["identifier"])
                        channel_failed = True
                        failure_code = type(exc).__name__
                        self._next_allowed_at = time.monotonic() + float(exc.seconds)
                        err_count += 1
                        await asyncio.sleep(min(float(exc.seconds), 30))
                    except Exception as exc:  # noqa: BLE001
                        err_count += 1
                        channel_failed = True
                        failure_code = type(exc).__name__
                        log.exception("poll failed for %s", ch["identifier"])
                    finally:
                        finish = getattr(self.db, "finish_collection_job", None)
                        if finish is not None and job_id is not None:
                            await (
                                finish(
                                    job_id,
                                    status="failed" if channel_failed else "succeeded",
                                    error=failure_code,
                                )
                            )
                prune = getattr(self.db, "prune_collection_jobs", None)
                if prune is not None:
                    try:
                        await (prune())
                    except Exception:
                        log.warning("collection job pruning failed")
                summary: dict[str, Any] = {
                    "reason": reason,
                    "started": started,
                    "finished": utcnow(),
                    "posts_seen": total_seen,
                    "snapshots_written": total_new,
                    "comments_seen": total_comments,
                    "comments_written": total_comments_changed,
                    "comments_deleted": total_comments_deleted,
                    "comment_errors": total_comment_errors,
                    "errors": err_count,
                }
                log.info(
                    "cycle(%s): %s posts, %s snapshots, %s comments (%s changed, %s deleted) (%.1fs)",
                    reason, total_seen, total_new, total_comments,
                    total_comments_changed, total_comments_deleted,
                    (summary["finished"] - started).total_seconds(),
                )
                return summary
            finally:
                self._running_poll = None
                self._running_channel_id = None
                self._running_reason = ""
