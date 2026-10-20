"""The chat's draft: create once, read any version, append edits.

Research evaluates sources independently. Saving a draft does not verify its
facts, infer a workflow, or rewrite its text. IDs and revision counters are
server metadata; the content has exactly four fields.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import ValidationError
from pydantic_ai import RunContext

from .drafts import DraftContent, DraftConflictError, DraftValidationError


def draft_content(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": row.get("title", row.get("working_title", "Untitled draft")),
        "body": row.get("body", ""),
        "source_links": list(row.get("source_links") or []),
        "media_content": list(row.get("media_ids") or []),
    }


def draft_result(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": int(row.get("current_version", row.get("version", 1))),
        "revision": row.get("revision"),
        **draft_content(row),
    }


def saved_draft_result(row: dict[str, Any], *, previous_body: str | None) -> dict[str, Any]:
    """What a save reports back: measurements, not an echo of the body.

    The body is stored exactly as supplied, so the caller already holds the
    saved text in its own tool call. Echoing it doubled the post in context on
    every save; lengths let the caller check the edit against the request.
    get_draft still returns the complete body.
    """
    result = draft_result(row)
    body = result.pop("body", "") or ""
    measured = {"body_chars": len(body)}
    if previous_body is not None:
        measured["previous_body_chars"] = len(previous_body)
        measured["length_ratio"] = round(len(body) / len(previous_body), 2) if previous_body else None
    return {**result, **measured}


# Tools that save or prepare the requested artifact; budget finalization keeps them.
DELIVERABLE = {"deliverable": True}


def register_draft_tools(agent) -> None:
    async def current(ctx):
        if ctx.deps.cancel_event.is_set():
            raise asyncio.CancelledError
        return await ctx.deps.repository.get_current_draft(
            conversation_id=ctx.deps.conversation_id, channel_id=ctx.deps.channel_id,
        )

    async def visible(ctx, definition):
        if ctx.deps.cancel_event.is_set():
            return None
        row = await current(ctx)
        if (definition.name == "create_draft") == (row is None):
            return definition
        return None

    def error(exc):
        if isinstance(exc, ValidationError):
            return {"status": "blocked", "error": {"code": "invalid_draft", "message": "Check the draft field types and limits."}}
        result = {"status": "blocked", "error": {"code": exc.code, "message": str(exc)}}
        if isinstance(exc, DraftConflictError):
            result["draft"] = draft_result(exc.current)
        return result

    @agent.tool(prepare=visible, sequential=True, metadata=DELIVERABLE)
    async def create_draft(
        ctx: RunContext[Any], title: str, body: str,
        source_links: list[str] | None = None, media_content: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create version 1 only when this chat has no draft.

        title is the artifact's name; body is the complete publication text.
        source_links are actual HTTP(S) URLs, not research source IDs.
        media_content holds uploaded image IDs; initially use [] and attach
        images through the draft panel. Text is saved exactly as supplied; the
        result reports its length instead of repeating it.
        """
        if await current(ctx) is not None:
            return {"status": "blocked", "error": {"code": "draft_exists", "message": "This chat already has a draft. Read it and use edit_draft."}}
        try:
            content = DraftContent(title=title, body=body, source_links=source_links or [], media_content=media_content or [])
            row = await ctx.deps.repository.create_draft(
                conversation_id=ctx.deps.conversation_id, channel_id=ctx.deps.channel_id,
                payload=content.storage_payload(), origin="generated",
            )
        except (DraftValidationError, DraftConflictError, ValidationError) as exc:
            return error(exc)
        ctx.deps.completed_tools.add("create_draft")
        return {"status": "created", "draft": saved_draft_result(row, previous_body=None)}

    @agent.tool(prepare=visible, metadata=DELIVERABLE)
    async def get_draft(ctx: RunContext[Any], version: int | None = None) -> dict[str, Any]:
        """Read this chat's current draft, or an older version by its number.

        Reading an old version does not restore it or modify the current draft.
        The current revision is needed for edit_draft's conflict check.
        """
        row = await current(ctx)
        if row is None:
            return {"status": "empty", "draft": None}
        if version is not None:
            versions = await ctx.deps.repository.list_draft_versions(
                draft_id=row["id"], conversation_id=ctx.deps.conversation_id, channel_id=ctx.deps.channel_id,
            )
            selected = next((item for item in versions if item["version"] == version), None)
            if selected is None:
                return {"status": "blocked", "error": {"code": "version_not_found", "message": "Use list_draft_versions to see available version numbers."}}
            row = {**selected, "revision": row["revision"]}
        ctx.deps.completed_tools.add("get_draft")
        draft = draft_result(row)
        return {"status": "ready", "draft": {**draft, "body_chars": len(draft.get("body") or "")}}

    @agent.tool(prepare=visible)
    async def list_draft_versions(ctx: RunContext[Any]) -> dict[str, Any]:
        """List this chat's draft version numbers. Use get_draft(version) to read one."""
        row = await current(ctx)
        if row is None:
            return {"status": "empty", "versions": []}
        versions = await ctx.deps.repository.list_draft_versions(
            draft_id=row["id"], conversation_id=ctx.deps.conversation_id, channel_id=ctx.deps.channel_id,
        )
        return {"status": "ready", "current_version": row["current_version"], "versions": [
            {"version": item["version"], "created_at": item.get("created_at")} for item in versions
        ]}

    @agent.tool(prepare=visible, sequential=True, metadata=DELIVERABLE)
    async def edit_draft(
        ctx: RunContext[Any], expected_revision: int, body: str | None = None,
        title: str | None = None, source_links: list[str] | None = None,
        media_content: list[str] | None = None,
    ) -> dict[str, Any]:
        """Edit this chat's draft and save a new version with all four fields.

        Read get_draft first and copy its revision into expected_revision.
        Omitted fields stay unchanged; [] clears links or media. Supply the
        complete body when changing text. Media IDs must belong to this draft.
        Previous versions are preserved. If a conflict occurs, read again.
        The result reports the new and previous body length and their ratio
        so you can check the edit against the request.
        """
        row = await current(ctx)
        if row is None:
            return {"status": "blocked", "error": {"code": "draft_not_found", "message": "This chat has no draft. Use create_draft."}}
        previous_body = str(row.get("body") or "")
        changes = {key: value for key, value in dict(title=title, body=body, source_links=source_links, media_content=media_content).items() if value is not None}
        if not changes:
            return {"status": "blocked", "error": {"code": "empty_edit", "message": "Supply at least one field to edit."}}
        try:
            content = DraftContent(**(draft_content(row) | changes))
            row = await ctx.deps.repository.update_draft(
                draft_id=row["id"], payload=content.storage_payload(),
                expected_revision=expected_revision, origin="regenerated", new_version=True,
            )
        except (DraftValidationError, DraftConflictError, ValidationError) as exc:
            return error(exc)
        ctx.deps.completed_tools.add("edit_draft")
        return {"status": "edited", "draft": saved_draft_result(row, previous_body=previous_body)}
