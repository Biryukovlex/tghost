"""T41 claim-article-verification tests (memory repository, scripted models)."""
from __future__ import annotations

import uuid

import pytest

from app.config import Settings
from app.studio.provenance import SourceEvidence, verify_claim_support
from app.studio.repository import MemoryStudioRepository

ARTICLE_A = (
    "Company A announced on 12 March 2026 that it raised $50 million in Series C "
    "funding led by North Peak. The round values the maker of Atlas phones at $400 million."
)
ARTICLE_B = (
    "Company B unveiled its Orbit tablet on 3 February 2026. The device ships in April "
    "with a starting price of $299 and a 12-hour battery."
)


def _evidence(source_id: str, text: str, **overrides) -> SourceEvidence:
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    base = dict(
        source_id=source_id,
        url=f"https://news.test/{source_id}",
        canonical_url=f"https://news.test/{source_id}",
        title=f"Title {source_id}",
        excerpt=text[:500],
        content=text,
        accessible=True,
        status="ok",
        retrieved_at=now,
    )
    base.update(overrides)
    return SourceEvidence(**base)


def test_unrelated_article_mapped_to_claim_is_blocked():
    sources = {
        "a": _evidence("a", ARTICLE_A),
        "b": _evidence("b", ARTICLE_B),
    }
    accepted, failures = verify_claim_support(sources, [{
        "claim": "Company A raised $50 million in 2026.",
        "source_ids": ["b"],
        "passage": "Company B unveiled its Orbit tablet on 3 February 2026.",
    }])
    assert accepted == []
    assert len(failures) == 1
    assert failures[0].code == "unsupported_detail"
    assert "$50" in failures[0].message or "50" in failures[0].message


def test_same_topic_article_lacking_number_or_date_is_blocked():
    sources = {"b": _evidence("b", ARTICLE_B)}
    accepted, failures = verify_claim_support(sources, [{
        "claim": "Company B priced the tablet at $199.",
        "source_ids": ["b"],
        "passage": "Company B unveiled its Orbit tablet on 3 February 2026.",
    }])
    assert accepted == []
    assert failures[0].code == "unsupported_detail"


def test_matching_readable_passage_succeeds():
    sources = {"a": _evidence("a", ARTICLE_A)}
    passage = "Company A announced on 12 March 2026 that it raised $50 million in Series C funding led by North Peak"
    accepted, failures = verify_claim_support(sources, [{
        "claim": "Company A raised $50 million.",
        "source_ids": ["a"],
        "passage": passage,
    }])
    assert failures == []
    assert len(accepted) == 1
    assert accepted[0]["verified"] is True
    assert accepted[0]["passage"] == passage


def test_matching_number_but_wrong_named_subject_is_blocked():
    sources = {"b": _evidence("b", "Company B raised $50 million in 2026.")}
    accepted, failures = verify_claim_support(sources, [{
        "claim": "Company A raised $50 million in 2026.",
        "source_ids": ["b"],
        "passage": "Company B raised $50 million in 2026.",
    }])
    assert accepted == []
    assert failures[0].code == "unsupported_detail"


def test_numeric_token_must_match_whole_number():
    sources = {"a": _evidence("a", "Company A raised $150 million in 2026.")}
    accepted, failures = verify_claim_support(sources, [{
        "claim": "Company A raised $50 million in 2026.",
        "source_ids": ["a"],
        "passage": "Company A raised $150 million in 2026.",
    }])
    assert accepted == []
    assert failures[0].code == "unsupported_detail"


def test_inaccessible_and_snippet_only_sources_cannot_verify():
    sources = {
        "blocked": _evidence("blocked", "", accessible=False, status="blocked"),
        "snippet": _evidence("snippet", "", content=""),
    }
    accepted, failures = verify_claim_support(sources, [
        {"claim": "Something happened.", "source_ids": ["blocked"], "passage": "Something happened."},
        {"claim": "Something else happened.", "source_ids": ["snippet"], "passage": "Something else happened."},
    ])
    assert accepted == []
    assert {failure.code for failure in failures} == {"unread_source", "snippet_only"}


def test_missing_and_invented_passages_fail():
    sources = {"a": _evidence("a", ARTICLE_A)}
    accepted, failures = verify_claim_support(sources, [
        {"claim": "Company A raised funds.", "source_ids": ["a"], "passage": ""},
        {"claim": "Company A raised funds.", "source_ids": ["a"], "passage": "A sentence the article never contained."},
        {"claim": "Company A raised funds.", "source_ids": ["ghost"], "passage": "Anything."},
    ])
    assert accepted == []
    assert [failure.code for failure in failures] == ["passage_required", "passage_not_found", "unknown_source"]


class _ScriptedReader:
    def __init__(self, documents):
        self._documents = list(documents)

    async def read(self, url: str):
        for document in self._documents:
            if document.url == url or document.canonical_url == url:
                return document
        from app.studio.sources import SourceDocument

        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        return SourceDocument(
            url=url, canonical_url=url, final_url=url, status="inaccessible",
            accessible=False, title="", text="", excerpt="", fetched_at=now,
        )


def _document(url: str, text: str, **overrides):
    from app.studio.sources import SourceDocument

    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    base = dict(
        url=url, canonical_url=url, final_url=url, status="ok", accessible=True,
        title=f"Title for {url}", text=text, excerpt=text[:500], fetched_at=now,
    )
    base.update(overrides)
    return SourceDocument(**base)


class _ScriptedSearch:
    def __init__(self, mapping):
        self._mapping = dict(mapping)
        self.queries: list[str] = []

    async def search(self, query, **kwargs):
        from app.studio.search import SearchQuery, SearchResponse

        text = str(query.text if hasattr(query, "text") else query)
        self.queries.append(text)
        return SearchResponse(
            query=SearchQuery(text=text),
            results=tuple(self._mapping.get(text, ())),
            provider="fixture",
        )


def _search_result(source_id: str, url: str, title: str, snippet: str):
    from datetime import datetime, timezone

    from app.studio.search import SearchResult

    now = datetime.now(timezone.utc)
    return SearchResult(
        url=url, canonical_url=url, title=title, snippet=snippet, source_name="News",
        domain="news.test", published_at=None, provider="fixture", query="q",
        fetched_at=now, result_index=0, source_id=source_id,
    )






async def _research_flow(repository, conversation_id):
    """Search two articles, read only the first; return their source IDs."""
    from app.studio.research import ResearchService

    search = _ScriptedSearch({
        "funding": [
            _search_result("s-a", "https://news.test/a", "Company A raises funds", "Company A funding snippet."),
            _search_result("s-b", "https://news.test/b", "Company B tablet", "Company B tablet snippet."),
        ],
    })
    reader = _ScriptedReader([_document("https://news.test/a", ARTICLE_A)])
    service = ResearchService(Settings(studio_test_mode=True), provider=search, reader=reader, repository=repository)
    await service.search(
        workspace_id=repository.workspace_id, conversation_id=conversation_id,
        channel_id=1, query="funding",
    )
    await service.read_sources(
        workspace_id=repository.workspace_id, conversation_id=conversation_id,
        channel_id=1, urls=["https://news.test/a"],
    )
    # Persist so the agent run (a separate service instance) reloads the
    # same read state through the repository, as production does.
    bundle = await service.get_bundle(
        workspace_id=repository.workspace_id, conversation_id=conversation_id, channel_id=1,
    )
    assert bundle is not None
    await repository.persist_research_bundle(
        conversation_id=conversation_id, channel_id=1, bundle=bundle.model_dump(mode="json"),
    )






@pytest.mark.asyncio
async def test_same_url_stays_scoped_to_each_conversation():
    repository = MemoryStudioRepository()
    first = await repository.create_conversation(channel_id=1)
    second = await repository.create_conversation(channel_id=1)
    await repository.persist_research_bundle(
        conversation_id=first["id"], channel_id=1,
        bundle={"sources": [{
            "source_id": "shared", "url": "https://news.test/shared",
            "title": "Shared story", "content": "Shared story raised $10 million in 2026.",
            "accessible": True, "status": "ok",
        }], "stories": []},
    )
    await repository.persist_research_bundle(
        conversation_id=second["id"], channel_id=1,
        bundle={"sources": [{
            "source_id": "shared", "url": "https://news.test/shared",
            "title": "Shared story",
            "accessible": True, "status": "ok",
        }], "stories": []},
    )
    from app.studio.research import ResearchService

    for conversation_id, readable in ((first["id"], True), (second["id"], False)):
        service = ResearchService(Settings(studio_test_mode=True), repository=repository)
        bundle = await service.get_bundle(
            workspace_id=repository.workspace_id, conversation_id=conversation_id, channel_id=1,
        )
        assert bundle is not None
        by_id = {source.source_id: source for source in bundle.sources}
        assert bool((by_id["shared"].content or "").strip()) is readable




@pytest.mark.asyncio
async def test_owner_body_edit_clears_verified_state():
    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    await _research_flow(repository, conversation["id"])
    draft = await repository.create_draft(
        conversation_id=conversation["id"], channel_id=1, payload={
            "body": "Company A raised $50 million in 2026.", "source_ids": ["s-a"],
            "claim_support": [{"claim": "Company A raised $50 million in 2026.", "source_ids": ["s-a"],
                               "passage": "Company A announced on 12 March 2026 that it raised $50 million in Series C funding", "verified": True}],
        },
    )
    assert draft["claim_support"][0]["verified"] is True
    edited = await repository.save_draft(
        draft_id=uuid.UUID(str(draft["id"])),
        payload={"body": "Company A raised $50 million in 2026, edited by owner."},
        expected_revision=draft["revision"],
    )
    assert edited["claim_support"][0]["verified"] is False
    assert edited["claim_support"][0]["passage"].startswith("Company A announced on 12 March 2026")


@pytest.mark.asyncio
async def test_owner_cannot_forge_or_reuse_verification_for_changed_mapping():
    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    await _research_flow(repository, conversation["id"])
    draft = await repository.create_draft(
        conversation_id=conversation["id"], channel_id=1, payload={
            "body": "Company A raised $50 million in 2026.", "source_ids": ["s-a"],
            "claim_support": [{"claim": "Company A raised $50 million in 2026.", "source_ids": ["s-a"],
                               "passage": "Company A announced on 12 March 2026 that it raised $50 million in Series C funding", "verified": True}],
        },
    )
    assert draft["claim_support"][0]["verified"] is True
    changed = await repository.save_draft(
        draft_id=uuid.UUID(str(draft["id"])),
        payload={"claim_support": [{
            **draft["claim_support"][0],
            "claim": "Company B raised $50 million in 2026.",
            "verified": True,
        }]},
        expected_revision=draft["revision"],
    )
    assert changed["claim_support"][0]["verified"] is False
    attempted = await repository.save_draft(
        draft_id=uuid.UUID(str(changed["id"])),
        payload={"claim_support": [{**changed["claim_support"][0], "verified": True}]},
        expected_revision=changed["revision"],
    )
    assert attempted["claim_support"][0]["verified"] is False
