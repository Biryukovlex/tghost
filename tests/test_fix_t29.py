from __future__ import annotations

import asyncio
import pytest

from app.config import Settings
from app.studio.provenance import SourceEvidence
from app.studio.research import ResearchService

@pytest.mark.asyncio
async def test_loaded_research_merges_sources_stored_during_the_read():
    started = asyncio.Event()
    release = asyncio.Event()

    class Repository:
        async def get_research_bundle(self, *, conversation_id, channel_id):
            started.set()
            await release.wait()
            return {"sources": [{"source_id": "old", "url": "https://example.test/old"}], "stories": []}

    service = ResearchService(Settings(studio_test_mode=True), repository=Repository())
    load = asyncio.create_task(service._ensure_loaded(workspace_id="w", conversation_id="c", channel_id=1))
    await started.wait()
    new = SourceEvidence(source_id="new", url="https://example.test/new", canonical_url="https://example.test/new")
    await service._store(workspace_id="w", conversation_id="c", channel_id=1, query="new", values=[new])
    release.set()
    state = await load
    assert set(state.sources) == {"old", "new"}
