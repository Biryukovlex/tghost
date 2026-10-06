from __future__ import annotations

import asyncio

import pytest
from pydantic_ai.models.test import TestModel

from app.config import Settings
from app.studio.agent import StudioDeps, build_agent
from app.studio.repository import MemoryStudioRepository


class DraftingTestModel(TestModel):
    """A deterministic model seam that emits valid M5 tool arguments."""

    def __init__(self, *, tool_args: dict[str, dict], **kwargs):
        super().__init__(**kwargs)
        self.tool_args = tool_args

    def _get_tool_calls(self, parameters):
        return [(tool.name, tool) for tool in parameters.function_tools if tool.name in self.call_tools]

    def gen_tool_args(self, tool_def):
        if tool_def.name in self.tool_args:
            return self.tool_args[tool_def.name]
        return super().gen_tool_args(tool_def)


def _deps(repository: MemoryStudioRepository, conversation_id):
    return StudioDeps(
        repository=repository,
        workspace_id=repository.workspace_id,
        conversation_id=conversation_id,
        channel_id=1,
        cancel_event=asyncio.Event(),
    )


@pytest.mark.asyncio
async def test_one_prompt_creates_source_supported_draft_without_questionnaire():
    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    await repository.persist_research_bundle(
        conversation_id=conversation["id"],
        channel_id=1,
        bundle={
            "sources": [{"source_id": "story-1", "url": "https://news.test/story-1"}],
            "stories": [],
        },
    )
    model = DraftingTestModel(
        call_tools=["get_channel_context", "create_draft"],
        custom_output_text="Created a source-backed draft.",
        tool_args={
            "create_draft": {
                "body": "A concise source-backed post.",
                "title": "Today's angle",
                "source_links": ["https://news.test/story-1"],
            }
        },
    )
    agent = build_agent(Settings(studio_test_mode=True), model=model)

    result = await agent.run(
        "Find something interesting for the channel and draft a post.",
        deps=_deps(repository, conversation["id"]),
    )

    draft = await repository.get_current_draft(conversation_id=conversation["id"], channel_id=1)
    assert result.output == "Created a source-backed draft."
    assert draft is not None
    assert draft["body"] == "A concise source-backed post."
    assert draft["source_links"] == ["https://news.test/story-1"]
    assert draft["claim_support"] == []
    assert len([message for message in result.all_messages() if message.__class__.__name__ == "ModelRequest"]) == 2


@pytest.mark.asyncio
async def test_conversational_revision_preserves_direct_user_edit_and_appends_candidate():
    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    await repository.persist_research_bundle(
        conversation_id=conversation["id"],
        channel_id=1,
        bundle={
            "sources": [{
                "source_id": "story-1",
                "url": "https://news.test/story-1",
                "title": "Story one",
                "content": "Story one reports that the council approved the plan.",
                "accessible": True,
                "status": "ok",
            }],
            "stories": [],
        },
    )
    draft = await repository.create_draft(
        conversation_id=conversation["id"],
        channel_id=1,
        payload={
            "body": "Generated first version.",
            "source_ids": ["story-1"],
            "claim_support": [{
                "claim": "Generated first version.",
                "source_ids": ["story-1"],
                "passage": "Story one reports that the council approved the plan.",
            }],
        },
    )
    user_edit = await repository.save_draft(
        draft_id=draft["id"],
        payload={"body": "The owner's careful edit."},
        expected_revision=draft["revision"],
        new_version=True,
    )
    model = DraftingTestModel(
        call_tools=["get_draft", "edit_draft"],
        custom_output_text="I kept your edit and saved the model candidate.",
        tool_args={
            "edit_draft": {
                "expected_revision": user_edit["revision"],
                "body": "A shorter regenerated candidate.",
            }
        },
    )
    agent = build_agent(Settings(studio_test_mode=True), model=model)

    result = await agent.run(
        "Make this draft shorter.",
        deps=_deps(repository, conversation["id"]),
    )

    current = await repository.get_current_draft(conversation_id=conversation["id"], channel_id=1)
    versions = await repository.list_draft_versions(draft_id=draft["id"])
    assert result.output == "I kept your edit and saved the model candidate."
    assert current is not None
    assert current["body"] == "A shorter regenerated candidate."
    assert [item["origin"] for item in versions] == ["generated", "user_edit", "regenerated"]
    assert versions[1]["body"] == user_edit["body"]
    assert versions[-1]["body"] == "A shorter regenerated candidate."
