"""Isolated publishing queue consumer and conservative Telegram reconciliation."""
from __future__ import annotations
import asyncio
import copy
import logging
from datetime import datetime, timedelta, timezone
from telethon import events
from telethon.tl import types
from .domain import PublishingError, snapshot, validate_future
from .repository import PublishingRepository
from .telegram import TelegramPublisher

log = logging.getLogger('publishing.worker')


class PublishingWorker:
    def __init__(self, db, adapter):
        self.repository = PublishingRepository(db)
        self.adapter = adapter
        self.channel_by_id = {}
        self.permission_at = None
        self.reconcile_at = None
        self.lock = asyncio.Lock()

    async def refresh_permissions(self):
        try:
            await self.adapter.load_limits()
        except Exception as exc:
            log.warning('Telegram caption limits refresh failed (%s); using the last account limits.', type(exc).__name__)
        channels = await self.repository.channels()
        self.channel_by_id = {c['id']: c for c in channels}
        for channel in channels:
            if channel['active']:
                allowed, reason, limit = await self.adapter.permission(channel)
                await self.repository.set_permission(channel['id'], allowed, reason, limit, self.adapter.account_id)
        self.permission_at = datetime.now(timezone.utc)

    async def process(self, row):
        # A channel can be deactivated after the command was queued.
        channel = next((c for c in await self.repository.channels() if c['id'] == row['channel_id']), None)
        parts = copy.deepcopy(row['parts'])
        sent_any = any(p.get('scheduled_id') for p in parts)
        # Once a mutating RPC starts, a missing response is ambiguous; never resend automatically.
        mutating = False
        try:
            if row['telegram_user_id'] != self.adapter.account_id:
                raise PublishingError('The connected Telegram account changed. Reconnect the original account to manage this schedule.', 'account_changed')
            if not channel:
                raise PublishingError('Channel is unavailable.', 'channel_unavailable')
            if not channel['active'] and row['action'] != 'cancel':
                raise PublishingError('Channel is inactive. Reactivate it before scheduling.', 'channel_inactive')
            allowed, reason, limit = await self.adapter.permission(channel)
            await self.repository.set_permission(channel['id'], allowed, reason, limit, self.adapter.account_id)
            if not allowed:
                raise PublishingError(reason, 'publishing_forbidden', 403)
            peer = await self.adapter.peer(channel)
            if row['action'] == 'cancel':
                mutating = True
                await self.adapter.cancel(peer, parts)
                await self.repository.finish(row, 'cancelled')
                return
            value = row['pending_snapshot'] if row['action'] == 'update' else row['snapshot']
            at = row['pending_at'] if row['action'] == 'update' else row['scheduled_at']
            validate_future(at)
            snapshot(value['body'], value['media'], value['mode'], value['caption'], limit, value['title'])
            if row['action'] == 'schedule':
                photos = [part for part in parts if part['kind'] == 'photo']
                if photos and not all(p.get('scheduled_id') for p in photos):
                    mutating = True
                    await self.adapter.send_photos(peer, photos, value, at, self.repository)
                    sent_any = True
                    await self.repository.save_parts(row, parts)
                for part in parts:
                    if part['kind'] == 'text' and not part.get('scheduled_id'):
                        mutating = True
                        await self.adapter.send_text(peer, part, value['body'], at + timedelta(seconds=1 if photos else 0))
                        sent_any = True
                        await self.repository.save_parts(row, parts)
            else:
                for index, part in enumerate(parts):
                    body = value['body'] if part['kind'] == 'text' else (value['body'] if value['mode'] == 'caption' else value['caption']) if index == 0 else ''
                    mutating = True
                    await self.adapter.edit(peer, part, body, at + timedelta(seconds=1 if part['kind'] == 'text' and value['media'] else 0))
                    await self.repository.save_parts(row, parts)
            # Verify every constituent message still exists after multi-RPC operations.
            history = await self.adapter.history(peer)
            if any(p.get('scheduled_id') not in history for p in parts):
                raise PublishingError('Not every message was confirmed in the Telegram queue.', 'uncertain_result')
            await self.repository.finish(row, 'scheduled', value=value, at=at, zone=row.get('pending_timezone') or row['timezone'])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            safe = str(exc) if isinstance(exc, PublishingError) else 'Telegram could not confirm this operation. Check its schedule queue before retrying.'
            status = 'needs_review' if mutating or sent_any or row['action'] != 'schedule' else 'failed'
            await self.repository.save_parts(row, parts)
            await self.repository.finish(row, status, safe)
            log.warning('Publishing operation %s: %s (%s)', row['id'], status, type(exc).__name__)

    async def reconcile(self):
        rows = await self.repository.reconcile_rows()
        history_by_channel = {}
        for row in rows:
            if row['telegram_user_id'] != self.adapter.account_id:
                if row['status'] != 'needs_review':
                    await self.repository.confirmed_update(row, status='needs_review', error='The connected Telegram account changed. Reconnect the original account.')
                continue
            parts = copy.deepcopy(row['parts'])
            if not parts or any(not p.get('scheduled_id') for p in parts):
                continue
            try:
                if row['channel_id'] not in history_by_channel:
                    peer = await self.adapter.peer(row)
                    history_by_channel[row['channel_id']] = await self.adapter.history(peer)
                history = history_by_channel[row['channel_id']]
                if any(p['scheduled_id'] not in history and not p.get('published_id') and not p.get('cancelled') for p in parts):
                    if row['status'] != 'needs_review':
                        await self.repository.confirmed_update(row, status='needs_review', error='A message disappeared from the Telegram queue. Publication is not confirmed; check Telegram.')
                    continue
                scheduled = [history[p['scheduled_id']] for p in parts if p['scheduled_id'] in history]
                if len(scheduled) != len(parts):
                    continue
                changed_media = any(p.get('photo_id') and p['photo_id'] != getattr(getattr(m, 'photo', None), 'id', None) for p, m in zip(parts, scheduled) if p['kind'] == 'photo')
                if changed_media:
                    if row['status'] != 'needs_review':
                        await self.repository.confirmed_update(row, status='needs_review', error='Images were changed in Telegram. Preview shows the original assets; review the Telegram queue.')
                    continue
                changed = any(p.get('text') != m.message or p.get('at') != m.date.isoformat() for p, m in zip(parts, scheduled))
                if changed or row['status'] == 'needs_review':
                    value = copy.deepcopy(row['snapshot'])
                    from ..telegram_formatting import render_telegram_html, normalize_entities
                    photo_messages = [m for p, m in zip(parts, scheduled) if p['kind'] == 'photo']
                    text_messages = [m for p, m in zip(parts, scheduled) if p['kind'] == 'text']
                    # Preserve the actual external edit as Telegram HTML with a safe plain body projection.
                    body_message = text_messages[0] if text_messages else scheduled[0]
                    value['body'] = body_message.message
                    value['external_body_html'] = render_telegram_html(body_message.message, normalize_entities(body_message.entities or []))
                    if photo_messages and value['mode'] == 'separate':
                        value['caption'] = photo_messages[0].message
                    for p, m in zip(parts, scheduled):
                        p['text'], p['at'] = m.message, m.date.isoformat()
                    date = scheduled[0].date
                    coherent = all(abs((m.date-date).total_seconds()) <= 1 for m in scheduled)
                    await self.repository.confirmed_update(row, parts=parts, value=value, at=date,
                        status='scheduled' if coherent else 'needs_review', error='' if coherent else 'Telegram messages have different times. Check the complete group.')
            except Exception:
                log.warning('Could not reconcile schedule %s', row['id'])
        self.reconcile_at = datetime.now(timezone.utc)

    async def update(self, update):
        if not isinstance(update, types.UpdateDeleteScheduledMessages):
            return
        async with self.lock:
            for row in await self.repository.reconcile_rows():
                if row['telegram_user_id'] != self.adapter.account_id:
                    continue
                if int(row['chat_id'] or 0) != int(getattr(update.peer, 'channel_id', 0)):
                    continue
                parts = copy.deepcopy(row['parts'])
                changed = False
                sent = getattr(update, 'sent_messages', None) or []
                for p in parts:
                    if p.get('scheduled_id') in update.messages:
                        index = update.messages.index(p['scheduled_id'])
                        if index < len(sent):
                            p['published_id'] = sent[index]
                        else:
                            p['cancelled'] = True
                        changed = True
                if changed or row['status'] == 'needs_review':
                    complete_sent = all(p.get('published_id') for p in parts)
                    complete_cancel = all(p.get('cancelled') for p in parts)
                    status = 'published' if complete_sent else 'cancelled' if complete_cancel else 'needs_review'
                    error = '' if complete_sent or complete_cancel else 'Only part of this group was published or cancelled. Check Telegram.'
                    await self.repository.confirmed_update(row, parts=parts, status=status, error=error)

    async def run(self):
        while True:
            try:
                async with self.lock:
                    now = datetime.now(timezone.utc)
                    if not self.permission_at or (now-self.permission_at).total_seconds() >= 60:
                        await self.refresh_permissions()
                    row = await self.repository.claim()
                    if row:
                        await self.process(row)
                    if not self.reconcile_at or (now-self.reconcile_at).total_seconds() >= 30:
                        await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Failure must never terminate Telegram collection or web serving.
                log.exception('Publishing queue cycle failed')
            await asyncio.sleep(2)


def start_worker(db, client):
    worker = PublishingWorker(db, TelegramPublisher(client))
    client.add_event_handler(worker.update, events.Raw(types.UpdateDeleteScheduledMessages))
    return asyncio.create_task(worker.run(), name='telegram-publishing')
