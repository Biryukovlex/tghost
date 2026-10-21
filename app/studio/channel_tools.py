"""Compact, page-able channel tools and the data-derived style brief.

The performance overview used to return the whole analytics model (about
64,000 characters, excerpts flattened to 600 characters, formatting lost).
Weak models then had no room left to work. These tools follow one contract:
summary statistics, the top posts as Markdown, an index of further posts, and
drill-down tools for anything else. Every number is computed from the
channel's own posts; nothing about a particular channel is assumed.
"""

from __future__ import annotations

import re
import statistics
from datetime import date, datetime
from typing import Any, Literal

from pydantic_ai import RunContext
from telethon.utils import add_surrogate, del_surrogate

from .. import limits
from ..telegram_formatting import normalize_entities
from .analytics import analyze_posts_async

_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐✅❌]")
_BULLET = re.compile(r"^\s*([-•▪▫◾◽🔹🔸➖—]|\d+[.)])\s")
_WRAP = {"bold": ("**", "**"), "italic": ("*", "*"), "strike": ("~~", "~~"), "code": ("`", "`")}


def to_markdown(text: str, entities: Any) -> str:
    """Render a Telegram post in the Studio Markdown dialect, keeping its formatting."""

    if not text:
        return ""
    source = add_surrogate(text)
    opens: dict[int, list[str]] = {}
    closes: dict[int, list[str]] = {}
    for entity in sorted(normalize_entities(entities), key=lambda e: (int(e.get("offset", 0)), -int(e.get("length", 0)))):
        start = int(entity.get("offset", 0))
        end = start + int(entity.get("length", 0))
        if start < 0 or end > len(source) or end <= start:
            continue
        kind = str(entity.get("type", ""))
        if kind == "text_url" and entity.get("url"):
            opening, closing = "[", f"]({entity['url']})"
        elif kind in _WRAP:
            opening, closing = _WRAP[kind]
        else:
            continue
        opens.setdefault(start, []).append(opening)
        closes.setdefault(end, []).insert(0, closing)
    out: list[str] = []
    for index in range(len(source) + 1):
        out.extend(closes.get(index, []))
        out.extend(opens.get(index, []))
        if index < len(source):
            out.append(source[index])
    return del_surrogate("".join(out))


def markdown_features(markdown: str) -> dict[str, Any]:
    """Measurable form of a post written in Studio Markdown."""

    lines = [line for line in (markdown or "").split("\n") if line.strip()]
    plain = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", markdown or "")
    plain = re.sub(r"[*_~`]", "", plain)
    first = lines[0].strip() if lines else ""
    return {
        "length": len(plain),
        "paragraphs": len([block for block in (markdown or "").split("\n\n") if block.strip()]),
        "bold_first_line": bool(re.match(r"^\W{0,3}\*\*", first)),
        "bullet_lines": sum(1 for line in lines if _BULLET.match(line)),
        "emoji_per_1k_chars": round(1000 * len(_EMOJI.findall(markdown or "")) / max(1, len(plain)), 2),
        "inline_links": len(re.findall(r"\]\(https?://", markdown or "")) + len(re.findall(r"(?<![(\[])https?://\S+", markdown or "")),
    }


def post_markdown(row: dict[str, Any]) -> str:
    return to_markdown(str(row.get("text") or ""), row.get("formatting_entities"))


def telegram_link(identifier: str | None, message_id: Any) -> str | None:
    handle = str(identifier or "").lstrip("@")
    if not handle or message_id is None or handle.lstrip("-").isdigit():
        return None
    return f"https://t.me/{handle}/{message_id}"


def index_row(row: dict[str, Any], identifier: str | None) -> dict[str, Any]:
    first = next((line.strip() for line in str(row.get("text") or "").split("\n") if line.strip()), "")[:120]
    posted = row.get("posted_at")
    return {"post_id": row["post_id"], "link": telegram_link(identifier, row.get("message_id")),
            "posted_at": posted.date().isoformat() if isinstance(posted, datetime) else (
                posted.isoformat() if isinstance(posted, date) else (str(posted)[:10] if posted else None)),
            "first_line": first, "views": row.get("views"), "reactions": row.get("reactions"),
            "comments": row.get("comments"), "shares": row.get("shares")}


async def ranked_posts(repository, channel_id: int) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """Posts with text, the channel row, and posts ordered by the analytics score (best first)."""

    rows = await repository.performance_rows(channel_id)
    raw = await repository.channel_context(channel_id)
    analysis = await analyze_posts_async(rows, channel_id, identifier=raw.get("identifier"))
    texted = [row for row in rows if str(row.get("text") or "").strip()]
    by_id = {row["post_id"]: row for row in texted}
    order: list[int] = []
    for post in [*analysis.top_posts, *analysis.evidence_posts]:
        if post.post_id in by_id and post.post_id not in order:
            order.append(post.post_id)
    ranked = [by_id[post_id] for post_id in order]
    ranked += sorted((row for row in texted if row["post_id"] not in set(order)),
                     key=lambda row: int(row.get("views") or 0), reverse=True)
    return texted, raw, ranked


def _median(values: list[float]) -> float:
    return round(statistics.median(values), 2) if values else 0


def _group_profile(group: list[dict[str, Any]]) -> dict[str, Any]:
    features = [markdown_features(post_markdown(row)) for row in group]
    count = max(1, len(features))
    return {"posts": len(group),
            "median_length": int(_median([f["length"] for f in features])),
            "median_paragraphs": int(_median([f["paragraphs"] for f in features])),
            "share_bold_first_line": round(sum(f["bold_first_line"] for f in features) / count, 2),
            "share_with_bullets": round(sum(f["bullet_lines"] >= 2 for f in features) / count, 2),
            "median_emoji_per_1k_chars": _median([f["emoji_per_1k_chars"] for f in features]),
            "share_with_inline_links": round(sum(f["inline_links"] > 0 for f in features) / count, 2),
            "median_views": int(_median([float(row.get("views") or 0) for row in group])),
            "median_shares": int(_median([float(row.get("shares") or 0) for row in group]))}


def summary_stats(rows: list[dict[str, Any]], top_ids: list[int]) -> dict[str, Any]:
    top_set = set(top_ids)
    dates = [row["posted_at"] for row in rows if hasattr(row.get("posted_at"), "date")]
    views = sorted(float(row.get("views") or 0) for row in rows)
    return {"posts_with_text": len(rows),
            "period": {"from": min(dates).date().isoformat() if dates else None, "to": max(dates).date().isoformat() if dates else None},
            "views": {"median": int(_median(views)), "p90": int(views[min(len(views) - 1, int(0.9 * len(views)))]) if views else 0},
            "top_posts_vs_rest": {"top": _group_profile([r for r in rows if r["post_id"] in top_set]),
                                  "rest": _group_profile([r for r in rows if r["post_id"] not in top_set])}}


def style_brief(ranked: list[dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Measured style targets from the channel's best posts, for the writer and the checks."""

    top = ranked[: int(limits.TOP_POSTS)]
    stats = summary_stats(rows, [row["post_id"] for row in top])
    lengths = sorted(markdown_features(post_markdown(row))["length"] for row in top)
    # Drop the most extreme top post on each side when there are enough posts.
    robust = lengths[1:-1] if len(lengths) >= 5 else lengths
    target = [robust[0], robust[-1]] if robust else [0, 0]
    exemplars = [post_markdown(row)[:2_500] for row in top[:3]]
    return {"target_length": target, "top_vs_rest": stats["top_posts_vs_rest"], "exemplars": exemplars}


def opening(markdown: str) -> dict[str, Any]:
    """The start of a post, enough to show its headline and formatting, and its full length."""
    size = int(limits.TOP_POST_OPENING_CHARS)
    text = markdown if len(markdown) <= size else markdown[:size].rstrip() + "…"
    return {"opening": text, "length": len(markdown)}


def register_channel_tools(agent, check_cancel) -> None:
    """Compact performance overview plus drill-down tools for the authenticated channel."""

    next_hint = ("Use read_channel_posts(post_ids) for the full text of any listed or top post and "
                 "list_channel_posts(sort, offset, limit) to page through all posts.")

    @agent.tool
    async def get_performance_evidence(ctx: RunContext[Any]) -> dict[str, Any]:
        """Channel performance overview: statistics, how the top posts differ from the rest
        (length, bold headline, bullets, emoji, links), the opening of each top post in Markdown
        and an index of further posts. Takes no arguments; the app supplies the channel."""

        check_cancel(ctx)
        rows, raw, ranked = await ranked_posts(ctx.deps.repository, ctx.deps.channel_id)
        identifier = raw.get("identifier")
        top = ranked[: int(limits.TOP_POSTS)]
        ctx.deps.completed_tools.add("get_performance_evidence")
        return {"summary": summary_stats(rows, [row["post_id"] for row in top]),
                "top_posts": [{**index_row(row, identifier), **opening(post_markdown(row))} for row in top],
                "more_posts": [index_row(row, identifier) for row in ranked[len(top): len(top) + 12]],
                "ranking": "Posts are ordered by the channel analytics score (views, shares, reactions and comments, adjusted for post age).",
                "next": next_hint}

    @agent.tool
    async def read_channel_posts(ctx: RunContext[Any], post_ids: list[int]) -> dict[str, Any]:
        """Read up to 5 posts of this channel in full Markdown, with metrics and links.

        Args:
            post_ids: post_id values from get_performance_evidence or list_channel_posts.
        """
        check_cancel(ctx)
        rows = await ctx.deps.repository.performance_rows(ctx.deps.channel_id)
        raw = await ctx.deps.repository.channel_context(ctx.deps.channel_id)
        by_id = {row["post_id"]: row for row in rows}
        wanted = [int(post_id) for post_id in (post_ids or [])][:5]
        return {"posts": [{**index_row(by_id[post_id], raw.get("identifier")),
                           "text": post_markdown(by_id[post_id])[: int(limits.POST_TEXT_MAX_CHARS)]}
                          for post_id in wanted if post_id in by_id],
                "missing": [post_id for post_id in wanted if post_id not in by_id]}

    @agent.tool
    async def list_channel_posts(ctx: RunContext[Any], sort: Literal["views", "shares", "reactions", "comments", "date"] = "views",
                                 offset: int = 0, limit: int = 10) -> dict[str, Any]:
        """Page through this channel's posts as an index (first line, metrics, link), without full text.

        Args:
            sort: views, shares, reactions, comments, or date (newest first).
            offset: Number of posts to skip.
            limit: Posts per page, at most 20.
        """
        check_cancel(ctx)
        rows = await ctx.deps.repository.performance_rows(ctx.deps.channel_id)
        raw = await ctx.deps.repository.channel_context(ctx.deps.channel_id)
        if sort == "date":
            ordered = sorted(rows, key=lambda row: str(row.get("posted_at") or ""), reverse=True)
        else:
            ordered = sorted(rows, key=lambda row: int(row.get(sort) or 0), reverse=True)
        size = max(1, min(int(limit), int(limits.CHANNEL_INDEX_PAGE)))
        start = max(0, int(offset))
        following = start + size
        return {"total": len(rows), "offset": start,
                "items": [index_row(row, raw.get("identifier")) for row in ordered[start:following]],
                "next_offset": following if following < len(rows) else None}
