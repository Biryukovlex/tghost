from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import uuid

import pytest
from pydantic_ai import UsageLimitExceeded
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models import ModelRequestContext, ModelRequestParameters
from pydantic_ai.models.function import FunctionModel, DeltaToolCall
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from app import limits
from app.config import Settings
from app.studio.agent import StudioDeps, build_agent
from app.studio.budget import StudioInputBudget
from app.studio.observability import safe_error
from app.studio.repository import MemoryStudioRepository
from app.studio.research import ResearchService, _agent_result
from app.studio.search import SearchQuery, SearchResponse, SearchResult
from app.studio.service import StudioService


def test_repeated_channel_evidence_is_losslessly_shared_and_bounded():
    evidence = [{"post_id": n, "excerpt": "Channel evidence " * 35} for n in range(20)]
    original = {"stories": [{"cluster_id": str(n), "channel_evidence_ids": list(range(20)), "channel_evidence": evidence} for n in range(8)], "sources": []}
    result = _agent_result(deepcopy(original))
    assert len(json.dumps(result)) < len(json.dumps(original)) / 4
    assert len(result["channel_evidence_sets"]) == 1
    for before, after in zip(original["stories"], result["stories"]):
        restored = dict(after)
        restored["channel_evidence"] = result["channel_evidence_sets"][restored.pop("channel_evidence_ref")]
        assert restored == before


class BatchProvider:
    async def search(self, query, **kwargs):
        now = datetime.now(timezone.utc)
        return SearchResponse(query=SearchQuery(query), provider="fixture", results=tuple(
            SearchResult(url=f"https://fixture.test/{n}", canonical_url=f"https://fixture.test/{n}", title=f"Independent source {n}", snippet="Useful findings " * 120, source_name="fixture", domain="fixture.test", published_at=now, provider="fixture", query=query, fetched_at=now, source_id=f"fixture-{n}", result_index=n)
            for n in range(120)
        ))


@pytest.mark.asyncio
async def test_overflow_source_ids_remain_accessible_only_in_the_original_scope():
    service = ResearchService(Settings(studio_test_mode=True), provider=BatchProvider())
    scope = dict(workspace_id="w", conversation_id="c", channel_id=1)
    result = await service.search(**scope, query="research")
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) <= limits.TOOL_RESULT_MAX_CHARS
    omitted = result["omitted_source_ids"]
    assert omitted
    seen = {s["source_id"] for s in result["sources"]}
    for offset in range(0, len(omitted), 12):
        page = await service.source_page(**scope, source_ids=omitted, offset=offset)
        seen.update(s["source_id"] for s in page["sources"])
    assert len(seen) == 120
    foreign = await service.source_page(workspace_id="other", conversation_id="c", channel_id=1, source_ids=omitted)
    assert foreign["sources"] == []
    foreign_chat = await service.source_page(workspace_id="w", conversation_id="other", channel_id=1, source_ids=omitted)
    assert foreign_chat["sources"] == []


@pytest.mark.asyncio
async def test_context_budget_rejects_before_model_execution(monkeypatch):
    monkeypatch.setattr(limits, "MAX_INPUT_TOKENS", 100)
    calls = []
    def respond(messages, info):
        calls.append(messages)
        return ModelResponse(parts=[])
    agent = build_agent(Settings(studio_test_mode=True), model=FunctionModel(respond))
    deps = StudioDeps(repository=MemoryStudioRepository(), workspace_id="w", conversation_id="c", channel_id=1, cancel_event=asyncio.Event())
    with pytest.raises(UsageLimitExceeded):
        await agent.run("A large task " * 1000, deps=deps)
    assert calls == []
    assert safe_error(UsageLimitExceeded("private prompt"))[0] == "run_budget_exceeded"
    assert safe_error(UsageLimitExceeded("private prompt"))[2] is False


@pytest.mark.asyncio
async def test_compaction_keeps_tool_pairs_and_original_results(monkeypatch):
    payload = {"sources": [{"source_id": "s1", "url": "https://fixture.test/a", "title": "Evidence", "excerpt": "Detailed result " * 9000}], "selected_source_ids": ["s1"]}
    part = ToolReturnPart("search_web", deepcopy(payload), tool_call_id="a")
    messages = [ModelRequest(parts=[UserPromptPart("Research")]), ModelResponse(parts=[ToolCallPart("search_web", {}, tool_call_id="a")]), ModelRequest(parts=[part]), ModelResponse(parts=[ToolCallPart("read_sources", {}, tool_call_id="b")]), ModelRequest(parts=[ToolReturnPart("read_sources", {"sources": [], "documents": [{"text": "Latest source text"}]}, tool_call_id="b")])]
    request = ModelRequestContext(model=TestModel(), messages=messages, model_settings=None, model_request_parameters=ModelRequestParameters())
    monkeypatch.setattr(limits, "MAX_INPUT_TOKENS", 4000)
    result = await StudioInputBudget().before_model_request(SimpleNamespace(usage=RunUsage()), request)
    assert part.content == payload
    compacted = result.messages[2].parts[0]
    assert compacted.content["status"] == "compacted"
    assert compacted.content["selected_source_ids"] == ["s1"]
    assert result.messages[-1].parts[0].content["documents"][0]["text"] == "Latest source text"
    assert [p.tool_call_id for m in result.messages for p in m.parts if isinstance(p, ToolCallPart)] == ["a", "b"]
    assert [p.tool_call_id for m in result.messages for p in m.parts if isinstance(p, ToolReturnPart)] == ["a", "b"]


def _request(conversation, run_id):
    return json.dumps({"threadId": str(conversation["id"]), "runId": str(run_id), "messages": [{"id": "user", "role": "user", "content": "Inspect this channel"}], "tools": [], "context": [], "forwardedProps": {}}).encode()


@pytest.mark.asyncio
async def test_failed_second_request_preserves_first_response_usage():
    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    requests = 0
    async def respond(messages, info):
        nonlocal requests
        requests += 1
        if requests > 1:
            raise RuntimeError("synthetic upstream failure")
        yield {0: DeltaToolCall(name="get_channel_context", json_args="{}")}
    model = FunctionModel(stream_function=respond)
    service = StudioService(repository, Settings(studio_test_mode=True), agent_factory=lambda settings: build_agent(settings, model=model))
    run_id = uuid.uuid4()
    response = await service.stream_request(None, _request(conversation, run_id))
    async for _ in response.body_iterator:
        pass
    run = await repository.get_run(run_id)
    assert run["status"] == "failed"
    assert run["usage"]["input_tokens"] > 0
    assert run["usage"]["output_tokens"] > 0


@pytest.mark.asyncio
async def test_cancellation_during_tool_preserves_usage_and_progress():
    entered = asyncio.Event()
    class WaitingRepository(MemoryStudioRepository):
        async def channel_context(self, channel_id):
            entered.set()
            await asyncio.Event().wait()
    repository = WaitingRepository()
    conversation = await repository.create_conversation(channel_id=1)
    async def respond(messages, info):
        yield {0: DeltaToolCall(name="get_channel_context", json_args="{}")}
    service = StudioService(repository, Settings(studio_test_mode=True), agent_factory=lambda settings: build_agent(settings, model=FunctionModel(stream_function=respond)))
    run_id = uuid.uuid4()
    response = await service.stream_request(None, _request(conversation, run_id))
    await asyncio.wait_for(entered.wait(), 2)
    progress = await repository.get_run(run_id)
    assert progress["usage"]["input_tokens"] > 0
    await service.cancel(run_id)
    async for _ in response.body_iterator:
        pass
    run = await repository.get_run(run_id)
    assert run["status"] == "cancelled"
    assert run["usage"]["input_tokens"] > 0
    assert run["usage"]["output_tokens"] > 0


def test_large_read_results_keep_the_read_text_and_deduplicate_channel_evidence():
    evidence = [{"post_id": n, "excerpt": "Relevant post evidence " * 25} for n in range(20)]
    payload = {
        "documents": [{"source_id": str(n), "text": "A read paragraph " * 180} for n in range(6)],
        "stories": [{"cluster_id": str(n), "summary": "Repeated article excerpt " * 80, "channel_evidence": evidence} for n in range(6)],
        "sources": [{"source_id": str(n), "excerpt": "Repeated excerpt " * 80} for n in range(6)],
        "selected_source_ids": [str(n) for n in range(6)],
    }
    documents = deepcopy(payload["documents"])
    result = _agent_result(payload)
    assert result["documents"] == documents
    assert len(result["channel_evidence_sets"]) == 1
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) <= limits.TOOL_RESULT_MAX_CHARS


@pytest.mark.asyncio
async def test_cumulative_budget_and_cross_message_evidence_deduplication(monkeypatch):
    evidence = [{"post_id": 1, "excerpt": "Scoped source"}]
    payload = {"stories": [{"channel_evidence": evidence, "channel_evidence_ids": [1]}], "sources": []}
    first, second = _agent_result(deepcopy(payload)), _agent_result(deepcopy(payload))
    messages = [ModelRequest(parts=[ToolReturnPart("search_web", first, tool_call_id="a"), ToolReturnPart("search_web", second, tool_call_id="b")])]
    request = ModelRequestContext(model=TestModel(), messages=messages, model_settings=None, model_request_parameters=ModelRequestParameters())
    result = await StudioInputBudget().before_model_request(SimpleNamespace(usage=RunUsage()), request)
    first_part, second_part = result.messages[0].parts
    ref = second_part.content["stories"][0]["channel_evidence_ref"]
    assert second_part.content["channel_evidence_sets"] == {}
    assert first_part.content["channel_evidence_sets"][ref] == evidence
    assert second["channel_evidence_sets"]  # the original archive was not mutated
    monkeypatch.setattr(limits, "MAX_RUN_INPUT_TOKENS", 100)
    with pytest.raises(UsageLimitExceeded):
        await StudioInputBudget().before_model_request(SimpleNamespace(usage=RunUsage(input_tokens=99)), request)
