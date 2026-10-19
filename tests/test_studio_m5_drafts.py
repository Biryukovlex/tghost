from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.studio.drafts import (
    DRAFT_WARNING_CHARS,
    MAX_DRAFT_CHARS,
    DraftConflictError,
    DraftValidationError,
    diff_summary,
    validate_draft_input,
)
from app.studio.repository import MemoryStudioRepository
from app.studio.agent import _draft_context, build_agent, StudioDeps
from app.config import Settings
from pydantic_ai.models.test import TestModel


@pytest.mark.parametrize(
    ("body", "expected"),
    [("🙂" * 3, 6), ("a\n\nб", 4)],
)
def test_server_counts_telegram_utf16_units_and_preserves_paragraphs(body, expected):
    payload = validate_draft_input({"body": body, "creative": True}, require_sources=False)
    assert payload.body == body
    assert payload.character_count == expected


def test_server_warns_near_limit_and_allows_save_but_copy_blocks_over_limit():
    near = validate_draft_input({"body": "x" * DRAFT_WARNING_CHARS, "creative": True}, require_sources=False)
    assert near.warning_threshold is True
    assert near.over_limit is False
    over = validate_draft_input({"body": "x" * (MAX_DRAFT_CHARS + 1), "creative": True}, require_sources=False)
    assert over.over_limit is True
    assert any("copying is blocked" in warning for warning in over.warnings)


def test_factual_claims_require_known_sources_and_every_claim_maps_to_one():
    with pytest.raises(DraftValidationError, match="at least one source"):
        validate_draft_input({"body": "A claim"})
    with pytest.raises(DraftValidationError, match="not part of this conversation"):
        validate_draft_input(
            {"body": "A claim", "source_ids": ["s-unknown"]},
            known_source_ids={"s-known"},
        )
    with pytest.raises(DraftValidationError, match="Each factual claim"):
        validate_draft_input(
            {"body": "A claim", "source_ids": ["s-known"], "claim_support": [{"claim": "A claim", "source_ids": []}]},
            known_source_ids={"s-known"},
        )
    payload = validate_draft_input(
        {"body": "A claim", "claim_support": [{"claim": "A claim", "source_ids": ["s-known"]}]},
        known_source_ids={"s-known"},
    )
    assert payload.source_ids == ["s-known"]


@pytest.mark.asyncio
async def test_memory_draft_versions_are_immutable_and_generated_revision_preserves_user_edit():
    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    await repository.persist_research_bundle(
        conversation_id=conversation["id"],
        channel_id=1,
        bundle={"sources": [{"source_id": "s1", "url": "https://example.test/a"}], "stories": []},
    )
    draft = await repository.create_draft(
        conversation_id=conversation["id"],
        channel_id=1,
        payload={"body": "First", "source_ids": ["s1"], "claim_support": [{"claim": "First", "source_ids": ["s1"]}]},
    )
    user = await repository.save_draft(draft_id=draft["id"], payload={"body": "My edit"}, expected_revision=draft["revision"], new_version=True)
    generated = await repository.revise_draft(draft_id=draft["id"], payload={"body": "Model replacement", "source_ids": ["s1"]})
    # An agent revision is a new version and becomes current; the owner's
    # version stays selectable in the list (no silent overwrite, no hiding).
    assert generated["current_version"] == 3 and generated["body"] == "Model replacement"
    current = await repository.get_draft(draft["id"])
    assert current and current["body"] == "Model replacement"
    versions = await repository.list_draft_versions(draft_id=draft["id"])
    assert [item["version"] for item in versions] == [1, 2, 3]
    assert [item["origin"] for item in versions] == ["generated", "user_edit", "regenerated"]
    assert versions[1]["body"] == "My edit"
    assert versions[2]["body"] == "Model replacement"
    with pytest.raises(DraftConflictError):
        await repository.save_draft(draft_id=draft["id"], payload={"body": "stale"}, expected_revision=user["revision"] - 1)
    copied = await repository.mark_draft_copied(draft_id=draft["id"])
    assert copied["copied_at"]


@pytest.mark.asyncio
async def test_memory_draft_source_and_story_scope_is_conversation_bound():
    repository = MemoryStudioRepository()
    first = await repository.create_conversation(channel_id=1)
    second = await repository.create_conversation(channel_id=1)
    await repository.persist_research_bundle(
        conversation_id=first["id"],
        channel_id=1,
        bundle={
            "sources": [{"source_id": "only-first", "url": "https://example.test/first"}],
            "stories": [{"cluster_id": "story-first", "source_ids": ["only-first"]}],
        },
    )
    with pytest.raises(DraftValidationError, match="not available"):
        await repository.create_draft(
            conversation_id=second["id"],
            channel_id=1,
            payload={"body": "Cross scope", "source_ids": ["only-first"]},
        )


def test_edit_summary_contains_lengths_not_private_text():
    summary = diff_summary("private old text", "private new text")
    assert "Unicode characters" in summary
    assert "private" not in summary


@pytest.mark.asyncio
async def test_history_context_exposes_current_draft_and_compact_version_history():
    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    draft = await repository.create_draft(
        conversation_id=conversation["id"],
        channel_id=1,
        payload={"body": "Creative first", "creative": True},
    )
    draft = await repository.save_draft(draft_id=draft["id"], payload={"body": "Owner edit"}, expected_revision=1, new_version=True)
    context = await _draft_context(SimpleNamespace(deps=StudioDeps(repository=repository, workspace_id="community-test", conversation_id=conversation["id"], channel_id=1, cancel_event=asyncio.Event())))
    assert context and context["body"] == "Owner edit"
    assert context["version"] == 2
    assert context["revision"] == 2
    assert set(context) == {"title", "body", "source_links", "media_content", "revision", "version"}


def test_m5_agent_registers_all_draft_tools():
    agent = build_agent(Settings(studio_test_mode=True))
    names = set(agent._function_toolset.tools)
    assert {"create_draft", "edit_draft", "get_draft", "list_draft_versions"}.issubset(names)


@pytest.mark.asyncio
async def test_channel_context_includes_profile_and_draft_pointer_with_on_demand_content():
    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    await repository.upsert_profile(
        {
            "channel_id": 1,
            "topics": [{"name": "technology", "claim": "Repeated high-traction theme."}],
            "style_profile": {"tone": "concise"},
            "editorial_rules": {},
            "confidence": "medium",
        }
    )
    await repository.create_draft(
        conversation_id=conversation["id"],
        channel_id=1,
        payload={"body": "History-aware draft", "creative": True},
    )
    agent = build_agent(
        Settings(studio_test_mode=True),
        model=TestModel(call_tools=["get_channel_context", "get_draft"], custom_output_text="context ready"),
    )
    result = await agent.run(
        "Use our history to help with the next post.",
        deps=StudioDeps(
            repository=repository,
            workspace_id=repository.workspace_id,
            conversation_id=conversation["id"],
            channel_id=1,
            cancel_event=asyncio.Event(),
        ),
    )
    serialized = str(result.all_messages())
    assert "technology" in serialized
    assert "draft_context" in serialized
    assert "History-aware draft" in serialized

    tool_parts = [part for message in result.all_messages() for part in message.parts if getattr(part, "part_kind", "") == "tool-return"]
    context = next(part.content for part in tool_parts if part.tool_name == "get_channel_context")
    context = context.model_dump(mode="json") if hasattr(context, "model_dump") else context
    assert context["draft_context"]["content_tool"] == "get_draft"
    assert "History-aware draft" not in str(context)
    draft = next(part.content for part in tool_parts if part.tool_name == "get_draft")
    assert draft["draft"]["body"] == "History-aware draft"
