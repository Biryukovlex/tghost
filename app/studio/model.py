"""PydanticAI model construction for the Studio.

OpenRouter and local Ollama use OpenAI-compatible chat. ChatGPT OAuth
uses streamed Responses with server-side storage disabled. The deterministic TestModel is available only for explicit local
tests; it is never selected silently in a production configuration.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from enum import Enum

from openai import APIError, APIStatusError
from pydantic_ai import UnexpectedModelBehavior
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.providers.openai import OpenAIProvider

from .. import limits


class StudioConfigurationError(RuntimeError):
    """A safe, actionable configuration problem."""

    code = "studio_not_configured"
    public_message = "Studio is not configured for an agent run."


def build_model(settings, *, test_model: bool | None = None) -> Model:
    """Build the configured model without exposing secrets to callers."""

    use_test_model = settings.studio_test_mode if test_model is None else test_model
    if use_test_model:
        return TestModel(
            call_tools=["get_channel_context"],
            custom_output_text=(
                "I reviewed the selected channel context and can help plan the next post."
            ),
        )

    provider = provider_name(settings)
    if provider == "ollama":
        from .connections import ollama_origin
        if not settings.ollama_model.strip():
            raise StudioConfigurationError("Choose an installed Ollama model in Settings")
        return OpenAIChatModel(settings.ollama_model.strip(), provider=OpenAIProvider(
            base_url=ollama_origin(settings.ollama_base_url) + "/v1", api_key="ollama"))
    if provider == "openai":
        from .connections import oauth_record
        record = oauth_record(settings)
        if not record or not settings.openai_model.strip():
            raise StudioConfigurationError("Connect ChatGPT and choose a model in Settings")
        return ChatGPTPlanModel(settings.openai_model.strip(), provider=OpenAIProvider(
            base_url="https://api.openai.com/v1", api_key=record["access_token"]))
    api_key = settings.openrouter_api_key.strip()
    if not api_key:
        raise StudioConfigurationError("OPENROUTER_API_KEY is required for Studio agent runs")
    model_name = settings.openrouter_model.strip() or "nex-agi/nex-n2.5-pro:free"
    return OpenAIChatModel(
        model_name,
        provider=OpenAIProvider(
            base_url=limits.OPENROUTER_BASE_URL,
            api_key=api_key,
        ),
    )


class ModelStreamFailure(str, Enum):
    FAILED = "provider_response_failed"
    INCOMPLETE = "provider_response_incomplete"
    TRUNCATED = "provider_stream_truncated"
    EMPTY = "provider_empty_response"


class StudioModelStreamError(UnexpectedModelBehavior):
    """A safe failure category captured from the stream lifecycle, never prose."""

    def __init__(self, reason: ModelStreamFailure) -> None:
        self.reason = reason
        super().__init__("The provider response stream did not complete successfully.")


class ChatGPTPlanModel(OpenAIResponsesModel):
    """ChatGPT-plan inference requires streaming, including structured extraction."""

    async def request(self, messages, model_settings, model_request_parameters):
        async with self.request_stream(messages, model_settings, model_request_parameters) as response:
            async for _event in response:
                pass
            return response.get()

    async def _process_streamed_response(self, response, model_settings, model_request_parameters, **kwargs):
        async def verified_events():
            completed = False
            has_output = False
            async for event in response:
                if event.type == "response.failed":
                    raise StudioModelStreamError(ModelStreamFailure.FAILED)
                if event.type == "response.incomplete":
                    raise StudioModelStreamError(ModelStreamFailure.INCOMPLETE)
                if event.type == "response.output_item.added" and event.item.type == "function_call":
                    has_output = True
                if event.type in {"response.output_text.delta", "response.refusal.delta"} and event.delta:
                    has_output = True
                if event.type == "response.completed":
                    completed = True
                yield event
            if not completed:
                raise StudioModelStreamError(ModelStreamFailure.TRUNCATED)
            if not has_output:
                raise StudioModelStreamError(ModelStreamFailure.EMPTY)
        return await super()._process_streamed_response(verified_events(), model_settings, model_request_parameters, **kwargs)

    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters, *args, **kwargs):
        from .observability import streaming_error_category

        settings = {**(model_settings or {}), "openai_store": False}
        for attempt in range(2):
            delivered = False
            try:
                async with super().request_stream(messages, settings, model_request_parameters, *args, **kwargs) as response:
                    delivered = True
                    yield response
                    return
            except APIError as exc:
                # The SDK retries HTTP failures, but not errors inside a 200
                # SSE stream. Retry one transient startup failure only; once
                # a response was exposed, a replay might duplicate tool work.
                if (
                    delivered or attempt or isinstance(exc, APIStatusError)
                    or streaming_error_category(exc) not in {"provider_unavailable", "provider_rate_limited"}
                ):
                    raise
                await asyncio.sleep(0.25)


def provider_name(settings) -> str:
    return str(getattr(settings, "studio_provider", "openrouter") or "openrouter")


def model_name(settings) -> str:
    provider = provider_name(settings)
    if provider == "ollama":
        return str(settings.ollama_model).strip()
    if provider == "openai":
        return str(settings.openai_model).strip()
    return settings.openrouter_model.strip() or "nex-agi/nex-n2.5-pro:free"


def model_configured(settings) -> bool:
    if getattr(settings, "studio_test_mode", False):
        return True
    provider = provider_name(settings)
    if provider == "ollama":
        return bool(settings.ollama_model.strip())
    if provider == "openai":
        from .connections import oauth_record
        return bool(oauth_record(settings) and settings.openai_model.strip())
    return bool(settings.openrouter_api_key.strip())


class ModelRunSettings:
    """Keep a run's provider stable when workspace settings change mid-run."""

    def __init__(self, settings):
        self._base = settings
        self._workspace_settings = getattr(settings, "_workspace_settings", None)
        names = ("studio_provider", "openrouter_model", "openrouter_api_key", "ollama_model",
                 "ollama_base_url", "openai_model", "studio_openai_oauth", "studio_test_mode")
        defaults = {"studio_provider": "openrouter", "studio_test_mode": False, "ollama_base_url": "http://127.0.0.1:11434"}
        self._provider_values = {name: getattr(settings, name, defaults.get(name, "")) for name in names}

    def __getattr__(self, name):
        if name in self._provider_values:
            return self._provider_values[name]
        return getattr(self._base, name)


def run_settings(settings):
    return settings if isinstance(settings, ModelRunSettings) else ModelRunSettings(settings)
