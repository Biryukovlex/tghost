import asyncio
from datetime import datetime, timezone

import pytest
from pydantic_ai.models.test import TestModel

from app.config import Settings
from app.studio.agent import StudioDeps, build_agent
from app.studio.model import StudioConfigurationError, build_model


class ContextFixture:
    async def get_current_draft(self, **scope):
        return None

    async def channel_context(self, channel_id: int):
        return {
            "channel_id": channel_id,
            "identifier": "@fixture",
            "title": "Fixture channel",
            "tracked_posts": 2,
            "oldest_post": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "newest_post": datetime(2024, 1, 2, tzinfo=timezone.utc),
            "recent_posts": [{"message_id": 1, "text": "A short fixture post"}],
            "note": "Synthetic context for the M2 agent test.",
        }


@pytest.mark.asyncio
async def test_bounded_test_agent_calls_only_authorized_context_tool():
    settings = Settings(studio_test_mode=True)
    agent = build_agent(settings)
    result = await agent.run(
        "Please inspect this channel.",
        deps=StudioDeps(
            repository=ContextFixture(),
            workspace_id="workspace-a",
            conversation_id="conversation-a",
            channel_id=7,
            cancel_event=asyncio.Event(),
        ),
    )

    assert "selected channel context" in result.output
    assert result.all_messages()


@pytest.mark.asyncio
async def test_agent_tool_fails_closed_when_run_is_cancelled():
    settings = Settings(studio_test_mode=True)
    agent = build_agent(settings)
    cancelled = asyncio.Event()
    cancelled.set()
    with pytest.raises(asyncio.CancelledError):
        await agent.run(
            "Inspect it.",
            deps=StudioDeps(
                repository=ContextFixture(),
                workspace_id="workspace-a",
                conversation_id="conversation-a",
                channel_id=7,
                cancel_event=cancelled,
            ),
        )


def test_production_model_requires_openrouter_key_without_test_mode():
    with pytest.raises(StudioConfigurationError):
        build_model(Settings(studio_test_mode=False, openrouter_api_key=""))


def test_default_agent_output_budget_supports_evidence_backed_answers():
    from app import limits

    assert limits.MAX_OUTPUT_TOKENS == 8192
    assert limits.RUN_TIMEOUT_SECONDS == 300
    assert limits.MAX_TOOL_CALLS == 12


def test_profile_rebuild_is_not_exposed_as_a_chat_tool():
    agent = build_agent(Settings(studio_test_mode=True))

    assert "analyze_channel" not in agent._function_toolset.tools


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["Прочитал источники?", "Search the web", "Make it shorter", "¿Qué sabemos?", "続けて"])
async def test_model_can_choose_a_reply_without_a_server_assigned_tool_chain(message):
    model = TestModel(call_tools=[], custom_output_text="Acknowledged.")
    agent = build_agent(Settings(studio_test_mode=True), model=model)
    deps = StudioDeps(repository=ContextFixture(), workspace_id="w", conversation_id="c", channel_id=7, cancel_event=asyncio.Event())
    result = await agent.run(message, deps=deps)
    assert result.output == "Acknowledged."
    assert result.usage.requests == 1 and not deps.completed_tools
