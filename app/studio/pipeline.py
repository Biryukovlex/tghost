"""Post pipeline and draft editor: fixed steps run by code, AI for judgment.

Measured on a real channel copy, a single free-form agent asked to "find a new
story and write a post" never saved a draft (0 of 12): it searched and browsed
until a limit stopped it. Splitting the job into small steps that code runs in
order, with the model answering one narrow question per step, saved 17 of 18
posts at about a sixth of the tokens. This module provides that flow as two
tools for the chat agent:

* ``write_post`` turns the owner's request into a saved draft. The chat agent
  fills structured fields (links, notes, research level); code starts at the
  first step whose input is missing, so a link skips search and the owner's
  notes skip research entirely.
* ``revise_draft`` hands a rewrite to a small editor whose draft tools report
  lengths, so it can check an edit against the request.

Owner material is never refused: novelty and age only produce warnings. Any
time window comes from the owner's system prompt or message, never from code.
"""

from __future__ import annotations

import asyncio
import math
import re
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field
from pydantic_ai import Agent, ModelRetry, RunContext, UnexpectedModelBehavior, UsageLimitExceeded, UsageLimits
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

from .. import limits
from .channel_tools import markdown_features, post_markdown, ranked_posts, style_brief
from .context import profile_block_from_mapping
from .draft_tools import register_draft_tools
from .drafts import DraftContent, DraftConflictError, DraftValidationError
from .effort import full_settings, light_settings
from .novelty import ArchiveCorpus, copied_fragments, signature_lines
from .search import canonicalize_url
from .tool_args import ToolArgumentBoundary, normalize_value

LenientList = Annotated[list[str], BeforeValidator(lambda value: normalize_value(value, {"type": "array"}))]


class Window(BaseModel):
    amount: int = Field(ge=1, description="Number of units back from today.")
    unit: Literal["hours", "days", "weeks"] = Field(description="Unit exactly as the owner's instructions use it.")


class SearchPlan(BaseModel):
    queries: LenientList = Field(description="3-8 short web search queries; mix languages when useful.")
    window: Window | None = Field(default=None, description="Time window only if the owner's instructions or request set one; otherwise null.")


class PagePicks(BaseModel):
    urls: LenientList = Field(description="Up to 6 URLs from the list, most promising first.")


class Story(BaseModel):
    headline: str
    summary: str = Field(description="2-4 sentences with the concrete facts and numbers, in the channel's language.")
    why_it_fits: str = Field(default="", description="Why this suits the channel's audience.")
    source_urls: LenientList = Field(description="URLs of the pages above that support the story.")
    published: str | None = Field(default=None, description="Publication date of the story, YYYY-MM-DD, if the pages show it.")


def _stories(value: Any) -> Any:
    value = normalize_value(value, {"type": "array"})
    return value


class Stories(BaseModel):
    stories: Annotated[list[Story], BeforeValidator(_stories)]


class PostText(BaseModel):
    title: str = Field(description="Short internal name for the draft.")
    body: str = Field(description="The complete post in the channel's Markdown dialect.")


class PipelineError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _retry_after(exc: Exception, attempt: int) -> float:
    """Seconds to wait before retrying a rate-limited step: the provider's hint, else exponential backoff."""
    body = getattr(exc, "body", None)
    hint = None
    if isinstance(body, dict):
        metadata = body.get("metadata") or (body.get("error") or {}).get("metadata") or {}
        headers = metadata.get("headers") if isinstance(metadata, dict) else None
        if isinstance(headers, dict):
            hint = headers.get("Retry-After") or headers.get("retry-after")
    try:
        seconds = float(hint) if hint is not None else None
    except (TypeError, ValueError):
        seconds = None
    base = float(limits.PIPELINE_RATE_LIMIT_BACKOFF_SECONDS) * (2 ** attempt)
    return max(1.0, min(seconds if seconds is not None else base, 60.0))


def _check_cancel(ctx: RunContext[Any]) -> None:
    if ctx.deps.cancel_event.is_set():
        raise asyncio.CancelledError


def _window_days(window: Window | None) -> int | None:
    if window is None:
        return None
    hours = window.amount * {"hours": 1, "days": 24, "weeks": 168}[window.unit]
    return max(1, math.ceil(hours / 24))


def _same_url(left: str, right: str) -> bool:
    return (canonicalize_url(left) or left).rstrip("/") == (canonicalize_url(right) or right).rstrip("/")


class _Job:
    """State of one write_post or revise_draft call."""

    def __init__(self, ctx: RunContext[Any], settings) -> None:
        self.ctx = ctx
        self.settings = settings
        # The run's own model, typed loosely: sub-agents accept any model object.
        self.model: Any = ctx.model
        self.deps = ctx.deps
        self.steps: dict[str, dict[str, Any]] = {}
        self.warnings: list[str] = []
        self.started_requests = ctx.usage.requests
        self.current_step = "prepare"

    def _limits(self) -> UsageLimits:
        allowed = self.started_requests + int(limits.PIPELINE_MAX_REQUESTS)
        return UsageLimits(request_limit=max(self.ctx.usage.requests + 1, allowed))

    async def ask(self, step: str, output_type, instructions: str, prompt: str, *, light: bool,
                  validator=None, tools=None, tool_calls: int = 0):
        _check_cancel(self.ctx)
        self.current_step = step
        agent = Agent(self.model, output_type=output_type, deps_type=type(self.deps), retries=2,
                      instructions=instructions, capabilities=[ToolArgumentBoundary()],
                      model_settings=light_settings(self.model) if light else full_settings(self.model))
        if validator is not None:
            agent.output_validator(validator)
        if tools:
            tools(agent)
        before = (self.ctx.usage.requests, self.ctx.usage.input_tokens, self.ctx.usage.output_tokens)
        started = asyncio.get_running_loop().time()
        usage_limits = self._limits()
        if tool_calls:
            usage_limits = UsageLimits(request_limit=usage_limits.request_limit, tool_calls_limit=self.ctx.usage.tool_calls + tool_calls)
        result = None
        for attempt in range(int(limits.PIPELINE_RATE_LIMIT_RETRIES) + 1):
            try:
                result = await agent.run(prompt, deps=self.deps, usage=self.ctx.usage, usage_limits=usage_limits)
                break
            except ModelHTTPError as exc:
                # 429 means "retry later": wait as the provider asks, a bounded number of times.
                if exc.status_code != 429 or attempt >= int(limits.PIPELINE_RATE_LIMIT_RETRIES):
                    raise
                await asyncio.sleep(_retry_after(exc, attempt))
                _check_cancel(self.ctx)
        if hasattr(self.deps, "pipeline_input_tokens"):
            self.deps.pipeline_input_tokens += self.ctx.usage.input_tokens - before[1]
        self.steps[step] = {"requests": self.ctx.usage.requests - before[0], "input_tokens": self.ctx.usage.input_tokens - before[1],
                            "output_tokens": self.ctx.usage.output_tokens - before[2],
                            "seconds": round(asyncio.get_running_loop().time() - started, 1)}
        assert result is not None  # the retry loop either assigns a result or raises
        return result.output


async def _channel_material(job: _Job) -> dict[str, Any]:
    repository, channel_id = job.deps.repository, job.deps.channel_id
    rows, raw, ranked = await ranked_posts(repository, channel_id)
    getter = getattr(repository, "get_profile", None)
    profile = await getter(channel_id) if getter is not None else None
    prompt_getter = getattr(repository, "get_system_prompt", None)
    owner_prompt = await prompt_getter(channel_id) if prompt_getter is not None else ""
    newest = sorted(rows, key=lambda row: str(row.get("posted_at") or ""), reverse=True)
    archive = [post_markdown(row) for row in newest[: int(limits.NOVELTY_MAX_POSTS)]]
    drafts_reader = getattr(repository, "recent_draft_bodies", None)
    drafts = await drafts_reader(channel_id, limit=100) if drafts_reader is not None else []
    other_drafts = [item["body"] for item in drafts if str(item.get("conversation_id")) != str(job.deps.conversation_id)]
    corpus = ArchiveCorpus(archive + other_drafts)
    return {
        "rows": rows, "ranked": ranked, "profile": profile_block_from_mapping(profile) if profile else "",
        "owner_prompt": owner_prompt or "", "archive": archive, "corpus": corpus,
        "covered_threshold": ArchiveCorpus(archive).covered_threshold(),
        "covered": [next((line.strip() for line in str(row.get("text") or "").split("\n") if line.strip()), "")[:110] for row in newest[:60]],
    }


def _context_prompt(material: dict[str, Any], request: str) -> str:
    covered = "\n".join(f"- {line}" for line in material["covered"] if line)
    return (f"Today is {datetime.now(timezone.utc).date().isoformat()} (UTC).\n\n"
            f"Channel owner's standing instructions:\n{material['owner_prompt'] or '(none)'}\n\n"
            f"Channel profile:\n{material['profile'][:2500] or '(not built yet)'}\n\n"
            f"Already covered by the channel (first lines of recent posts, newest first):\n{covered}\n\n"
            f"Owner's current request (it takes priority over the standing instructions):\n{request}")


async def _search(job: _Job, plan: SearchPlan, *, window: bool) -> tuple[list[dict[str, Any]], str]:
    queries = [query.strip() for query in plan.queries if query and query.strip()][: int(limits.PIPELINE_MAX_QUERIES)]
    if not queries:
        return [], "empty"
    result = await job.deps.research.search(
        workspace_id=job.deps.workspace_id, conversation_id=job.deps.conversation_id, channel_id=job.deps.channel_id,
        query=queries[0], alternate_queries=queries[1:], recency_days=_window_days(plan.window) if window else None,
    )
    result = result if isinstance(result, dict) else result.model_dump(mode="json")
    job.deps.completed_tools.add("search_web")
    return list(result.get("sources") or []), str(result.get("search_outcome") or "healthy")


async def _read(job: _Job, urls: list[str]) -> list[dict[str, Any]]:
    if not urls:
        return []
    # Read without attaching channel evidence: the pipeline needs page text only.
    result = await job.deps.research.read_sources(
        workspace_id=job.deps.workspace_id, conversation_id=job.deps.conversation_id, channel_id=job.deps.channel_id,
        urls=urls[: int(limits.PIPELINE_MAX_PAGES)],
    )
    job.deps.completed_tools.add("read_sources")
    documents = []
    for document in result.get("documents") or []:
        text = str(document.get("text") or "")
        if document.get("status") == "ok" and len(text.strip()) > 200:
            documents.append({"url": document.get("url"), "title": document.get("title"),
                              "published_at": document.get("published_at"), "text": text[: int(limits.PIPELINE_PAGE_CHARS)]})
    return documents


async def _find_pages(job: _Job, material: dict[str, Any], request: str, *, drop_covered: bool, use_window: bool,
                      focus: str = "") -> tuple[list[dict[str, Any]], str]:
    """Plan queries, search, filter covered stories, pick pages and read them."""
    context = _context_prompt(material, request) + (f"\n\nFocus: {focus}" if focus else "")
    plan = await job.ask("plan", SearchPlan, (
        "You plan web searches for a Telegram channel's news researcher. Return 3-8 short queries that would surface stories "
        "for this channel's audience. Take any time window from the owner's instructions or request in the owner's own unit; "
        "do not invent one."), context, light=True)
    sources, outcome = await _search(job, plan, window=use_window)
    job.steps["search"] = {"results": len(sources), "outcome": outcome,
                           "window_days": _window_days(plan.window) if use_window else None}
    if not sources:
        return [], outcome
    listed = []
    for source in sources:
        text = f"{source.get('title') or ''} {source.get('excerpt') or ''}"
        if drop_covered and material["corpus"].max_similarity(text) > material["covered_threshold"]:
            continue
        listed.append(source)
    job.steps["search"]["after_novelty_filter"] = len(listed)
    if not listed:
        return [], "covered"
    listed = listed[: int(limits.PIPELINE_MAX_LISTED)]
    lines = "\n".join(f"- {s.get('url')} | {str(s.get('published_at') or '')[:10]} | {str(s.get('title') or '')[:120]} | "
                      f"{str(s.get('excerpt') or '')[:200]}" for s in listed)
    picks = await job.ask("pick", PagePicks, (
        "Pick up to 6 pages worth reading for the channel: concrete, fitting its topics and the owner's request. Return their URLs."),
        context + "\n\nSearch results (url | date | title | snippet):\n" + lines, light=True)
    known = [str(s.get("url")) for s in listed]
    urls = [url for url in picks.urls if any(_same_url(url, other) for other in known)][: int(limits.PIPELINE_MAX_PAGES)]
    return await _read(job, urls or known[: int(limits.PIPELINE_MAX_PAGES)]), outcome


async def _extract(job: _Job, material: dict[str, Any], request: str, documents: list[dict[str, Any]]) -> list[Story]:
    read_urls = [str(document["url"]) for document in documents]

    def must_cite_read_page(output: Stories) -> Stories:
        cited = [story for story in output.stories if any(_same_url(url, read) for url in story.source_urls for read in read_urls)]
        if not cited:
            raise ModelRetry("Return at least one story that cites one of these page URLs: " + ", ".join(read_urls))
        return Stories(stories=cited)

    pages = "\n\n".join(f"PAGE {d['url']}\nTITLE {d.get('title') or ''}\nPUBLISHED {d.get('published_at') or 'unknown'}\n{d['text']}"
                        for d in documents)
    result = await job.ask("extract", Stories, (
        "From the pages below, return 1-5 story candidates for the channel, best first. Cite only URLs of these pages. "
        "Write headline and summary in the language of the channel's posts."),
        _context_prompt(material, request) + "\n\nPages:\n" + pages, light=False, validator=must_cite_read_page)
    return result.stories


def _choose(material: dict[str, Any], stories: list[Story], *, owner_material: bool) -> tuple[Story, float, bool]:
    scored = [(material["corpus"].max_similarity(f"{story.headline} {story.summary}"), story) for story in stories]
    threshold = material["covered_threshold"]
    if owner_material:
        similarity, story = scored[0]
    else:
        similarity, story = sorted(scored, key=lambda item: (item[0] > threshold, item[0]))[0]
    return story, similarity, similarity > threshold


def _write_instructions(brief: dict[str, Any] | None = None) -> str:
    low, high = (brief or {}).get("target_length") or (0, 0)
    length = f"Keep the post between {low} and {high} characters, like the channel's top posts. " if high else ""
    return ("You are the channel's writer. Write one Telegram post that looks and sounds like the channel's best posts, in the "
            "language of those posts. Use the channel's Markdown dialect: **bold**, *italic*, ~~strike~~, `code`, [text](https://url), "
            "> quote; no headings, images or HTML. Follow the channel profile's editorial rules. Use only facts from the supplied pages "
            "or the owner's notes, and name limitations. Do not copy sentences or distinctive phrases from the channel's existing posts. "
            "Mention the story's publication date naturally where it matters; never present older news as today's. " + length)


def _style_prompt(brief: dict[str, Any]) -> str:
    exemplars = "\n\n---\n\n".join(brief["exemplars"])
    low, high = brief["target_length"]
    return (f"Measured from the channel's own data, the top posts versus the rest: {brief['top_vs_rest']}.\n"
            f"Target length: {low}-{high} characters.\n\n"
            f"Three top-performing posts (study the form only, never reuse their sentences):\n{exemplars}")


async def _write_and_check(job: _Job, material: dict[str, Any], request: str, story: Story | None,
                           documents: list[dict[str, Any]], notes: str | None) -> PostText:
    brief = style_brief(material["ranked"], material["rows"])
    material["brief"] = brief
    evidence = []
    cited = {url for url in (story.source_urls if story else [])}
    for document in documents:
        if not story or any(_same_url(document["url"], url) for url in cited):
            evidence.append(f"PAGE {document['url']} (published {document.get('published_at') or 'unknown'})\n{document['text']}")
    if notes:
        evidence.append("OWNER'S NOTES (the base of the post: keep the owner's thesis; present the owner's claims as the author's "
                        "opinion, and use only facts from the pages above, if any)\n" + notes)
    prompt = (f"Today is {datetime.now(timezone.utc).date().isoformat()} (UTC).\nOwner's request: {request}\n\n"
              f"Channel profile:\n{material['profile'] or '(not built yet)'}\n\n{_style_prompt(brief)}\n\n"
              + (f"Story to write about:\n{story.model_dump_json()}\n\n" if story else "")
              + "\n\n".join(evidence))
    post = await job.ask("write", PostText, _write_instructions(brief), prompt, light=False)
    issues = _post_issues(material, post.body)
    job.steps["checks"] = {"issues": issues}
    if issues:
        # The fix improves a complete post; if the model fails here, keep the written post.
        try:
            # Fixing named issues is a mechanical edit: low reasoning effort keeps it fast and within the output cap.
            post = await job.ask("fix", PostText, _write_instructions(brief) + " Fix only the listed issues and keep everything else.",
                                 prompt + "\n\nYOUR DRAFT:\n" + post.body + "\n\nISSUES TO FIX:\n- " + "\n- ".join(issues), light=True)
        except (UnexpectedModelBehavior, ModelAPIError, UsageLimitExceeded) as exc:
            job.warnings.append(f"The fix step failed ({_describe(exc)}); the written draft was saved unfixed.")
        job.steps["checks"]["after_fix"] = _post_issues(material, post.body)
    return post


def _post_issues(material: dict[str, Any], body: str) -> list[str]:
    issues = []
    brief = material["brief"]
    tops = sorted(markdown_features(text)["length"] for text in brief["exemplars"]) if brief["exemplars"] else []
    low, high = brief["target_length"]
    low, high = (min([low, *tops]), max([high, *tops])) if tops else (low, high)
    length = markdown_features(body)["length"]
    if high and not low <= length <= high:
        target = high if length > high else low
        verb = "Cut" if length > high else "Extend"
        issues.append(f"The post is {length} characters; the channel's top posts are {low}-{high}. "
                      f"{verb} it by at least {abs(length - target)} characters, to about {target}, "
                      f"{'removing the least important sentences' if length > high else 'adding substance, not filler'}.")
    copied = copied_fragments(body, material["archive"], words=int(limits.COPY_SHINGLE_WORDS),
                              ignore=signature_lines(material["archive"]))
    if copied:
        issues.append("These fragments repeat the channel's existing posts word for word; rewrite them: " + " | ".join(copied[:5]))
    return issues


async def _save(job: _Job, post: PostText, links: list[str]) -> dict[str, Any]:
    repository, deps = job.deps.repository, job.deps
    current = await repository.get_current_draft(conversation_id=deps.conversation_id, channel_id=deps.channel_id)
    content = DraftContent(title=post.title.strip()[:200] or "Post", body=post.body, source_links=list(dict.fromkeys(links))[:10],
                           media_content=[str(item) for item in ((current or {}).get("media_ids") or [])])
    if current is None:
        row = await repository.create_draft(conversation_id=deps.conversation_id, channel_id=deps.channel_id,
                                            payload=content.storage_payload(), origin="generated")
        deps.completed_tools.add("create_draft")
    else:
        row = await repository.update_draft(draft_id=current["id"], payload=content.storage_payload(),
                                            expected_revision=current.get("revision"), origin="regenerated", new_version=True)
        deps.completed_tools.add("edit_draft")
    return {"version": int(row.get("current_version", row.get("version", 1))), "revision": row.get("revision"),
            "title": content.title, "body_chars": len(content.body), "source_links": content.source_links,
            "opening": content.body[:400]}


def _owner_links(job: _Job, links: list[str], stored: list[str]) -> tuple[list[str], list[str]]:
    """A link is the owner's only if it appears in the owner's messages or in this chat's stored sources."""
    owner_text = "\n".join(getattr(job.deps, "owner_messages", ()) or ())
    accepted, rejected = [], []
    for link in links:
        link = str(link or "").strip()
        if not re.match(r"https?://", link):
            continue
        in_messages = link.rstrip("/") in owner_text or any(_same_url(link, found) for found in re.findall(r"https?://\S+", owner_text))
        if in_messages or any(_same_url(link, url) for url in stored):
            accepted.append(link)
        else:
            rejected.append(link)
    return accepted, rejected


async def write_post_job(job: _Job, *, research: str, links: list[str], notes: str | None,
                         topic: str | None) -> dict[str, Any]:
    settings = job.settings
    if job.deps.research is None:
        from .research import ResearchService
        job.deps.research = ResearchService(settings, repository=job.deps.repository)
    request = (getattr(job.deps, "owner_messages", ()) or ("",))[-1] or (notes or topic or "")
    material = await _channel_material(job)
    bundle = await job.deps.research.get_bundle(workspace_id=job.deps.workspace_id, conversation_id=job.deps.conversation_id,
                                                channel_id=job.deps.channel_id)
    stored = [source.url for source in (bundle.sources if bundle else []) if source.url]
    accepted, rejected = _owner_links(job, list(links or []), stored)
    if rejected:
        job.warnings.append("Ignored links that are not in the owner's messages or this chat's sources: " + ", ".join(rejected[:5]))
    owner_notes = (notes or "").strip() or None
    story: Story | None = None
    documents: list[dict[str, Any]] = []
    entry = research if research in {"none", "supporting", "discover"} else "discover"
    owner_material = bool(accepted) or entry in {"none", "supporting"}
    if accepted:
        entry = "link"
        documents = await _read(job, accepted)
        if not documents:
            job.warnings.append("The owner's link could not be read; the same story was looked up elsewhere.")
            documents, outcome = await _find_pages(job, material, request, drop_covered=False, use_window=False,
                                                   focus="Find this exact story: " + " ".join(accepted))
            if not documents and outcome == "unavailable":
                raise PipelineError("search_unavailable", "The owner's link could not be read and web search is unavailable.")
    elif entry in {"supporting", "discover"}:
        documents, outcome = await _find_pages(job, material, request, drop_covered=entry == "discover", use_window=True,
                                               focus=topic or owner_notes or "")
        if not documents and not owner_notes:
            if outcome == "unavailable":
                raise PipelineError("search_unavailable", "Web search is unavailable right now; no story could be found.")
            raise PipelineError("no_story_found", "No suitable new story was found in the search results.")
    elif not owner_notes:
        raise PipelineError("nothing_to_write", "There are no notes or links to write from.")
    if documents:
        stories = await _extract(job, material, request, documents)
        story, similarity, covered = _choose(material, stories, owner_material=owner_material)
        if covered and owner_material:
            job.warnings.append("The channel has covered a similar story; the post was written as requested.")
        elif covered:
            job.warnings.append("Every story found resembles an earlier post or draft of this channel; the least similar one was "
                                "used. Tell the owner and offer to search again with a different focus.")
        job.steps["chosen"] = {"headline": story.headline[:120], "published": story.published,
                               "resembles_earlier_post": covered}
    elif owner_notes and entry != "none":
        job.warnings.append("Research found nothing usable; the post was written from the owner's notes.")
    post = await _write_and_check(job, material, request, story, documents, owner_notes if (entry in {"none", "supporting"} or not documents) else None)
    cited = [d["url"] for d in documents if not story or any(_same_url(d["url"], url) for url in story.source_urls)]
    draft = await _save(job, post, [*accepted, *cited])
    remaining = job.steps.get("checks", {}).get("after_fix", job.steps.get("checks", {}).get("issues", []))
    if remaining:
        job.warnings.append("Checks still flag: " + " ".join(remaining)[:400])
    return {"status": "saved", "entry": entry, "draft": draft,
            "story": job.steps.get("chosen"), "warnings": job.warnings, "steps": job.steps,
            "note": ("The draft is saved and shown to the owner in the draft panel. Tell the owner in a few lines what it covers, "
                     "using the opening and story above, and report any warnings. Do not read the draft again unless asked.")}


async def revise_draft_job(job: _Job, *, instruction: str) -> dict[str, Any]:
    repository, deps = job.deps.repository, job.deps
    current = await repository.get_current_draft(conversation_id=deps.conversation_id, channel_id=deps.channel_id)
    if current is None:
        return {"status": "blocked", "error": {"code": "draft_not_found", "message": "This chat has no draft yet."}}
    getter = getattr(repository, "get_profile", None)
    profile = await getter(deps.channel_id) if getter is not None else None
    before = str(current.get("body") or "")
    edit_failure: Exception | None = None
    try:
        await job.ask("edit", str, (
            "You revise the chat's existing draft on the owner's request. Call get_draft, then save the complete new body with "
            "edit_draft. The save result reports the new length and the ratio to the old one: check it against the request and "
            "save again if it does not match. Keep the channel's Markdown dialect and language. Follow the channel profile:\n"
            + (profile_block_from_mapping(profile) if profile else "(not built yet)")),
            f"Owner's request: {instruction}", light=False, tools=register_draft_tools, tool_calls=6)
    except (UnexpectedModelBehavior, ModelAPIError, UsageLimitExceeded) as exc:
        edit_failure = exc
    # The saved draft, not the editor's closing message, decides whether the job succeeded.
    after = await repository.get_current_draft(conversation_id=deps.conversation_id, channel_id=deps.channel_id)
    body = str((after or {}).get("body") or "")
    changed = body != before
    if edit_failure is not None and not changed:
        raise edit_failure
    if edit_failure is not None:
        job.warnings.append(f"The editor's final message failed ({_describe(edit_failure)}); the saved version is complete.")
    return {"status": "edited" if changed else "unchanged",
            "draft": {"version": int((after or {}).get("current_version", 1)), "revision": (after or {}).get("revision"),
                      "body_chars": len(body), "previous_body_chars": len(before),
                      "length_ratio": round(len(body) / len(before), 2) if before else None},
            "steps": job.steps, "warnings": job.warnings,
            "note": ("The new version is shown to the owner in the draft panel. Tell the owner in a few lines what changed. "
                     "Do not read the draft again unless asked.")}


def _describe(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    return f"HTTP {status}" if status else f"{type(exc).__name__}: {str(exc)[:160]}"


def _model_failure(job: _Job, exc: Exception) -> dict[str, Any]:
    """A model failure inside a step ends the job, not the run; the chat agent explains it."""
    status = getattr(exc, "status_code", None)
    code = "model_rate_limited" if status == 429 else "model_step_failed"
    detail = _describe(exc)
    return {"status": "failed", "error": {"code": code, "step": job.current_step,
                                          "message": f"The model failed during the '{job.current_step}' step ({detail}). Saved research is kept; ask the owner to retry later."},
            "steps": job.steps}


# Job tools deliver the turn's artifact: once one succeeds, the budget gives the
# next model turn no tools, so the chat agent summarizes instead of redoing work.
# They are also deliverable: a finalizing turn that has run out of budget for
# information tools can still produce the requested post or edit.
JOB = {"completes_turn": True, "deliverable": True}


def register_pipeline_tools(agent, settings) -> None:
    async def has_draft(ctx: RunContext[Any], definition):
        row = await ctx.deps.repository.get_current_draft(conversation_id=ctx.deps.conversation_id, channel_id=ctx.deps.channel_id)
        return definition if row is not None else None

    @agent.tool(timeout=float(limits.PIPELINE_TIMEOUT_SECONDS), sequential=True, metadata=JOB)
    async def write_post(ctx: RunContext[Any], research: Literal["discover", "supporting", "none"] = "discover",
                         links: list[str] | None = None, notes: str | None = None, topic: str | None = None) -> dict[str, Any]:
        """Write and save a new post draft for this channel: research, style and checks run as fixed steps.

        Use it whenever the owner asks for a new post, whether they want a story found, give a link, or give their own notes.
        The draft is saved in this chat (a new version if a draft exists) and the result summarizes it.

        Args:
            research: discover = find a new story the channel has not covered; supporting = the owner gave a topic or idea
                that needs facts from the web; none = write only from the owner's links or notes, no web search.
            links: Links the owner gave for this post, copied exactly. Links not in the owner's messages or this chat's
                sources are ignored.
            notes: The owner's own idea, notes or opinion for the post, copied faithfully.
            topic: Optional subject that should steer a discovery search.
        """
        job = _Job(ctx, settings)
        try:
            return await write_post_job(job, research=research, links=list(links or []), notes=notes, topic=topic)
        except PipelineError as exc:
            return {"status": "failed", "error": {"code": exc.code, "message": str(exc)}, "steps": job.steps}
        except (DraftValidationError, DraftConflictError) as exc:
            return {"status": "failed", "error": {"code": getattr(exc, "code", "draft_error"), "message": str(exc)}}
        except (ModelAPIError, UnexpectedModelBehavior, UsageLimitExceeded) as exc:
            return _model_failure(job, exc)

    @agent.tool(timeout=float(limits.PIPELINE_TIMEOUT_SECONDS), sequential=True, prepare=has_draft, metadata=JOB)
    async def revise_draft(ctx: RunContext[Any], instruction: str) -> dict[str, Any]:
        """Rewrite this chat's draft on the owner's request (shorten, restructure, change tone, add or cut parts).

        A dedicated editor applies the change, checks the new length against the request and saves a new version.

        Args:
            instruction: The owner's requested change, copied faithfully.
        """
        job = _Job(ctx, settings)
        try:
            return await revise_draft_job(job, instruction=instruction)
        except (ModelAPIError, UnexpectedModelBehavior, UsageLimitExceeded) as exc:
            return _model_failure(job, exc)
