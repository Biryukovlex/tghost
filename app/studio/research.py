"""Application-owned orchestration for the M4 research tools.

The service is intentionally small: one provider-neutral search adapter, one
safe source reader, deterministic provenance helpers, and an optional
workspace-scoped repository projection.  Normalized records are retained in a
bounded process cache for the current run and upserted into PostgreSQL when the
production repository is available, so reloads never depend on one process's
memory. Scope keys include workspace, conversation, and channel so a tool can
never reuse another conversation's source set accidentally.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Sequence

from .. import limits
from .provenance import (
    _aware,
    _now,
    ResearchBundle,
    SourceEvidence,
    StoryCluster,
    build_research_bundle,
    cluster_stories,
    story_from_dict,
    to_source_evidence,
)
from .search import SearchProvider, SearchResponse, SearchCache, build_search_provider
from .sources import SafeSourceReader, SourceDocument


def _search_status_summary(summary: dict[str, Any]) -> str:
    """One agent-facing line: usable results stay usable despite engine issues."""

    outcome = str(summary.get("outcome", "healthy"))
    failed = [str(name) for name in summary.get("failed_engines", [])]
    if outcome == "healthy":
        return "Search completed with usable results and no relevant engine failures."
    if outcome == "partial":
        engines = ", ".join(failed) if failed else "a query or search provider"
        return f"Search returned usable results, but {engines} failed; treat the results as partial, not unavailable."
    if outcome == "empty":
        return "Search completed successfully but returned no results."
    engines = ", ".join(failed) if failed else "the provider"
    return f"Search is unavailable ({engines} failed) and returned nothing usable."


def _clean_instruction(value: str, limit: int = 2_000) -> str:    return " ".join(str(value or "").split())[:limit].strip()


def _topic_names(value: Any) -> list[str]:
    if isinstance(value, str):
        return [_clean_instruction(value, 120)] if _clean_instruction(value, 120) else []
    if isinstance(value, dict):
        name = value.get("name") or value.get("topic") or value.get("label")
        return [_clean_instruction(name, 120)] if name else []
    return [name for item in (value or []) for name in _topic_names(item) if name]


def _scope(workspace_id: Any, conversation_id: Any, channel_id: int) -> str:
    return f"{workspace_id}:{conversation_id}:{int(channel_id)}"


def _source_for_agent(source: SourceEvidence) -> dict[str, Any]:
    """Keep model-facing provenance useful without returning the full archive."""

    value = source.model_dump(mode="json")
    if source.published_at and source.published_at > _now():
        value["warnings"] = [*value["warnings"], "Publication date is in the future; verify it before treating this as a current event."]
    return {
        key: value[key]
        for key in (
            "source_id",
            "url",
            "title",
            "publisher",
            "domain",
            "published_at",
            "retrieved_at",
            "accessible",
            "status",
            "quality_score",
            "quality_notes",
            "warnings",
            "injection_flags",
            "query",
        )
    } | {
        "excerpt": str(value.get("excerpt") or "")[:400],
        "has_read_content": bool(source.content and source.accessible and source.status == "ok"),
        "search_relevance": dict((value.get("metadata") or {}).get("provenance") or {}),
        "source_role": str((value.get("metadata") or {}).get("source_role") or "editorial_or_official"),
    }


def _story_for_agent(story: StoryCluster) -> dict[str, Any]:
    value = story.model_dump(mode="json")
    return {
        key: value[key]
        for key in (
            "cluster_id",
            "headline",
            "summary",
            "source_ids",
            "primary_source_id",
            "supporting_source_ids",
            "score",
            "topic_relevance",
            "freshness",
            "source_quality",
            "novelty",
            "matched_topics",
            "conflict_flags",
            "warnings",
            "channel_evidence_ids",
            "score_breakdown",
            "channel_evidence",
            "published_at",
            "retrieved_at",
        )
    }


def _agent_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Deduplicate evidence without changing the stored research or its IDs.

    The complete archive stays scoped on the server. An oversized source list
    explicitly names omitted IDs so the agent can fetch the next page.
    """
    evidence_sets: dict[str, Any] = {}
    for key in ("stories", "suggestions"):
        for story in payload.get(key, []):
            evidence = story.pop("channel_evidence", [])
            if evidence:
                encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
                reference = hashlib.sha256(encoded.encode()).hexdigest()[:16]
                evidence_sets[reference] = evidence
                story["channel_evidence_ref"] = reference
    if evidence_sets:
        payload["channel_evidence_sets"] = evidence_sets
    sources = payload.get("sources", [])
    omitted: list[str] = []
    def size() -> int:
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str))
    if size() > limits.TOOL_RESULT_MAX_CHARS:
        # Story summaries often duplicate article text already in documents.
        # Preserve the actual read pages before paging source metadata.
        for key in ("stories", "suggestions"):
            for story in payload.get(key, []):
                summary = str(story.get("summary") or "")
                if len(summary) > 600:
                    story["summary"] = summary[:599].rstrip() + "…"
                    story["summary_truncated"] = True
    while sources and size() > limits.TOOL_RESULT_MAX_CHARS:
        omitted.insert(0, str(sources.pop()["source_id"]))
        payload["omitted_source_ids"] = omitted
        payload["continuation"] = "Use get_research_sources with omitted_source_ids to read the remaining results."
    if size() > limits.TOOL_RESULT_MAX_CHARS:
        # Pathological diagnostics/evidence must not bypass the final bound.
        # Keep all source identities and explicitly offer the scoped archive.
        payload = {
            "status": "details_available",
            "selected_source_ids": payload.get("selected_source_ids", []) or omitted,
            "omitted_source_ids": payload.get("selected_source_ids", []) or omitted,
            "continuation": "Use get_research_sources for source details and get_performance_evidence for channel evidence.",
            "search_outcome": payload.get("search_outcome"),
            "warnings": ["The full result exceeded the context budget; detailed records remain in the scoped archive."],
        }
    return payload


def _document_for_agent(document: SourceDocument) -> dict[str, Any]:
    value = document.model_dump(mode="json")
    return {
        key: value[key]
        for key in (
            "url",
            "final_url",
            "status",
            "accessible",
            "status_code",
            "title",
            "fetched_at",
            "source_id",
            "injection_flags",
            "warnings",
        )
    } | {"text": str(value.get("text") or value.get("excerpt") or "")[:3_500]}


@dataclass(slots=True)
class ResearchState:
    sources: dict[str, SourceEvidence] = field(default_factory=dict)
    query: str = ""
    bundle: ResearchBundle | None = None
    persistence_warning: str | None = None
    last_persisted_hash: str | None = None


class ResearchService:
    """Coordinate bounded search/read/compare operations for one app process."""

    def __init__(
        self,
        settings,
        *,
        provider: SearchProvider | None = None,
        reader: SafeSourceReader | None = None,
        cache: SearchCache | None = None,
        repository: Any | None = None,
        scorer: Any | None = None,
        max_sources: int = 12,
        max_queries: int | None = None,
        concurrency: int = 6,
    ) -> None:
        self.settings = settings
        self.provider = provider or build_search_provider(settings, cache=cache)
        self.reader = reader or SafeSourceReader(settings)
        self.repository = repository
        self.scorer = scorer
        self.max_sources = max(1, min(int(max_sources), 12))
        self.max_queries = max(1, min(int(max_queries if max_queries is not None else limits.SEARCH_MAX_QUERIES), 12))
        self.concurrency = max(1, min(int(concurrency), 6))
        self._search_slots = asyncio.Semaphore(self.concurrency)
        self._states: dict[str, ResearchState] = {}
        self._lock = asyncio.Lock()

    def _state(self, workspace_id: Any, conversation_id: Any, channel_id: int) -> ResearchState:
        key = _scope(workspace_id, conversation_id, channel_id)
        self._states.setdefault(key, ResearchState())
        # Bound process memory: evict the oldest conversation beyond 64.
        while len(self._states) > 64:
            oldest = next(iter(self._states))
            if oldest == key:
                break
            del self._states[oldest]
        return self._states[key]

    async def _store(
        self,
        *,
        workspace_id: Any,
        conversation_id: Any,
        channel_id: int,
        query: str,
        values: Iterable[SourceEvidence],
        topics: Sequence[str] = (),
        recent_posts: Sequence[str] = (),
        channel_evidence_ids: Sequence[int] = (),
        channel_evidence: Sequence[dict[str, Any]] = (),
        search_results: bool = False,
    ) -> ResearchBundle:
        async with self._lock:
            state = self._state(workspace_id, conversation_id, channel_id)
            stored = dict(state.sources)
            by_url = {source.canonical_url: source for source in stored.values() if source.canonical_url}
            for source in values:
                previous = by_url.get(source.canonical_url) if source.canonical_url else None
                if previous is not None:
                    source = replace(source, source_id=previous.source_id)
                    # Rediscovery is not a new article read. Keep the actual
                    # fetched text and its retrieval time for verification.
                    # Explicit rereads (including failed ones) still replace it.
                    if search_results and previous.content and previous.accessible and previous.status == "ok":
                        source = previous
                stored.pop(source.source_id, None)
                stored[source.source_id] = source
                if source.canonical_url:
                    by_url[source.canonical_url] = source
            merged = list(stored.values())
            max_sources = int(getattr(self.settings, "studio_research_max_sources", 300) or 300)
            max_sources = max(12, min(max_sources, 1000))
            if len(merged) > max_sources:
                merged = merged[-max_sources:]
            state.sources = {source.source_id: source for source in merged}
            state.query = query or state.query
            state.bundle = build_research_bundle(
                merged,
                query=state.query,
                topics=topics,
                recent_posts=recent_posts,
                channel_evidence_ids=channel_evidence_ids,
                channel_evidence=channel_evidence,
                scorer=self.scorer,
            )
            # Story clustering may deduplicate copies by title/hash. The
            # evidence registry must retain every returned source ID/URL.
            state.bundle = replace(state.bundle, sources=tuple(merged))
            bundle = state.bundle
        persist = getattr(self.repository, "persist_research_bundle", None)
        if persist is not None:
            import hashlib as _hashlib
            import json as _json

            try:
                fingerprint = _hashlib.sha256(
                    _json.dumps(bundle.model_dump(mode="json"), sort_keys=True, default=str).encode()
                ).hexdigest()
            except Exception:  # noqa: BLE001 - fall through to persisting
                fingerprint = None
            async with self._lock:
                state = self._state(workspace_id, conversation_id, channel_id)
                if fingerprint is not None and state.last_persisted_hash == fingerprint:
                    return bundle
            try:
                await persist(
                    conversation_id=conversation_id,
                    channel_id=int(channel_id),
                    bundle=bundle.model_dump(mode="json"),
                )
            except Exception:  # noqa: BLE001 - research persistence must not stop a run
                async with self._lock:
                    state = self._state(workspace_id, conversation_id, channel_id)
                    state.persistence_warning = "Research results could not be persisted; this result is available for the current run only."
                    state.bundle = replace(bundle, warnings=tuple(dict.fromkeys([*bundle.warnings, state.persistence_warning])))
                    bundle = state.bundle
            else:
                if fingerprint is not None:
                    async with self._lock:
                        self._state(workspace_id, conversation_id, channel_id).last_persisted_hash = fingerprint
        return bundle

    async def _ensure_loaded(self, *, workspace_id: Any, conversation_id: Any, channel_id: int) -> ResearchState:
        # The loader is intentionally outside the lock so a slow database
        # read does not block a concurrent search.  The merge itself is
        # serialized: a result stored while the read is in flight must never
        # be replaced by the older persisted snapshot.
        async with self._lock:
            state = self._state(workspace_id, conversation_id, channel_id)
            if state.bundle is not None:
                return state
        loader = getattr(self.repository, "get_research_bundle", None)
        if loader is None:
            return state
        try:
            raw = await loader(conversation_id=conversation_id, channel_id=int(channel_id))
        except Exception:  # noqa: BLE001 - a reload should fail closed to an empty bundle
            return state
        if not raw:
            return state
        import logging as _logging

        _research_log = _logging.getLogger("studio.research")
        sources = [to_source_evidence(item) for item in (raw.get("sources") or [])]
        stories = []
        for item in raw.get("stories") or []:
            if not isinstance(item, dict):
                continue
            try:
                stories.append(story_from_dict(item))
            except Exception:  # noqa: BLE001 - one corrupt row must not break every research tool
                _research_log.warning("skipping corrupt persisted story cluster")
        async with self._lock:
            state = self._state(workspace_id, conversation_id, channel_id)
            # In-memory sources are newer than the snapshot we just loaded.
            # Keep them and add only IDs that are not already present.
            for source in sources:
                state.sources.setdefault(source.source_id, source)
            if state.bundle is None:
                state.query = str(raw.get("query") or state.query)
                state.bundle = ResearchBundle(
                    query=state.query,
                    sources=tuple(state.sources.values()),
                    stories=tuple(stories),
                    warnings=tuple(str(item) for item in (raw.get("warnings") or [])[:20]),
                    retrieved_at=_aware(raw.get("retrieved_at")) or _now(),
                    selected_source_ids=tuple(str(item) for item in (raw.get("selected_source_ids") or [])[:12]),
                )
            else:
                # A concurrent _store won the race.  Preserve its bundle and
                # make the merged source map visible to subsequent tools.
                state.bundle = replace(state.bundle, sources=tuple(state.sources.values()))
        return state

    @staticmethod
    def aggregate_search_outcomes(responses: Sequence[Any]) -> dict[str, Any]:
        """Combine parallel query variants without losing successful ones.

        Rules: usable results plus any relevant engine failure is `partial`;
        usable results with none is `healthy`; nothing usable with a relevant
        failure is `unavailable`; a clean miss is `empty`. A batch is never
        reported healthy when a requested engine failed.
        """

        total_results = sum(len(response.results) for response in responses)
        relevant = sum(
            1
            for response in responses
            for item in getattr(response, "engine_failures", ())
            if isinstance(item, dict) and item.get("relevant")
        )
        # Transport/provider failures carry no per-engine record. Preserve
        # them when combining parallel variants instead of mislabelling a
        # failed variant as a clean empty search (or the whole batch healthy).
        unavailable_variant = any(
            getattr(response, "outcome", "healthy") == "unavailable"
            for response in responses
        )
        partial_variant = any(
            getattr(response, "outcome", "healthy") == "partial"
            for response in responses
        )
        if total_results:
            outcome = "partial" if relevant or unavailable_variant or partial_variant else "healthy"
        else:
            outcome = "unavailable" if relevant or unavailable_variant or partial_variant else "empty"
        failed_engines = sorted({
            str(item.get("engine", ""))
            for response in responses
            for item in getattr(response, "engine_failures", ())
            if isinstance(item, dict) and item.get("relevant") and item.get("engine")
        })
        variants = [
            {
                "query": str(getattr(getattr(response, "query", None), "text", ""))[:200],
                "outcome": str(getattr(response, "outcome", "healthy") or "healthy"),
                "result_count": len(response.results),
            }
            for response in responses
        ]
        return {
            "outcome": outcome,
            "failed_engines": failed_engines,
            "variants": variants,
        }

    async def _record_activity(
        self,
        *,
        workspace_id: Any,
        conversation_id: Any,
        channel_id: int,
        event_type: str,
        provider: str,
        query_count: int,
        result_count: int,
        cache_hit: bool,
        degraded: bool,
        trace_id: str,
        search_outcome: str = "healthy",
        failed_engines: Sequence[str] = (),
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        outcome = str(search_outcome or "healthy")[:32]
        failed = [str(name)[:80] for name in list(failed_engines)[:12]]
        activity: dict[str, Any] = {
            "event_type": str(event_type)[:80],
            "provider": str(provider)[:80],
            "query_count": max(0, int(query_count)),
            "result_count": max(0, int(result_count)),
            "cache_hit": bool(cache_hit),
            "degraded": bool(degraded),
            "trace_id": str(trace_id)[:128],
            "search_outcome": outcome,
            "failed_engines": failed,
            "metadata": dict(metadata or {}),
        }
        activity["metadata"].setdefault("search_outcome", outcome)
        activity["metadata"].setdefault("failed_engines", failed)
        recorder = getattr(self.repository, "record_research_event", None)
        if recorder is not None:
            try:
                await recorder(conversation_id=conversation_id, channel_id=int(channel_id), event=activity)
                activity["persisted"] = True
            except Exception:  # noqa: BLE001 - activity is best effort and never leaks provider details
                activity["persisted"] = False
        else:
            activity["persisted"] = False
        return activity

    async def search(
        self,
        *,
        workspace_id: Any,
        conversation_id: Any,
        channel_id: int,
        query: str,
        alternate_queries: Sequence[str] = (),
        topics: Sequence[str] = (),
        recent_posts: Sequence[str] = (),
        channel_evidence_ids: Sequence[int] = (),
        channel_evidence: Sequence[dict[str, Any]] = (),
        language: str | None = None,
        recency_days: int | None = None,
        limit: int | None = None,
        categories: Sequence[str] = (),
        domains: Sequence[str] = (),
        engines: Sequence[str] = (),
        exclude_domains: Sequence[str] = (),
    ) -> dict[str, Any]:
        query = _clean_instruction(query, 500)
        if not query:
            raise ValueError("research query must not be empty")
        await self._ensure_loaded(
            workspace_id=workspace_id, conversation_id=conversation_id, channel_id=channel_id,
        )
        queries = list(
            dict.fromkeys(
                candidate
                for candidate in [query, *(_clean_instruction(item, 500) for item in alternate_queries)]
                if candidate
            )
        )[: self.max_queries]

        async def run(candidate: str) -> SearchResponse:
            async with self._search_slots:
                return await self.provider.search(
                    candidate,
                    language=language,
                    recency_days=recency_days,
                    limit=limit,
                    categories=tuple(categories),
                    domains=tuple(domains),
                    engines=tuple(engines),
                    excluded_domains=tuple(exclude_domains),
                )

        responses = await asyncio.gather(*(run(candidate) for candidate in queries))
        values = [to_source_evidence(result) for response in responses for result in response.results]
        combined_query = " | ".join(queries)
        outcome_summary = self.aggregate_search_outcomes(responses)
        outcome = outcome_summary["outcome"]
        degraded = outcome in ("partial", "unavailable")
        bundle = await self._store(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            channel_id=channel_id,
            query=combined_query,
            values=values,
            topics=topics,
            recent_posts=recent_posts,
            channel_evidence_ids=channel_evidence_ids,
            channel_evidence=channel_evidence,
            search_results=True,
        )
        activity = await self._record_activity(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            channel_id=channel_id,
            event_type="search",
            provider=",".join(sorted({response.provider for response in responses})),
            query_count=len(queries),
            result_count=sum(len(response.results) for response in responses),
            cache_hit=all(response.cache_hit for response in responses),
            degraded=degraded,
            trace_id=responses[0].trace_id,
            search_outcome=outcome,
            failed_engines=outcome_summary["failed_engines"],
        )
        # Preserve the provider/cache/degraded activity fields while attaching
        # the deterministic clusters and source links to the tool result.
        # Return this batch, not a ranked subset of the conversation archive.
        # The model decides relevance, credibility and domain diversity.
        by_url = {source.canonical_url: source for source in bundle.sources if source.canonical_url}
        selected = list({
            source.source_id: source for source in
            (by_url.get(source.canonical_url, source) for source in values)
        }.values())
        selected_ids = {source.source_id for source in selected}
        stories = [story for story in bundle.stories if selected_ids.intersection(story.source_ids)][:8]
        response_bundle = {
            "query": query,
            "queries": queries,
            "strategy": {
                "language": language,
                "recency_days": recency_days,
                "categories": list(categories),
                "domains": list(domains),
                "engines": list(engines),
                "excluded_domains": list(exclude_domains),
            },
            "sources": [_source_for_agent(source) for source in selected],
            "stories": [_story_for_agent(story) for story in stories],
            "selected_source_ids": [source.source_id for source in selected],
            "retrieved_at": bundle.model_dump(mode="json")["retrieved_at"],
        }
        response_bundle["provider"] = ",".join(sorted({response.provider for response in responses}))
        response_bundle["cache_hit"] = all(response.cache_hit for response in responses)
        response_bundle["degraded"] = degraded
        response_bundle["search_outcome"] = outcome
        response_bundle["search_variants"] = outcome_summary["variants"]
        response_bundle["failed_engines"] = outcome_summary["failed_engines"]
        response_bundle["status_summary"] = _search_status_summary(outcome_summary)
        response_bundle["trace_id"] = responses[0].trace_id
        response_bundle["activity"] = activity
        response_bundle["warnings"] = list(dict.fromkeys([*(warning for response in responses for warning in response.warnings), *bundle.warnings]))
        return _agent_result(response_bundle)

    async def read_sources(
        self,
        *,
        workspace_id: Any,
        conversation_id: Any,
        channel_id: int,
        urls: Sequence[str],
        query: str = "",
        topics: Sequence[str] = (),
        recent_posts: Sequence[str] = (),
        channel_evidence_ids: Sequence[int] = (),
        channel_evidence: Sequence[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        unique_urls = list(dict.fromkeys(str(url).strip() for url in urls if str(url).strip()))[: min(self.max_sources, 6)]
        semaphore = asyncio.Semaphore(self.concurrency)

        async def read_one(url: str) -> SourceDocument:
            async with semaphore:
                return await self.reader.read(url)

        documents = await asyncio.gather(*(read_one(url) for url in unique_urls))
        prior_state = await self._ensure_loaded(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            channel_id=channel_id,
        )
        values: list[SourceEvidence] = []
        for document in documents:
            value = to_source_evidence(document)
            prior = next(
                (
                    source
                    for source in prior_state.sources.values()
                    if source.canonical_url and source.canonical_url == value.canonical_url
                ),
                None,
            )
            if prior is not None:
                value = replace(
                    value,
                    source_id=prior.source_id,
                    published_at=value.published_at or prior.published_at,
                    provider=value.provider or prior.provider,
                    query=value.query or prior.query,
                )
            values.append(value)
        bundle = await self._store(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            channel_id=channel_id,
            query=_clean_instruction(query, 500),
            values=values,
            topics=topics,
            recent_posts=recent_posts,
            channel_evidence_ids=channel_evidence_ids,
            channel_evidence=channel_evidence,
        )
        activity = await self._record_activity(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            channel_id=channel_id,
            event_type="read_sources",
            provider=next((source.provider for source in values if source.provider), "source_reader"),
            query_count=0,
            result_count=len(documents),
            cache_hit=False,
            degraded=any(not document.accessible for document in documents),
            trace_id=str(uuid.uuid4()),
        )
        by_url = {source.canonical_url: source for source in bundle.sources if source.canonical_url}
        selected = [by_url.get(source.canonical_url, source) for source in values]
        selected_ids = {source.source_id for source in selected}
        stories = [story for story in bundle.stories if selected_ids.intersection(story.source_ids)][:8]
        return _agent_result({
            "documents": [
                {**_document_for_agent(document), "source_id": source.source_id}
                for document, source in zip(documents, selected)
            ],
            "sources": [_source_for_agent(source) for source in selected],
            "stories": [_story_for_agent(story) for story in stories],
            "warnings": list(bundle.warnings),
            "selected_source_ids": [source.source_id for source in selected],
            "retrieved_at": bundle.model_dump(mode="json")["retrieved_at"],
            "activity": activity,
        })

    async def compare_sources(
        self,
        *,
        workspace_id: Any,
        conversation_id: Any,
        channel_id: int,
        source_ids: Sequence[str] = (),
        urls: Sequence[str] = (),
        query: str = "",
        topics: Sequence[str] = (),
        recent_posts: Sequence[str] = (),
        channel_evidence_ids: Sequence[int] = (),
        channel_evidence: Sequence[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        state = await self._ensure_loaded(workspace_id=workspace_id, conversation_id=conversation_id, channel_id=channel_id)
        wanted = set(str(item) for item in source_ids if str(item))
        wanted_urls = set(str(item) for item in urls if str(item))
        selected = [
            source
            for source in state.sources.values()
            if (not wanted and not wanted_urls) or source.source_id in wanted or source.url in wanted_urls or source.canonical_url in wanted_urls
        ][: min(self.max_sources, 8)]
        if not selected:
            return {"sources": [], "stories": [], "agreements": [], "conflicts": [], "missing_support": ["No stored sources match this request."], "warnings": ["Read or search sources before comparing them."]}
        bundle = await self._store(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            channel_id=channel_id,
            query=_clean_instruction(query, 500) or state.query,
            values=selected,
            topics=topics,
            recent_posts=recent_posts,
            channel_evidence_ids=channel_evidence_ids,
            channel_evidence=channel_evidence,
        )
        stories = cluster_stories(
            selected,
            query=query or state.query,
            topics=topics,
            recent_posts=recent_posts,
            channel_evidence_ids=channel_evidence_ids,
            channel_evidence=channel_evidence,
            scorer=self.scorer,
        )
        conflicts = [flag for story in stories for flag in story.conflict_flags if flag not in {"single_source", "undated_source"}]
        common_terms = self._common_terms(selected)
        missing = []
        if any(source.published_at is None for source in selected):
            missing.append("publication_date")
        if any(not source.excerpt for source in selected):
            missing.append("bounded_excerpt")
        return _agent_result({
            "sources": [_source_for_agent(source) for source in selected],
            "stories": [_story_for_agent(story) for story in stories[:8]],
            "agreements": common_terms,
            "conflicts": list(dict.fromkeys(conflicts)),
            "missing_support": missing,
            "warnings": list(bundle.warnings),
            "selected_source_ids": list(bundle.selected_source_ids),
            "retrieved_at": bundle.model_dump(mode="json")["retrieved_at"],
        })

    @staticmethod
    def _common_terms(sources: Sequence[SourceEvidence]) -> list[str]:
        token_sets = []
        for source in sources:
            token_sets.append({token.lower() for token in re.findall(r"[\w\u0400-\u04ff]{3,}", f"{source.title} {source.excerpt}")})
        if not token_sets:
            return []
        return sorted(set.intersection(*token_sets))[:20]

    async def find_novel_topics(
        self,
        *,
        workspace_id: Any,
        conversation_id: Any,
        channel_id: int,
        topics: Sequence[str] = (),
        instruction: str = "",
        recent_posts: Sequence[str] = (),
        channel_evidence_ids: Sequence[int] = (),
        channel_evidence: Sequence[dict[str, Any]] = (),
        categories: Sequence[str] = (),
        domains: Sequence[str] = (),
    ) -> dict[str, Any]:
        requested = _clean_instruction(instruction, 500)
        await self._ensure_loaded(
            workspace_id=workspace_id, conversation_id=conversation_id, channel_id=channel_id,
        )
        base_topics = [topic for topic in _topic_names(topics) if topic]
        queries: list[str] = [requested] if requested else []
        queries.extend(base_topics)
        if not queries:
            queries.append("emerging topics and stories")
        queries = list(dict.fromkeys(queries))[: self.max_queries]

        async def run(query: str):
            async with self._search_slots:
                return await self.provider.search(
                    query,
                    limit=limits.SEARCH_MAX_RESULTS,
                    categories=tuple(categories),
                    domains=tuple(domains),
                )

        responses = await asyncio.gather(*(run(query) for query in queries))
        values = [to_source_evidence(result) for response in responses for result in response.results]
        combined_query = " | ".join(queries)
        bundle = await self._store(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            channel_id=channel_id,
            query=combined_query,
            values=values,
            topics=base_topics,
            recent_posts=recent_posts,
            channel_evidence_ids=channel_evidence_ids,
            channel_evidence=channel_evidence,
            search_results=True,
        )
        by_url = {source.canonical_url: source for source in bundle.sources if source.canonical_url}
        selected = list({
            source.source_id: source for source in
            (by_url.get(source.canonical_url, source) for source in values)
        }.values())
        selected_ids = {source.source_id for source in selected}
        selected_stories = [story for story in bundle.stories if selected_ids.intersection(story.source_ids)][:6]
        suggestions = []
        for story in selected_stories:
            suggestions.append(
                {
                    **_story_for_agent(story),
                    "reason": f"Fits the inferred channel topics {', '.join(base_topics[:3]) or 'from the request'}; compare its linked sources before drafting.",
                }
            )
        warnings = list(bundle.warnings)
        warnings.extend(warning for response in responses for warning in response.warnings)
        outcome_summary = self.aggregate_search_outcomes(responses)
        activities = []
        for response in responses:
            activities.append(await self._record_activity(
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                channel_id=channel_id,
                event_type="find_novel_topics",
                provider=response.provider,
                query_count=1,
                result_count=len(response.results),
                cache_hit=response.cache_hit,
                degraded=response.degraded,
                trace_id=response.trace_id,
                search_outcome=str(getattr(response, "outcome", "healthy") or "healthy"),
                failed_engines=[
                    str(item.get("engine", ""))
                    for item in getattr(response, "engine_failures", ())
                    if isinstance(item, dict) and item.get("relevant")
                ],
            ))
        return _agent_result({
            "queries": queries,
            "suggestions": suggestions,
            "sources": [_source_for_agent(source) for source in selected],
            "warnings": list(dict.fromkeys(warnings)),
            "degraded": outcome_summary["outcome"] in ("partial", "unavailable"),
            "search_outcome": outcome_summary["outcome"],
            "failed_engines": outcome_summary["failed_engines"],
            "providers": sorted({response.provider for response in responses}),
            "activity": activities,
        })

    async def source_page(
        self, *, workspace_id: Any, conversation_id: Any, channel_id: int,
        source_ids: Sequence[str], offset: int = 0, limit: int = 12,
        include_content: bool = False,
    ) -> dict[str, Any]:
        state = await self._ensure_loaded(workspace_id=workspace_id, conversation_id=conversation_id, channel_id=channel_id)
        ids = list(dict.fromkeys(str(item) for item in source_ids))[:1000] if source_ids else sorted(state.sources)
        offset, limit = max(0, int(offset)), max(1, min(int(limit), 20))
        selected = ids[offset:offset + limit]
        available = {item: state.sources[item] for item in selected if item in state.sources}
        missing = [item for item in selected if item not in available]
        reader = getattr(self.repository, "get_research_sources", None)
        if missing and reader is not None:
            rows = await reader(conversation_id=conversation_id, channel_id=channel_id, source_ids=missing)
            for row in rows:
                source = to_source_evidence(row)
                available[source.source_id] = source
        sources = [available[item] for item in selected if item in available]
        payload = _agent_result({
            "sources": [dict(_source_for_agent(source), **({"content": source.content[:3500]} if include_content and source.content else {})) for source in sources],
            "selected_source_ids": selected,
        })
        payload["next_offset"] = offset + limit if offset + limit < len(ids) else None
        payload["unknown_source_ids"] = [item for item in selected if item not in available]
        if not source_ids:
            payload["catalog_source_count"] = len(state.sources)
            payload["catalog_read_source_count"] = sum(bool(source.content and source.accessible and source.status == "ok") for source in state.sources.values())
        if payload["unknown_source_ids"]:
            payload["continuation"] = "Use get_research_sources with source_ids=[] to browse this conversation's stored IDs; include_content=true returns already-read text."
        return payload

    async def get_source(self, *, workspace_id: Any, conversation_id: Any, channel_id: int, source_id: str) -> SourceEvidence | None:
        state = await self._ensure_loaded(workspace_id=workspace_id, conversation_id=conversation_id, channel_id=channel_id)
        return state.sources.get(str(source_id))

    async def get_bundle(self, *, workspace_id: Any, conversation_id: Any, channel_id: int) -> ResearchBundle | None:
        state = await self._ensure_loaded(workspace_id=workspace_id, conversation_id=conversation_id, channel_id=channel_id)
        return state.bundle
