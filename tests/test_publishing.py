from __future__ import annotations
import io
import re
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from PIL import Image
from telethon.tl import types
from app.publishing.domain import PublishingError, ScheduleInput, local_to_utc, snapshot, make_parts, telegram_text
from app.publishing.media import prepare_image
from app.publishing.repository import PublishingRepository
from app.publishing.worker import PublishingWorker
from app.publishing.telegram import TelegramPublisher
from app.studio.drafts import DraftValidationError

UTC = timezone.utc


def test_scheduling_time_guards_and_dst():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(PublishingError, match='does not exist'):
        local_to_utc('2026-03-29T02:30', 'Europe/Madrid', now=now)
    with pytest.raises(PublishingError, match='occurs twice'):
        local_to_utc('2026-10-25T02:30', 'Europe/Madrid', now=now)
    first = local_to_utc('2026-10-25T02:30', 'Europe/Madrid', 0, now=now)
    second = local_to_utc('2026-10-25T02:30', 'Europe/Madrid', 1, now=now)
    assert second - first == timedelta(hours=1)
    with pytest.raises(PublishingError, match='two minutes'):
        local_to_utc('2026-01-01T00:01', 'UTC', now=now)
    with pytest.raises(PublishingError, match='without an offset'):
        local_to_utc('2026-01-02T10:00+02:00', 'Europe/Madrid', now=now)
    with pytest.raises(PublishingError):
        local_to_utc('2026-01-02T10:00', 'Not/AZone', now=now)


def jpeg(color='cyan'):
    output = io.BytesIO()
    Image.new('RGB', (160, 120), color).save(output, format='PNG')
    return output.getvalue()


def test_uploaded_images_are_decoded_and_metadata_removed():
    result = prepare_image(jpeg())
    assert result['content'].startswith(b'\xff\xd8')
    assert result['width'] == 160 and result['height'] == 120
    assert result['content_type'] == 'image/jpeg'
    for payload in (b'<svg></svg>', b'not an image', b''):
        with pytest.raises(PublishingError):
            prepare_image(payload)


def test_caption_limits_use_visible_utf16_and_no_silent_splitting():
    media = [{'id': str(uuid.uuid4())}]
    with pytest.raises(PublishingError, match='caption limit'):
        snapshot('🙂' * 513, media, 'caption', '', 1024)
    value = snapshot('**Long** ' + 'a'*1500, media, 'separate', 'Short caption', 1024)
    assert [p['kind'] for p in make_parts(value)] == ['photo', 'text']
    assert telegram_text('**Hello**\n\n[Link](https://example.test)')[0] == 'Hello\n\nLink'
    with pytest.raises(PublishingError, match='ten images'):
        snapshot('Album', media * 11, 'caption', '', 1024)


async def make_draft(app, body='A **synthetic** post'):
    repository = app.state.studio_repository
    conversation = await repository.create_conversation(channel_id=app.state.test_channel_id)
    return await repository.create_draft(conversation_id=uuid.UUID(str(conversation['id'])), channel_id=app.state.test_channel_id, payload={'body': body, 'creative': True})


async def login(client, settings):
    await client.post('/login', data={'username': settings.admin_username, 'password': settings.admin_password})
    response = await client.get('/calendar')
    return re.search(r'<meta name="studio-csrf-token" content="([^"]+)"', response.text).group(1)


def schedule_input(draft, channel, **changes):
    at = datetime.now(UTC)+timedelta(hours=2)
    value = {'draft_id': draft['id'], 'expected_revision': draft['revision'], 'channel_id': channel, 'local_datetime': at.strftime('%Y-%m-%dT%H:%M'), 'timezone': 'UTC', 'confirm': True, 'mode': 'caption', 'caption': '', 'idempotency_key': str(uuid.uuid4())}
    return {**value, **changes}


@pytest.mark.asyncio
async def test_image_versions_snapshot_idempotency_and_channel_delete_guard(app, client, settings):
    token = await login(client, settings)
    draft = await make_draft(app)
    publishing = PublishingRepository(app.state.db)
    await publishing.set_permission(app.state.test_channel_id, True, '', account_id=777)
    upload = await client.post(f"/studio/api/drafts/{draft['id']}/media", files={'file': ('fixture.png', jpeg(), 'image/png')}, headers={'x-csrf-token': token})
    assert upload.status_code == 200
    oversized = await client.post(f"/studio/api/drafts/{draft['id']}/media", files={'file': ('large.png', b'x' * (11 * 1024 * 1024), 'image/png')}, headers={'x-csrf-token': token})
    assert oversized.status_code == 413
    asset = upload.json()['media']
    assert (await client.get(asset['url'])).content.startswith(b'\xff\xd8')
    saved = await client.patch(f"/studio/api/drafts/{draft['id']}", json={'expected_revision': draft['revision'], 'media_ids': [asset['id']], 'save_as_new_version': True}, headers={'x-csrf-token': token})
    assert saved.status_code == 200, saved.text
    draft = saved.json()['draft']
    data = schedule_input(draft, app.state.test_channel_id)
    response = await client.post('/studio/api/publishing/posts', json=data, headers={'x-csrf-token': token})
    assert response.status_code == 200, response.text
    post = response.json()['post']
    repeat = await client.post('/studio/api/publishing/posts', json=data, headers={'x-csrf-token': token})
    assert repeat.json()['post']['id'] == post['id']
    assert post['snapshot']['media'][0]['id'] == asset['id']
    assert 'random_id' not in post['parts'][0]
    newer = await app.state.studio_repository.save_draft(draft_id=draft['id'], payload={'body': 'Changed later', 'media_ids': []}, expected_revision=draft['revision'], new_version=True)
    unchanged = await publishing.get(uuid.UUID(post['id']))
    assert unchanged['snapshot']['body'] == 'A **synthetic** post' and unchanged['snapshot']['media'][0]['id'] == asset['id']
    restored = await app.state.studio_repository.choose_draft_version(draft_id=draft['id'], version=2, expected_revision=newer['revision'])
    assert restored['media_ids'] == [asset['id']]
    with pytest.raises(ValueError, match='scheduled posts'):
        await app.state.db.delete_channel(app.state.test_channel_id, confirmation='@sample_channel')
    assert len(await publishing.events(uuid.UUID(post['id']))) == 1


@pytest.mark.asyncio
async def test_permissions_confirmation_revision_media_scope_and_csrf(app, client, settings):
    draft = await make_draft(app)
    data = schedule_input(draft, app.state.test_channel_id)
    assert (await client.post('/studio/api/publishing/posts', json=data)).status_code == 401
    assert (await client.post(f"/studio/api/drafts/{draft['id']}/media", json={})).status_code == 401
    token = await login(client, settings)
    assert (await client.post('/studio/api/publishing/posts', json=data)).status_code == 403
    headers = {'x-csrf-token': token}
    assert (await client.post('/studio/api/publishing/posts', json=data, headers=headers)).status_code == 409
    publishing = PublishingRepository(app.state.db)
    await publishing.set_permission(app.state.test_channel_id, True, '', account_id=777)
    assert (await client.post('/studio/api/publishing/posts', json={**data, 'confirm': False}, headers=headers)).status_code == 422
    assert (await client.post('/studio/api/publishing/posts', json={**data, 'expected_revision': 999}, headers=headers)).status_code == 409
    second = await make_draft(app)
    asset = await publishing.upload(uuid.UUID(second['id']), 'test.png', prepare_image(jpeg()))
    with pytest.raises(DraftValidationError, match='unavailable'):
        await app.state.studio_repository.save_draft(draft_id=draft['id'], payload={'media_ids': [asset['id']]}, expected_revision=draft['revision'])
    rows = await publishing.list(datetime.now(UTC)-timedelta(days=1), datetime.now(UTC)+timedelta(days=2))
    assert rows == ([], False)


class FakeTelegram:
    """Deterministic schedule server; never sends a real Telegram request."""
    def __init__(self):
        self.account_id = 777
        self.messages = {}
        self.calls = []
        self.allowed = True
        self.fail_text = False
        self.next_id = 100
    async def permission(self, channel):
        return self.allowed, '' if self.allowed else 'No publishing permission', 1024
    async def load_limits(self): pass
    async def peer(self, channel): return channel
    async def history(self, peer): return self.messages
    def insert(self, part, body, at):
        self.next_id += 1
        part.update(scheduled_id=self.next_id, text=telegram_text(body)[0], at=at.isoformat())
        self.messages[self.next_id] = SimpleNamespace(id=self.next_id, message=part['text'], date=at, entities=[], media=None)
        return part
    async def send_photos(self, peer, parts, value, at, repository):
        self.calls.append(('album', len(parts)))
        for i,p in enumerate(parts): self.insert(p, (value['body'] if value['mode']=='caption' else value['caption']) if i==0 else '', at)
    async def send_text(self, peer, part, body, at):
        self.calls.append(('text', part['random_id']))
        if self.fail_text: raise TimeoutError()
        return self.insert(part, body, at)
    async def edit(self, peer, part, body, at):
        self.calls.append(('edit', part['scheduled_id']))
        part.update(text=telegram_text(body)[0], at=at.isoformat())
        self.messages[part['scheduled_id']] = SimpleNamespace(id=part['scheduled_id'], message=part['text'], date=at, entities=[], media=None)
    async def cancel(self, peer, parts):
        self.calls.append(('cancel', len(parts)))
        for part in parts: self.messages.pop(part['scheduled_id'], None)


async def queued_post(app, images=0, mode='caption'):
    draft = await make_draft(app)
    repo = PublishingRepository(app.state.db)
    ids = []
    for i in range(images): ids.append((await repo.upload(uuid.UUID(draft['id']), 'fixture.png', prepare_image(jpeg())))['id'])
    if ids: draft = await app.state.studio_repository.save_draft(draft_id=draft['id'], payload={'media_ids': ids}, expected_revision=draft['revision'], new_version=True)
    await repo.set_permission(app.state.test_channel_id, True, '', account_id=777)
    payload = ScheduleInput.model_validate(schedule_input(draft, app.state.test_channel_id, mode=mode))
    return repo, await repo.create(payload, local_to_utc(payload.local_datetime, payload.timezone), draft, [])


@pytest.mark.asyncio
async def test_worker_album_text_update_cancel_and_audit(app):
    repo, post = await queued_post(app, images=3, mode='separate')
    adapter = FakeTelegram(); worker = PublishingWorker(app.state.db, adapter)
    await worker.refresh_permissions()
    row = await repo.claim(); assert row
    await worker.process(row)
    scheduled = await repo.get(uuid.UUID(post['id']))
    assert scheduled['status'] == 'scheduled'
    assert [c[0] for c in adapter.calls] == ['album', 'text']
    assert len(scheduled['parts']) == 4 and all(p.get('scheduled_id') for p in scheduled['parts'])
    at = datetime.now(UTC)+timedelta(hours=3)
    value = {**scheduled['snapshot'], 'body': 'Edited synthetic post'}
    await repo.command(uuid.UUID(post['id']), scheduled['revision'], 'update', at=at, zone='UTC', value=value)
    await worker.process(await repo.claim())
    edited = await repo.get(uuid.UUID(post['id']))
    assert edited['status'] == 'scheduled' and edited['snapshot']['body'] == value['body']
    assert len([c for c in adapter.calls if c[0]=='edit']) == 4
    await repo.command(uuid.UUID(post['id']), edited['revision'], 'cancel')
    await worker.process(await repo.claim())
    assert (await repo.get(uuid.UUID(post['id'])))['status'] == 'cancelled'
    assert not adapter.messages
    assert [e['status'] for e in await repo.events(uuid.UUID(post['id']))] == ['cancelled','cancelling','scheduled','updating','scheduled','queued']


@pytest.mark.asyncio
async def test_partial_album_is_not_retried_and_missing_queue_is_not_publication(app):
    repo, post = await queued_post(app, images=2, mode='separate')
    adapter = FakeTelegram(); adapter.fail_text = True
    worker = PublishingWorker(app.state.db, adapter); await worker.refresh_permissions()
    await worker.process(await repo.claim())
    result = await repo.get(uuid.UUID(post['id']))
    assert result['status'] == 'needs_review'
    assert [p.get('scheduled_id') for p in result['parts'][:2]] == [101,102]
    assert await repo.claim() is None
    assert len(adapter.calls)==2
    # In another, complete text-only schedule the history disappearance is ambiguous.
    repo, post = await queued_post(app)
    adapter.fail_text = False
    await worker.process(await repo.claim())
    adapter.messages.clear()
    await worker.reconcile()
    assert (await repo.get(uuid.UUID(post['id'])))['status'] == 'needs_review'


@pytest.mark.asyncio
async def test_expired_or_forbidden_commands_never_send(app):
    repo, post = await queued_post(app)
    adapter = FakeTelegram(); worker = PublishingWorker(app.state.db, adapter); await worker.refresh_permissions()
    row = await repo.claim(); row['scheduled_at'] = datetime.now(UTC)-timedelta(seconds=1)
    await worker.process(row)
    assert not adapter.calls
    assert (await repo.get(uuid.UUID(post['id'])))['status']=='failed'
    repo, post = await queued_post(app)
    adapter.allowed = False
    await worker.process(await repo.claim())
    assert not adapter.calls
    assert (await repo.get(uuid.UUID(post['id'])))['status']=='failed'
    repo, post = await queued_post(app)
    adapter.allowed = True
    await app.state.db._execute('UPDATE channels SET active=false WHERE workspace_id=:workspace_id AND id=:id', {'id': post['channel_id']})
    await worker.process(await repo.claim())
    assert not adapter.calls
    assert (await repo.get(uuid.UUID(post['id'])))['status']=='failed'


@pytest.mark.asyncio
async def test_dead_lease_never_replays_and_confirmed_telegram_updates(app):
    repo, post = await queued_post(app)
    row = await repo.claim()
    await app.state.db._execute("UPDATE scheduled_posts SET lease_until=now()-interval '1 second' WHERE workspace_id=:workspace_id AND id=:id", {'id': row['id']})
    assert await repo.claim() is None
    assert (await repo.get(row['id']))['status']=='needs_review'
    repo, post = await queued_post(app, images=2)
    adapter = FakeTelegram(); worker=PublishingWorker(app.state.db, adapter); await worker.refresh_permissions(); await worker.process(await repo.claim())
    result = await repo.get(uuid.UUID(post['id']))
    ids=[p['scheduled_id'] for p in result['parts']]
    await worker.update(types.UpdateDeleteScheduledMessages(peer=types.PeerChannel(123456),messages=ids,sent_messages=[900,901]))
    published=await repo.get(uuid.UUID(post['id']))
    assert published['status']=='published' and [p['published_id'] for p in published['parts']]==[900,901]


@pytest.mark.asyncio
async def test_real_adapter_uses_persisted_random_ids_album_and_schedule_date(app):
    class Client:
        def __init__(self): self.calls=[]
        async def upload_file(self, file): return types.InputFile(1,1,'photo.jpg','')
        async def __call__(self, request, **kwargs):
            self.calls.append(request)
            from telethon.tl import functions
            if isinstance(request, functions.messages.UploadMediaRequest):
                return types.MessageMediaPhoto(photo=types.Photo(id=1,access_hash=2,file_reference=b'a',date=datetime.now(UTC),sizes=[],dc_id=1))
            updates=[]
            items=request.multi_media if hasattr(request,'multi_media') else [request]
            for i,item in enumerate(items):
                updates += [types.UpdateMessageID(random_id=item.random_id,id=i+1), types.UpdateNewScheduledMessage(message=types.Message(id=i+1,peer_id=types.PeerChannel(123456),date=request.schedule_date,message=item.message))]
            return SimpleNamespace(updates=updates)
    repo, post=await queued_post(app, images=2)
    row=await repo.claim(); client=Client(); adapter=TelegramPublisher(client)
    parts=row['parts']; randoms=[p['random_id'] for p in parts]
    await adapter.send_photos(types.InputPeerChannel(123456,1),parts,row['snapshot'],row['scheduled_at'],repo)
    request=client.calls[-1]
    from telethon.tl import functions
    assert isinstance(request,functions.messages.SendMultiMediaRequest)
    assert request.schedule_date==row['scheduled_at']
    assert [m.random_id for m in request.multi_media]==randoms
    assert [p['scheduled_id'] for p in parts]==[1,2]


@pytest.mark.asyncio
async def test_long_album_schedule_uses_refreshed_premium_account_limit(app, client, settings):
    from unittest.mock import AsyncMock
    from tests.test_publishing_limits import LimitsClient

    token = await login(client, settings)
    draft = await make_draft(app, '**Synthetic album**\n' + 'a' * 2000)
    repo = PublishingRepository(app.state.db)
    assets = [await repo.upload(uuid.UUID(draft['id']), 'fixture.png', prepare_image(jpeg())) for _ in range(3)]
    saved = await client.patch(f"/studio/api/drafts/{draft['id']}", json={
        'expected_revision': draft['revision'], 'media_ids': [asset['id'] for asset in assets],
        'save_as_new_version': True,
    }, headers={'x-csrf-token': token})
    assert saved.status_code == 200
    draft = saved.json()['draft']
    account = LimitsClient(False)
    adapter = TelegramPublisher(account)
    adapter.permission = AsyncMock(side_effect=lambda channel: (True, '', adapter.caption_limit))
    worker = PublishingWorker(app.state.db, adapter)
    await worker.refresh_permissions()
    payload = schedule_input(draft, app.state.test_channel_id)
    response = await client.post('/studio/api/publishing/posts', json=payload, headers={'x-csrf-token': token})
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'caption_too_long'
    account.premium = True
    await worker.refresh_permissions()
    response = await client.post('/studio/api/publishing/posts', json=payload, headers={'x-csrf-token': token})
    assert response.status_code == 200, response.text
    post = response.json()['post']
    assert post['snapshot']['body'] == draft['body']
    assert [asset['id'] for asset in post['snapshot']['media']] == [asset['id'] for asset in assets]
    assert [part['kind'] for part in post['parts']] == ['photo'] * 3


@pytest.mark.asyncio
async def test_cross_workspace_media_posts_and_drafts_are_unavailable(app, client, settings):
    from app.postgres_db import PostgresDatabase
    from app.studio.repository import StudioRepository
    token = await login(client, settings)
    other = PostgresDatabase(app.state.db.database_url, workspace_slug='publishing-foreign-'+uuid.uuid4().hex[:10])
    await other.init_db(admin_username=settings.admin_username)
    try:
        channel = await other.upsert_channel('@foreign_fixture', 'Foreign fixture', 91919)
        drafts = StudioRepository(other)
        conversation = await drafts.create_conversation(channel_id=channel)
        draft = await drafts.create_draft(conversation_id=uuid.UUID(str(conversation['id'])), channel_id=channel, payload={'body':'Foreign synthetic post','creative':True})
        publishing = PublishingRepository(other)
        asset = await publishing.upload(uuid.UUID(draft['id']), 'foreign.png', prepare_image(jpeg()))
        await publishing.set_permission(channel, True, '', account_id=777)
        payload = ScheduleInput.model_validate(schedule_input(draft,channel))
        post = await publishing.create(payload, local_to_utc(payload.local_datetime, 'UTC'), draft, [])
        assert (await client.get(asset['url'])).status_code == 404
        assert (await client.get(f"/studio/api/publishing/posts/{post['id']}")).status_code == 404
        assert (await client.post('/studio/api/publishing/posts',json=payload.model_dump(),headers={'x-csrf-token':token})).status_code == 404
    finally:
        await other._execute('DELETE FROM workspaces WHERE id=:workspace_id')
        await other.close()


@pytest.mark.asyncio
async def test_cancel_before_transfer_retry_and_account_change(app):
    repo, post = await queued_post(app)
    cancelled = await repo.command(uuid.UUID(post['id']),post['revision'],'cancel')
    assert cancelled['status']=='cancelled' and await repo.claim() is None
    repo, post = await queued_post(app)
    adapter=FakeTelegram();worker=PublishingWorker(app.state.db,adapter);await worker.refresh_permissions()
    row=await repo.claim();row['scheduled_at']=datetime.now(UTC)-timedelta(seconds=1);await worker.process(row)
    failed=await repo.get(uuid.UUID(post['id']))
    retry=await repo.retry(uuid.UUID(post['id']),failed['revision'],datetime.now(UTC)+timedelta(hours=2),'UTC',failed['snapshot'])
    assert retry['status']=='queued'
    await worker.process(await repo.claim())
    assert (await repo.get(uuid.UUID(post['id'])))['status']=='scheduled'
    repo, post = await queued_post(app)
    adapter.account_id=999
    calls_before=len(adapter.calls)
    await worker.process(await repo.claim())
    assert len(adapter.calls)==calls_before
    assert 'account changed' in (await repo.get(uuid.UUID(post['id'])))['error']


@pytest.mark.asyncio
async def test_external_text_date_edits_are_reconciled_and_unsafe_entities_removed(app):
    repo,post=await queued_post(app)
    adapter=FakeTelegram();worker=PublishingWorker(app.state.db,adapter);await worker.refresh_permissions();await worker.process(await repo.claim())
    message=next(iter(adapter.messages.values()))
    message.message='Unsafe link'
    message.entities=[types.MessageEntityTextUrl(offset=0,length=11,url='javascript:alert(1)')]
    message.date += timedelta(hours=1)
    await worker.reconcile()
    updated=await repo.get(uuid.UUID(post['id']))
    assert updated['snapshot']['body']=='Unsafe link'
    assert 'javascript' not in updated['snapshot']['external_body_html']
    assert updated['scheduled_at']==message.date.isoformat()
    assert updated['status']=='scheduled'


@pytest.mark.asyncio
async def test_manual_resolution_requires_explicit_queue_cleanup_and_audits_owner(app, client, settings):
    token = await login(client, settings)
    repo, post = await queued_post(app)
    row = await repo.claim()
    await repo.finish(row, 'needs_review', 'Network result is uncertain.')
    current = await repo.get(uuid.UUID(post['id']))
    path = f"/studio/api/publishing/posts/{post['id']}/resolve"
    payload = {'expected_revision': current['revision'], 'confirm': True}
    assert (await client.post(path, json=payload)).status_code == 403
    assert (await client.post(path, json=payload, headers={'x-csrf-token': token})).status_code == 422
    assert (await repo.get(uuid.UUID(post['id'])))['status'] == 'needs_review'
    payload['confirmed_removed_in_telegram'] = True
    resolved = await client.post(path, json=payload, headers={'x-csrf-token': token})
    assert resolved.status_code == 200 and resolved.json()['post']['status'] == 'cancelled'
    assert not any(p.get('published_id') for p in resolved.json()['post']['parts'])
    assert 'Manually resolved by the owner' in (await repo.events(uuid.UUID(post['id'])))[0]['message']
    assert (await client.post(path, json=payload, headers={'x-csrf-token': token})).status_code == 409
