"""Synthetic regressions for drafting from evidence read in an earlier turn."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.studio.agent import StudioDeps, build_agent
from app.studio.repository import MemoryStudioRepository
from app.studio.research import ResearchService
from app.studio.search import SearchQuery, SearchResponse, SearchResult
from app.studio.sources import SourceDocument

URL = "https://example.test/release"
ARTICLE = "Example Labs released Atlas on 6 October 2026. Atlas supports 12 tasks."


class Provider:
    source_id = "initial-search"

    async def search(self, query, **kwargs):
        return SearchResponse(
            query=SearchQuery(str(query)), provider="fixture",
            results=(SearchResult(
                url=URL, canonical_url=URL, title="Example Labs release",
                snippet="Search summary. " * 30, source_id=self.source_id,
                published_at=datetime(2026, 10, 6, tzinfo=timezone.utc),
                provider="fixture", source_name="Fixture", domain="example.test",
                query=str(query), fetched_at=datetime.now(timezone.utc), result_index=0,
            ),),
        )


class Reader:
    async def read(self, url):
        return SourceDocument(
            url=url, canonical_url=url, final_url=url,
            title="Example Labs release", text=ARTICLE, excerpt=ARTICLE,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("changed_id", [False, True])
async def test_draft_keeps_read_evidence_after_another_search(restart, changed_id):
    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    settings = Settings(studio_test_mode=True, studio_search_enabled=True)
    provider = Provider()
    service = ResearchService(settings, provider=provider, reader=Reader(), repository=repository)
    scope = dict(workspace_id=repository.workspace_id, conversation_id=conversation["id"], channel_id=1)
    await service.search(**scope, query="Example Labs release")
    read = await service.read_sources(**scope, urls=[URL])
    original_id = read["sources"][0]["source_id"]
    assert read["documents"][0]["source_id"] == original_id
    if restart:
        service = ResearchService(settings, provider=provider, reader=Reader(), repository=repository)
    if changed_id:
        provider.source_id = "later-search"
    searched = await service.search(**scope, query="Example Labs release again")
    assert searched["sources"][0]["source_id"] == original_id
    assert searched["sources"][0]["has_read_content"] is True
    bundle = await service.get_bundle(**scope)
    source = next(source for source in bundle.sources if source.source_id == original_id)
    assert source.content == ARTICLE
    # The next turn uses persisted evidence, just as production does.
    service = ResearchService(settings, provider=provider, reader=Reader(), repository=repository)
    deps = StudioDeps(repository=repository, **scope, research=service, cancel_event=asyncio.Event())
    agent = build_agent(settings)
    result = await agent._function_toolset.tools["create_draft"].function(
        SimpleNamespace(deps=deps), body="Example Labs released Atlas. It supports 12 tasks.",
        title="Example Labs", source_links=[URL],
    )
    assert result["status"] == "created"
    draft = await repository.get_current_draft(conversation_id=conversation["id"], channel_id=1)
    assert draft["source_links"] == [URL]
    assert draft["claim_support"] == []


@pytest.mark.asyncio
async def test_identical_search_copies_keep_each_returned_id_for_drafting():
    from app.studio.provenance import SourceEvidence

    service = ResearchService(Settings(studio_test_mode=True))
    sources = [SourceEvidence(
        source_id=f"source-{i}", url=f"https://example.test/{i}",
        canonical_url=f"https://example.test/{i}", title="Identical release",
        content_hash="same-content", excerpt=ARTICLE,
    ) for i in range(2)]
    bundle = await service._store(
        workspace_id="w", conversation_id="c", channel_id=1, query="release", values=sources,
    )
    assert {source.source_id for source in bundle.sources} == {"source-0", "source-1"}
    assert len(bundle.stories) == 1


@pytest.mark.asyncio
async def test_explicit_reread_replaces_old_content_and_failed_read_cannot_verify():
    from app.studio.provenance import SourceEvidence, verify_claim_support

    service = ResearchService(Settings(studio_test_mode=True))
    scope = dict(workspace_id="w", conversation_id="c", channel_id=1)
    first = SourceEvidence(source_id="stable", url=URL, canonical_url=URL, content=ARTICLE)
    await service._store(**scope, query="release", values=[first])
    updated = SourceEvidence(source_id="new-read", url=URL, canonical_url=URL, content="Example Labs withdrew Atlas.")
    bundle = await service._store(**scope, query="", values=[updated])
    assert bundle.sources[0].source_id == "stable"
    assert bundle.sources[0].content == updated.content
    failed = SourceEvidence(source_id="failed-read", url=URL, canonical_url=URL, status="blocked", accessible=False)
    bundle = await service._store(**scope, query="", values=[failed])
    accepted, failures = verify_claim_support({source.source_id: source for source in bundle.sources}, [{
        "claim": ARTICLE, "source_ids": ["stable"], "passage": ARTICLE,
    }])
    assert accepted == []
    assert failures[0].code == "unread_source"
