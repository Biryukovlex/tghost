from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import uuid

import pytest
from pydantic_ai import UsageLimitExceeded, UsageLimits
from pydantic_ai.messages import InstructionPart, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models import ModelRequestContext, ModelRequestParameters
from pydantic_ai.models.function import FunctionModel, DeltaToolCall
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RequestUsage, RunUsage

from app import limits
from app.config import Settings
from app.studio.agent import StudioDeps, build_agent
from app.studio.budget import StudioInputBudget, estimate_request_tokens
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


def _budget_request(messages, **params):
    return ModelRequestContext(model=TestModel(), messages=messages, model_settings=None,
                               model_request_parameters=ModelRequestParameters(**params))


def test_request_estimate_excludes_replayed_instructions_and_execution_metadata():
    messages = [ModelRequest(parts=[UserPromptPart("Inspect")])]
    for n in range(6):
        messages += [ModelResponse(parts=[ToolCallPart("search_web", {"query": "Evidence"}, tool_call_id=str(n))]),
                     ModelRequest(parts=[ToolReturnPart("search_web", {"sources": []}, tool_call_id=str(n))])]
    params = {"instruction_parts": [InstructionPart(content="Current authoritative instructions")]}
    baseline = estimate_request_tokens(_budget_request(deepcopy(messages), **params))
    for message in messages:
        if isinstance(message, ModelRequest):
            message.instructions = "Obsolete archived instructions " * 10000
        else:
            message.provider_details = {"private_payload": "Provider bookkeeping " * 10000}
            message.usage = RequestUsage(input_tokens=40000, output_tokens=1000)
    assert estimate_request_tokens(_budget_request(messages, **params)) == baseline
    params["instruction_parts"] = [InstructionPart(content="Larger current instructions " * 1000)]
    assert estimate_request_tokens(_budget_request(messages, **params)) > baseline + 1000
    messages[-1].parts[0].content = {"text": "Read evidence " * 1000}
    assert estimate_request_tokens(_budget_request(messages, **params)) > baseline + 2000


@pytest.mark.asyncio
async def test_latest_oversized_research_can_yield_to_archive_without_mutation(monkeypatch):
    payload = {"sources": [{"source_id": "read-1", "url": "https://fixture.test/read", "has_read_content": True}],
               "documents": [{"source_id": "read-1", "text": "Archived article body " * 10000}]}
    original = deepcopy(payload)
    request = _budget_request([ModelResponse(parts=[ToolCallPart("read_sources", {}, tool_call_id="r")]),
                               ModelRequest(parts=[ToolReturnPart("read_sources", payload, tool_call_id="r")])])
    monkeypatch.setattr(limits, "MAX_INPUT_TOKENS", 4000)
    result = await StudioInputBudget().before_model_request(SimpleNamespace(usage=RunUsage()), request)
    compacted = result.messages[-1].parts[0].content
    assert compacted["status"] == "compacted" and compacted["selected_source_ids"] == ["read-1"]
    assert compacted["sources"][0]["has_read_content"] is True
    assert payload == original
    assert estimate_request_tokens(result) <= 4000


@pytest.mark.asyncio
async def test_real_agent_reserves_a_final_answer_turn_before_cumulative_exhaustion(monkeypatch):
    monkeypatch.setattr(limits, "MAX_RUN_INPUT_TOKENS", 20000)
    snapshots, calls = [], []
    async def observe(snapshot):
        snapshots.append(snapshot)
    def respond(messages, info):
        calls.append(info)
        if len(calls) == 1:
            assert info.function_tools
            return ModelResponse(parts=[ToolCallPart("get_channel_context", {})],
                                 usage=RequestUsage(input_tokens=13000, output_tokens=10))
        # Finalization hides information tools but keeps the tools that save the
        # requested artifact, so a finishing turn can still deliver a draft.
        assert info.function_tools and all((tool.metadata or {}).get("deliverable") for tool in info.function_tools)
        assert {tool.name for tool in info.function_tools} <= {"create_draft", "get_draft", "edit_draft", "write_post", "revise_draft", "find_stories"}
        assert info.model_settings["tool_choice"] == "auto"
        assert "final response" in info.instructions
        assert "never claim unread sources were read" in info.instructions
        assert any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts)
        return ModelResponse(parts=[TextPart("Collected evidence is limited; here is what it supports.")],
                             usage=RequestUsage(input_tokens=4000, output_tokens=20))
    deps = StudioDeps(repository=MemoryStudioRepository(), workspace_id="w", conversation_id="c", channel_id=1,
                      cancel_event=asyncio.Event(), on_input_budget=observe)
    result = await build_agent(Settings(studio_test_mode=True), model=FunctionModel(respond)).run(
        "Continue the investigation", deps=deps,
        usage_limits=UsageLimits(input_tokens_limit=20000, per_request_input_tokens_limit=48000, tool_calls_limit=12))
    assert result.output.startswith("Collected evidence")
    assert result.usage.requests == 2 and result.usage.tool_calls == 1
    assert result.usage.input_tokens == 17000
    assert [s["finalizing"] for s in snapshots] == [False, True]
    assert all(s["estimated_input_tokens"] <= s["input_limit_tokens"] for s in snapshots)


@pytest.mark.asyncio
async def test_exhausted_tool_allowance_gets_a_final_turn_and_hard_input_failure_is_observable(monkeypatch):
    request = _budget_request([ModelRequest(parts=[UserPromptPart("Summarize")])])
    result = await StudioInputBudget().before_model_request(SimpleNamespace(usage=RunUsage(tool_calls=limits.MAX_TOOL_CALLS)), request)
    assert result.model_settings["tool_choice"] == "none"
    snapshots = []
    async def observe(snapshot):
        snapshots.append(snapshot)
    monkeypatch.setattr(limits, "MAX_INPUT_TOKENS", 100)
    with pytest.raises(UsageLimitExceeded):
        await StudioInputBudget().before_model_request(
            SimpleNamespace(usage=RunUsage(), deps=SimpleNamespace(on_input_budget=observe)),
            _budget_request([ModelRequest(parts=[UserPromptPart("A large owner request " * 5000)])]))
    assert snapshots[0]["status"] == "rejected"
    assert snapshots[0]["estimated_input_tokens"] > snapshots[0]["input_limit_tokens"]
    assert not any(isinstance(v, str) and "owner request" in v for v in snapshots[0].values())


@pytest.mark.asyncio
async def test_finalization_is_enforced_before_tool_effects_and_is_run_local(monkeypatch):
    monkeypatch.setattr(limits, "MAX_RUN_INPUT_TOKENS", 20000)
    class CountingRepository(MemoryStudioRepository):
        calls = 0
        async def channel_context(self, channel_id):
            self.calls += 1
            return await super().channel_context(channel_id)
    repository = CountingRepository()
    turns = []
    def ignores_finalization(messages, info):
        turns.append(info)
        return ModelResponse(parts=[ToolCallPart("get_channel_context", {})],
                             usage=RequestUsage(input_tokens=13000 if len(turns) == 1 else 1000, output_tokens=10))
    agent = build_agent(Settings(studio_test_mode=True), model=FunctionModel(ignores_finalization))
    def deps():
        return StudioDeps(repository=repository, workspace_id="w", conversation_id="c", channel_id=1,
                          cancel_event=asyncio.Event())
    with pytest.raises(UsageLimitExceeded):
        await agent.run("Inspect", deps=deps())
    assert repository.calls == 1  # no execution when the model ignores tool_choice=none
    turns.clear()
    with pytest.raises(UsageLimitExceeded):
        await agent.run("Independent new run", deps=deps())
    assert repository.calls == 2  # previous run's finalization state did not leak


def test_instruction_fallback_and_explicit_empty_instructions():
    request = _budget_request([ModelRequest(parts=[UserPromptPart("Inspect")], instructions="Owner instructions " * 1000)])
    fallback = estimate_request_tokens(request)
    request.model_request_parameters.instruction_parts = [InstructionPart(content="Owner instructions " * 1000)]
    assert estimate_request_tokens(request) == fallback
    request.model_request_parameters.instruction_parts = []
    assert estimate_request_tokens(request) < fallback - 1000
    request.model_request_parameters.instruction_parts = None
    request.messages.append(ModelRequest(parts=[ToolReturnPart("search_web", {"sources": []}, tool_call_id="a")]))
    continuation = estimate_request_tokens(request)
    request.messages[-1] = ModelRequest(parts=[UserPromptPart("New instruction-free request")])
    assert estimate_request_tokens(request) < continuation - 1000


@pytest.mark.asyncio
async def test_source_catalog_can_be_discovered_and_paged_without_known_ids():
    from app.studio.sources import SourceDocument
    class Reader:
        async def read(self, url):
            return SourceDocument(url=url, canonical_url=url, final_url=url, title="Synthetic article", text="Read synthetic evidence.")
    service = ResearchService(Settings(studio_test_mode=True), reader=Reader())
    scope = dict(workspace_id="w", conversation_id="c", channel_id=1)
    await service.read_sources(**scope, urls=[f"https://fixture.test/{n}" for n in range(3)])
    first = await service.source_page(**scope, source_ids=[], limit=2, include_content=True)
    assert len(first["sources"]) == 2 and first["next_offset"] == 2
    assert first["catalog_source_count"] == first["catalog_read_source_count"] == 3
    assert all(s["has_read_content"] and s["content"] for s in first["sources"])
    last = await service.source_page(**scope, source_ids=[], offset=2, limit=2)
    assert len(last["sources"]) == 1 and last["next_offset"] is None
    assert {s["source_id"] for s in first["sources"]}.isdisjoint(s["source_id"] for s in last["sources"])
    unknown = await service.source_page(**scope, source_ids=["unknown"])
    assert unknown["unknown_source_ids"] == ["unknown"]
    assert "source_ids=[]" in unknown["continuation"]
    for other in [dict(workspace_id="other", conversation_id="c", channel_id=1), dict(workspace_id="w", conversation_id="other", channel_id=1)]:
        assert (await service.source_page(**other, source_ids=[]))["sources"] == []
    deps = StudioDeps(repository=MemoryStudioRepository(), **scope, research=service, cancel_event=asyncio.Event())
    model = TestModel(call_tools=["get_research_sources"], custom_output_text="Three saved sources were read.")
    result = await build_agent(Settings(studio_test_mode=True), model=model).run("Прочитал источники?", deps=deps)
    assert result.output == "Three saved sources were read."
    assert deps.completed_tools == {"get_research_sources"}


@pytest.mark.asyncio
async def test_read_status_recovers_from_unknown_ids_before_accepting_an_answer():
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel
    from app.studio.sources import SourceDocument
    class Reader:
        async def read(self, url):
            return SourceDocument(url=url, canonical_url=url, final_url=url, title="Synthetic article", text="Synthetic saved text.")
    research = ResearchService(Settings(studio_test_mode=True), reader=Reader())
    scope = dict(workspace_id="w", conversation_id="c", channel_id=1)
    await research.read_sources(**scope, urls=["https://fixture.test/read"])
    calls = []
    def model(messages, info):
        calls.append(messages)
        if len(calls) == 1:
            return ModelResponse(parts=[ToolCallPart("get_research_sources", {"source_ids": ["unknown"]})])
        if len(calls) == 2:
            results = [part.content for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
            assert "source_ids=[]" in results[-1]["continuation"]
            return ModelResponse(parts=[ToolCallPart("get_research_sources", {"source_ids": [], "include_content": True})])
        return ModelResponse(parts=[TextPart("One saved article has been read.")])
    deps = StudioDeps(repository=MemoryStudioRepository(), **scope, research=research, cancel_event=asyncio.Event())
    result = await build_agent(Settings(studio_test_mode=True), model=FunctionModel(model)).run("Прочитал источники?", deps=deps)
    assert result.output == "One saved article has been read."
    assert len(calls) == 3
    assert deps.completed_tools == {"get_research_sources"}


@pytest.mark.asyncio
async def test_pipeline_input_has_its_own_allowance_outside_the_chat_budget():
    request = _budget_request([ModelRequest(parts=[UserPromptPart("Summarize")])])
    snapshots = []

    async def observe(snapshot):
        snapshots.append(snapshot)
    # 190k used in total, 150k of it inside a pipeline job: the chat agent has spent 40k of its own budget.
    deps = SimpleNamespace(on_input_budget=observe, pipeline_input_tokens=150_000)
    await StudioInputBudget().before_model_request(SimpleNamespace(usage=RunUsage(input_tokens=190_000), deps=deps), request)
    assert snapshots[-1]["remaining_input_tokens"] == limits.MAX_RUN_INPUT_TOKENS - 40_000
    assert snapshots[-1]["finalizing"] is False
