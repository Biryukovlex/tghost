"""ChatGPT SSE startup retries must never replay a delivered response."""
import json

import httpx
import pytest
from openai import APIError
from pydantic_ai import Agent
from pydantic_ai.providers.openai import OpenAIProvider

from app.studio.diagnostics import build_run_diagnostics
from app.studio.model import ChatGPTPlanModel
from app.studio.observability import safe_error


def completed_events():
    response = {"id": "resp_fixture", "object": "response", "model": "fixture", "created_at": 1,
                "status": "in_progress", "output": [], "usage": None}
    message = {"id": "msg_fixture", "type": "message", "role": "assistant", "status": "in_progress", "content": []}
    return [
        {"type": "response.created", "response": response, "sequence_number": 0},
        {"type": "response.output_item.added", "output_index": 0, "item": message, "sequence_number": 1},
        {"type": "response.content_part.added", "item_id": message["id"], "output_index": 0,
         "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}, "sequence_number": 2},
        {"type": "response.output_text.delta", "item_id": message["id"], "output_index": 0,
         "content_index": 0, "delta": "Saved", "sequence_number": 3},
        {"type": "response.completed", "sequence_number": 4, "response": {
            **response, "status": "completed", "output": [{**message, "status": "completed",
            "content": [{"type": "output_text", "text": "Saved", "annotations": []}]}],
            "usage": {"input_tokens": 4, "output_tokens": 1, "total_tokens": 5,
                      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}},
        }},
    ]


def error_event(code):
    return {"error": {"code": code, "type": code, "message": "synthetic-secret"}}


def sse(events):
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content="".join("data: " + json.dumps(event) + "\n\n" for event in events))


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["server_error", "rate_limit_exceeded"])
async def test_transient_startup_error_retries_once_and_completes(code):
    requests = []
    async def endpoint(request):
        body = json.loads(request.content)
        assert body["stream"] is True and body["store"] is False
        requests.append(body)
        return sse([error_event(code)] if len(requests) == 1 else completed_events())
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        model = ChatGPTPlanModel("fixture", provider=OpenAIProvider(api_key="synthetic", http_client=client))
        result = await Agent(model).run("Synthetic draft acknowledgement")
    assert result.output == "Saved"
    assert len(requests) == 2
    assert requests[0] == requests[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(("code", "attempts", "category"), [
    ("server_error", 2, "provider_unavailable"),
    ("authentication_error", 1, "provider_auth_failed"),
    ("invalid_request_error", 1, "provider_bad_request"),
    ("unknown_fixture_error", 1, "agent_failed"),
])
async def test_startup_retry_is_bounded_and_permanent_errors_are_not_retried(code, attempts, category):
    calls = 0
    async def endpoint(request):
        nonlocal calls
        calls += 1
        return sse([error_event(code)])
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        model = ChatGPTPlanModel("fixture", provider=OpenAIProvider(api_key="synthetic", http_client=client))
        with pytest.raises(APIError) as raised:
            await Agent(model).run("Synthetic request")
    assert calls == attempts
    assert safe_error(raised.value)[0] == category
    assert "synthetic-secret" not in str(build_run_diagnostics(raised.value))
    assert "synthetic-secret" not in safe_error(raised.value)[1]


@pytest.mark.asyncio
async def test_error_after_stream_delivery_never_retries():
    calls = 0
    async def endpoint(request):
        nonlocal calls
        calls += 1
        return sse([*completed_events()[:-1], error_event("server_error")])
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        model = ChatGPTPlanModel("fixture", provider=OpenAIProvider(api_key="synthetic", http_client=client))
        with pytest.raises(APIError):
            await Agent(model).run("Synthetic request")
    assert calls == 1
