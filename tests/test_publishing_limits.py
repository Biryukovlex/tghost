"""Caption limits come from the publishing account, including later upgrades."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from telethon.tl import functions, types

from app.publishing.domain import PublishingError, make_parts, snapshot
from app.publishing.telegram import TelegramPublisher
from app.publishing.worker import PublishingWorker


class LimitsClient:
    def __init__(self, premium, config=None, error=None):
        self.premium, self.config, self.error = premium, config or {}, error

    async def get_me(self):
        return SimpleNamespace(id=777, premium=self.premium)

    async def __call__(self, request):
        assert isinstance(request, functions.help.GetAppConfigRequest)
        if self.error:
            raise self.error
        return SimpleNamespace(config=types.JsonObject([
            types.JsonObjectValue(key, types.JsonNumber(value))
            for key, value in self.config.items()
        ]))


@pytest.mark.asyncio
@pytest.mark.parametrize('premium,expected', [(False, 1024), (True, 4096)])
async def test_account_defaults_when_config_omits_caption_limit(premium, expected):
    adapter = TelegramPublisher(LimitsClient(premium))
    await adapter.load_limits()
    assert adapter.account_id == 777
    assert adapter.caption_limit == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('premium,expected', [(False, 1024), (True, 4096)])
async def test_verified_account_limit_survives_config_outage(premium, expected):
    adapter = TelegramPublisher(LimitsClient(premium, error=TimeoutError()))
    with pytest.raises(TimeoutError):
        await adapter.load_limits()
    assert adapter.account_id == 777
    assert adapter.caption_limit == expected


@pytest.mark.asyncio
async def test_configured_limit_overrides_premium_default():
    adapter = TelegramPublisher(LimitsClient(True, {'caption_length_limit_premium': 2048}))
    await adapter.load_limits()
    assert adapter.caption_limit == 2048


@pytest.mark.asyncio
async def test_permission_refresh_tracks_account_upgrade_and_downgrade():
    client = LimitsClient(False)
    adapter = TelegramPublisher(client)
    await adapter.load_limits()
    adapter.permission = AsyncMock(side_effect=lambda channel: (True, '', adapter.caption_limit))
    worker = PublishingWorker.__new__(PublishingWorker)
    worker.adapter = adapter
    worker.repository = SimpleNamespace(
        channels=AsyncMock(return_value=[{'id': 1, 'active': True}]),
        set_permission=AsyncMock(),
    )
    client.premium = True
    await worker.refresh_permissions()
    worker.repository.set_permission.assert_awaited_with(1, True, '', 4096, 777)
    client.premium = False
    await worker.refresh_permissions()
    worker.repository.set_permission.assert_awaited_with(1, True, '', 1024, 777)


@pytest.mark.asyncio
async def test_long_premium_album_is_one_scheduled_group_in_original_order():
    client = LimitsClient(True)
    adapter = TelegramPublisher(client)
    await adapter.load_limits()
    media = [{'id': str(uuid.uuid4())} for _ in range(3)]
    value = snapshot('**Album**\n' + 'a' * 2000, media, 'caption', '', adapter.caption_limit)
    parts = make_parts(value)
    at = datetime.now(timezone.utc) + timedelta(hours=1)
    requests = []

    async def rpc(request, **kwargs):
        requests.append(request)
        if isinstance(request, functions.messages.UploadMediaRequest):
            return types.MessageMediaPhoto(photo=types.Photo(
                id=len(requests), access_hash=2, file_reference=b'fixture',
                date=at, sizes=[], dc_id=1,
            ))
        assert isinstance(request, functions.messages.SendMultiMediaRequest)
        return SimpleNamespace(updates=[update for i, item in enumerate(request.multi_media)
            for update in (types.UpdateMessageID(random_id=item.random_id, id=i + 1),
                types.UpdateNewScheduledMessage(message=types.Message(
                    id=i + 1, peer_id=types.PeerChannel(123), date=at, message=item.message,
                )))])

    class UploadClient:
        upload_file = AsyncMock(return_value=types.InputFile(1, 1, 'photo.jpg', ''))
        __call__ = staticmethod(rpc)
    adapter.client = UploadClient()
    repository = SimpleNamespace(asset=AsyncMock(return_value={'content': b'synthetic JPEG'}))
    await adapter.send_photos(types.InputPeerChannel(123, 1), parts, value, at, repository)
    request = requests[-1]
    assert request.schedule_date == at
    assert [item.random_id for item in request.multi_media] == [part['random_id'] for part in parts]
    assert [item.media.id.id for item in request.multi_media] == [1, 2, 3]
    assert [item.message for item in request.multi_media] == ['Album\n' + 'a' * 2000, '', '']
    assert len(request.multi_media[0].entities) == 1
    assert [part['scheduled_id'] for part in parts] == [1, 2, 3]
    assert [part['kind'] for part in parts] == ['photo'] * 3
    assert sum(isinstance(item, functions.messages.SendMultiMediaRequest) for item in requests) == 1
    with pytest.raises(PublishingError, match='caption limit'):
        snapshot(value['body'], media, 'caption', '', 1024)
