from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from app import limits
from app.config import Settings
from app.studio.research import ResearchService
from app.studio.search import SearchQuery, SearchResponse, SearchResult, SearXNGSearchProvider
from app.studio.sources import SafeSourceReader
from app.studio.agent import workflow_tool_sequence


def test_search_does_not_force_a_declined_draft_or_misread_post_analysis():
    assert workflow_tool_sequence("Найди истории, пока без черновика") == ("get_channel_context", "search_web")
    assert "create_draft" not in workflow_tool_sequence("Проанализируй лучшие посты")
    assert workflow_tool_sequence("Подготовь пост по найденным источникам без нового поиска") == ()
    assert () == workflow_tool_sequence("Подготовь пост, не делай заявлений о бенчмарках")


class ParallelProvider:
    def __init__(self):
        self.active = self.peak = 0
        self.queries = []

    async def search(self, query, **kwargs):
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.queries.append(query)
        await asyncio.sleep(0.01)
        self.active -= 1
        return SearchResponse(query=SearchQuery(query), provider="fixture", results=(SearchResult(
            url=f"https://example.org/{query}", canonical_url=f"https://example.org/{query}",
            title="Same headline", snippet="Same excerpt from another URL", domain="example.org",
            source_name="Example", provider="fixture", query=query, fetched_at=datetime.now(timezone.utc),
            result_index=0, source_id=query, published_at=None,
        ),))


@pytest.mark.asyncio
async def test_twelve_parallel_queries_preserve_all_candidate_ids_and_provider_wording():
    provider = ParallelProvider()
    service = ResearchService(Settings(), provider=provider)
    result = await service.search(workspace_id="w", conversation_id="c", channel_id=1,
                                  query="q0", alternate_queries=[f"q{i}" for i in range(1, 12)])
    assert provider.peak == 6
    assert provider.queries == [f"q{i}" for i in range(12)]
    assert len(result["sources"]) == 12  # No six-source or one-domain cutoff.
    assert result["activity"]["query_count"] == 12
    bundle = await service.get_bundle(workspace_id="w", conversation_id="c", channel_id=1)
    assert set(result["selected_source_ids"]) == {source.source_id for source in bundle.sources}
    newer = await service.search(workspace_id="w", conversation_id="c", channel_id=1, query="new")
    assert [source["source_id"] for source in newer["sources"]] == ["new"]


@pytest.mark.asyncio
async def test_novel_topics_keeps_user_instruction_first_without_canned_suffix(monkeypatch):
    provider = ParallelProvider()
    monkeypatch.setattr(limits, "SEARCH_MAX_QUERIES", 3)
    service = ResearchService(Settings(), provider=provider)
    await service.find_novel_topics(workspace_id="w", conversation_id="c", channel_id=1,
                                   instruction="Exact user angle", topics=["Alpha", "Beta", "Gamma"])
    assert provider.queries == ["Exact user angle", "Alpha", "Beta"]


@pytest.mark.asyncio
async def test_engine_failures_and_effective_settings_are_not_hidden_by_cache(monkeypatch):
    calls = []
    def handler(request):
        calls.append(dict(request.url.params))
        return httpx.Response(200, json={"results": [], "unresponsive_engines": [["github", "timeout"]]})
    monkeypatch.setattr(limits, "SEARCH_ENGINES", "github")
    settings = Settings(studio_search_enabled=True, studio_search_base_url="http://search:8080")
    provider = SearXNGSearchProvider(settings, transport=httpx.MockTransport(handler))
    first = await provider.search("Any wording")
    assert first.degraded and "github" in " ".join(first.warnings)
    monkeypatch.setattr(provider, "engines", ("arxiv",))
    await provider.search("Any wording")
    assert [item["engines"] for item in calls] == ["github", "arxiv"]
    rejected = await provider.search("Any wording", engines=["unavailable-engine"])
    assert rejected.degraded and len(calls) == 2  # No silent engine substitution.


@pytest.mark.asyncio
async def test_reader_pins_connection_ip_and_retains_tls_and_http_hostname():
    resolutions = []
    def resolve(host, port):
        resolutions.append(host)
        return ["93.184.216.34"] if len(resolutions) == 1 else ["127.0.0.1"]
    def handler(request):
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "example.org"
        assert request.extensions["sni_hostname"] == "example.org"
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<nav>Menu noise</nav><article>Useful report</article><footer>Footer noise</footer>")
    reader = SafeSourceReader(resolver=resolve, transport=httpx.MockTransport(handler))
    document = await reader.read("https://example.org/report")
    assert document.accessible and len(resolutions) == 1
    assert "Useful report" in document.text and "noise" not in document.text
