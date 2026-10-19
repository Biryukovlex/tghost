import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from ag_ui.core import RunStartedEvent, RunFinishedEvent, TextMessageStartEvent, TextMessageContentEvent, TextMessageEndEvent
from pydantic_ai.messages import ModelResponse
from pydantic_ai.ui.ag_ui import AGUIAdapter

from app.config import Settings
from app.studio.repository import MemoryStudioRepository
from app.studio.service import StudioService


ROOT = Path(__file__).resolve().parents[1]


def test_system_prompt_control_is_channel_local_inside_studio():
    source = (ROOT / "studio-frontend/src/main.tsx").read_text(encoding="utf-8")
    settings_template = (ROOT / "app/web/templates/settings.html").read_text(encoding="utf-8")
    bundle = (ROOT / "app/web/static/studio-dist/assets/studio.js").read_text(encoding="utf-8")

    # T44: System Prompt moved to MoreActionsMenu (one menuitem); rail footer buttons removed
    assert "System Prompt" in source
    assert source.count(">System Prompt") >= 1
    assert '<h2 id="studio-settings-title">System Prompt</h2>' in source
    assert '<label htmlFor="studio-system-prompt">System Prompt</label>' in source
    assert ">Settings</button>" not in source
    assert 'name="system_prompt"' not in settings_template
    assert "each channel's System Prompt is edited inside Studio" in settings_template
    assert "System Prompt" in bundle
    assert "Studio settings" not in bundle


@pytest.mark.asyncio
async def test_settings_authenticated_csrf_validated_and_channel_scoped(client, settings, app, channel_id):
    settings.studio_test_mode = True
    await client.post("/login", data={"username": settings.admin_username, "password": settings.admin_password})
    home = await client.get("/studio")
    token = re.search(r'<meta name="studio-csrf-token" content="([^"]+)"', home.text).group(1)
    repository = app.state.studio_repository
    other_id = await repository.db.upsert_channel("@other_channel", "Other", 7004)
    assert (await client.get(f"/studio/api/settings?channel_id={channel_id}")).json() == {"channel_id": channel_id, "system_prompt": ""}
    assert (await client.patch("/studio/api/settings", json={"channel_id": channel_id, "system_prompt": "x"})).status_code == 403
    headers = {"x-csrf-token": token}
    for invalid in [None, 42, "x" * 12001]:
        assert (await client.patch("/studio/api/settings", headers=headers, json={"channel_id": channel_id, "system_prompt": invalid})).status_code == 422
    prompt = "Пиши коротко. Без служебных комментариев в постах."
    assert (await client.patch("/studio/api/settings", headers=headers, json={"channel_id": channel_id, "system_prompt": prompt})).status_code == 200
    assert (await client.get(f"/studio/api/settings?channel_id={channel_id}")).json()["system_prompt"] == prompt
    assert (await client.get(f"/studio/api/settings?channel_id={other_id}")).json()["system_prompt"] == ""
    other = MemoryStudioRepository(workspace_id="other-workspace")
    assert await other.get_system_prompt(1) == ""
    assert (await client.patch("/studio/api/settings", headers=headers, json={"channel_id": channel_id, "system_prompt": ""})).status_code == 200


@pytest.mark.asyncio
async def test_final_reply_is_persisted_before_finished_and_history_contains_answers(monkeypatch):
    repository = MemoryStudioRepository()
    await repository.set_system_prompt(1, "Use a warm editorial voice.")
    seen = []
    class Adapter:
        build_run_input = staticmethod(AGUIAdapter.build_run_input)
        def __init__(self, **kwargs): pass
        async def run_stream(self, **kwargs):
            seen.append(kwargs)
            yield RunStartedEvent(thread_id=kwargs["conversation_id"], run_id=kwargs["run_id"])
            yield TextMessageStartEvent(message_id="interim")
            yield TextMessageContentEvent(message_id="interim", delta="I am thinking about the old question")
            yield TextMessageEndEvent(message_id="interim")
            await kwargs["on_complete"](SimpleNamespace(output="The final answer", usage=None, response=None))
            yield RunFinishedEvent(thread_id=kwargs["conversation_id"], run_id=kwargs["run_id"])
        def encode_stream(self, stream): return stream
    monkeypatch.setattr("app.studio.service.AGUIAdapter", Adapter)
    service = StudioService(repository, Settings(studio_test_mode=True))
    for request in ["Continue", "Prepare a draft"]:
        conversation = await repository.create_conversation(channel_id=1)
        await repository.append_message(conversation_id=conversation["id"], role="user", content="Old question")
        await repository.append_message(conversation_id=conversation["id"], role="assistant", content="Already answered")
        payload = {"threadId": str(conversation["id"]), "runId": str(uuid.uuid4()), "messages": [{"id": "user", "role": "user", "content": request}], "tools": [], "context": [], "forwardedProps": {}}
        response = await service.stream_request(None, json.dumps(payload).encode())
        events = []
        async for event in response.body_iterator:
            events.append(event)
            if event.type == "RUN_FINISHED":
                assert (await repository.list_messages(conversation["id"]))[-1]["content"] == "The final answer"
        assert [event.delta for event in events if event.type == "TEXT_MESSAGE_CONTENT"] == ["The final answer"]
        assert "Use a warm editorial voice." in seen[-1]["instructions"]
        assert any(isinstance(item, ModelResponse) and item.parts[0].content == "Already answered" for item in seen[-1]["message_history"])




@pytest.mark.asyncio
async def test_existing_artifact_does_not_force_a_tool_from_keywords(monkeypatch):
    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    await repository.create_draft(
        conversation_id=conversation["id"],
        channel_id=1,
        payload={"body": "Existing owner artifact.", "creative": True},
    )
    seen = []

    class Adapter:
        build_run_input = staticmethod(AGUIAdapter.build_run_input)

        def __init__(self, **kwargs):
            pass

        async def run_stream(self, **kwargs):
            seen.append(kwargs["instructions"])
            yield RunStartedEvent(thread_id=kwargs["conversation_id"], run_id=kwargs["run_id"])
            await kwargs["on_complete"](SimpleNamespace(output="Revision handled.", usage=None, response=None))
            yield RunFinishedEvent(thread_id=kwargs["conversation_id"], run_id=kwargs["run_id"])

        def encode_stream(self, stream):
            return stream

    monkeypatch.setattr("app.studio.service.AGUIAdapter", Adapter)
    service = StudioService(repository, Settings(studio_test_mode=True))
    payload = {
        "threadId": str(conversation["id"]),
        "runId": str(uuid.uuid4()),
        "messages": [{"id": "user", "role": "user", "content": "Сделай драфт поста по первой"}],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    response = await service.stream_request(None, json.dumps(payload).encode())
    async for _ in response.body_iterator:
        pass

    assert len(seen) == 1
    assert "Server evidence contract" not in seen[0]
    assert "<conversation_evidence_state>" in seen[0]


@pytest.mark.asyncio
async def test_system_prompt_conflict_preserves_the_saved_prompt(client, settings, app, channel_id):
    settings.studio_test_mode = True
    await client.post('/login', data={'username': settings.admin_username, 'password': settings.admin_password})
    page = await client.get('/studio')
    token = re.search(r'<meta name="studio-csrf-token" content="([^"]+)"', page.text).group(1)
    headers = {'x-csrf-token': token}
    first = await client.patch('/studio/api/settings', headers=headers, json={
        'channel_id': channel_id, 'system_prompt': 'First editor', 'expected_prompt': ''})
    assert first.status_code == 200
    stale = await client.patch('/studio/api/settings', headers=headers, json={
        'channel_id': channel_id, 'system_prompt': 'Stale editor', 'expected_prompt': ''})
    assert stale.status_code == 409
    assert stale.json()['error']['code'] == 'system_prompt_conflict'
    assert await app.state.studio_repository.get_system_prompt(channel_id) == 'First editor'
