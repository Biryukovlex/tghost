from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.studio.agent import StudioDeps, build_agent
from app.studio.research import ResearchService
from app.studio.search import SearchResponse, SearchResult
from app.studio.sources import SourceDocument
from pydantic_ai.models.test import TestModel


class FakeProvider:
    def __init__(self):
        self.queries: list[str] = []

    async def search(self, query, **kwargs):
        self.queries.append(str(query))
        now = datetime.now(timezone.utc)
        result = SearchResult(
            url="https://news.test/story",
            canonical_url="https://news.test/story",
            title="Channel topic update",
            snippet="A bounded report about the channel topic.",
            source_name="News fixture",
            domain="news.test",
            published_at=now,
            provider="fixture",
            query=str(query),
            fetched_at=now,
            result_index=0,
            source_id="source-1",
            provenance={"provider": "fixture", "query": str(query)},
        )
        return SearchResponse(query=__import__("app.studio.search", fromlist=["SearchQuery"]).SearchQuery(str(query)), results=(result,), provider="fixture")


class FakeReader:
    async def read(self, url: str):
        now = datetime.now(timezone.utc)
        return SourceDocument(
            url=url,
            canonical_url=url,
            final_url=url,
            title="Read fixture",
            text="A safe source excerpt.",
            excerpt="A safe source excerpt.",
            content_type="text/plain",
            bytes_read=20,
            fetched_at=now,
            source_hash="hash-1",
            source_id="source-read",
            provenance={"retrieved_at": now.isoformat()},
        )


class Context:
    async def get_current_draft(self, **scope):
        return None

    async def channel_context(self, channel_id: int):
        return {
            "channel_id": channel_id,
            "identifier": "@fixture",
            "title": "Fixture channel",
            "tracked_posts": 1,
            "recent_posts": [{"message_id": 1, "text": "A channel topic post"}],
        }

    async def performance_rows(self, channel_id: int):
        now = datetime.now(timezone.utc)
        return [{
            "id": 1,
            "channel_id": channel_id,
            "message_id": 1,
            "posted_at": now - timedelta(days=10),
            "snapshot_at": now,
            "text": "A channel topic post",
            "views": 10,
            "reactions": 2,
            "comments": 1,
            "shares": 1,
        }]


@pytest.mark.asyncio
async def test_research_service_scopes_results_and_returns_source_backed_bundle():
    provider = FakeProvider()
    service = ResearchService(Settings(studio_search_enabled=True), provider=provider, reader=FakeReader())
    result = await service.search(
        workspace_id="workspace-a",
        conversation_id="conversation-a",
        channel_id=7,
        query="topic update",
        topics=["topic"],
        channel_evidence_ids=[10],
    )
    assert result["provider"] == "fixture"
    assert result["degraded"] is False
    assert result["sources"][0]["url"] == "https://news.test/story"
    assert result["stories"][0]["channel_evidence_ids"] == [10]
    assert await service.get_source(workspace_id="workspace-a", conversation_id="conversation-a", channel_id=7, source_id="source-1")
    assert await service.get_source(workspace_id="workspace-b", conversation_id="conversation-a", channel_id=7, source_id="source-1") is None


@pytest.mark.asyncio
async def test_research_service_runs_agent_planned_query_variants():
    provider = FakeProvider()
    service = ResearchService(
        Settings(studio_search_enabled=True),
        provider=provider,
        reader=FakeReader(),
        max_queries=3,
    )
    result = await service.search(
        workspace_id="w",
        conversation_id="c",
        channel_id=1,
        query="official agent release",
        alternate_queries=["agent GitHub releases", "independent agent launch coverage", "ignored fourth query"],
        engines=["github", "yandex"],
        exclude_domains=["weak.example"],
    )

    assert provider.queries == ["official agent release", "agent GitHub releases", "independent agent launch coverage"]
    assert result["queries"] == provider.queries
    assert result["activity"]["query_count"] == 3
    assert result["strategy"]["engines"] == ["github", "yandex"]
    assert result["strategy"]["excluded_domains"] == ["weak.example"]


@pytest.mark.asyncio
async def test_research_service_bounds_queries_and_read_sources():
    provider = FakeProvider()
    service = ResearchService(Settings(studio_search_enabled=True), provider=provider, reader=FakeReader(), max_sources=2, max_queries=2)
    novel = await service.find_novel_topics(
        workspace_id="w", conversation_id="c", channel_id=1, topics=["one", "two", "three"], instruction="four"
    )
    assert len(novel["queries"]) == 2
    assert len(provider.queries) == 2
    read = await service.read_sources(
        workspace_id="w", conversation_id="c", channel_id=1,
        urls=["https://news.test/story", "https://news.test/story", "https://other.test/a"],
    )
    assert len(read["documents"]) == 2
    assert any(source["source_id"] == "source-read" for source in read["sources"])


@pytest.mark.asyncio
async def test_agent_registers_research_tools_and_fails_closed_when_search_disabled():
    settings = Settings(studio_test_mode=True, studio_search_enabled=False)
    agent = build_agent(settings, model=TestModel(call_tools=["search_web"], custom_output_text="research complete"))
    result = await agent.run(
        "Find a current story.",
        deps=StudioDeps(repository=Context(), workspace_id="w", conversation_id="c", channel_id=1, cancel_event=asyncio.Event()),
    )
    assert result.output == "research complete"
    assert "search_web" in str(result.all_messages())
    assert "disabled" in str(result.all_messages()).lower()
    assert {"search_web", "read_sources", "compare_sources", "find_novel_topics"}.issubset(agent._function_toolset.tools)
