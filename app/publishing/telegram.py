"""MTProto scheduling adapter, always using the worker's existing user session."""
from __future__ import annotations
import io
import asyncio
import uuid
from telethon import utils
from telethon.tl import functions, types
from .domain import PublishingError, telegram_text, validate_future


def config_value(value):
    if isinstance(value, types.JsonObject):
        return {item.key: config_value(item.value) for item in value.value}
    if isinstance(value, types.JsonArray):
        return [config_value(item) for item in value.value]
    return getattr(value, 'value', None)


class TelegramPublisher:
    def __init__(self, client):
        self.client = client
        self.caption_limit = 1024
        self.account_id = None

    async def load_limits(self):
        me = await self.client.get_me()
        if me is None:
            raise PublishingError('Reconnect Telegram before scheduling a post.', 'account_unavailable')
        self.account_id = me.id
        premium = bool(getattr(me, 'premium', False))
        # Set the verified account's default before fetching optional config.
        # A missing key or transient config failure must not impose the free
        # account's limit on a Premium account (or keep it after a downgrade).
        self.caption_limit = 4096 if premium else 1024
        config = config_value((await self.client(functions.help.GetAppConfigRequest(hash=0))).config) or {}
        name = 'caption_length_limit_premium' if premium else 'caption_length_limit_default'
        limit = int(config.get(name, self.caption_limit))
        if limit > 0:
            self.caption_limit = limit

    async def peer(self, channel):
        chat_id = int(channel.get('chat_id') or 0)
        if not chat_id:
            raise PublishingError('Wait until Telegram resolves this channel.', 'channel_unavailable')
        try:
            entity = await self.client.get_entity(types.PeerChannel(chat_id))
        except (ValueError, TypeError):
            entity = await self.client.get_entity(channel['identifier'])
        if not isinstance(entity, types.Channel) or entity.id != chat_id:
            raise PublishingError('The channel username now resolves to a different Telegram channel.', 'channel_identity_changed')
        return entity

    async def mutate(self, request):
        # Do not let Telethon sleep/retry a flood-wait past the chosen schedule time.
        return await asyncio.wait_for(self.client(request, flood_sleep_threshold=0), timeout=30)

    async def permission(self, channel):
        try:
            entity = await self.peer(channel)
            if not isinstance(entity, types.Channel) or not entity.broadcast:
                return False, 'Publishing supports broadcast Telegram channels.', self.caption_limit
            permissions = await self.client.get_permissions(entity, 'me')
            allowed = bool(permissions.is_creator or permissions.post_messages)
            return allowed, '' if allowed else 'The connected Telegram account cannot publish in this channel.', self.caption_limit
        except Exception:
            return False, 'Telegram could not verify publishing permissions. Check the connection and channel access.', self.caption_limit

    async def history(self, peer):
        result = await self.client(functions.messages.GetScheduledHistoryRequest(peer=peer, hash=0))
        return {message.id: message for message in result.messages}

    @staticmethod
    def accepted(result, parts):
        updates = getattr(result, 'updates', [])
        by_random = {u.random_id: u.id for u in updates if isinstance(u, types.UpdateMessageID)}
        scheduled = [u.message for u in updates if isinstance(u, types.UpdateNewScheduledMessage)]
        if len(scheduled) != len(parts):
            raise PublishingError('Telegram did not confirm every scheduled message. Check its schedule queue.', 'uncertain_result', 409)
        # UpdateMessageID provides explicit random_id mapping; albums preserve request order as fallback.
        for part, message in zip(parts, scheduled):
            part['scheduled_id'] = by_random.get(part['random_id'], message.id)
            match = next((m for m in scheduled if m.id == part['scheduled_id']), None)
            if match is None:
                raise PublishingError('Telegram returned inconsistent scheduled message IDs.', 'uncertain_result', 409)
            part['text'] = match.message
            part['at'] = match.date.isoformat()
            if getattr(getattr(match, 'media', None), 'photo', None):
                part['photo_id'] = match.media.photo.id
        return parts

    async def send_photos(self, peer, parts, value, at, repository):
        inputs = []
        caption, entities = telegram_text(value['body'] if value['mode'] == 'caption' else value['caption'])
        for index, part in enumerate(parts):
            asset = await repository.asset(uuid.UUID(part['asset_id']))
            if not asset:
                raise PublishingError('An image is unavailable. Nothing was sent.', 'media_missing')
            upload = io.BytesIO(bytes(asset['content']))
            upload.name = 'photo.jpg'
            file = await self.client.upload_file(upload)
            media = await self.client(functions.messages.UploadMediaRequest(peer=peer, media=types.InputMediaUploadedPhoto(file)))
            inputs.append(types.InputSingleMedia(media=utils.get_input_media(media), random_id=part['random_id'], message=caption if index == 0 else '', entities=entities if index == 0 else []))
        validate_future(at)  # Upload can take minutes; never turn an expired schedule into a send-now.
        if len(inputs) == 1:
            item = inputs[0]
            request = functions.messages.SendMediaRequest(peer=peer, media=item.media, message=item.message, entities=item.entities, random_id=item.random_id, schedule_date=at)
        else:
            request = functions.messages.SendMultiMediaRequest(peer=peer, multi_media=inputs, schedule_date=at)
        return self.accepted(await self.mutate(request), parts)

    async def send_text(self, peer, part, body, at):
        validate_future(at)
        text, entities = telegram_text(body)
        result = await self.mutate(functions.messages.SendMessageRequest(peer=peer, message=text, entities=entities, random_id=part['random_id'], schedule_date=at, no_webpage=True))
        return self.accepted(result, [part])[0]

    async def edit(self, peer, part, body, at):
        validate_future(at)
        current = await self.history(peer)
        if part['scheduled_id'] not in current:
            raise PublishingError('A message is no longer in Telegram’s schedule queue. Check it before editing.', 'uncertain_result', 409)
        text, entities = telegram_text(body)
        await self.mutate(functions.messages.EditMessageRequest(peer=peer, id=part['scheduled_id'], message=text, entities=entities, schedule_date=at, no_webpage=True))
        actual = (await self.history(peer)).get(part['scheduled_id'])
        if not actual or actual.message != text or abs((actual.date-at).total_seconds()) > 1:
            raise PublishingError('Telegram did not confirm the edit.', 'uncertain_result', 409)
        part['text'], part['at'] = actual.message, actual.date.isoformat()
        return part

    async def cancel(self, peer, parts):
        history = await self.history(peer)
        ids = [p['scheduled_id'] for p in parts]
        if any(item not in history for item in ids):
            raise PublishingError('A message is no longer scheduled. Check Telegram before cancelling this group.', 'uncertain_result', 409)
        await self.mutate(functions.messages.DeleteScheduledMessagesRequest(peer=peer, id=ids))
        history = await self.history(peer)
        if any(item in history for item in ids):
            raise PublishingError('Telegram has not confirmed cancellation of every message.', 'uncertain_result', 409)
