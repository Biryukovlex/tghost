import os
import uuid
from datetime import datetime, timezone

import pytest
import httpx
from fastapi import FastAPI

from app.config import Settings
from app.postgres_db import PostgresDatabase
from app.web.routes import create_app, reset_login_rate_limiter


class FakeCollector:
    def __init__(self, db: PostgresDatabase) -> None:
        self.db = db

    async def poll_all(self, reason: str = "test") -> dict[str, object]:
        return {"reason": reason, "posts_seen": 0}


def _pg_url() -> str:
    url = os.environ.get("TEST_POSTGRES_URL", "")
    if not url:
        pytest.skip("TEST_POSTGRES_URL is required")
    return url


async def _make_app(settings: Settings) -> FastAPI:
    db = PostgresDatabase(_pg_url(), workspace_slug=f"t02-{uuid.uuid4().hex[:8]}")
    await db.init_db(admin_username=settings.admin_username)
    await db.upsert_channel("@sample_channel", "Sample channel", 123456)
    return create_app(FakeCollector(db), settings)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    reset_login_rate_limiter()
    yield
    reset_login_rate_limiter()


async def test_cyrillic_credentials_succeed_and_fail(tmp_path):
    settings = Settings(
        api_id=1,
        api_hash="test-api-hash",
        session_string="test-session",
        channels="@sample_channel",
        data_dir=str(tmp_path),
        admin_username="тест-админ",
        admin_password="пароль123",
        session_secret="test-secret-cyrillic",
    )
    app = await _make_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        ok = await client.post(
            "/login",
            data={"username": "тест-админ", "password": "пароль123"},
            follow_redirects=False,
        )
        assert ok.status_code == 303, ok.text
        assert ok.headers["location"] == "/"

        bad = await client.post(
            "/login",
            data={"username": "тест-админ", "password": "неправильный"},
            follow_redirects=False,
        )
        assert bad.status_code == 401
        assert "Invalid username or password" in bad.text


async def test_login_rate_limited_after_five_failures(tmp_path):
    settings = Settings(
        api_id=1,
        api_hash="test-api-hash",
        session_string="test-session",
        channels="@sample_channel",
        data_dir=str(tmp_path),
        admin_username="admin",
        admin_password="correct",
        session_secret="test-secret-ratelimit",
    )
    app = await _make_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for i in range(5):
            resp = await client.post(
                "/login",
                data={"username": "admin", "password": "wrong"},
                follow_redirects=False,
            )
            assert resp.status_code == 401, f"attempt {i+1} should be 401"
        sixth = await client.post(
            "/login",
            data={"username": "admin", "password": "wrong"},
            follow_redirects=False,
        )
        assert sixth.status_code == 429
        assert "Retry-After" in sixth.headers
        assert int(sixth.headers["Retry-After"]) > 0

        reset_login_rate_limiter()
        ok = await client.post(
            "/login",
            data={"username": "admin", "password": "correct"},
            follow_redirects=False,
        )
        assert ok.status_code == 303


async def test_security_headers_on_login(tmp_path):
    settings = Settings(
        data_dir=str(tmp_path),
        admin_username="a",
        admin_password="b",
        session_secret="s",
    )
    app = await _make_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/login")
        assert resp.status_code == 200
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("Referrer-Policy") == "same-origin"
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "default-src 'self'" in csp
        assert "script-src 'self'" in csp
        assert "style-src 'self' 'unsafe-inline'" in csp
        assert "Strict-Transport-Security" not in resp.headers


async def test_secure_cookie_and_hsts_when_behind_tls(tmp_path):
    settings = Settings(
        data_dir=str(tmp_path),
        admin_username="admin",
        admin_password="pw",
        session_secret="sec",
        behind_tls=True,
    )
    app = await _make_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/login")
        assert resp.headers.get("Strict-Transport-Security") == "max-age=31536000; includeSubDomains"

        login = await client.post(
            "/login",
            data={"username": "admin", "password": "pw"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        cookie = login.headers.get("set-cookie", "")
        assert "secure" in cookie.lower()
        assert "samesite=lax" in cookie.lower()


def _extract_error(body: dict) -> dict:
    if "error" in body:
        return body["error"]
    if "detail" in body and isinstance(body["detail"], dict) and "error" in body["detail"]:
        return body["detail"]["error"]
    return {}


async def test_unauthenticated_api_returns_401_json(tmp_path):
    settings = Settings(
        data_dir=str(tmp_path),
        admin_username="admin",
        admin_password="pw",
        session_secret="sec",
        studio_test_mode=True,
    )
    app = await _make_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        api = await client.get("/studio/api/conversations")
        assert api.status_code == 401
        body = api.json()
        err = _extract_error(body)
        assert err.get("code") == "unauthenticated"
        assert err.get("retryable") is False

        html = await client.get("/", follow_redirects=False)
        assert html.status_code == 303
        assert html.headers["location"] == "/login"

        compat = await client.get("/studio/api/runs/00000000-0000-0000-0000-000000000000/events")
        assert compat.status_code == 401
        assert _extract_error(compat.json()).get("code") == "unauthenticated"

        html_json = await client.get("/", headers={"Accept": "application/json"})
        assert html_json.status_code == 401


async def test_csv_formula_injection_escaped(tmp_path):
    db = PostgresDatabase(_pg_url(), workspace_slug=f"t02-{uuid.uuid4().hex[:8]}")
    await db.init_db(admin_username="admin")
    try:
        ch_id = await db.upsert_channel("@sample_channel", "Sample", 1)
        post_id = await db.upsert_post(ch_id, message_id=1, posted_at=datetime.now(timezone.utc), text="hello")
        await db.add_snapshot_if_changed(post_id, views=1, comments=1, reactions=0, shares=0)
        await db.upsert_comment(
            post_id=post_id,
            telegram_message_id=99,
            discussion_chat_id=-1001234567890,
            discussion_username="disc",
            sender_id=1,
            sender_name="Attacker",
            sender_username="evil",
            posted_at=datetime.now(timezone.utc),
            edited_at=None,
            text='=HYPERLINK("http://evil")',
            media_type="",
            reactions=0,
            reply_to_message_id=None,
            sync_token="tok1",
        )
        post2 = await db.upsert_post(ch_id, message_id=2, posted_at=datetime.now(timezone.utc), text='=HYPERLINK("x2")')
        await db.add_snapshot_if_changed(post2, views=2, comments=0, reactions=0, shares=0)

        settings = Settings(data_dir=str(tmp_path), admin_username="admin", admin_password="pw", session_secret="sec")
        app = create_app(FakeCollector(db), settings)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await client.post("/login", data={"username": "admin", "password": "pw"})
            resp = await client.get("/export.csv")
            assert resp.status_code == 200
            assert "'=HYPERLINK" in resp.text

            resp2 = await client.get("/export-comments.csv")
            assert resp2.status_code == 200
            assert "'=HYPERLINK" in resp2.text
            # Negative numeric ids are not free text and must not be quoted.
            assert "-1001234567890" in resp2.text
            assert "'-1001234567890" not in resp2.text
            # Operator-configured channel identifiers keep their leading @.
            assert ",@sample_channel," in resp.text
            # normal text should still appear in posts export
            assert "hello" in resp.text
    finally:
        await db.close()


async def test_base_html_has_no_cdn(tmp_path):
    settings = Settings(data_dir=str(tmp_path), admin_username="admin", admin_password="pw", session_secret="sec")
    app = await _make_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/login")
        assert "cdn.jsdelivr.net" not in resp.text

        await client.post("/login", data={"username": "admin", "password": "pw"})
        dash = await client.get("/")
        assert "cdn.jsdelivr.net" not in dash.text
        assert "/static/vendor/chartjs/chart.umd.min.js" in dash.text
        assert "data-chart=" in dash.text


async def test_compat_events_filters_allowlist(tmp_path):
    settings = Settings(
        data_dir=str(tmp_path),
        admin_username="admin",
        admin_password="pw",
        session_secret="sec",
        studio_test_mode=True,
    )
    db = PostgresDatabase(_pg_url(), workspace_slug=f"t02-{uuid.uuid4().hex[:8]}")
    await db.init_db(admin_username="admin")
    await db.upsert_channel("@sample_channel", "Sample", 123)
    collector = FakeCollector(db)
    app = create_app(collector, settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await client.post("/login", data={"username": "admin", "password": "pw"})
        from app.studio.routes import _public_event_payload

        filtered = _public_event_payload({"tool_name": "search_web", "malicious": "evil", "source_count": 5})
        assert "tool_name" in filtered
        assert "malicious" not in filtered
        assert filtered["source_count"] == 5

        repo = app.state.studio_repository
        conv_id = uuid.uuid4()
        run_id = uuid.uuid4()

        async def fake_get_run(rid):
            return {"conversation_id": conv_id, "id": rid, "status": "succeeded", "stage": "complete"}

        async def fake_get_events(rid, after=0):
            return [
                {
                    "id": 1,
                    "sequence": 1,
                    "event_type": "TOOL_CALL_START",
                    "safe_payload": {"tool_name": "search_web", "malicious": "should_be_hidden", "source_count": 1},
                    "created_at": "2026-09-19T00:00:00+00:00",
                }
            ]

        orig_get_run = repo.get_run
        orig_get_events = repo.get_events
        repo.get_run = fake_get_run  # type: ignore
        repo.get_events = fake_get_events  # type: ignore
        try:
            resp = await client.get(f"/studio/api/runs/{run_id}/events")
            assert resp.status_code == 200
            events = resp.json()["events"]
            assert len(events) == 1
            assert events[0]["event_type"] == "TOOL_CALL_START"
            assert "malicious" not in events[0]["safe_payload"]
            assert events[0]["safe_payload"]["tool_name"] == "search_web"
        finally:
            repo.get_run = orig_get_run  # type: ignore
            repo.get_events = orig_get_events  # type: ignore


def test_validate_required_change_me_guard(tmp_path):
    settings = Settings(
        data_dir=str(tmp_path),
        admin_username="admin",
        admin_password="change-me",
        web_host="0.0.0.0",
        api_id=1,
        api_hash="x",
        session_string="s",
        channels="@c",
    )
    problems = settings.validate_required()
    assert any("ADMIN_PASSWORD" in p and "change-me" in p for p in problems), problems

    ok = Settings(
        data_dir=str(tmp_path),
        admin_username="admin",
        admin_password="change-me",
        web_host="127.0.0.1",
        api_id=1,
        api_hash="x",
        session_string="s",
        channels="@c",
    )
    assert not any("change-me" in p for p in ok.validate_required())

    localhost_ok = Settings(
        data_dir=str(tmp_path),
        admin_username="admin",
        admin_password="change-me",
        web_host="localhost",
        api_id=1,
        api_hash="x",
        session_string="s",
        channels="@c",
    )
    assert not any("change-me" in p for p in localhost_ok.validate_required())


async def test_static_assets_use_content_hash_versions(tmp_path):
    """A changed app.js must never be served from a cache keyed on a hand-bumped ?v=."""
    from app.web.routes import static_asset_version

    version = static_asset_version()
    assert len(version) == 12 and all(c in "0123456789abcdef" for c in version)
    settings = Settings(data_dir=str(tmp_path), admin_username="admin", admin_password="pw", session_secret="sec")
    app = await _make_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.get("/login")
        assert f"/static/style.css?v={version}" in login.text
        await client.post("/login", data={"username": "admin", "password": "pw"})
        dash = await client.get("/")
        assert f"/static/app.js?v={version}" in dash.text
        assert f"/static/style.css?v={version}" in dash.text
        assert 'app.js?v=2"' not in dash.text


def test_provider_catalog_changes_invalidate_browser_assets(tmp_path, monkeypatch):
    from app.web import routes

    static = tmp_path / "static"
    static.mkdir()
    script = static / "provider-connections.js"
    script.write_text("previous catalog loader")
    monkeypatch.setattr(routes, "WEB_DIR", tmp_path)
    routes.static_asset_version.cache_clear()
    try:
        before = routes.static_asset_version()
        script.write_text("updated catalog loader")
        routes.static_asset_version.cache_clear()
        assert routes.static_asset_version() != before
    finally:
        routes.static_asset_version.cache_clear()
