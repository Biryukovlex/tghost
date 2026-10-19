"""ChatGPT SSE startup retries must never replay a delivered response."""
import json

import httpx
import pytest
from openai import APIError
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError
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
        with pytest.raises(ModelAPIError) as raised:
            await Agent(model).run("Synthetic request")
    assert isinstance(raised.value.__cause__, APIError)
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
        with pytest.raises(ModelAPIError) as raised:
            await Agent(model).run("Synthetic request")
    assert isinstance(raised.value.__cause__, APIError)
    assert calls == 1


@pytest.mark.parametrize("message", [
    "ChatGPT returned an unsuccessful response",
    "ChatGPT stream ended before response.completed",
    "Streamed response ended without content or tool calls",
    "Exceeded maximum retries (4) for output validation",
    "synthetic-secret-response-body",
])
def test_error_prose_never_controls_failure_classification(message):
    from pydantic_ai import UnexpectedModelBehavior
    result = build_run_diagnostics(UnexpectedModelBehavior(message))
    assert result["failure_kind"] == "unexpected_model_behavior"
    assert "synthetic-secret" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal,kind", [("failed", "provider_response_failed"), ("incomplete", "provider_response_incomplete"), ("truncated", "provider_stream_truncated"), ("empty", "provider_empty_response")])
async def test_stream_failure_categories_come_from_structured_events(terminal, kind):
    from app.studio.model import StudioModelStreamError
    events = completed_events()
    if terminal in {"failed", "incomplete"}:
        events[-1] = {**events[-1], "type": "response." + terminal,
                      "response": {**events[-1]["response"], "status": terminal}}
    elif terminal == "truncated":
        events = events[:-1]
    else:
        events = [events[0], {**events[-1], "response": {**events[-1]["response"], "output": []}}]
    requests = []
    async def endpoint(request):
        requests.append(request)
        return sse(events)
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        model = ChatGPTPlanModel("fixture", provider=OpenAIProvider(api_key="synthetic", http_client=client))
        with pytest.raises(StudioModelStreamError) as error:
            await Agent(model).run("Synthetic request")
    assert len(requests) == 1
    assert build_run_diagnostics(error.value)["failure_kind"] == kind
    assert safe_error(error.value)[0] == "provider_invalid_response"


@pytest.mark.asyncio
async def test_a_tool_only_stream_is_valid_and_the_tool_is_executed_once():
    requests = []
    executed = []
    created = completed_events()[0]
    item = {"id": "fc_fixture", "type": "function_call", "call_id": "call_fixture", "name": "saved_sources", "arguments": "", "status": "in_progress"}
    done = {**item, "arguments": "{}", "status": "completed"}
    tool_events = [created,
        {"type": "response.output_item.added", "sequence_number": 1, "output_index": 0, "item": item},
        {"type": "response.function_call_arguments.delta", "sequence_number": 2, "output_index": 0, "item_id": item["id"], "delta": "{}"},
        {"type": "response.function_call_arguments.done", "sequence_number": 3, "output_index": 0, "item_id": item["id"], "arguments": "{}"},
        {"type": "response.output_item.done", "sequence_number": 4, "output_index": 0, "item": done},
        {**completed_events()[-1], "sequence_number": 5, "response": {**completed_events()[-1]["response"], "output": [done]}}]
    async def endpoint(request):
        requests.append(request)
        return sse(tool_events if len(requests) == 1 else completed_events())
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        model = ChatGPTPlanModel("fixture", provider=OpenAIProvider(api_key="synthetic", http_client=client))
        agent = Agent(model)
        @agent.tool_plain
        def saved_sources() -> dict:
            executed.append(True)
            return {"source_count": 3}
        result = await agent.run("Synthetic request")
    assert result.output == "Saved"
    assert len(requests) == 2 and len(executed) == 1
