"""Agent rebuild contracts: argument boundary, compact channel tools, read results,
publication dates, novelty and copy checks. Synthetic data only."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic_ai.messages import ModelResponse, RetryPromptPart, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from app import limits
from app.config import Settings
from app.studio.agent import StudioDeps, build_agent
from app.studio.channel_tools import markdown_features, to_markdown
from app.studio.novelty import ArchiveCorpus, copied_fragments, signature_lines
from app.studio.repository import MemoryStudioRepository
from app.studio.research import _agent_result
from app.studio.sources import _HTMLTextExtractor
from app.studio.tool_args import normalize_value

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
TOPICS = ["agent memory", "model pricing", "open weights", "coding tools", "evaluation", "inference chips",
          "search engines", "voice assistants", "robotics", "security incidents", "data centers", "context windows"]


def synthetic_rows(channel_id: int = 1, count: int = 12) -> list[dict]:
    rows = []
    for index in range(count):
        headline = f"Release note {index}: {TOPICS[index % len(TOPICS)]}"
        bullets = "\n".join(f"- point {index}.{n} about {TOPICS[(index + n) % len(TOPICS)]}" for n in range(3))
        text = f"{headline}\n\nA short story about {TOPICS[index % len(TOPICS)]} number {index}.\n\n{bullets}\n\nSignature line"
        rows.append({"post_id": 100 + index, "message_id": 10 + index, "channel_id": channel_id,
                     "posted_at": NOW - timedelta(days=index + 2), "text": text,
                     "formatting_entities": [{"type": "bold", "offset": 0, "length": len(headline)}],
                     "is_deleted": False, "snapshot_at": NOW, "has_snapshot": True,
                     "views": 1000 + 300 * (count - index), "comments": index, "reactions": 10 + index, "shares": 5 + (count - index)})
    return rows


class PostsRepository(MemoryStudioRepository):
    async def performance_rows(self, channel_id: int, limit: int = 2000):
        await super().performance_rows(channel_id, limit)
        return synthetic_rows(channel_id)


async def _deps(repository=None):
    repository = repository or PostsRepository()
    chat = await repository.create_conversation(channel_id=1)
    return StudioDeps(repository=repository, workspace_id=repository.workspace_id, conversation_id=chat["id"],
                      channel_id=1, cancel_event=asyncio.Event())


@pytest.mark.asyncio
async def test_undeclared_arguments_are_ignored_and_reported_for_every_tool():
    deps = await _deps()
    calls = []

    def model(messages, info):
        calls.append(messages)
        if len(calls) == 1:
            # A weak model adds a channel_id the server owns and quotes a number.
            return ModelResponse(parts=[ToolCallPart("list_channel_posts", {"channel_id": 7, "limit": "3", "sort": "views"})])
        return ModelResponse(parts=[TextPart("Done")])

    result = await build_agent(Settings(studio_test_mode=True), model=FunctionModel(model)).run("List posts", deps=deps)
    parts = [part for message in result.all_messages() for part in message.parts]
    assert not any(isinstance(part, RetryPromptPart) for part in parts)
    returned = next(part.content for part in parts if isinstance(part, ToolReturnPart) and part.tool_name == "list_channel_posts")
    assert returned["ignored_arguments"] == ["channel_id"]
    assert len(returned["items"]) == 3 and returned["total"] == 12


def test_shape_repairs_follow_the_declared_schema_only():
    assert normalize_value('["a", "b"]', {"type": "array"}) == ["a", "b"]
    assert normalize_value({"item": ["a"]}, {"type": "array"}) == ["a"]
    assert normalize_value("5", {"type": "integer"}) == 5
    assert normalize_value("true", {"anyOf": [{"type": "boolean"}, {"type": "null"}]}) is True
    # A string field stays a string even when it looks like a number.
    assert normalize_value("5", {"type": "string"}) == "5"


@pytest.mark.asyncio
async def test_performance_overview_is_compact_and_keeps_formatting():
    deps = await _deps()
    tools = build_agent(Settings(studio_test_mode=True))._function_toolset.tools
    from types import SimpleNamespace
    result = await tools["get_performance_evidence"].function(SimpleNamespace(deps=deps))
    assert len(json.dumps(result, default=str)) < 20_000
    assert len(result["top_posts"]) == limits.TOP_POSTS
    top = result["top_posts"][0]
    assert top["opening"].startswith("**Release note") and len(top["opening"]) <= limits.TOP_POST_OPENING_CHARS + 1
    assert top["length"] >= len(top["opening"].rstrip("…"))
    assert result["top_posts"][0]["link"].startswith("https://t.me/sample_channel/")
    profile = result["summary"]["top_posts_vs_rest"]["top"]
    assert profile["share_bold_first_line"] == 1.0 and profile["share_with_bullets"] == 1.0
    read = await tools["read_channel_posts"].function(SimpleNamespace(deps=deps), post_ids=[100, 999])
    assert read["missing"] == [999] and read["posts"][0]["post_id"] == 100


def test_markdown_rendering_uses_utf16_offsets_and_text_links():
    text = "🔥 Big news\nRead more"
    entities = [{"type": "bold", "offset": 0, "length": 11}, {"type": "text_url", "offset": 12, "length": 9, "url": "https://example.test/a"}]
    assert to_markdown(text, entities) == "**🔥 Big news**\n[Read more](https://example.test/a)"
    features = markdown_features(to_markdown(text, entities))
    assert features["bold_first_line"] and features["inline_links"] == 1 and features["emoji_per_1k_chars"] > 0


def test_large_read_keeps_every_page_and_drops_channel_evidence_first():
    documents = [{"url": f"https://example.test/{n}", "status": "ok", "text": "x" * 3_500} for n in range(6)]
    evidence = [{"post_id": n, "excerpt": "y" * 900} for n in range(20)]
    payload = {"documents": documents, "sources": [{"source_id": str(n), "url": f"https://example.test/{n}"} for n in range(6)],
               "stories": [{"headline": "h", "summary": "s", "channel_evidence": evidence}], "selected_source_ids": []}
    result = _agent_result(payload)
    assert result.get("status") != "details_available"
    assert len(result["documents"]) == 6 and all(len(doc["text"]) >= 600 for doc in result["documents"])
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) <= limits.TOOL_RESULT_MAX_CHARS


def test_publication_date_comes_from_page_metadata():
    parser = _HTMLTextExtractor()
    parser.feed('<head><meta property="article:published_time" content="2026-10-13T09:30:00Z"></head><body><p>Body</p></body>')
    parser.close()
    assert parser.published_at() == datetime(2026, 10, 13, 9, 30, tzinfo=timezone.utc)
    parser = _HTMLTextExtractor()
    parser.feed('<script type="application/ld+json">{"datePublished": "2026-10-12"}</script><p>Body</p>')
    parser.close()
    assert parser.published_at() == datetime(2026, 10, 12, tzinfo=timezone.utc)
    parser = _HTMLTextExtractor()
    parser.feed("<p>No date here</p>")
    parser.close()
    assert parser.published_at() is None


def test_novelty_threshold_comes_from_the_archive_and_copying_ignores_signatures():
    archive = [row["text"] for row in synthetic_rows()]
    corpus = ArchiveCorpus(archive)
    threshold = corpus.covered_threshold()
    assert 0 < threshold < 1
    assert corpus.max_similarity(archive[3]) > threshold
    assert corpus.max_similarity("Совершенно другая тема про кулинарию и рецепты пирогов на выходные") < threshold
    signatures = signature_lines(archive)
    assert "signature line" in signatures
    copied = copied_fragments("Fresh text.\n\nSignature line", archive, words=2, ignore=signatures)
    assert copied == []
    copied = copied_fragments("we wrote a short story about agent memory number 0 today", archive, words=8, ignore=signatures)
    assert copied and "short story about agent memory number 0" in copied[0]
