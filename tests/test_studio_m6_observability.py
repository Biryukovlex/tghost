from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest

from app.studio.observability import emit_observation, normalize_usage, safe_error
from app.studio.repository import MemoryStudioRepository
from app.studio.service import StudioService
from app.config import Settings


ROOT = Path(__file__).resolve().parents[1]


def _payload(conversation_id: uuid.UUID, run_id: uuid.UUID) -> bytes:
    return json.dumps(
        {
            "threadId": str(conversation_id),
            "runId": str(run_id),
            "messages": [{"id": str(uuid.uuid4()), "role": "user", "content": "Summarize the channel."}],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        }
    ).encode()


@pytest.mark.asyncio
async def test_testmodel_usage_is_persisted_as_bounded_run_metadata():
    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    run_id = uuid.uuid4()
    service = StudioService(repository, Settings(studio_test_mode=True))

    response = await service.stream_request(None, _payload(conversation["id"], run_id))
    async for _ in response.body_iterator:
        pass

    run = await repository.get_run(run_id)
    assert run and run["status"] == "succeeded"
    assert run["usage"]["requests"] >= 1
    assert run["usage"]["total_tokens"] == run["usage"]["input_tokens"] + run["usage"]["output_tokens"]
    assert run["usage"]["latency_ms"] >= 0
    assert "provider_response" not in json.dumps(run["usage"])


def test_usage_projection_discards_provider_payloads_and_bounds_values():
    usage = normalize_usage(
        {
            "prompt_tokens": 12,
            "completion_tokens": 8,
            "cost": "0.00123456789",
            "details": {"reasoning_tokens": 3, "provider_response": "private"},
            "private_provider_payload": "private",
        },
        latency_ms=42,
    )
    assert usage == {
        "requests": 0,
        "tool_calls": 0,
        "input_tokens": 12,
        "output_tokens": 8,
        "total_tokens": 20,
        "details": {"reasoning_tokens": 3},
        "estimated_cost_usd": 0.00123457,
        "latency_ms": 42,
    }
    assert "private" not in json.dumps(usage)


def test_error_projection_never_returns_provider_exception_text():
    code, message, retryable = safe_error(RuntimeError("api_key=do-not-return response body secret"))
    assert code == "agent_failed"
    assert message == "The agent could not complete this run. Try again."
    assert retryable is True
    assert "do-not-return" not in message


def test_structured_observation_contains_only_allowlisted_metadata(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="studio"):
        emit_observation(
            logging.getLogger("studio"),
            "run",
            run_id="run-1",
            conversation_id="conversation-1",
            status="succeeded",
            duration_ms=12,
            usage={"input_tokens": 4, "output_tokens": 3, "prompt": "private"},
            estimated_input_tokens=12000,
            remaining_input_tokens=20000,
            input_limit_tokens=20000,
            compacted_results=2,
            finalizing=True,
            prompt="private prompt must not be logged",
        )
    record = next(item for item in caplog.records if item.name == "studio")
    assert record.studio_observation["run_id"] == "run-1"
    assert record.studio_observation["usage"]["total_tokens"] == 7
    assert record.studio_observation["estimated_input_tokens"] == 12000
    assert record.studio_observation["remaining_input_tokens"] == record.studio_observation["input_limit_tokens"] == 20000
    assert record.studio_observation["compacted_results"] == 2
    assert record.studio_observation["finalizing"] is True
    assert "prompt" not in record.studio_observation
    assert "private" not in str(record.studio_observation)
    assert '"input_tokens": 4' in record.getMessage()
    assert "private" not in record.getMessage()


@pytest.mark.asyncio
async def test_run_details_is_quiet_scoped_and_filters_unknown_event_fields(client, settings, app, channel_id):
    settings.studio_test_mode = True
    login = await client.post(
        "/login",
        data={"username": settings.admin_username, "password": settings.admin_password},
        follow_redirects=False,
    )
    assert login.status_code == 303
    page = await client.get("/studio")
    token = re.search(r'<meta name="studio-csrf-token" content="([^"]+)"', page.text).group(1)
    created = await client.post(
        "/studio/api/conversations",
        json={"channel_id": channel_id},
        headers={"x-csrf-token": token},
    )
    conversation_id = uuid.UUID(created.json()["conversation"]["id"])
    run_id = uuid.uuid4()
    message = await app.state.studio_repository.append_message(
        conversation_id=conversation_id,
        role="user",
        content="safe operational details",
    )
    run = await app.state.studio_repository.create_run(
        conversation_id=conversation_id,
        user_message_id=message["id"],
        requested_model="test-model",
        run_id=run_id,
    )
    await app.state.studio_repository.append_event(
        run_id,
        event_type="TOOL_CALL_RESULT",
        safe_payload={
            "tool_name": "search_web",
            "source_count": 2,
            "private_excerpt": "must not be returned",
            "usage": {"input_tokens": 2, "output_tokens": 1},
        },
    )
    details = await client.get(f"/studio/api/runs/{run_id}/details")
    assert details.status_code == 200
    body = details.json()
    assert body["details"]["provider"] == run["provider"]
    assert body["details"]["prompt_version"]
    assert body["details"]["activity"]["source_count"] == 2
    assert "private_excerpt" not in details.text
    assert "safe operational details" not in details.text

    unauthenticated = await client.get(f"/studio/api/runs/{uuid.uuid4()}/details", follow_redirects=False)
    assert unauthenticated.status_code == 404


def test_frontend_keeps_run_metadata_out_of_the_main_chat_and_builds_details_panel():
    source = (ROOT / "studio-frontend/src/main.tsx").read_text(encoding="utf-8")
    styles = (ROOT / "studio-frontend/src/styles.css").read_text(encoding="utf-8")
    bundle = (ROOT / "app/web/static/studio-dist/assets/studio.js").read_text(encoding="utf-8")
    assert "Run details" in source
    assert "/studio/api/runs/${run.id}/details" in source
    assert "aria-expanded={open}" in source
    assert ".studio-run-details" in styles
    assert "Run details" in bundle
