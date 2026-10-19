import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from packaging.requirements import Requirement

from app.config import Settings
from app.studio.provider import run_openrouter_smoke


@pytest.mark.asyncio
async def test_openrouter_smoke_is_skipped_without_a_key(monkeypatch):
    def fail_if_constructed(*args, **kwargs):
        raise AssertionError("the provider must not be constructed without a key")

    monkeypatch.setattr("app.studio.provider.OpenAIChatModel", fail_if_constructed)

    result = await run_openrouter_smoke(Settings())

    assert result.model_dump() == {
        "status": "skipped",
        "model": "",
        "output_length": 0,
        "reason": "OPENROUTER_API_KEY is not configured",
    }


@pytest.mark.asyncio
async def test_public_health_check_reports_postgres_storage_without_auth(client):
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "storage": "postgresql", "last_successful_cycle_at": None}


@pytest.mark.asyncio
async def test_openrouter_smoke_uses_configured_openai_compatible_boundary(monkeypatch):
    seen = {}

    class FakeModel:
        def __init__(self, model_name, *, provider):
            seen["model_name"] = model_name
            seen["provider"] = provider

    class FakeProvider:
        def __init__(self, *, base_url, api_key):
            seen["base_url"] = base_url
            seen["api_key"] = api_key

    class FakeResult:
        output = "READY"

    class FakeAgent:
        def __init__(self, model, *, output_type, instructions):
            seen["output_type"] = output_type
            seen["instructions"] = instructions

        async def run(self, prompt, *, usage_limits):
            seen["prompt"] = prompt
            seen["request_limit"] = usage_limits.request_limit
            return FakeResult()

    monkeypatch.setattr("app.studio.provider.OpenAIChatModel", FakeModel)
    monkeypatch.setattr("app.studio.provider.OpenAIProvider", FakeProvider)
    monkeypatch.setattr("app.studio.provider.Agent", FakeAgent)

    monkeypatch.setattr("app.limits.OPENROUTER_BASE_URL", "https://example.test/v1")
    result = await run_openrouter_smoke(
        Settings(
            openrouter_api_key="secret-key",
            openrouter_model="openai/test-model",
        )
    )

    assert result.status == "ok"
    assert result.model == "openai/test-model"
    assert result.output_length == 5
    assert seen["base_url"] == "https://example.test/v1"
    assert seen["api_key"] == "secret-key"
    assert seen["request_limit"] == 1
    assert "secret-key" not in json.dumps(result.model_dump())


def test_openrouter_cli_requires_explicit_opt_in(tmp_path):
    env = dict(os.environ)
    env.pop("STUDIO_OPENROUTER_SMOKE", None)
    env["PYTHONPATH"] = os.getcwd()
    result = subprocess.run(
        [sys.executable, "scripts/smoke_openrouter.py"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "status": "skipped",
        "reason": "explicit opt-in required",
    }
    assert result.stderr == ""


def test_pydantic_ai_matches_the_reviewed_exact_pin():
    root = Path(__file__).resolve().parents[1]
    pins = []
    for filename in ("requirements.txt", "requirements.lock"):
        entries = [Requirement(line) for line in (root / filename).read_text().splitlines()
                   if line.startswith("pydantic-ai-slim[")]
        assert len(entries) == 1
        specifiers = list(entries[0].specifier)
        assert len(specifiers) == 1 and specifiers[0].operator == "=="
        assert "*" not in specifiers[0].version
        pins.append(specifiers[0].version)
    assert pins[0] == pins[1] == importlib.metadata.version("pydantic-ai-slim")
