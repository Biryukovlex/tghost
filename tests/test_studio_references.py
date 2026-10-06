"""Reference opt-in, persisted scope and full-archive access use synthetic data."""
import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.config import Settings
from app.postgres_db import PostgresDatabase
from app.studio.agent import StudioDeps, build_agent
from app.studio.references import ReferenceChannels
from pydantic_ai.models.test import TestModel

from app.studio.repository import ConversationNotFound, MemoryStudioRepository, StudioRepository


@pytest.mark.asyncio
async def test_reference_tools_respect_opt_in_revocation_and_private_context():
    repo = MemoryStudioRepository()
    repo.channels.append({'id': 2, 'identifier': '@reference', 'title': 'Reference', 'active': True})
    repo.profiles[2] = {'topics_text': 'Reference topics', 'editorial_text': 'Reference focus', 'style_text': 'Concise', 'system_prompt': 'PRIVATE OTHER PROMPT'}
    repo.system_prompts[2] = 'PRIVATE OTHER PROMPT'
    conversation = await repo.create_conversation(channel_id=1, title='Current')
    other = await repo.create_conversation(channel_id=1, title='Separate')
    post = {'id': 91, 'channel_id': 2, 'identifier': '@reference', 'message_id': 7, 'text': 'A stored reference post', 'posted_at': datetime(2024, 1, 1, tzinfo=timezone.utc), 'views': 23, 'comment_body': 'PRIVATE COMMENT'}
    async def rows(channel_id, limit=2000):
        return [post] if channel_id == 2 else []
    repo.performance_rows = rows
    settings = Settings(studio_test_mode=True)
    refs = ReferenceChannels(repo, settings)
    deps = StudioDeps(repository=repo, workspace_id=repo.workspace_id, conversation_id=conversation['id'], channel_id=1, cancel_event=asyncio.Event())
    agent = build_agent(settings)
    tools = agent._function_toolset.tools
    ctx = SimpleNamespace(deps=deps)
    search = tools['search_reference_posts'].function
    read = tools['read_reference_post'].function
    profile = tools['get_reference_profile'].function
    assert (await search(ctx, 2))['status'] == 'blocked'
    with pytest.raises(ValueError):
        await refs.set(conversation['id'], 2, enabled=True, permission=False, user_id='test')
    await refs.set(conversation['id'], 2, enabled=True, permission=True, user_id='test')
    assert not (await refs.list(other['id']))[0]['selected']
    result = await search(ctx, 2)
    assert result['posts'][0]['source_link'] == 'https://t.me/reference/7'
    assert result['posts'][0]['channel_id'] == 2
    assert (await read(ctx, 2, 91))['text'] == post['text']
    result_profile = await profile(ctx, 2)
    assert result_profile['profile']['topics_text'] == 'Reference topics'
    assert 'PRIVATE' not in json.dumps([result, result_profile])
    assert deps.channel_id == 1
    draft_result = await tools['create_draft'].function(ctx, title='Synthetic angle', body='A synthetic creative angle.', source_links=['https://t.me/reference/7'])
    assert draft_result['draft']['source_links'] == ['https://t.me/reference/7']
    assert 'decision_summary' not in draft_result
    class ReferenceTestModel(TestModel):
        def gen_tool_args(self, tool_def):
            return {
                "search_reference_posts": {"channel_id": 2, "query": "stored"},
                "read_reference_post": {"channel_id": 2, "post_id": 91},
                "get_reference_profile": {"channel_id": 2},
            }.get(tool_def.name, super().gen_tool_args(tool_def))
    routed_agent = build_agent(settings, model=ReferenceTestModel(
        call_tools=["get_channel_context", "search_reference_posts", "read_reference_post", "get_reference_profile"],
        custom_output_text="Reference research complete",
    ))
    response = await routed_agent.run("Use the selected reference to research an angle.", deps=deps)
    returns = [part for message in response.all_messages() for part in message.parts if getattr(part, "part_kind", "") == "tool-return"]
    assert {part.tool_name for part in returns} >= {"get_channel_context", "search_reference_posts", "read_reference_post"}
    assert any('https://t.me/reference/7' in str(part.content) for part in returns)
    context = next(part.content for part in returns if part.tool_name == "get_channel_context")
    assert "reference_channels" in str(context) and "Reference" in str(context)
    run = await repo.create_run(conversation_id=conversation['id'], user_message_id=1, requested_model='fixture')
    with pytest.raises(ValueError, match='Stop'):
        await refs.set(conversation['id'], 2, enabled=False, permission=False, user_id='test')
    repo.runs[run['id']]['status'] = 'cancelled'
    settings.openrouter_model = 'different-model'
    assert (await refs.list(conversation['id']))[0]['needs_renewal']
    assert (await search(ctx, 2))['status'] == 'blocked'
    await refs.set(conversation['id'], 2, enabled=True, permission=True, user_id='test')
    await refs.set(conversation['id'], 2, enabled=False, permission=False, user_id='test')
    assert (await read(ctx, 2, 91))['status'] == 'blocked'
    repo.channels[1]['active'] = False
    assert await refs.list(conversation['id']) == []


@pytest.mark.asyncio
async def test_reference_api_auth_csrf_validation_and_persistence(client, app, channel_id, settings):
    settings.studio_test_mode = True
    unauthenticated = await client.get(f'/studio/api/conversations/{uuid.uuid4()}/references')
    assert unauthenticated.status_code in (303, 401)
    await client.post('/login', data={'username': settings.admin_username, 'password': settings.admin_password})
    page = await client.get('/studio')
    token = re.search(r'<meta name="studio-csrf-token" content="([^"]+)"', page.text).group(1)
    headers = {'x-csrf-token': token}
    db = app.state.db
    other_channel = await db.upsert_channel('@second_fixture', 'Second fixture', 222)
    created = await client.post('/studio/api/conversations', json={'channel_id': channel_id}, headers=headers)
    cid = created.json()['conversation']['id']
    url = f'/studio/api/conversations/{cid}/references/{other_channel}'
    assert (await client.put(url, json={'enabled': True, 'permission': True})).status_code == 403
    assert (await client.put(url, json=[], headers=headers)).status_code == 422
    assert (await client.put(url, json={'enabled': 'true'}, headers=headers)).status_code == 422
    assert (await client.put(url, json={'enabled': True}, headers=headers)).status_code == 422
    assert (await client.put(f'/studio/api/conversations/{cid}/references/{channel_id}', json={'enabled': True, 'permission': True}, headers=headers)).status_code == 404
    added = await client.put(url, json={'enabled': True, 'permission': True}, headers=headers)
    assert added.status_code == 200, added.text
    assert added.json()['references'][0]['selected']
    reloaded = await client.get(f'/studio/api/conversations/{cid}/references')
    assert reloaded.json()['references'][0]['selected']
    settings.studio_test_mode = False
    settings.openrouter_model = 'new-fixture-model'
    assert (await client.put(url, json={'enabled': True, 'permission': True}, headers=headers)).status_code == 409
    assert (await client.put(url, json={'enabled': False}, headers=headers)).status_code == 200
    assert (await client.get(f'/studio/api/conversations/{uuid.uuid4()}/references')).status_code == 404


@pytest.mark.asyncio
async def test_reference_archive_queries_past_latest_sample_and_workspace_boundaries(app, channel_id, settings):
    db = app.state.db
    settings.studio_test_mode = True
    repository = StudioRepository(db)
    reference_id = await db.upsert_channel('@archive_fixture', 'Archive fixture', 333)
    old_post = await db.upsert_post(reference_id, 1, datetime(2020, 1, 1, tzinfo=timezone.utc), 'Unique old archive needle ' + 'full content ' * 300)
    await db.add_snapshot_if_changed(old_post, 5000, 2, 30, 4)
    # More than the normal performance sample of 2,000 posts. The reference
    # query must search the archive in SQL, not filter that recent sample.
    async with db.sessions.session() as session:
        await session.execute(text("""INSERT INTO posts (workspace_id,channel_id,message_id,posted_at,text,created_at,is_deleted)
            SELECT :w,:c,n,NOW() - n * INTERVAL '1 minute','Recent synthetic post',NOW(),false FROM generate_series(2,2102) n"""), {'w':db._workspace(), 'c':reference_id})
        await session.commit()
    conv = await repository.create_conversation(channel_id=channel_id, title='Archive scope')
    refs = ReferenceChannels(repository, settings)
    await refs.set(conv['id'], reference_id, enabled=True, permission=True, user_id=db.user_id)
    reopened = ReferenceChannels(StudioRepository(db), settings)
    result = await reopened.search(conv['id'], reference_id, query='Unique old archive needle')
    assert result['total'] == 1 and result['posts'][0]['post_id'] == old_post
    assert len(result['posts'][0]['text']) == 1500
    full = await reopened.read(conv['id'], reference_id, old_post)
    assert len(full['text']) > 3000 and full['source_link'] == 'https://t.me/archive_fixture/1'
    page = await refs.search(conv['id'], reference_id, offset=2101, limit=12)
    assert page['total'] == 2102 and page['posts'][0]['post_id'] == old_post
    metric = await refs.search(conv['id'], reference_id, sort='views', limit=1)
    assert metric['posts'][0]['post_id'] == old_post
    with pytest.raises(ConversationNotFound):
        await refs.read(conv['id'], reference_id, (await db.explorer_posts(channel_id=channel_id))[0][0]['id'])
    foreign = PostgresDatabase(settings.database_url, workspace_slug=f'ref-foreign-{uuid.uuid4().hex[:8]}')
    try:
        await foreign.init_db(admin_username='foreign-fixture')
        foreign_channel = await foreign.upsert_channel('@foreign_fixture', 'Foreign', 999)
        foreign_conv = await StudioRepository(foreign).create_conversation(channel_id=foreign_channel, title='Private')
        with pytest.raises(ConversationNotFound):
            await refs.list(foreign_conv['id'])
        with pytest.raises(ConversationNotFound):
            await refs.set(conv['id'], foreign_channel, enabled=True, permission=True, user_id=db.user_id)
    finally:
        async with foreign.sessions.session() as session:
            await session.execute(text('DELETE FROM workspaces WHERE id=:w'), {'w':foreign._workspace()})
            await session.commit()
        await foreign.close()
