"""Compact web research for the chat agent: ranked search results, ranked page
passages, per-run caps, a finishing turn that always answers, and the
find_stories job. Synthetic data and a word-overlap fake embedder only."""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace

import numpy as np
import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from app import limits
from app.config import Settings
from app.studio import embeddings
from app.studio.agent import StudioDeps, build_agent
from app.studio.web_results import compact_search, ranked_passages, split_passages
from tests.test_studio_pipeline import StubResearch, scripted
from tests.test_studio_rebuild import PostsRepository


class WordEmbedder:
    """Hashed bag-of-words vectors: texts sharing words are similar."""

    def encode(self, texts):
        out = np.zeros((len(texts), 256), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in re.findall(r"\w+", str(text).lower()):
                out[row, hash(word) % 256] += 1.0
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.clip(norms, 1e-9, None)


@pytest.fixture
def fake_embedder():
    previous = (embeddings._embedder, embeddings._loaded)
    embeddings.set_embedder(WordEmbedder())
    yield
    embeddings._embedder, embeddings._loaded = previous


@pytest.fixture
def no_embedder():
    previous = (embeddings._embedder, embeddings._loaded)
    embeddings.set_embedder(None)
    yield
    embeddings._embedder, embeddings._loaded = previous


def research_payload(count=30):
    sources = [{"source_id": f"s{i}", "url": f"https://news.example.test/{i}", "title": f"Unrelated story number {i}",
                "excerpt": "Weather and sports. " * 40, "domain": "news.example.test", "published_at": "2026-10-19T10:00:00Z"}
               for i in range(count)]
    sources[17]["title"] = "Microsoft Maia chips power AI inference"
    sources[17]["excerpt"] = "Microsoft will run AI inference mostly on its own Maia chips."
    return {"sources": sources, "stories": [{"headline": "x", "channel_evidence": [{"post": "y" * 900}] * 20}],
            "channel_evidence_sets": {"a": ["z" * 18_000]}, "search_outcome": "healthy", "warnings": ["w1", "w2", "w3", "w4"]}


@pytest.mark.asyncio
async def test_search_returns_a_short_list_ranked_by_meaning(fake_embedder):
    out = await compact_search(research_payload(), ["Microsoft Maia chips inference"])
    assert len(out["results"]) == limits.SEARCH_RESULTS_SHOWN
    assert out["results"][0]["source_id"] == "s17"
    assert all(len(item["snippet"]) <= limits.SEARCH_SNIPPET_CHARS for item in out["results"])
    assert out["more_results_found"] == 20 and len(out["warnings"]) == 3
    assert "channel_evidence_sets" not in out and "stories" not in out
    assert len(json.dumps(out, ensure_ascii=False)) < 5_000


@pytest.mark.asyncio
async def test_search_keeps_provider_order_without_a_model(no_embedder):
    out = await compact_search(research_payload(), ["Microsoft Maia chips inference"])
    assert [item["source_id"] for item in out["results"][:2]] == ["s0", "s1"]


PAGE = "\n".join(
    [f"Paragraph {i} talks about football results and the weather forecast for the weekend in detail." * 3 for i in range(10)]
    + ["The company said its new model costs 70 percent less per agent task than the previous version." * 3]
    + [f"Closing paragraph {i} about unrelated travel tips and restaurant reviews in the city." * 3 for i in range(5)])


@pytest.mark.asyncio
async def test_page_reads_return_the_most_relevant_passages_within_budget(fake_embedder):
    out = await ranked_passages(PAGE, "model costs per agent task", budget=800)
    assert out["ranked_by"] == "relevance to the focus"
    assert any("70 percent" in passage["text"] for passage in out["passages"])
    assert sum(len(passage["text"]) for passage in out["passages"]) <= 800 + 700
    assert out["passages_total"] == len(split_passages(PAGE)) and out["next_passage_offset"] is not None
    more = await ranked_passages(PAGE, "model costs per agent task", budget=800, offset=out["next_passage_offset"])
    assert not {p["n"] for p in out["passages"]} & {p["n"] for p in more["passages"]}


@pytest.mark.asyncio
async def test_page_reads_fall_back_to_page_order(no_embedder):
    out = await ranked_passages(PAGE, "model costs per agent task", budget=800)
    assert out["ranked_by"] == "page order" and out["passages"][0]["n"] == 1


class ReadingResearch(StubResearch):
    """Stub research service that also serves stored page text."""

    async def read_sources(self, **kwargs):
        urls = list(kwargs["urls"])
        self.reads.append(urls)
        return {"documents": [{"source_id": f"id-{n}", "url": url, "status": "ok", "title": "Page", "text": "short"}
                              for n, url in enumerate(urls)]}

    async def get_source(self, **kwargs):
        return SimpleNamespace(content=PAGE, title="Page")

    async def search(self, **kwargs):
        self.searches.append(kwargs)
        return research_payload()


async def deps_with(research, message="What do agents cost now?"):
    repository = PostsRepository()
    chat = await repository.create_conversation(channel_id=1)
    return StudioDeps(repository=repository, workspace_id=repository.workspace_id, conversation_id=chat["id"], channel_id=1,
                      cancel_event=asyncio.Event(), research=research, owner_messages=(message,))


@pytest.mark.asyncio
async def test_read_tool_reads_at_most_three_pages_and_returns_passages(fake_embedder):
    research = ReadingResearch()
    deps = await deps_with(research)
    tools = build_agent(Settings(studio_test_mode=True))._function_toolset.tools
    out = await tools["read_sources"].function(SimpleNamespace(deps=deps), urls=[f"https://x.test/{i}" for i in range(6)],
                                               focus="cost per agent task")
    assert research.reads == [[f"https://x.test/{i}" for i in range(3)]]
    assert len(out["pages"]) == 3 and all(page["passages"] for page in out["pages"])
    assert len(json.dumps(out, ensure_ascii=False)) < 3 * (limits.READ_CHARS_PER_PAGE + 1_500)
    assert deps.tool_call_counts["read_sources"] == 1


@pytest.mark.asyncio
async def test_search_and_read_disappear_after_their_caps(fake_embedder):
    research = ReadingResearch()
    deps = await deps_with(research)
    offered = []

    def model(messages, info):
        offered.append({tool.name for tool in info.function_tools})
        step = len(offered)
        if step <= 3:
            return ModelResponse(parts=[ToolCallPart("search_web", {"query": f"agent costs {step}"})])
        if step <= 5:
            return ModelResponse(parts=[ToolCallPart("read_sources", {"urls": ["https://x.test/1"], "focus": "costs"})])
        return ModelResponse(parts=[TextPart("Answer from the evidence.")])

    result = await build_agent(Settings(studio_test_mode=True), model=FunctionModel(model)).run("What do agents cost?", deps=deps)
    assert result.output == "Answer from the evidence."
    assert "search_web" in offered[2] and "search_web" not in offered[3]
    assert "read_sources" in offered[4] and "read_sources" not in offered[5]
    assert len(research.searches) == limits.CHAT_MAX_SEARCHES and len(research.reads) == limits.CHAT_MAX_READS


@pytest.mark.asyncio
async def test_finishing_turn_answers_instead_of_failing_on_a_hidden_tool(monkeypatch, fake_embedder):
    monkeypatch.setattr(limits, "MAX_RUN_INPUT_TOKENS", 20_000)
    research = ReadingResearch()
    deps = await deps_with(research)
    replies = []

    def model(messages, info):
        replies.append(info)
        if len(replies) == 1:
            return ModelResponse(parts=[ToolCallPart("get_channel_context", {})], usage=RequestUsage(input_tokens=9_000))
        if len(replies) == 2:
            # Finalizing: search is not offered, but a weak model calls it anyway.
            assert "search_web" not in {tool.name for tool in info.function_tools}
            return ModelResponse(parts=[ToolCallPart("search_web", {"query": "more"})], usage=RequestUsage(input_tokens=100))
        returned = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart) and p.tool_name == "search_web"]
        assert returned and returned[-1].content["status"] == "unavailable"
        return ModelResponse(parts=[TextPart("Here is what the evidence supports.")])

    result = await build_agent(Settings(studio_test_mode=True), model=FunctionModel(model)).run("Research", deps=deps)
    assert result.output == "Here is what the evidence supports."
    assert research.searches == []


@pytest.mark.asyncio
async def test_find_stories_returns_candidates_without_a_draft(fake_embedder):
    fresh = "https://news.example.test/2026/10/19/new-model"
    research = StubResearch(sources=[{"url": fresh, "title": "A different model release", "excerpt": "Совершенно новая история"}],
                            documents={fresh: "A different model release with new benchmark numbers. " * 20})
    repository = PostsRepository()
    chat = await repository.create_conversation(channel_id=1)
    deps = StudioDeps(repository=repository, workspace_id=repository.workspace_id, conversation_id=chat["id"], channel_id=1,
                      cancel_event=asyncio.Event(), research=research, owner_messages=("Сначала найди повод",))
    model, seen = scripted({"tool": "find_stories", "args": {}}, story_url=fresh, picks=[fresh])
    result = await build_agent(Settings(studio_test_mode=True), model=model).run("Сначала найди повод", deps=deps)
    returned = next(p.content for m in result.all_messages() for p in m.parts if isinstance(p, ToolReturnPart) and p.tool_name == "find_stories")
    assert returned["status"] == "found" and 1 <= len(returned["stories"]) <= limits.FIND_STORIES_MAX
    assert returned["stories"][0]["sources"] == [fresh]
    assert "write" not in seen and await repository.get_current_draft(conversation_id=chat["id"], channel_id=1) is None
    assert seen[-1] == "summary-tools:"  # the finished job ends the turn with a summary


def test_onnxruntime_telemetry_is_disabled_before_the_library_loads():
    import os
    from pathlib import Path

    # The module sets it at import, before onnxruntime is imported lazily.
    assert os.environ.get("ORT_DISABLE_TELEMETRY") == "1"
    source = Path("app/studio/embeddings.py").read_text()
    assert source.index('ORT_DISABLE_TELEMETRY') < source.index("import onnxruntime")
    assert "ORT_DISABLE_TELEMETRY=1" in Path("Dockerfile").read_text()
