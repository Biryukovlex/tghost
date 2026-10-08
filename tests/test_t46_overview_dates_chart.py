"""T46 Overview dates, KPIs, chart, sync and export acceptance tests.

Editable inclusive From/To UTC publication dates scope cards and graph;
validation keeps the last applied range; empty ranges show zero plus a
recovery path. The chart shows one selected metric in its KPI accent with
Day/Week/Month and Daily/Cumulative modes, keyboard point inspection and an
exact text summary (no data-table disclosure). Sync reports scheduling only;
exports use the real server CSV routes with stored-history scope.
"""

from __future__ import annotations

import asyncio
import html as html_module
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.postgres_db import cohort_bounds
from app.web.dependencies import WorkspaceContext
from app.web.routes import create_app


ROOT = Path(__file__).resolve().parents[1]


def _dt(day: str, hour: int = 10) -> str:
    return f"{day} {hour:02d}:00:00+00:00"


class _T46FakeDB:
    """Deterministic Overview fixture: three posts on three UTC dates."""

    def __init__(self) -> None:
        self.user_id = "00000000-0000-0000-0000-000000000001"
        self.workspace_id = "00000000-0000-0000-0000-000000000002"
        self.channels = [
            {"id": 1, "identifier": "@chan_one", "title": "Channel One", "chat_id": -1001, "active": True},
            {"id": 2, "identifier": "@chan_two", "title": "Channel Two", "chat_id": -1002, "active": True},
        ]
        self.posts = [
            {
                "id": 1, "message_id": 11, "channel_id": 1, "identifier": "@chan_one",
                "chat_id": -1001, "posted_at": _dt("2026-08-24"), "text": "first post",
                "views": 1000, "reactions": 50, "comments": 10, "shares": 5,
                "collected_comments": 2, "updated_at": _dt("2026-09-06", 12), "views_delta": 0,
            },
            {
                "id": 2, "message_id": 12, "channel_id": 1, "identifier": "@chan_one",
                "chat_id": -1001, "posted_at": _dt("2026-08-30"), "text": "second post",
                "views": 2000, "reactions": 60, "comments": 20, "shares": 6,
                "collected_comments": 3, "updated_at": _dt("2026-09-06", 12), "views_delta": 0,
            },
            {
                "id": 3, "message_id": 13, "channel_id": 2, "identifier": "@chan_two",
                "chat_id": -1002, "posted_at": _dt("2026-09-06"), "text": "third post",
                "views": 5120, "reactions": 184, "comments": 37, "shares": 12,
                "collected_comments": 4, "updated_at": _dt("2026-09-06", 12), "views_delta": 0,
            },
        ]

    def _scoped(self, channel_id=None, from_date=None, to_date=None):
        start, end_exclusive = cohort_bounds(from_date, to_date)
        rows = list(self.posts)
        if channel_id is not None:
            rows = [r for r in rows if r["channel_id"] == channel_id]
        if start is not None:
            rows = [r for r in rows if r["posted_at"] >= start.strftime("%Y-%m-%d %H:%M:%S+00:00")]
        if end_exclusive is not None:
            rows = [r for r in rows if r["posted_at"] < end_exclusive.strftime("%Y-%m-%d %H:%M:%S+00:00")]
        return rows

    async def get_channels(self):
        return self.channels

    async def kpis(self, channel_id=None, from_date=None, to_date=None):
        rows = self._scoped(channel_id, from_date, to_date)
        return {
            "posts": len(rows),
            "views": sum(r["views"] for r in rows),
            "reactions": sum(r["reactions"] for r in rows),
            "comments": sum(r["comments"] for r in rows),
            "shares": sum(r["shares"] for r in rows),
            "collected_comments": sum(r["collected_comments"] for r in rows),
            "last_poll": "2026-09-06 12:00:00+00:00",
        }

    async def timeseries_totals(self, days=None, channel_id=None, from_date=None, to_date=None):
        rows = self._scoped(channel_id, from_date, to_date)
        start, end_exclusive = cohort_bounds(from_date, to_date)
        if start is not None and end_exclusive is not None:
            first = start.date()
            last = (end_exclusive - timedelta(days=1)).date()
        elif start is not None:
            first = start.date()
            last = datetime(2026, 9, 6, tzinfo=timezone.utc).date()
        else:
            span_days = days if days is not None else 14
            last = datetime(2026, 9, 6, tzinfo=timezone.utc).date()
            first = last - timedelta(days=span_days - 1)
        span = max(1, (last - first).days + 1)
        labels = [(datetime(first.year, first.month, first.day, tzinfo=timezone.utc) + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(span)]
        by_day: dict[str, list[int]] = {label: [0, 0, 0, 0, 0] for label in labels}
        for row in rows:
            key = row["posted_at"][:10]
            if key in by_day:
                by_day[key][0] += 1
                by_day[key][1] += row["views"]
                by_day[key][2] += row["reactions"]
                by_day[key][3] += row["comments"]
                by_day[key][4] += row["shares"]
        return {
            "days": labels,
            "views": [by_day[label][1] for label in labels],
            "reactions": [by_day[label][2] for label in labels],
            "comments": [by_day[label][3] for label in labels],
            "shares": [by_day[label][4] for label in labels],
            "posts_per_day": [by_day[label][0] for label in labels],
        }

    async def latest_stats(self, channel_id=None, order="date", limit=100, offset=0):
        rows = [r for r in self.posts if channel_id is None or r["channel_id"] == channel_id]
        key = {"date": "posted_at", "views": "views", "reactions": "reactions", "comments": "comments", "shares": "shares"}[order]
        rows = sorted(rows, key=lambda r: r[key], reverse=True)
        return [dict(r) for r in rows[offset:offset + limit]]

    async def all_comments(self, channel_id=None, limit=100000):
        return []


class _T46Collector:
    client = object()

    def __init__(self, db):
        self.db = db
        self.workspace_settings = None
        self.scheduled: list[str] = []

    def schedule_poll(self, reason="manual"):
        self.scheduled.append(reason)
        return asyncio.create_task(asyncio.sleep(0))


def _make_app(tmp_path, db=None, process_role="all"):
    settings = Settings(
        _env_file=None,
        data_dir=str(tmp_path),
        admin_username="admin",
        admin_password="pw",
        session_secret="secret",
        process_role=process_role,
    )
    database = db if db is not None else _T46FakeDB()
    collector = _T46Collector(database)
    app = create_app(collector, settings)
    app.state.workspace_context = WorkspaceContext(
        user_id=database.user_id,
        workspace_id=database.workspace_id,
        workspace_slug="community",
        role="owner",
    )
    return app, collector, database


def _chart_payload(page_text: str) -> dict:
    match = re.search(r"<canvas id=\"trendChart\" data-chart='(.*?)'", page_text, flags=re.DOTALL)
    assert match, "trend chart canvas with embedded data must exist"
    return json.loads(html_module.unescape(match.group(1)))


@pytest.mark.asyncio
async def test_dashboard_cohort_scopes_cards_and_chart(tmp_path):
    app, _, _ = _make_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/login", data={"username": "admin", "password": "pw"})
        page = await client.get("/?from=2026-08-24&to=2026-09-06")
        assert page.status_code == 200
        # Cards show the cohort totals (all three posts), not all-time drift.
        assert "8,120" in page.text
        assert "294" in page.text
        assert "Posts published 2026-08-24 – 2026-09-06 (UTC)" in page.text
        chart = _chart_payload(page.text)
        assert chart["labels"][0] == "2026-08-24"
        assert chart["labels"][-1] == "2026-09-06"
        assert len(chart["labels"]) == 14
        assert sum(chart["views"]) == 8120
        assert sum(chart["postsPerDay"]) == 3
        # Sparse days carry zero without breaking the series.
        assert chart["views"][1] == 0
        assert chart["postsPerDay"][1] == 0


@pytest.mark.asyncio
async def test_open_ended_date_labels_and_full_archive_export_copy(tmp_path):
    app, _, _ = _make_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/login", data={"username": "admin", "password": "pw"})
        from_only = await client.get("/?from=2026-09-01")
        to_only = await client.get("/?to=2026-09-01")
        empty = await client.get("/?from=2027-01-01&to=2027-01-02")
        assert "From 2026-09-01" in from_only.text
        assert "Through 2026-09-01" in to_only.text
        assert "Nothing was published in this date range (UTC)." in empty.text
        assert "Nothing was published Posts published" not in empty.text
        assert "No retained successful job record" in empty.text
        assert 'aria-label="Sync now"' in from_only.text
        assert 'aria-label="Export"' in from_only.text
        assert "Export all posts" in from_only.text
        assert "Export all comments" in from_only.text
        assert "All stored history across active channels, independent of the selected date range and Explorer filters." in from_only.text
        assert "Post · published ↓" not in from_only.text
        assert "Last successful collection:" in from_only.text
        assert "Last counter change:" in from_only.text


@pytest.mark.asyncio
async def test_overview_collection_freshness_uses_job_completion_and_preserves_last_poll(tmp_path):
    from datetime import datetime, timezone

    class FreshnessDB(_T46FakeDB):
        async def last_successful_collection_at(self, channel_id=None):
            return datetime(2026, 10, 7, 23, 53, tzinfo=timezone.utc) if channel_id == 1 else None

    app, _, _ = _make_app(tmp_path, db=FreshnessDB())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/login", data={"username": "admin", "password": "pw"})
        page = await client.get("/?channel=1")
        assert "Last successful collection: <strong>2026-10-07 23:53 UTC" in page.text
        assert "Last counter change: <strong>2026-09-06 12:00 UTC" in page.text
        data = (await client.get("/api/overview?channel=1")).json()
        assert data["last_successful_collection_at"] == "2026-10-07T23:53:00+00:00"
        assert data["kpis"]["last_poll"] == "2026-09-06 12:00:00+00:00"


@pytest.mark.asyncio
async def test_explorer_clamps_page_to_filtered_result_count(tmp_path):
    class ExplorerDB(_T46FakeDB):
        def __init__(self):
            super().__init__()
            self.offsets = []

        async def explorer_posts(self, **query):
            rows = [row for row in self.posts if query["channel_id"] is None or row["channel_id"] == query["channel_id"]]
            self.offsets.append(query["offset"])
            return rows[query["offset"]:query["offset"] + query["limit"]], len(rows)

    db = ExplorerDB()
    app, _, _ = _make_app(tmp_path, db=db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/login", data={"username": "admin", "password": "pw"})
        response = await client.get("/api/explorer?channel=1&page=12&page_size=1")
        assert response.status_code == 200
        payload = response.json()
        assert payload["page"] == payload["total_pages"] == 2
        assert [row["id"] for row in payload["rows"]] == [2]
        assert db.offsets == [11, 1]


@pytest.mark.asyncio
async def test_single_day_cohort_selects_one_post(tmp_path):
    app, _, _ = _make_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/login", data={"username": "admin", "password": "pw"})
        page = await client.get("/?from=2026-09-06&to=2026-09-06")
        assert page.status_code == 200
        assert "5,120" in page.text
        chart = _chart_payload(page.text)
        assert chart["labels"] == ["2026-09-06"]
        assert chart["views"] == [5120]
        assert chart["postsPerDay"] == [1]


@pytest.mark.asyncio
async def test_empty_cohort_shows_zero_and_recovery(tmp_path):
    app, _, _ = _make_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/login", data={"username": "admin", "password": "pw"})
        page = await client.get("/?from=2026-01-01&to=2026-01-02")
        assert page.status_code == 200
        assert "No posts in this window" in page.text
        assert "Show available history" in page.text
        chart = _chart_payload(page.text)
        assert chart["labels"] == ["2026-01-01", "2026-01-02"]
        assert sum(chart["views"]) == 0


@pytest.mark.asyncio
async def test_reversed_cohort_is_rejected_without_partial_apply(tmp_path):
    app, _, _ = _make_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/login", data={"username": "admin", "password": "pw"})
        bad = await client.get("/?from=2026-09-06&to=2026-08-24")
        assert bad.status_code == 422
        assert bad.json()["error"]["code"] == "invalid_date"


@pytest.mark.asyncio
async def test_ui_and_api_share_one_cohort(tmp_path):
    app, _, _ = _make_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/login", data={"username": "admin", "password": "pw"})
        params = "channel=1&from=2026-08-24&to=2026-08-30"
        html_payload = (await client.get(f"/?{params}&format=json")).json()
        api_payload = (await client.get(f"/api/overview?{params}")).json()
        assert html_payload["kpis"] == api_payload["kpis"]
        assert html_payload["kpis"]["views"] == 3000
        assert html_payload["chart"]["labels"] == api_payload["series"]["days"]
        assert html_payload["chart"]["views"] == api_payload["series"]["views"]


def test_overview_controls_expose_dates_metric_modes_and_summary():
    source = (ROOT / "app/web/templates/dashboard.html").read_text(encoding="utf-8")
    assert 'name="from"' in source
    assert 'name="to"' in source
    assert 'type="date"' in source
    assert "Apply dates" in source
    assert "Available history" in source
    assert 'aria-label="Date range"' in source
    assert 'id="chartMetric"' in source
    for metric in ("views", "reactions", "comments", "shares"):
        assert f'value="{metric}"' in source
    assert 'data-chart-mode="daily"' in source
    assert 'data-chart-mode="cumulative"' in source
    assert 'data-chart-period="day"' in source
    assert 'data-chart-period="week"' in source
    assert 'data-chart-period="month"' in source
    assert "Daily posts" in source
    assert "Cumulative in window" in source
    assert "data-chart-summary" in source
    assert "data-chart-prev" in source
    assert "data-chart-next" in source
    assert 'data-chart-readout' in source
    assert 'aria-live="polite"' in source
    assert "Views · selected dates" in source
    assert "Latest known totals grouped by publication date" in source
    assert "Find what resonated" in source
    assert 'class="explorer-filter-popover"' in source
    assert "View data" not in source
    assert "data-table" not in source


def test_chart_client_uses_one_metric_accent_monotone_and_keyboard_values():
    source = (ROOT / "app/web/static/app.js").read_text(encoding="utf-8")
    assert "cubicInterpolationMode: 'monotone'" in source
    assert "chartFills" in source
    assert "getElementById('chartMode')" in source
    assert "setActiveElements" in source
    assert "data-chart-readout" in source
    assert "data-chart-summary" in source
    assert "running.push(total)" in source
    # A single selected-metric dataset replaces the old four-series Overview
    # chart (the per-post page chart keeps its own four series).
    assert "[data.views, data.reactions, data.comments, data.shares]" not in source
    assert "borderColor: chartColors[state.metric]" in source


@pytest.mark.asyncio
async def test_export_covers_stored_history_regardless_of_chart_window(tmp_path):
    app, _, _ = _make_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/login", data={"username": "admin", "password": "pw"})
        plain = await client.get("/export.csv?channel=1")
        scoped = await client.get("/export.csv?channel=1&from=2026-09-06&to=2026-09-06")
        assert plain.status_code == 200 == scoped.status_code
        assert plain.text == scoped.text
        assert plain.headers["X-Export-Scope"] == "stored-history:channel-1"
        assert plain.headers["X-Export-Limit"] == "100000"
        assert plain.headers["X-Export-Total"] == "2"
        assert plain.headers["X-Export-Truncated"] == "false"
        assert plain.text.splitlines()[0] == "post_db_id,message_id,channel,posted_at_utc,text,views,reactions,comments,shares,updated_at_utc"


@pytest.mark.asyncio
async def test_export_channel_cells_stay_formula_safe(tmp_path):
    db = _T46FakeDB()
    db.channels = [
        {"id": 1, "identifier": "=HYPERLINK(\"https://evil.test\")", "title": "Evil", "chat_id": 1, "active": True},
    ]
    for post in db.posts:
        post["identifier"] = "=HYPERLINK(\"https://evil.test\")"
    app, _, _ = _make_app(tmp_path, db=db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/login", data={"username": "admin", "password": "pw"})
        response = await client.get("/export.csv?channel=1")
        assert response.status_code == 200
        assert "'=HYPERLINK" in response.text


@pytest.mark.asyncio
async def test_sync_reports_scheduling_not_completion(tmp_path):
    app, collector, _ = _make_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/login", data={"username": "admin", "password": "pw"})
        refresh = await client.post("/refresh", data={"channel": "1"}, follow_redirects=False)
        assert refresh.status_code == 303
        assert collector.scheduled == ["web"]
        location = refresh.headers["location"]
        assert "Refresh+started" in location
        assert "complet" not in location.lower()
        # Feedback belongs to the existing header, leaving the data layout intact.
        page = await client.get(location)
        assert page.status_code == 200
        header, main = page.text.split('<main id="main-content">', 1)
        assert 'class="sync-feedback" role="status"' in header
        assert "Refresh started - numbers will update shortly" in header
        assert "Refresh started" not in main
        assert "Latest known metrics · selected publication dates" not in main


@pytest.mark.asyncio
async def test_web_only_deployments_keep_refresh_truthful(tmp_path):
    app, _, _ = _make_app(tmp_path, process_role="web")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/login", data={"username": "admin", "password": "pw"})
        refresh = await client.post("/refresh", follow_redirects=False)
        assert refresh.status_code == 303
        assert "collector+worker" in refresh.headers["location"]
