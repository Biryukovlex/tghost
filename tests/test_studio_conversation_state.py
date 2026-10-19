"""The model receives scoped evidence state without a lexical workflow router."""
import json
import uuid
from datetime import datetime, timezone

import pytest
from pydantic_ai.models.function import FunctionModel

from app.config import Settings
from app.studio.agent import build_agent
from app.studio.repository import MemoryStudioRepository, StudioRepository
from app.studio.service import StudioService, _model_history


async def seed_research(repo, conversation, channel_id):
    now = datetime.now(timezone.utc)
    sources = [
        {"source_id": "read", "canonical_url": "https://fixture.test/read", "accessible": True, "status": "ok", "content": "PRIVATE_ARTICLE_BODY", "retrieved_at": now},
        {"source_id": "unread", "canonical_url": "https://fixture.test/search", "accessible": True, "status": "ok", "content": "", "retrieved_at": now},
        {"source_id": "failed", "canonical_url": "https://fixture.test/fail", "accessible": False, "status": "inaccessible", "content": "", "retrieved_at": now},
    ]
    await repo.persist_research_bundle(conversation_id=conversation["id"], channel_id=channel_id,
        bundle={"query": "PRIVATE_QUERY", "sources": sources, "stories": [], "retrieved_at": now})


@pytest.mark.asyncio
async def test_memory_summary_distinguishes_read_unread_failed_empty_and_wrong_scope():
    repo = MemoryStudioRepository()
    conversation = await repo.create_conversation(channel_id=1)
    await seed_research(repo, conversation, 1)
    summary = await repo.get_research_summary(conversation_id=conversation["id"], channel_id=1)
    assert (summary["source_count"], summary["read_count"], summary["failed_count"]) == (3, 1, 1)
    assert "PRIVATE" not in json.dumps(summary, default=str)
    assert "https://" not in json.dumps(summary, default=str)
    assert await repo.get_research_summary(conversation_id=conversation["id"], channel_id=2) is None
    empty = await repo.create_conversation(channel_id=1)
    assert (await repo.get_research_summary(conversation_id=empty["id"], channel_id=1))["source_count"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_summary_is_aggregate_and_workspace_conversation_channel_scoped(app, channel_id):
    repo = StudioRepository(app.state.db)
    conversation = await repo.create_conversation(channel_id=channel_id)
    await seed_research(repo, conversation, channel_id)
    result = await repo.get_research_summary(conversation_id=conversation["id"], channel_id=channel_id)
    assert (result["source_count"], result["read_count"], result["failed_count"]) == (3, 1, 1)
    assert await repo.get_research_summary(conversation_id=uuid.uuid4(), channel_id=channel_id) is None
    assert await repo.get_research_summary(conversation_id=conversation["id"], channel_id=channel_id+1) is None
    original = app.state.db.workspace_id
    try:
        app.state.db.workspace_id = uuid.uuid4()
        assert await repo.get_research_summary(conversation_id=conversation["id"], channel_id=channel_id) is None
    finally:
        app.state.db.workspace_id = original
    assert "PRIVATE" not in json.dumps(result, default=str)


def test_failed_execution_notices_are_not_replayed_as_completed_answers():
    rows = [{"role": "user", "content": "Earlier request"}, {"role": "assistant", "content": "Completed reply"}]
    rows += [{"role": "assistant", "content": "FAILED_RUN_NOTICE", "metadata_json": {"run_failed": True}} for _ in range(45)]
    text = repr(_model_history(rows))
    assert "Completed reply" in text and "Earlier request" in text
    assert "FAILED_RUN_NOTICE" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["Прочитал источники?", "Найди ещё новости", "¿Qué material tenemos?", "Explain the saved draft", "続けて"])
async def test_each_request_gets_current_evidence_state_without_forced_tools(message):
    repo = MemoryStudioRepository()
    conversation = await repo.create_conversation(channel_id=1)
    await seed_research(repo, conversation, 1)
    captured = []
    async def respond(messages, info):
        captured.append(info)
        assert info.model_settings["tool_choice"] == "auto"
        instructions = info.instructions or ""
        state = json.loads(instructions.split("<conversation_evidence_state>",1)[1].split("</conversation_evidence_state>",1)[0])
        assert state["research"]["source_count"] == 3
        assert state["research"]["read_count"] == 1
        assert "PRIVATE_ARTICLE_BODY" not in instructions and "PRIVATE_QUERY" not in instructions
        yield "The saved state is available."
    service = StudioService(repo, Settings(studio_test_mode=True), agent_factory=lambda settings: build_agent(settings, model=FunctionModel(stream_function=respond)))
    payload = {"threadId": str(conversation["id"]), "runId": str(uuid.uuid4()), "messages": [{"id": str(uuid.uuid4()), "role": "user", "content": message}], "tools": [], "context": [], "forwardedProps": {}}
    response = await service.stream_request(None, json.dumps(payload).encode())
    async for _ in response.body_iterator:
        pass
    run = await repo.get_run(uuid.UUID(payload["runId"]))
    assert run["status"] == "succeeded"
    assert len(captured) == 1 and run["usage"]["tool_calls"] == 0
    assert not any(e["event_type"] == "TOOL_CALL_START" for e in repo.events[uuid.UUID(payload["runId"])])


@pytest.mark.asyncio
async def test_summary_failure_stays_unknown_without_leaking_the_exception():
    from app.studio.service import _conversation_evidence_state
    class Broken:
        async def get_research_summary(self, **scope):
            raise RuntimeError("PRIVATE_DATABASE_SECRET")
    result = await _conversation_evidence_state(Broken(), conversation_id=uuid.uuid4(), channel_id=1)
    assert result["research"]["status"] == "unavailable"
    assert "source_count" not in result["research"]
    assert "PRIVATE" not in json.dumps(result)
