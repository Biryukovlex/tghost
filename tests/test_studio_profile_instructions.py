"""The saved channel profile travels in the chat agent's instructions on every request."""
import json
import uuid

import pytest
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from app.config import Settings
from app.studio.agent import build_agent
from app.studio.context import profile_instructions
from app.studio.pipeline import _context_prompt
from app.studio.repository import MemoryStudioRepository
from app.studio.service import StudioService


async def _run(repo, conversation, respond) -> dict:
    service = StudioService(repo, Settings(studio_test_mode=True),
                            agent_factory=lambda settings: build_agent(settings, model=FunctionModel(stream_function=respond)))
    payload = {"threadId": str(conversation["id"]), "runId": str(uuid.uuid4()),
               "messages": [{"id": str(uuid.uuid4()), "role": "user", "content": "Suggest a hook for tomorrow"}],
               "tools": [], "context": [], "forwardedProps": {}}
    response = await service.stream_request(None, json.dumps(payload).encode())
    async for _ in response.body_iterator:
        pass
    return await repo.get_run(uuid.UUID(payload["runId"]))


@pytest.mark.asyncio
async def test_every_request_carries_the_saved_profile_and_the_context_tool_does_not_repeat_it():
    repo = MemoryStudioRepository()
    await repo.upsert_profile({"channel_id": 1, "topics_text": "AI agents in production",
                               "editorial_text": "Name the source of every figure", "style_text": "Bold first line"})
    conversation = await repo.create_conversation(channel_id=1)
    seen: list[str] = []
    tool_results: list[str] = []

    async def respond(messages, info):
        seen.append(info.instructions or "")
        if len(seen) == 1:
            yield {0: DeltaToolCall(name="get_channel_context", json_args="{}", tool_call_id="ctx")}
            return
        tool_results.extend(str(part.content) for message in messages for part in message.parts
                            if getattr(part, "part_kind", "") == "tool-return")
        yield "Here is a hook."

    run = await _run(repo, conversation, respond)
    assert run["status"] == "succeeded"
    assert len(seen) == 2
    for instructions in seen:
        block = instructions[instructions.index("CHANNEL PROFILE"):]
        assert "AI agents in production" in block and "Name the source of every figure" in block and "Bold first line" in block
        # The block closes the instructions, after the owner's standing instructions.
        assert instructions.rstrip().endswith(block.rstrip())
    assert tool_results and not any("Name the source of every figure" in result for result in tool_results)


def test_a_channel_without_a_saved_profile_says_so():
    assert "none saved" in profile_instructions(None)
    assert "none saved" in profile_instructions({"topics_text": " ", "editorial_text": "", "style_text": ""})
    assert "CHANNEL PROFILE (written and approved" in profile_instructions({"topics_text": "Robotics", "version": 3})


def test_job_steps_see_the_whole_profile():
    profile = "CHANNEL PROFILE\n" + "Editorial rule line. " * 300 + "LAST STYLE RULE"
    material = {"owner_prompt": "", "profile": profile, "covered": []}
    assert "LAST STYLE RULE" in _context_prompt(material, "Find a story")
