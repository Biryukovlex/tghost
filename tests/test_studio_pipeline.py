"""Post pipeline and draft editor with scripted model steps and a stub research
service. Synthetic data only; no network and no provider calls."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from app.config import Settings
from app.studio.agent import StudioDeps, build_agent
from tests.test_studio_rebuild import PostsRepository, synthetic_rows

OLD_LINK = "https://news.example.test/2026/10/13/payments-framework"
FRESH = "https://news.example.test/2026/10/19/new-model"
COVERED = "https://news.example.test/2026/10/19/repeat"


class StubResearch:
    def __init__(self, *, sources=(), documents=None, outcome="healthy"):
        self.sources = list(sources)
        self.documents = dict(documents or {})
        self.outcome = outcome
        self.searches: list[dict[str, Any]] = []
        self.reads: list[list[str]] = []

    async def search(self, **kwargs):
        self.searches.append(kwargs)
        return {"sources": self.sources, "search_outcome": self.outcome}

    async def read_sources(self, **kwargs):
        urls = list(kwargs["urls"])
        self.reads.append(urls)
        return {"documents": [{"url": url, "status": "ok", "title": "Page", "published_at": None, "text": self.documents[url]}
                              for url in urls if url in self.documents]}

    async def get_bundle(self, **kwargs):
        return None


def scripted(main_call: dict[str, Any], *, writer_body: str = "**A new headline**\n\nFresh text about the payments framework.",
             story_url: str = OLD_LINK, picks=None, revise=None, fail_writer: bool = False):
    seen: list[str] = []

    def respond(messages, info):
        instructions = info.instructions or ""
        output = info.output_tools[0].name if info.output_tools else None

        def final(args):
            return ModelResponse(parts=[ToolCallPart(output, args)])

        if "You are the TGhost agent" in instructions:
            returned = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
            if not returned:
                seen.append("main")
                return ModelResponse(parts=[ToolCallPart(main_call["tool"], main_call["args"])])
            seen.append("summary-tools:" + ",".join(sorted(tool.name for tool in info.function_tools)))
            return ModelResponse(parts=[TextPart("Saved.")])
        if instructions.startswith("You plan web searches"):
            seen.append("plan")
            return final({"queries": ["payments agents", "new model release"], "window": {"amount": 48, "unit": "hours"}})
        if instructions.startswith("Pick up to 6 pages"):
            seen.append("pick")
            return final({"urls": picks or [FRESH]})
        if instructions.startswith("From the pages below"):
            seen.append("extract")
            return final({"stories": [{"headline": "A new story", "summary": "Facts and numbers.", "source_urls": [story_url],
                                       "published": "2026-10-13"}]})
        if instructions.startswith("You are the channel's writer"):
            if fail_writer:
                from pydantic_ai.exceptions import ModelHTTPError
                seen.append("write-429")
                raise ModelHTTPError(429, "test-model", {"message": "rate limited"})
            seen.append("fix" if "Fix only" in instructions else "write")
            return final({"title": "Post", "body": writer_body})
        if instructions.startswith("You revise the chat's existing draft"):
            returned = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
            seen.append("edit")
            if not returned:
                return ModelResponse(parts=[ToolCallPart("get_draft", {})])
            if len(returned) == 1:
                return ModelResponse(parts=[ToolCallPart("edit_draft", {"expected_revision": returned[0].content["draft"]["revision"],
                                                                        "body": revise})])
            return ModelResponse(parts=[TextPart("Edited.")])
        raise AssertionError(f"unexpected step: {instructions[:80]}")

    return FunctionModel(respond), seen


async def run(main_call, *, research, owner_message, **kwargs):
    repository = PostsRepository()
    chat = await repository.create_conversation(channel_id=1)
    deps = StudioDeps(repository=repository, workspace_id=repository.workspace_id, conversation_id=chat["id"], channel_id=1,
                      cancel_event=asyncio.Event(), research=research, owner_messages=(owner_message,))
    model, seen = scripted(main_call, **kwargs)
    result = await build_agent(Settings(studio_test_mode=True), model=model).run(owner_message, deps=deps)
    returned = next(p.content for m in result.all_messages() for p in m.parts
                    if isinstance(p, ToolReturnPart) and p.tool_name == main_call["tool"])
    draft = await repository.get_current_draft(conversation_id=chat["id"], channel_id=1)
    return returned, draft, seen, repository, deps


@pytest.mark.asyncio
async def test_week_old_owner_link_is_written_without_search():
    research = StubResearch(documents={OLD_LINK: "Payments framework announced on 13 October with details. " * 20})
    returned, draft, seen, *_ = await run(
        {"tool": "write_post", "args": {"research": "none", "links": [OLD_LINK]}},
        research=research, owner_message=f"Напиши пост по этой статье: {OLD_LINK}")
    assert returned["status"] == "saved" and returned["entry"] == "link"
    assert research.searches == [] and research.reads == [[OLD_LINK]]
    assert "plan" not in seen and "pick" not in seen and "extract" in seen
    assert draft["source_links"] == [OLD_LINK]
    assert "body" not in returned["draft"] and returned["draft"]["body_chars"] == len(draft["body"])


@pytest.mark.asyncio
async def test_links_not_from_the_owner_are_rejected_and_notes_are_written():
    research = StubResearch(documents={FRESH: "text " * 100})
    returned, draft, seen, *_ = await run(
        {"tool": "write_post", "args": {"research": "none", "links": [FRESH], "notes": "My view: cost per finished task matters most."}},
        research=research, owner_message="My view: cost per finished task matters most. Write a post, no search.")
    assert returned["status"] == "saved" and returned["entry"] == "none"
    assert research.reads == [] and research.searches == []
    assert any("Ignored links" in warning for warning in returned["warnings"])
    assert draft["source_links"] == [] and seen.count("write") == 1


@pytest.mark.asyncio
async def test_discovery_drops_covered_results_and_takes_the_window_from_the_plan():
    archive_text = synthetic_rows()[0]["text"]
    research = StubResearch(
        sources=[{"url": COVERED, "title": archive_text.split("\n")[0], "excerpt": archive_text},
                 {"url": FRESH, "title": "A different model release", "excerpt": "Совершенно новая история про выпуск модели"}],
        documents={FRESH: "A different model release with new benchmark numbers. " * 20})
    returned, draft, seen, *_ = await run(
        {"tool": "write_post", "args": {"research": "discover"}}, research=research,
        owner_message="Find a story the channel has not covered and write a post.", story_url=FRESH)
    assert returned["status"] == "saved" and returned["entry"] == "discover"
    assert research.searches[0]["recency_days"] == 2  # 48 hours from the owner's own unit
    assert returned["steps"]["search"]["results"] == 2 and returned["steps"]["search"]["after_novelty_filter"] == 1
    assert seen[:5] == ["main", "plan", "pick", "extract", "write"]
    assert draft["source_links"] == [FRESH]


@pytest.mark.asyncio
async def test_search_outage_is_reported_as_a_typed_failure():
    research = StubResearch(outcome="unavailable")
    returned, draft, *_ = await run({"tool": "write_post", "args": {"research": "discover"}}, research=research,
                                    owner_message="Find a new story and write a post.")
    assert returned["status"] == "failed"
    assert returned["error"] == {"code": "search_unavailable", "message": "Web search is unavailable right now; no story could be found."}
    assert draft is None


@pytest.mark.asyncio
async def test_copied_text_triggers_one_fix_step():
    research = StubResearch(documents={OLD_LINK: "Payments framework details. " * 30})
    copied = "**Headline**\n\n" + synthetic_rows()[2]["text"].split("\n\n")[1] + " More words follow here."
    returned, draft, seen, *_ = await run(
        {"tool": "write_post", "args": {"research": "none", "links": [OLD_LINK]}}, research=research,
        owner_message=f"Write about {OLD_LINK}", writer_body=copied)
    assert "fix" in seen
    assert any("word for word" in issue for issue in returned["steps"]["checks"]["issues"])


@pytest.mark.asyncio
async def test_revise_draft_uses_an_editor_that_sees_lengths():
    repository = PostsRepository()
    chat = await repository.create_conversation(channel_id=1)
    from app.studio.drafts import DraftContent
    body = "Long draft text. " * 30
    await repository.create_draft(conversation_id=chat["id"], channel_id=1,
                                  payload=DraftContent(title="Draft", body=body).storage_payload(), origin="generated")
    deps = StudioDeps(repository=repository, workspace_id=repository.workspace_id, conversation_id=chat["id"], channel_id=1,
                      cancel_event=asyncio.Event(), owner_messages=("Make it a third shorter",))
    shorter = "Long draft text. " * 20
    model, seen = scripted({"tool": "revise_draft", "args": {"instruction": "Make it a third shorter"}}, revise=shorter)
    result = await build_agent(Settings(studio_test_mode=True), model=model).run("Make it a third shorter", deps=deps)
    returned = next(p.content for m in result.all_messages() for p in m.parts if isinstance(p, ToolReturnPart) and p.tool_name == "revise_draft")
    assert returned["status"] == "edited"
    assert returned["draft"]["length_ratio"] == round(len(shorter) / len(body), 2)
    current = await repository.get_current_draft(conversation_id=chat["id"], channel_id=1)
    assert current["body"] == shorter and current["current_version"] == 2


@pytest.mark.asyncio
async def test_a_finished_job_completes_the_turn_without_more_tools():
    research = StubResearch(documents={OLD_LINK: "Payments framework details. " * 30})
    returned, draft, seen, *_ = await run({"tool": "write_post", "args": {"research": "none", "links": [OLD_LINK]}},
                                          research=research, owner_message=f"Write about {OLD_LINK}")
    assert returned["status"] == "saved"
    assert seen[-1] == "summary-tools:"  # the summarizing turn had no tools


@pytest.mark.asyncio
async def test_a_rate_limited_step_fails_the_job_not_the_run(monkeypatch):
    from app import limits
    monkeypatch.setattr(limits, "PIPELINE_RATE_LIMIT_RETRIES", 1)
    monkeypatch.setattr(limits, "PIPELINE_RATE_LIMIT_BACKOFF_SECONDS", 0.0)
    research = StubResearch(documents={OLD_LINK: "Payments framework details. " * 30})
    returned, draft, seen, *_ = await run({"tool": "write_post", "args": {"research": "none", "links": [OLD_LINK]}},
                                          research=research, owner_message=f"Write about {OLD_LINK}", fail_writer=True)
    assert seen.count("write-429") == 2  # one retry, then the job reports the failure
    assert returned["status"] == "failed"
    assert returned["error"]["code"] == "model_rate_limited" and returned["error"]["step"] == "write"
    assert draft is None
    assert seen[-1].startswith("summary-tools:") and seen[-1] != "summary-tools:"  # a failed job leaves tools available


@pytest.mark.asyncio
async def test_a_failed_fix_step_keeps_the_written_post(monkeypatch):
    from pydantic_ai import UnexpectedModelBehavior
    research = StubResearch(documents={OLD_LINK: "Payments framework details. " * 30})
    copied = "**Headline**\n\n" + synthetic_rows()[2]["text"].split("\n\n")[1] + " More words follow here."
    model, seen = scripted({"tool": "write_post", "args": {"research": "none", "links": [OLD_LINK]}}, writer_body=copied)
    original = model.function

    def failing_fix(messages, info):
        if (info.instructions or "").startswith("You are the channel's writer") and "Fix only" in (info.instructions or ""):
            raise UnexpectedModelBehavior("Exceeded maximum retries for output validation")
        return original(messages, info)

    model.function = failing_fix
    repository = PostsRepository()
    chat = await repository.create_conversation(channel_id=1)
    deps = StudioDeps(repository=repository, workspace_id=repository.workspace_id, conversation_id=chat["id"], channel_id=1,
                      cancel_event=asyncio.Event(), research=research, owner_messages=(f"Write about {OLD_LINK}",))
    result = await build_agent(Settings(studio_test_mode=True), model=model).run(f"Write about {OLD_LINK}", deps=deps)
    returned = next(p.content for m in result.all_messages() for p in m.parts if isinstance(p, ToolReturnPart) and p.tool_name == "write_post")
    assert returned["status"] == "saved"
    assert any("fix step failed" in warning for warning in returned["warnings"])
    draft = await repository.get_current_draft(conversation_id=chat["id"], channel_id=1)
    assert draft["body"] == copied
