"""Draft versioning UX: Save overwrites, Save as new version appends, Choose moves
the pointer, agent revisions become current, nothing is minted implicitly."""
import os
import re
import uuid

import pytest

from app.studio.drafts import DraftConflictError
from app.studio.repository import DraftNotFound, MemoryStudioRepository


async def _seeded_repo():
    repo = MemoryStudioRepository()
    conversation = await repo.create_conversation(channel_id=1)
    draft = await repo.create_draft(conversation_id=conversation["id"], channel_id=1, payload={"body": "v1 agent text", "creative": True}, origin="generated")
    return repo, conversation, draft


@pytest.mark.asyncio
async def test_save_overwrites_current_version_without_creating_one():
    repo, _conversation, draft = await _seeded_repo()
    saved = await repo.save_draft(draft_id=draft["id"], payload={"body": "v1 edited by owner"}, expected_revision=draft["revision"])
    assert saved["current_version"] == 1 and saved["revision"] == draft["revision"] + 1
    versions = await repo.list_draft_versions(draft_id=draft["id"])
    assert [(v["version"], v["body"], v["origin"]) for v in versions] == [(1, "v1 edited by owner", "user_edit")]


@pytest.mark.asyncio
async def test_save_as_new_version_appends_and_becomes_current():
    repo, _conversation, draft = await _seeded_repo()
    saved = await repo.save_draft(draft_id=draft["id"], payload={"body": "owner's alternative"}, expected_revision=draft["revision"], new_version=True)
    assert saved["current_version"] == 2 and saved["body"] == "owner's alternative"
    versions = await repo.list_draft_versions(draft_id=draft["id"])
    assert [(v["version"], v["body"]) for v in versions] == [(1, "v1 agent text"), (2, "owner's alternative")]


@pytest.mark.asyncio
async def test_choose_moves_pointer_without_new_version_and_agent_revision_becomes_current():
    repo, _conversation, draft = await _seeded_repo()
    v2 = await repo.revise_draft(draft_id=draft["id"], payload={"body": "v2 agent revision"}, instruction="shorter")
    assert v2["current_version"] == 2 and v2["current_version_origin"] == "regenerated"
    chosen = await repo.choose_draft_version(draft_id=draft["id"], version=1, expected_revision=v2["revision"])
    assert chosen["current_version"] == 1 and chosen["body"] == "v1 agent text"
    assert chosen["revision"] == v2["revision"] + 1
    assert len(await repo.list_draft_versions(draft_id=draft["id"])) == 2
    # Choosing the version that is already current is a no-op.
    again = await repo.choose_draft_version(draft_id=draft["id"], version=1, expected_revision=chosen["revision"])
    assert again["revision"] == chosen["revision"]
    # A new agent revision builds on the chosen body and becomes v3, current.
    v3 = await repo.revise_draft(draft_id=draft["id"], payload={"body": "v3 from v1"}, instruction="again")
    assert v3["current_version"] == 3 and (await repo.get_draft(draft["id"]))["body"] == "v3 from v1"
    with pytest.raises(DraftConflictError):
        await repo.choose_draft_version(draft_id=draft["id"], version=2, expected_revision=1)
    with pytest.raises(DraftNotFound):
        await repo.choose_draft_version(draft_id=draft["id"], version=99, expected_revision=v3["revision"])
    # Compatibility alias behaves as choose.
    restored = await repo.restore_draft_version(draft_id=draft["id"], version=2, expected_revision=v3["revision"])
    assert restored["current_version"] == 2 and len(await repo.list_draft_versions(draft_id=draft["id"])) == 3


@pytest.mark.asyncio
async def test_patch_route_choose_and_save_modes(client, app, settings, channel_id):
    settings.studio_test_mode = True
    await client.post("/login", data={"username": settings.admin_username, "password": settings.admin_password}, follow_redirects=False)
    home = await client.get("/studio")
    token = re.search(r'<meta name="studio-csrf-token" content="([^"]+)"', home.text).group(1)
    headers = {"x-csrf-token": token, "content-type": "application/json"}
    repo = app.state.studio_repository
    conversation = await repo.create_conversation(channel_id=channel_id)
    draft = await repo.create_draft(conversation_id=uuid.UUID(str(conversation["id"])), channel_id=channel_id, payload={"body": "agent v1", "creative": True})
    did = draft["id"]

    saved = await client.patch(f"/studio/api/drafts/{did}", json={"expected_revision": draft["revision"], "body": "owner edit", "working_title": "T"}, headers=headers)
    assert saved.status_code == 200 and saved.json()["draft"]["current_version"] == 2
    appended = await client.patch(f"/studio/api/drafts/{did}", json={"expected_revision": saved.json()["draft"]["revision"], "body": "owner alt", "save_as_new_version": True}, headers=headers)
    assert appended.status_code == 200 and appended.json()["draft"]["current_version"] == 3
    chosen = await client.patch(f"/studio/api/drafts/{did}", json={"expected_revision": appended.json()["draft"]["revision"], "choose_version": 1}, headers=headers)
    assert chosen.status_code == 200
    body = chosen.json()["draft"]
    assert body["current_version"] == 1 and body["body"] == "agent v1"
    versions = await client.get(f"/studio/api/drafts/{did}/versions")
    assert [v["version"] for v in versions.json()["versions"]] == [1, 2, 3]
    stale = await client.patch(f"/studio/api/drafts/{did}", json={"expected_revision": 1, "choose_version": 2}, headers=headers)
    assert stale.status_code == 409 and "server_draft" in stale.json()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_versioning_semantics():
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("set TEST_POSTGRES_URL to run the versioning PostgreSQL proof")
    from app.postgres_db import PostgresDatabase
    from app.studio.repository import StudioRepository

    db = PostgresDatabase(url, workspace_slug=f"t20-{uuid.uuid4().hex[:8]}")
    try:
        await db.init_db(admin_username="t20-admin")
        channel_id = await db.upsert_channel(f"@t20_{uuid.uuid4().hex[:6]}", "T20", 7020)
        repo = StudioRepository(db)
        conversation = await repo.create_conversation(channel_id=channel_id)
        draft = await repo.create_draft(conversation_id=conversation["id"], channel_id=channel_id, payload={"body": "v1 agent", "creative": True}, origin="generated")
        saved = await repo.save_draft(draft_id=draft["id"], payload={"body": "v1 owner overwrite"}, expected_revision=draft["revision"])
        assert saved["current_version"] == 1
        assert [(v["version"], v["body"], v["origin"]) for v in await repo.list_draft_versions(draft_id=draft["id"])] == [(1, "v1 owner overwrite", "user_edit")]
        v2 = await repo.save_draft(draft_id=draft["id"], payload={"body": "v2 owner new"}, expected_revision=saved["revision"], new_version=True)
        v3 = await repo.revise_draft(draft_id=draft["id"], payload={"body": "v3 agent"}, instruction="x")
        assert (v2["current_version"], v3["current_version"]) == (2, 3)
        chosen = await repo.choose_draft_version(draft_id=draft["id"], version=1, expected_revision=v3["revision"])
        assert chosen["current_version"] == 1 and chosen["body"] == "v1 owner overwrite" and chosen["current_version_origin"] == "user_edit"
        assert len(await repo.list_draft_versions(draft_id=draft["id"])) == 3
        reloaded = await repo.get_current_draft(conversation_id=conversation["id"], channel_id=channel_id)
        assert reloaded["current_version"] == 1 and reloaded["body"] == "v1 owner overwrite"
    finally:
        await db.close()
