from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import limits
from app.collector import Collector
from app.config import Settings


NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)


def _collector():
    message = SimpleNamespace(id=1, action=None, date=NOW, message="Synthetic post", entities=[], views=5, forwards=0, reactions=None, replies=SimpleNamespace(replies=2))
    class Client:
        async def iter_messages(self, entity, **kwargs):
            yield message
        async def get_entity(self, identifier):
            return SimpleNamespace(id=1, title="Synthetic")
    db = SimpleNamespace(
        get_channels=AsyncMock(return_value=[{"id": 1, "identifier": "@fixture"}, {"id": 2, "identifier": "@other"}]),
        upsert_channel=AsyncMock(return_value=1), upsert_post=AsyncMock(return_value=10),
        add_snapshot_if_changed=AsyncMock(return_value=False), has_comments=AsyncMock(return_value=True),
    )
    collector = Collector(Client(), db, Settings(track_days=0, backfill_limit=1))
    collector.sync_comments = AsyncMock(return_value=(2, 0, 0))
    return collector, message


@pytest.mark.asyncio
async def test_unchanged_threads_skip_until_reconciliation_but_count_change_and_manual_refresh_do_not(monkeypatch):
    collector, message = _collector()
    monkeypatch.setattr("app.collector.utcnow", lambda: NOW)
    monkeypatch.setattr("app.collector.asyncio.sleep", AsyncMock())
    channel = {"id": 1, "identifier": "@fixture"}
    await collector.poll_channel(channel)
    await collector.poll_channel(channel)
    assert collector.sync_comments.await_count == 1
    message.replies.replies = 3
    await collector.poll_channel(channel)
    assert collector.sync_comments.await_count == 2
    await collector.poll_channel(channel, force_comments=True)
    assert collector.sync_comments.await_count == 3
    monkeypatch.setattr("app.collector.utcnow", lambda: NOW + timedelta(seconds=limits.COMMENT_RECHECK_SECONDS))
    await collector.poll_channel(channel)
    assert collector.sync_comments.await_count == 4


@pytest.mark.asyncio
async def test_failed_thread_scan_never_counts_as_fresh(monkeypatch):
    collector, _ = _collector()
    monkeypatch.setattr("app.collector.utcnow", lambda: NOW)
    monkeypatch.setattr("app.collector.asyncio.sleep", AsyncMock())
    collector.sync_comments.side_effect = [RuntimeError("synthetic partial read"), (2, 0, 0)]
    await collector.poll_channel({"id": 1, "identifier": "@fixture"})
    await collector.poll_channel({"id": 1, "identifier": "@fixture"})
    assert collector.sync_comments.await_count == 2


@pytest.mark.asyncio
async def test_repeated_refreshes_share_one_task_and_targeted_refresh_stays_scoped():
    collector, _ = _collector()
    entered, release = asyncio.Event(), asyncio.Event()
    async def poll_channel(channel, job_id=None, **kwargs):
        entered.set()
        await release.wait()
        return (0, 0, 0, 0, 0, 0)
    collector.poll_channel = AsyncMock(side_effect=poll_channel)
    first = collector.schedule_channel_poll(1, reason="web")
    second = collector.schedule_channel_poll(1, reason="web")
    assert first is second
    await entered.wait()
    assert collector.schedule_channel_poll(1, reason="web") is first
    release.set()
    await first
    assert collector.poll_channel.await_count == 1
    assert collector.poll_channel.call_args.args[0]["id"] == 1


@pytest.mark.asyncio
async def test_manual_refresh_queued_behind_scheduled_cycle_forces_one_fresh_check():
    collector, _ = _collector()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def poll_channel(channel, job_id=None, **kwargs):
        calls.append((channel["id"], kwargs.get("force_comments", False)))
        if len(calls) == 1:
            entered.set()
            await release.wait()
        return (0, 0, 0, 0, 0, 0)
    collector.poll_channel = poll_channel
    scheduled = asyncio.create_task(collector.poll_all())
    await entered.wait()
    first = collector.schedule_channel_poll(1, reason="web")
    assert first is collector.schedule_channel_poll(1, reason="web")
    release.set()
    await asyncio.gather(scheduled, first)
    assert calls == [(1, False), (2, False), (1, True)]
    assert collector._running_poll is None


@pytest.mark.asyncio
async def test_failed_cycle_releases_running_scope():
    collector, _ = _collector()
    collector.db.get_channels.side_effect = RuntimeError("synthetic database unavailable")
    with pytest.raises(RuntimeError):
        await collector.poll_all()
    assert collector._running_poll is None
