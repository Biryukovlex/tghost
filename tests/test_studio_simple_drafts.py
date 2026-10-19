"""Draft state transitions and complete version snapshots, using synthetic data."""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

from app.config import Settings
from app.studio.agent import StudioDeps, build_agent
from app.studio.drafts import DraftValidationError
from app.studio.repository import MemoryStudioRepository, StudioRepository


async def setup(repository=None, channel_id=1):
    repository = repository or MemoryStudioRepository()
    chat = await repository.create_conversation(channel_id=channel_id)
    deps = StudioDeps(repository=repository, workspace_id=repository.workspace_id,
                     conversation_id=chat["id"], channel_id=channel_id, cancel_event=asyncio.Event())
    agent = build_agent(Settings(studio_test_mode=True))
    return repository, deps, SimpleNamespace(deps=deps), agent._function_toolset.tools


@pytest.mark.asyncio
async def test_create_read_edit_and_read_history_preserve_complete_content():
    repo, deps, ctx, tools = await setup()
    body = "I could not open the page.\n\nSources:\nhttps://example.test/original"
    first = await tools["create_draft"].function(ctx, title="First", body=body, source_links=["https://example.test/a"])
    assert first["status"] == "created"
    assert first["draft"]["body"] == body  # no hidden rewriting
    duplicate = await tools["create_draft"].function(ctx, title="Second", body="Duplicate")
    assert duplicate["error"]["code"] == "draft_exists"
    second = await tools["edit_draft"].function(ctx, expected_revision=1, title="Second", source_links=["https://example.test/b"])
    assert second["draft"] == {"version": 2, "revision": 2, "title": "Second", "body": body,
                               "source_links": ["https://example.test/b"], "media_content": []}
    older = await tools["get_draft"].function(ctx, version=1)
    assert older["draft"] == {"version": 1, "revision": 2, "title": "First", "body": body,
                              "source_links": ["https://example.test/a"], "media_content": []}
    assert (await tools["get_draft"].function(ctx))["draft"] == second["draft"]
    history = await tools["list_draft_versions"].function(ctx)
    assert [row["version"] for row in history["versions"]] == [1, 2]
    await tools["edit_draft"].function(ctx, expected_revision=2, body="Edited", source_links=[])
    assert (await tools["get_draft"].function(ctx))["draft"]["source_links"] == []
    assert len(repo.drafts) == 1


@pytest.mark.asyncio
async def test_invalid_and_conflicting_edits_never_complete_or_create_versions():
    repo, deps, ctx, tools = await setup()
    missing = await tools["edit_draft"].function(ctx, expected_revision=1, body="Absent")
    assert missing["error"]["code"] == "draft_not_found"
    for _ in range(2):
        result = await tools["create_draft"].function(ctx, title="Test", body="Post", source_links=["javascript:alert(1)"])
        assert result["status"] == "blocked"
    assert "create_draft" not in deps.completed_tools
    assert not repo.drafts
    await tools["create_draft"].function(ctx, title="Test", body="Post")
    deps.completed_tools.clear()
    result = await tools["edit_draft"].function(ctx, expected_revision=0, body="Stale")
    assert result["error"]["code"] == "draft_conflict"
    assert "edit_draft" not in deps.completed_tools
    assert len((await tools["list_draft_versions"].function(ctx))["versions"]) == 1
    assert (await tools["get_draft"].function(ctx, version=999))["error"]["code"] == "version_not_found"


@pytest.mark.asyncio
async def test_tools_are_scoped_to_chat_and_visibility_changes_after_creation():
    repo, deps, ctx, tools = await setup()
    other = await repo.create_conversation(channel_id=1)
    await repo.create_draft(conversation_id=other["id"], channel_id=1, payload={"body": "Private other chat"})
    seen = []

    async def model(messages, info):
        seen.append({tool.name for tool in info.function_tools})
        if len(seen) == 1:
            return ModelResponse(parts=[ToolCallPart("create_draft", {"title": "This chat", "body": "Post"})])
        return ModelResponse(parts=[TextPart("Created")])

    agent = build_agent(Settings(studio_test_mode=True), model=FunctionModel(model))
    await agent.run("Create a draft", deps=deps)
    assert {"create_draft", "get_draft", "edit_draft", "list_draft_versions"} & seen[0] == {"create_draft"}
    assert {"create_draft", "get_draft", "edit_draft", "list_draft_versions"} & seen[1] == {"get_draft", "edit_draft", "list_draft_versions"}
    result = await tools["get_draft"].function(ctx)
    assert result["draft"]["body"] == "Post"
    assert "draft_id" not in tools["get_draft"].tool_def.parameters_json_schema["properties"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_creation_race_reload_snapshots_and_media_ownership():
    from app.postgres_db import PostgresDatabase
    from tests.helpers.postgres import pg_url
    url = pg_url()
    if not url:
        pytest.skip("TEST_POSTGRES_URL is required")
    db = PostgresDatabase(url, workspace_slug="simple-draft-proof")
    try:
        await db.init_db(admin_username="simple-draft-proof-admin")
        channel_id = await db.upsert_channel("@simple_draft_proof", "Synthetic", 992299)
        repo = StudioRepository(db)
        chat = await repo.create_conversation(channel_id=channel_id)
        async def create():
            return await repo.create_draft(conversation_id=chat["id"], channel_id=channel_id,
                                           payload={"working_title": "Original", "body": "First", "source_links": ["https://example.test/a"]})
        results = await asyncio.gather(create(), create(), return_exceptions=True)
        assert sum(isinstance(row, DraftValidationError) and row.code == "draft_exists" for row in results) == 1, results
        first = next(row for row in results if isinstance(row, dict))
        from app.publishing.media import prepare_image
        from app.publishing.repository import PublishingRepository
        from tests.test_publishing import jpeg
        import uuid
        asset = await PublishingRepository(db).upload(uuid.UUID(first["id"]), "synthetic.png", prepare_image(jpeg()))
        next_row = await repo.update_draft(draft_id=first["id"], expected_revision=1,
                                          payload={"working_title": "Revised", "body": "Second", "source_links": ["https://example.test/b"], "media_ids": [asset["id"]]})
        reload = StudioRepository(db)
        versions = await reload.list_draft_versions(draft_id=first["id"], conversation_id=chat["id"], channel_id=channel_id)
        assert [(v["title"], v["body"], v["source_links"], v["media_ids"]) for v in versions] == [
            ("Original", "First", ["https://example.test/a"], []),
            ("Revised", "Second", ["https://example.test/b"], [asset["id"]]),
        ]
        foreign = await repo.create_conversation(channel_id=channel_id)
        foreign_draft = await repo.create_draft(conversation_id=foreign["id"], channel_id=channel_id, payload={"body": "Foreign"})
        with pytest.raises(DraftValidationError, match="unavailable"):
            await reload.update_draft(draft_id=foreign_draft["id"], expected_revision=1, payload={"media_ids": [asset["id"]]})
        with pytest.raises(DraftValidationError, match="unavailable"):
            await reload.update_draft(draft_id=first["id"], expected_revision=next_row["revision"], payload={"media_ids": ["00000000-0000-0000-0000-000000000001"]})
        restored = await reload.choose_draft_version(draft_id=first["id"], version=1, expected_revision=2)
        assert restored["title"] == "Original" and restored["source_links"] == ["https://example.test/a"]
        assert restored["media_ids"] == []
    finally:
        await db.close()
