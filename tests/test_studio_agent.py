import asyncio
from datetime import datetime, timezone

import pytest
from pydantic_ai import UnexpectedModelBehavior
from pydantic_ai.models.test import TestModel

from app.config import Settings
from app.studio.agent import StudioDeps, build_agent, is_short_continuation_request, workflow_tool_sequence
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


def test_explicit_intents_require_the_evidence_tools_before_text_output():
    assert workflow_tool_sequence(
        "Search the web for current stories, cross-check sources, and explain their fit."
    ) == ("get_channel_context", "search_web", "compare_sources")
    assert workflow_tool_sequence(
        "Prepare a Telegram-ready draft from the strongest sourced story."
    ) == ("get_channel_context", "search_web")
    assert workflow_tool_sequence(
        "Analyze which posts perform best and explain the metrics."
    ) == ("get_performance_evidence",)
    assert workflow_tool_sequence("Make the tone friendlier.") == ()
    assert workflow_tool_sequence("Сделай драфт поста по первой") == ()
    assert workflow_tool_sequence("Сделай драфт поста про новую GPT 6, найди информацию") == (
        "get_channel_context",
        "search_web",
    )


def test_short_research_followups_inherit_the_previous_user_intent():
    expected = ("get_channel_context", "search_web")
    assert is_short_continuation_request("Ищем еще")
    assert is_short_continuation_request("Continue")
    assert not is_short_continuation_request("Search for current AI news")
    assert workflow_tool_sequence("Ищем еще") == expected
    assert workflow_tool_sequence(
        "Продолжи",
        history=[{"role": "user", "content": "Найди свежие новости про AI"}],
    ) == expected
    assert workflow_tool_sequence(
        "Продолжи",
        history=[
            {"role": "user", "content": "Подготовь черновик поста"},
            {"role": "user", "content": "Поищи темы в инете"},
        ],
    ) == expected


@pytest.mark.asyncio
async def test_model_cannot_claim_success_without_required_tools():
    agent = build_agent(
        Settings(studio_test_mode=True),
        model=TestModel(call_tools=[], custom_output_text="I completed the analysis."),
    )
    with pytest.raises(UnexpectedModelBehavior):
        await agent.run(
            "Analyze performance.",
            deps=StudioDeps(
                repository=ContextFixture(),
                workspace_id="workspace-a",
                conversation_id="conversation-a",
                channel_id=7,
                cancel_event=asyncio.Event(),
                required_tools=("get_performance_evidence",),
            ),
        )


@pytest.mark.parametrize("question", ["Прочитал источники?", "Ты читал ссылки?", "Какие источники прочитаны?", "Have you read the sources?", "Did you open the links?"])
def test_source_status_questions_check_saved_evidence(question):
    assert workflow_tool_sequence(question) == ("get_research_sources",)


def test_source_status_followed_by_explicit_new_search_still_requires_search():
    assert workflow_tool_sequence("Прочитал источники? Найди ещё новости.") == ("get_channel_context", "search_web")
