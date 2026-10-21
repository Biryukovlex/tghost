"""Compact web research results for the chat agent.

Chat products keep web search affordable by returning short results: a search
gives a ranked list of snippets, and reading a page gives the passages most
relevant to what is being looked for, with a way to read more. Before this,
each search or read returned 40-48k characters, and a dozen calls exhausted the
run's input budget. The research service still stores everything; only what
the model sees is shortened. Ranking uses the local embedding model and falls
back to provider or page order when the model is not installed.
"""

from __future__ import annotations

from typing import Any, Sequence

from .. import limits
from .embeddings import embed, rank


def split_passages(text: str, *, target: int | None = None, cap: int | None = None) -> list[str]:
    """Split page text into passages of roughly `target` characters on line and sentence boundaries."""
    target = int(target or limits.PASSAGE_TARGET_CHARS)
    cap = int(cap or target * 3 // 2)
    out: list[str] = []
    buf = ""
    for line in (line.strip() for line in str(text or "").split("\n")):
        if not line:
            continue
        while len(line) > cap:
            cut = line.rfind(". ", 0, cap)
            cut = cut + 1 if cut > target // 2 else cap
            if buf:
                out.append(buf)
                buf = ""
            out.append(line[:cut].strip())
            line = line[cut:].strip()
        buf = f"{buf} {line}".strip() if buf else line
        if len(buf) >= target:
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return [passage for passage in out if len(passage) > 40]


async def ranked_passages(text: str, focus: str, *, offset: int = 0, budget: int | None = None) -> dict[str, Any]:
    """The passages most relevant to `focus`, within a character budget, shown in page order."""
    budget = int(budget or limits.READ_CHARS_PER_PAGE)
    passages = split_passages(text)
    order = await rank(focus, passages) if focus and passages else None
    ranked_by = "relevance to the focus" if order is not None else "page order"
    order = order if order is not None else list(range(len(passages)))
    chosen: list[int] = []
    used = 0
    position = max(0, int(offset))
    while position < len(order):
        index = order[position]
        size = len(passages[index])
        if chosen and used + size > budget:
            break
        chosen.append(index)
        used += size
        position += 1
    return {
        # Shown in page order; rank 1 is the passage most relevant to the focus.
        "passages": [{"n": index + 1, "rank": chosen.index(index) + 1, "text": passages[index]} for index in sorted(chosen)],
        "passages_total": len(passages),
        "ranked_by": ranked_by,
        "next_passage_offset": position if position < len(order) else None,
    }


def shrink(value: Any, *, depth: int = 0) -> Any:
    """Generic compact projection: short strings, short lists, shallow nesting; keys and IDs stay."""
    if isinstance(value, str):
        return value if len(value) <= 300 else value[:299].rstrip() + "…"
    if isinstance(value, list):
        return [shrink(item, depth=depth + 1) for item in value[:10]]
    if isinstance(value, dict):
        if depth > 4:
            return {"omitted": True}
        return {key: shrink(item, depth=depth + 1) for key, item in value.items() if key != "channel_evidence"}
    return value


def _date(value: Any) -> str | None:
    return str(value)[:10] if value else None


async def compact_search(result: dict[str, Any], queries: Sequence[str]) -> dict[str, Any]:
    """A short ranked list of search results instead of the full research payload."""
    sources = [source for source in result.get("sources") or [] if source.get("url")]
    texts = [f"{source.get('title') or ''}. {source.get('excerpt') or ''}" for source in sources]
    order = list(range(len(sources)))
    wanted = [query for query in queries if query][:4]
    if sources and wanted:
        vectors = await embed([*wanted, *texts])
        if vectors is not None:
            scores = (vectors[len(wanted):] @ vectors[:len(wanted)].T).max(axis=1)
            order = [int(index) for index in (-scores).argsort(kind="stable")]
    shown = [sources[index] for index in order[: int(limits.SEARCH_RESULTS_SHOWN)]]
    outcome = result.get("search_outcome") or result.get("status")
    compact: dict[str, Any] = {
        "search_outcome": outcome,
        "results": [{
            "source_id": source.get("source_id"),
            "title": str(source.get("title") or "")[:160],
            "url": source.get("url"),
            "domain": source.get("domain"),
            "published": _date(source.get("published_at")),
            "snippet": str(source.get("excerpt") or "")[: int(limits.SEARCH_SNIPPET_CHARS)],
        } for source in shown],
        "more_results_found": max(0, len(sources) - len(shown)),
        "next": "Read the most promising pages with read_sources(urls, focus) before relying on details.",
    }
    if outcome in {"partial", "unavailable"}:
        compact["failed_engines"] = list(result.get("failed_engines") or [])[:6]
    warnings = list(result.get("warnings") or [])[:3]
    if warnings:
        compact["warnings"] = warnings
    if result.get("status") == "blocked":
        compact["status"] = "blocked"
        compact["error"] = result.get("error")
    return compact
