"""Local preflight for the M6.6 manual evaluation worksheet.

The test creates the same sized story, first-draft, and revision sets that a
reviewer uses in the manual worksheet.  It is not a substitute for visual
judgement; it prevents the worksheet from silently shrinking below the
minimum sample size and verifies that every synthetic case remains grounded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.studio.drafts import copy_allowed
from app.studio.provenance import build_research_bundle
from app.studio.repository import MemoryStudioRepository


ROOT = Path(__file__).resolve().parents[1]


def _story_values(count: int = 20) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    for index in range(1, count + 1):
        token = f"signal-{index:02d}"
        values.append(
            {
                "source_id": f"source-{index:02d}",
                "url": f"https://fixture-{index:02d}.test/{token}",
                "title": f"alpha{index:02d} beta{index:02d}",
                "snippet": f"gamma{index:02d} delta{index:02d}",
                "source_name": f"Fixture publisher {index:02d}",
                "published_at": "2026-09-02T00:00:00+00:00",
                "fetched_at": "2026-09-03T00:00:00+00:00",
                "source_hash": token,
                "provider": "fixture",
                "accessible": True,
            }
        )
    return values


def test_manual_story_set_contains_twenty_distinct_source_linked_candidates():
    bundle = build_research_bundle(
        _story_values(),
        query="synthetic manual stories",
        topics=["editorial"],
    )
    assert len(bundle.stories) == 20
    assert len({story.cluster_id for story in bundle.stories}) == 20
    assert all(story.source_ids for story in bundle.stories)
    assert all(
        source.url.startswith("https://") and source.source_id
        for source in bundle.sources
    )
    assert all(
        set(story.source_ids) <= {source.source_id for source in bundle.sources}
        for story in bundle.stories
    )


@pytest.mark.asyncio
async def test_manual_draft_and_revision_sets_keep_grounding_and_owner_edits():
    repository = MemoryStudioRepository()
    source_id = "manual-source"
    conversation = await repository.create_conversation(channel_id=1)
    await repository.persist_research_bundle(
        conversation_id=conversation["id"],
        channel_id=1,
        bundle={
            "sources": [{"source_id": source_id, "url": "https://fixture.test/manual"}],
            "stories": [],
        },
    )

    first_drafts = []
    for index in range(1, 11):
        draft_chat = await repository.create_conversation(channel_id=1)
        await repository.persist_research_bundle(conversation_id=draft_chat["id"], channel_id=1, bundle={"sources": [{"source_id": source_id, "url": "https://example.test/manual-source"}], "stories": []})
        item = await repository.create_draft(
            conversation_id=draft_chat["id"],
            channel_id=1,
            payload={
                "working_title": f"Manual draft {index:02d}",
                "body": f"Source-backed manual draft {index:02d}.",
                "source_ids": [source_id],
                "claim_support": [
                    {
                        "claim": f"Source-backed manual draft {index:02d}.",
                        "source_ids": [source_id],
                    }
                ],
                "confidence": "medium",
            },
        )
        first_drafts.append(item)
    assert len(first_drafts) == 10
    assert all(item["source_ids"] == [source_id] for item in first_drafts)
    assert all(copy_allowed({"body": item["body"], "creative": True}) for item in first_drafts)

    draft = first_drafts[-1]
    owner = await repository.save_draft(
        draft_id=draft["id"],
        payload={"body": "Owner's manual edit remains authoritative."},
        expected_revision=draft["revision"],
        new_version=True,
    )
    candidates = []
    for index in range(1, 21):
        candidate = await repository.revise_draft(
            draft_id=draft["id"],
            payload={
                "body": f"Generated revision candidate {index:02d}.",
                "source_ids": [source_id],
                "claim_support": [
                    {
                        "claim": f"Generated revision candidate {index:02d}.",
                        "source_ids": [source_id],
                    }
                ],
            },
            instruction=f"Manual revision {index:02d}",
        )
        candidates.append(candidate)
    assert len(candidates) == 20
    assert all(item["current_version_origin"] == "regenerated" for item in candidates)
    current = await repository.get_current_draft(
        conversation_id=draft_chat["id"], channel_id=1
    )
    # The latest agent revision is current; the owner's version is retained.
    assert current and current["body"] == candidates[-1]["body"]
    kept = await repository.list_draft_versions(draft_id=draft["id"])
    assert any(item["body"] == owner["body"] and item["origin"] == "user_edit" for item in kept)
    versions = await repository.list_draft_versions(draft_id=draft["id"])
    assert len(versions) == 22  # initial + owner edit + 20 generated candidates
    assert versions[1]["origin"] == "user_edit"
    assert all(version["body"] for version in versions)


def test_manual_accessibility_and_reduced_motion_contract_is_present():
    source = (ROOT / "studio-frontend/src/main.tsx").read_text(encoding="utf-8")
    styles = (ROOT / "studio-frontend/src/styles.css").read_text(encoding="utf-8")
    assert ":focus-visible" in styles
    assert "prefers-reduced-motion: reduce" in styles
    # T44: Full post (not "Telegram post") in artifact label
    assert "aria-label=\"Full post" in source
    assert "aria-label=\"Draft version\"" in source
    assert "aria-expanded={open}" in source
    assert "overflow-x: hidden" in styles
    assert "claim_support.slice" not in source
    assert "Source unavailable" not in source
    assert "clickableSources.map" in source
    assert "type=\"button\"" in source
