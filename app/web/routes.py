"""Web admin panel: FastAPI + server-rendered Jinja2 templates.

Auth is a signed-cookie session (itsdangerous) behind a simple login form,
so it works both locally and behind a reverse proxy on Hetzner.
"""

from __future__ import annotations

import csv
import functools
import hashlib
import io
import logging
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from ..collector import Collector
from ..channel_metrics import empty_metrics
from ..config import Settings
from ..postgres_db import cohort_bounds
from ..workspace_settings import RuntimeSettings
from ..telegram_formatting import normalize_entities, render_telegram_html
from .dependencies import csrf_token as shared_csrf_token
from .dependencies import require_auth as shared_require_auth
from .dependencies import require_csrf as shared_require_csrf
from .dependencies import WorkspaceContext
from ..studio.routes import _TEMPLATES as _studio_templates
from ..studio.routes import build_router as build_studio_router
from ..studio.repository import StudioRepository
from ..studio.service import StudioService
from .settings_routes import router as settings_router
from .links import telegram_message_link

log = logging.getLogger("web")

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

_ORDER_KEYS = {"date", "views", "reactions", "comments", "shares"}

# Login rate limiting: 5 failures per (client IP, username) per 5 minutes, so
# one actor's guessing cannot lock the owner out behind a shared address.
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 5 * 60
_LOGIN_ATTEMPTS: dict[tuple[str, str], list[float]] = {}


def _client_ip(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _rate_limit_key(request: Request, username: str) -> tuple[str, str]:
    return (_client_ip(request), (username or "").lower())


def _prune_attempts(now: float) -> None:
    cutoff = now - _LOGIN_WINDOW_SECONDS
    for key, stamps in list(_LOGIN_ATTEMPTS.items()):
        filtered = [t for t in stamps if t > cutoff]
        if filtered:
            _LOGIN_ATTEMPTS[key] = filtered
        else:
            _LOGIN_ATTEMPTS.pop(key, None)


def _is_rate_limited(key: tuple[str, str]) -> tuple[bool, int]:
    now = time.monotonic()
    _prune_attempts(now)
    stamps = _LOGIN_ATTEMPTS.get(key, [])
    if len(stamps) >= _LOGIN_MAX_ATTEMPTS:
        oldest = min(stamps) if stamps else now
        retry_after = int(max(1, min(_LOGIN_WINDOW_SECONDS, _LOGIN_WINDOW_SECONDS - (now - oldest))))
        return True, retry_after
    return False, 0


def _record_failed_attempt(key: tuple[str, str]) -> None:
    now = time.monotonic()
    _prune_attempts(now)
    _LOGIN_ATTEMPTS.setdefault(key, []).append(now)


def _clear_attempts(key: tuple[str, str]) -> None:
    _LOGIN_ATTEMPTS.pop(key, None)


def _sanitize_csv_cell(value: object) -> object:
    """Neutralise spreadsheet formula triggers in free-text cells.

    Only string values are touched: numeric ids such as negative discussion
    chat ids and datetime objects must round-trip unchanged.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    if value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _sanitize_channel_csv_cell(value: object) -> object:
    """Protect a channel cell while keeping normal ``@name`` identifiers readable."""

    text = str(value or "")
    # A valid username is not a spreadsheet formula; preserve its conventional
    # leading @ so exports remain easy to paste back into Settings.
    if text.startswith("@") and text[1:] and all(ch.isalnum() or ch == "_" for ch in text[1:]):
        return text
    return _sanitize_csv_cell(value)


def reset_login_rate_limiter() -> None:
    _LOGIN_ATTEMPTS.clear()


@functools.lru_cache(maxsize=1)
def static_asset_version() -> str:
    """Short content hash of the first-party static assets referenced by templates."""
    digest = hashlib.sha256()
    for name in ("favicon.svg", "app.js", "style.css", "ui-controls.js", "dropdowns.css", "chevron-down.svg", "studio-dist/assets/studio.js", "studio-dist/assets/index.css"):
        path = WEB_DIR / "static" / name
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(name.encode("utf-8"))
    return digest.hexdigest()[:12]


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings | RuntimeSettings):
        super().__init__(app)
        self._settings = settings

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'"
        )
        if self._settings.behind_tls:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


def _optional_positive_int(value: str | int | None) -> int | None:
    """Treat an empty HTML select value as no filter instead of a 422 error."""
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _history_window(value: str | int | None) -> tuple[int | None, str]:
    """Return (days, UI value); None represents the entire stored history."""
    raw = str(value or "14").strip().lower()
    if raw == "all":
        return None, "all"
    try:
        parsed = int(raw)
    except ValueError:
        parsed = 14
    parsed = max(3, min(parsed, 3650))
    return parsed, str(parsed)


def _page_number(value: str | int | None) -> int:
    """Parse a dashboard page defensively; malformed values return page one."""

    try:
        return max(1, int(value or 1))
    except (TypeError, ValueError):
        return 1


_EXPORT_ROW_LIMIT = 100000
_EXPLORER_SORTS = {"date", "views", "reactions", "comments", "shares"}
_EXPLORER_PAGE_SIZE_MAX = 100


def _wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    if "application/json" in accept.lower():
        # Explicit ?format=html still forces HTML for debugging.
        if request.query_params.get("format") == "html":
            return False
        return True
    return request.query_params.get("format") == "json"


def _json_error(code: str, message: str, status_code: int = 422) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, "retryable": False}},
        status_code=status_code,
    )


def _parse_cohort(request: Request) -> tuple[object | None, object | None, JSONResponse | None]:
    """Parse inclusive UTC From/To (half-open next-day) or return a 422 JSON."""

    raw_from = request.query_params.get("from", "")
    raw_to = request.query_params.get("to", "")
    if not raw_from.strip() and not raw_to.strip():
        return None, None, None
    try:
        cohort_bounds(raw_from if raw_from.strip() else None, raw_to if raw_to.strip() else None)
    except ValueError as exc:
        return None, None, _json_error("invalid_date", str(exc), 422)
    # Return raw strings; the repository parses again so SQL binding stays UTC.
    return (raw_from.strip() or None, raw_to.strip() or None, None)


def _overview_cohort(from_date, to_date, days):
    if from_date is not None or to_date is not None or days is None:
        return from_date, to_date
    today = datetime.now(timezone.utc).date()
    return (today - timedelta(days=days - 1)).isoformat(), today.isoformat()


def _strict_channel(value: str | int | None) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("Invalid channel: expected a positive integer.") from None
    if parsed <= 0:
        raise ValueError("Invalid channel: expected a positive integer.")
    return parsed


def _parse_channel_list(value: str | None) -> list[int] | None:
    if value is None or not str(value).strip():
        return None
    parts = [part.strip() for part in str(value).split(",") if part.strip()]
    if not parts:
        return None
    if len(parts) > 50:
        raise ValueError("Invalid channels: at most 50 channels.")
    parsed: list[int] = []
    for part in parts:
        try:
            number = int(part)
        except (TypeError, ValueError):
            raise ValueError("Invalid channels: expected comma-separated positive integers.") from None
        if number <= 0:
            raise ValueError("Invalid channels: expected comma-separated positive integers.")
        parsed.append(number)
    return list(dict.fromkeys(parsed))


def _parse_sort(value: str | None) -> str:
    normalized = str(value or "date").strip().lower()
    if normalized not in _EXPLORER_SORTS:
        raise ValueError("Invalid sort: expected one of date, views, reactions, comments, shares.")
    return normalized


def _parse_metric_bound(name: str, value: str | int | None) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"Invalid {name}: expected a non-negative integer.") from None
    if parsed < 0 or parsed > 2_147_483_647:
        raise ValueError(f"Invalid {name}: expected 0..2147483647.")
    return parsed


def _parse_search(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = str(value).strip()
    if not candidate:
        return None
    if len(candidate) > 200:
        raise ValueError("Invalid search: must be at most 200 characters.")
    return candidate


def _parse_page(value: str | int | None, *, name: str = "page", minimum: int = 1, maximum: int = 10000) -> int:
    try:
        parsed = int(str(value or minimum).strip())
    except (TypeError, ValueError):
        raise ValueError(f"Invalid {name}: expected {minimum}..{maximum}.") from None
    if not minimum <= parsed <= maximum:
        raise ValueError(f"Invalid {name}: expected {minimum}..{maximum}.")
    return parsed


def _parse_page_size(value: str | int | None) -> int:
    return _parse_page(value, name="page_size", minimum=1, maximum=_EXPLORER_PAGE_SIZE_MAX)


def _num(v) -> str:
    try:
        return f"{int(v or 0):,}"
    except (TypeError, ValueError):
        return str(v)


def _dt(v) -> str:
    return str(v or "")[:16]


def _post_link(row) -> str:
    """Public Telegram link when we can build one, else ``#``."""

    row = row if hasattr(row, "get") else dict(row)
    return telegram_message_link(
        identifier=row.get("identifier"),
        chat_id=row.get("chat_id"),
        message_id=row.get("message_id"),
    )


def _comment_link(row) -> str:
    row = row if hasattr(row, "get") else dict(row)
    return telegram_message_link(
        identifier=row.get("discussion_username"),
        chat_id=row.get("discussion_chat_id"),
        message_id=row.get("telegram_message_id"),
    )


def _load_or_create_secret(settings: Settings | RuntimeSettings) -> str:
    if settings.session_secret:
        return settings.session_secret
    secret_file = settings.data_path / ".secret"
    if secret_file.exists():
        key = secret_file.read_text(encoding="utf-8").strip()
        if key:
            return key
    key = secrets.token_urlsafe(32)
    secret_file.write_text(key, encoding="utf-8")
    try:
        secret_file.chmod(0o600)
    except OSError:
        pass
    return key


def create_app(collector: Collector, settings: Settings | RuntimeSettings, workspace_settings=None) -> FastAPI:
    db = collector.db
    app = FastAPI(title="TGhost", docs_url=None, redoc_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=_load_or_create_secret(settings),
        https_only=settings.behind_tls,
        same_site="lax",
    )
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
    db_context = WorkspaceContext(
        user_id=getattr(db, "user_id", None)
        or uuid.uuid5(uuid.NAMESPACE_URL, f"telegram-stats:user:{settings.admin_username}"),
        workspace_id=getattr(db, "workspace_id", None),
        workspace_slug=getattr(db, "workspace_slug", "community"),
        role="owner",
    )
    app.state.workspace_context = db_context
    app.state.settings = settings
    app.state.workspace_settings = workspace_settings
    app.state.db = db
    # The combined process replaces this with its APScheduler callback.  A
    # web-only process leaves it unset because its worker owns scheduling.
    app.state.on_poll_interval_change = None
    studio_repository = StudioRepository(db)
    app.state.studio_repository = studio_repository
    app.state.studio_service = StudioService(studio_repository, settings)

    templates.env.filters["num"] = _num
    templates.env.filters["dt"] = _dt
    templates.env.globals["app_name"] = "TGhost"
    # Content-hash cache busting: a changed app.js/style.css must never be
    # served from a browser cache keyed on a hand-bumped ?v= number.
    templates.env.globals["asset_version"] = static_asset_version()
    _studio_templates.env.globals["asset_version"] = static_asset_version()
    _studio_templates.env.globals["app_name"] = "TGhost"
    # Settings page needs the same globals
    from .settings_routes import templates as _settings_templates

    _settings_templates.env.filters["num"] = _num
    _settings_templates.env.filters["dt"] = _dt
    _settings_templates.env.globals["app_name"] = "TGhost"
    _settings_templates.env.globals["asset_version"] = static_asset_version()

    def render(request: Request, name: str, ctx: dict, status_code: int = 200):
        # can_manage_settings is owner-only, used for sidebar link
        ctx.setdefault("can_manage_settings", getattr(request.app.state.workspace_context, "role", "") == "owner")
        ctx.update({"request": request})
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def require_auth(request: Request) -> None:
        shared_require_auth(request)

    def csrf_token(request: Request) -> str:
        return shared_csrf_token(request)

    def require_csrf(request: Request) -> None:
        shared_require_csrf(request)

    # Keep auth/CSRF and service wiring in one production Studio router.
    app.state.csrf_token = csrf_token
    from .settings_routes import chatgpt_callback
    app.add_api_route("/auth/callback", chatgpt_callback, methods=["GET"], include_in_schema=False)
    app.include_router(build_studio_router())
    app.include_router(settings_router)
    from ..publishing.routes import router as publishing_router
    app.include_router(publishing_router)

    # ---------------- auth ----------------

    @app.get("/healthz")
    async def healthz():
        """Unauthenticated process/readiness probe with no private payload."""
        check = getattr(db, "healthcheck", None)
        if check is not None:
            try:
                await (check())
            except Exception as exc:  # noqa: BLE001 - health endpoint must be bounded
                log.warning("database readiness check failed: %s", type(exc).__name__)
                return Response(content='{"status":"not_ready","storage":"postgresql"}', media_type="application/json", status_code=503)
        payload: dict[str, object] = {"status": "ok", "storage": "postgresql", "last_successful_cycle_at": None}
        try:
            cycle_getter = getattr(db, "last_successful_cycle_at", None)
            last = await (cycle_getter()) if cycle_getter is not None else None
            if last is not None:
                payload["last_successful_cycle_at"] = last.isoformat() if hasattr(last, "isoformat") else str(last)
                try:
                    poll_minutes = float(settings.poll_minutes)
                except (TypeError, ValueError):
                    poll_minutes = 15.0
                moment = last if getattr(last, "tzinfo", None) else last.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - moment > timedelta(minutes=3 * poll_minutes):
                    payload["status"] = "degraded"
        except Exception:  # noqa: BLE001 - health must stay bounded without a cycle signal
            pass
        return payload

    @app.get("/login")
    async def login_form(request: Request):
        if request.session.get("auth"):
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html", {"error": ""})

    @app.post("/login")
    async def login(request: Request, username: str = Form(""), password: str = Form("")):
        key = _rate_limit_key(request, username)
        limited, retry_after = _is_rate_limited(key)
        if limited:
            return Response(
                content="Too many failed login attempts. Try again later.",
                status_code=429,
                headers={"Retry-After": str(retry_after)},
                media_type="text/plain",
            )
        ok_user = secrets.compare_digest(username.encode("utf-8"), settings.admin_username.encode("utf-8"))
        ok_pass = secrets.compare_digest(password.encode("utf-8"), settings.admin_password.encode("utf-8"))
        if not (ok_user and ok_pass):
            _record_failed_attempt(key)
            log.warning("failed admin login from %s", key[0])
            return render(request, "login.html", {"error": "Invalid username or password"}, 401)
        _clear_attempts(key)
        request.session["auth"] = True
        if db_context.user_id is not None:
            request.session["user_id"] = str(db_context.user_id)
        request.session["workspace_slug"] = db_context.workspace_slug
        return RedirectResponse("/", status_code=303)

    @app.get("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # ---------------- pages ----------------

    @app.get("/")
    async def dashboard(
        request: Request,
        channel: str = "",
        order: str = "date",
        days: str = "14",
        page: str = "1",
        msg: str = "",
    ):
        require_auth(request)
        channel_id = _optional_positive_int(channel)
        from_param, to_param, cohort_error = _parse_cohort(request)
        if cohort_error is not None:
            return cohort_error
        use_cohort = from_param is not None or to_param is not None
        window_days, window_value = _history_window(days)
        from_param, to_param = _overview_cohort(from_param, to_param, window_days)
        use_cohort = from_param is not None or to_param is not None
        current_page = _page_number(page)
        page_size = 100
        channels = await (db.get_channels())
        try:
            if use_cohort:
                k = await (db.kpis(channel_id, from_date=from_param, to_date=to_param))
            else:
                k = await (db.kpis(channel_id))
        except TypeError as exc:
            if "from_date" in str(exc) or "to_date" in str(exc):
                return _json_error("unavailable", "Date-cohort queries are unavailable for this storage backend.", 503)
            raise
        history_totals = await db.kpis(channel_id) if use_cohort else k
        total_rows = max(0, int(history_totals.get("posts", 0) or 0))
        total_pages = max(1, (total_rows + page_size - 1) // page_size)
        current_page = min(current_page, total_pages)
        offset = (current_page - 1) * page_size
        try:
            if use_cohort:
                ts = await (db.timeseries_totals(days=None, channel_id=channel_id, from_date=from_param, to_date=to_param))
            else:
                ts = await (db.timeseries_totals(days=window_days, channel_id=channel_id))
        except TypeError as exc:
            if "from_date" in str(exc) or "to_date" in str(exc):
                return _json_error("unavailable", "Date-cohort queries are unavailable for this storage backend.", 503)
            raise
        stats_order = order if order in _ORDER_KEYS else "date"
        reader = getattr(db, "latest_stats")
        try:
            rows = await (
                reader(channel_id=channel_id, order=stats_order, limit=page_size, offset=offset)
            )
        except TypeError as exc:
            # A short-lived compatibility facade used by older local plugins
            # may not expose ``offset`` yet. Fetch a bounded page and slice it
            # rather than making the dashboard fail during an upgrade.
            if "offset" not in str(exc).lower():
                raise
            legacy_rows = await (
                reader(channel_id=channel_id, order=stats_order, limit=100_000)
            )
            rows = legacy_rows[offset : offset + page_size]
        rows = [dict(r) | {"link": _post_link(r)} for r in rows]
        first_row = offset + 1 if rows else 0
        last_row = offset + len(rows)
        has_next = last_row < total_rows
        first_page_link = max(1, min(current_page - 2, total_pages - 4))
        last_page_link = min(total_pages, first_page_link + 4)
        page_numbers = list(range(first_page_link, last_page_link + 1))
        metrics_reader = getattr(db, "channel_metrics", None)
        cm = await metrics_reader(channel_id, from_param, to_param) if metrics_reader is not None else empty_metrics()
        collection_reader = getattr(db, "last_successful_collection_at", None)
        last_successful_collection_at = await collection_reader(channel_id) if collection_reader is not None else None
        chart = {
            "labels": ts["days"],
            "views": ts["views"],
            "reactions": ts["reactions"],
            "comments": ts["comments"],
            "shares": ts["shares"],
            "postsPerDay": ts["posts_per_day"],
        }
        if _wants_json(request):
            return {
                "channel_id": channel_id,
                "from": from_param,
                "to": to_param,
                "cohort": {"from": from_param, "to": to_param} if use_cohort else None,
                "kpis": {
                    "posts": int(k.get("posts", 0) or 0),
                    "views": int(k.get("views", 0) or 0),
                    "reactions": int(k.get("reactions", 0) or 0),
                    "comments": int(k.get("comments", 0) or 0),
                    "shares": int(k.get("shares", 0) or 0),
                    "collected_comments": int(k.get("collected_comments", 0) or 0),
                    "last_poll": str(k.get("last_poll") or "") or None,
                },
                "last_successful_collection_at": last_successful_collection_at.isoformat() if hasattr(last_successful_collection_at, "isoformat") else (str(last_successful_collection_at) if last_successful_collection_at else None),
                "channel_metrics": cm,
                "chart": chart,
                "pagination": {
                    "page": current_page,
                    "page_size": page_size,
                    "total_rows": total_rows,
                    "total_pages": total_pages,
                },
            }
        return render(request, "dashboard.html", {
            "channels": channels,
            "current_channel": channel_id,
            "order": stats_order,
            "days": window_value,
            "applied_from": from_param or "",
            "applied_to": to_param or "",
            "msg": msg,
            "k": k,
            "rows": rows,
            "page": current_page,
            "page_size": page_size,
            "total_rows": total_rows,
            "total_pages": total_pages,
            "page_numbers": page_numbers,
            "first_row": first_row,
            "last_row": last_row,
            "has_next": has_next,
            "channel_metrics": cm,
            "last_successful_collection_at": last_successful_collection_at,
            "chart": chart,
        })

    @app.get("/api/overview")
    async def api_overview(request: Request):
        """Same UTC publication-date cohort for chart and KPIs (JSON).

        Query: ``channel`` (optional positive int, None=All active),
        ``from``/``to`` (optional YYYY-MM-DD UTC, inclusive via half-open
        next-day).  Latest known metric totals of selected posts; All active
        excludes paused, explicit stored channels remain readable.
        """

        require_auth(request)
        try:
            channel_id = _strict_channel(request.query_params.get("channel", ""))
        except ValueError as exc:
            return _json_error("invalid_channel", str(exc), 422)
        from_param, to_param, cohort_error = _parse_cohort(request)
        if cohort_error is not None:
            return cohort_error
        window_days, _ = _history_window(request.query_params.get("days", "14"))
        from_param, to_param = _overview_cohort(from_param, to_param, window_days)
        try:
            try:
                k = await (db.kpis(channel_id, from_date=from_param, to_date=to_param))
            except TypeError as exc:
                if "from_date" in str(exc) or "to_date" in str(exc):
                    return _json_error("unavailable", "Date-cohort queries are unavailable for this storage backend.", 503)
                raise
            try:
                ts = await (
                    db.timeseries_totals(days=None, channel_id=channel_id, from_date=from_param, to_date=to_param)
                    if (from_param is not None or to_param is not None)
                    else db.timeseries_totals(days=window_days, channel_id=channel_id)
                )
            except TypeError as exc:
                if "from_date" in str(exc) or "to_date" in str(exc):
                    return _json_error("unavailable", "Date-cohort queries are unavailable for this storage backend.", 503)
                raise
        except ValueError as exc:
            return _json_error("invalid_date", str(exc), 422)
        metrics_reader = getattr(db, "channel_metrics", None)
        cm = await metrics_reader(channel_id, from_param, to_param) if metrics_reader is not None else empty_metrics()
        collection_reader = getattr(db, "last_successful_collection_at", None)
        last_successful_collection_at = await collection_reader(channel_id) if collection_reader is not None else None
        return {
            "channel_id": channel_id,
            "from": from_param,
            "to": to_param,
            "kpis": {
                "posts": int(k.get("posts", 0) or 0),
                "views": int(k.get("views", 0) or 0),
                "reactions": int(k.get("reactions", 0) or 0),
                "comments": int(k.get("comments", 0) or 0),
                "shares": int(k.get("shares", 0) or 0),
                "collected_comments": int(k.get("collected_comments", 0) or 0),
                "last_poll": str(k.get("last_poll") or "") or None,
            },
            "last_successful_collection_at": last_successful_collection_at.isoformat() if hasattr(last_successful_collection_at, "isoformat") else (str(last_successful_collection_at) if last_successful_collection_at else None),
            "channel_metrics": cm,
            "series": {
                "days": ts["days"],
                "views": ts["views"],
                "reactions": ts["reactions"],
                "comments": ts["comments"],
                "shares": ts["shares"],
                "posts_per_day": ts["posts_per_day"],
            },
        }

    @app.get("/api/explorer")
    async def api_explorer(request: Request):
        """Server-side Post Explorer (JSON): search, one sort, channel subset,
        optional min/max for all four metrics, stable tie-breaks, total count
        and bounded pagination.  Filters never expand the workspace scope;
        Explorer history is independent of chart dates.
        """

        require_auth(request)
        params = request.query_params
        try:
            if params.get("channel") and params.get("channels"):
                return _json_error("invalid_channel", "Specify channel or channels, not both.", 422)
            channel_id = _strict_channel(params.get("channel", ""))
            channel_ids = _parse_channel_list(params.get("channels"))
            sort = _parse_sort(params.get("sort", "date"))
            search = _parse_search(params.get("q"))
            min_views = _parse_metric_bound("min_views", params.get("min_views"))
            max_views = _parse_metric_bound("max_views", params.get("max_views"))
            min_reactions = _parse_metric_bound("min_reactions", params.get("min_reactions"))
            max_reactions = _parse_metric_bound("max_reactions", params.get("max_reactions"))
            min_comments = _parse_metric_bound("min_comments", params.get("min_comments"))
            max_comments = _parse_metric_bound("max_comments", params.get("max_comments"))
            min_shares = _parse_metric_bound("min_shares", params.get("min_shares"))
            max_shares = _parse_metric_bound("max_shares", params.get("max_shares"))
            page = _parse_page(params.get("page", "1"))
            page_size = _parse_page_size(params.get("page_size", "50"))
            for low, high, name in (
                (min_views, max_views, "views"),
                (min_reactions, max_reactions, "reactions"),
                (min_comments, max_comments, "comments"),
                (min_shares, max_shares, "shares"),
            ):
                if low is not None and high is not None and low > high:
                    return _json_error("invalid_range", f"Invalid {name} range: min must not exceed max.", 422)
        except ValueError as exc:
            message = str(exc)
            code = "invalid_sort" if "sort" in message.lower() else (
                "invalid_search" if "search" in message.lower() else (
                    "invalid_channel" if "channel" in message.lower() else (
                        "invalid_page" if "page" in message.lower() else "invalid_range"
                    )
                )
            )
            return _json_error(code, message, 422)
        explorer = getattr(db, "explorer_posts", None)
        if explorer is None:
            return _json_error("unavailable", "Post Explorer is unavailable for this storage backend.", 503)
        try:
            rows, total = await explorer(
                channel_id=channel_id,
                channel_ids=channel_ids,
                search=search,
                sort=sort,
                min_views=min_views,
                max_views=max_views,
                min_reactions=min_reactions,
                max_reactions=max_reactions,
                min_comments=min_comments,
                max_comments=max_comments,
                min_shares=min_shares,
                max_shares=max_shares,
                limit=page_size,
                offset=(page - 1) * page_size,
            )
        except ValueError as exc:
            return _json_error("invalid_range", str(exc), 422)
        total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
        if page > total_pages:
            page = total_pages
            rows, total = await explorer(
                channel_id=channel_id,
                channel_ids=channel_ids,
                search=search,
                sort=sort,
                min_views=min_views,
                max_views=max_views,
                min_reactions=min_reactions,
                max_reactions=max_reactions,
                min_comments=min_comments,
                max_comments=max_comments,
                min_shares=min_shares,
                max_shares=max_shares,
                limit=page_size,
                offset=(page - 1) * page_size,
            )
            total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
        payload_rows = []
        for row in rows:
            item = dict(row)
            item["posted_at"] = str(item.get("posted_at") or "")
            item["updated_at"] = str(item.get("updated_at") or "")
            item["link"] = _post_link(item)
            payload_rows.append(item)
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "sort": sort,
            "rows": payload_rows,
        }

    @app.get("/api/posts/{post_id}")
    async def api_post_detail(request: Request, post_id: int):
        """Full post read model (JSON): complete sanitized text/entities,
        metrics, permitted discussion, snapshot timestamps and validated
        source URL.  IDs are authorized server-side against the workspace.
        """

        require_auth(request)
        if post_id <= 0:
            return _json_error("not_found", "Post not found.", 404)
        post = await (db.post_row(post_id))
        if post is None:
            return _json_error("not_found", "Post not found.", 404)
        hist = await (db.post_history(post_id))
        raw_comments = await (db.comments_for_post(post_id))
        # Permitted discussion only: workspace-scoped, non-deleted.
        comments = [dict(item) for item in raw_comments if not item.get("is_deleted")]
        row = dict(post)
        entities = normalize_entities(row.get("formatting_entities"))
        formatted = render_telegram_html(row.get("text"), entities)
        source_url = _post_link(row)
        validated_url: str | None = None if source_url == "#" else source_url
        snapshots = [
            {
                "taken_at": str(item.get("taken_at") or ""),
                "views": int(item.get("views", 0) or 0),
                "comments": int(item.get("comments", 0) or 0),
                "reactions": int(item.get("reactions", 0) or 0),
                "shares": int(item.get("shares", 0) or 0),
            }
            for item in hist
        ]
        discussion = [
            {
                "id": int(item.get("id", 0) or 0),
                "telegram_message_id": int(item.get("telegram_message_id", 0) or 0),
                "sender_name": str(item.get("sender_name") or ""),
                "sender_username": str(item.get("sender_username") or ""),
                "posted_at": str(item.get("posted_at") or ""),
                "edited_at": str(item.get("edited_at") or "") or None,
                "text": str(item.get("text") or ""),
                "reactions": int(item.get("reactions", 0) or 0),
                "link": _comment_link(item),
            }
            for item in comments
        ]
        return {
            "id": int(row["id"]),
            "message_id": int(row.get("message_id", 0) or 0),
            "channel_id": int(row.get("channel_id", 0) or 0),
            "channel_identifier": str(row.get("identifier") or ""),
            "channel_title": str(row.get("channel_title") or ""),
            "posted_at": str(row.get("posted_at") or ""),
            "text": str(row.get("text") or ""),
            "formatting_entities": entities,
            "formatted_html": formatted,
            "metrics": {
                "views": int(row.get("views", 0) or 0),
                "reactions": int(row.get("reactions", 0) or 0),
                "comments": int(row.get("comments", 0) or 0),
                "shares": int(row.get("shares", 0) or 0),
                "collected_comments": int(row.get("collected_comments", 0) or 0),
                "updated_at": str(row.get("updated_at") or "") or None,
            },
            "snapshots": snapshots,
            "snapshot_note": "Snapshot timestamps are collection times; the Overview chart uses publication dates.",
            "source_url": validated_url,
            "discussion": discussion,
        }

    @app.get("/post/{post_id}")
    async def post_detail(request: Request, post_id: int):
        require_auth(request)
        post = await (db.post_row(post_id))
        if post is None:
            raise HTTPException(status_code=404, detail="Post not found")
        hist = await (db.post_history(post_id))
        comments = [
            dict(comment) | {"link": _comment_link(comment)}
            for comment in await (db.comments_for_post(post_id))
        ]
        row = dict(post)
        row["link"] = _post_link(row)
        row["formatted_html"] = render_telegram_html(
            row.get("text"), row.get("formatting_entities")
        )
        chart = {
            "labels": [_dt(h["taken_at"]) for h in hist],
            "views": [h["views"] for h in hist],
            "comments": [h["comments"] for h in hist],
            "reactions": [h["reactions"] for h in hist],
            "shares": [h["shares"] for h in hist],
        }
        return render(request, "post.html", {
            "row": row,
            "hist": list(reversed(hist)),
            "comments": comments,
            "chart": chart,
        })

    @app.post("/refresh")
    async def refresh(request: Request, channel: str = Form("")):
        require_auth(request)
        if settings.process_role.strip().lower() == "web":
            # In split deployments only the worker owns a Telegram session.
            # Keep the button harmless and explain where refresh work lives.
            return RedirectResponse("/?msg=Refresh+is+handled+by+the+collector+worker", status_code=303)
        if hasattr(collector, "client") and collector.client is None:
            # PROCESS_ROLE=all intentionally serves the UI before Telegram is
            # configured.  Do not enqueue a background task that cannot run.
            request.session["flash_msg"] = "Configure Telegram before starting a refresh."
            return RedirectResponse("/settings#telegram", status_code=303)
        scheduler = getattr(collector, "schedule_poll", None)
        if scheduler is not None:
            scheduler(reason="web")
        else:
            # Keep compatibility with a collector supplied by an older plugin;
            # production Collector.schedule_poll provides supervised logging.
            import asyncio

            asyncio.create_task(collector.poll_all(reason="web"))
        url = "/?msg=Refresh+started+-+numbers+will+update+shortly"
        channel_id = _optional_positive_int(channel)
        if channel_id:
            url += f"&channel={channel_id}"
        return RedirectResponse(url, status_code=303)

    @app.get("/export.csv")
    async def export_csv(request: Request, channel: str = ""):
        require_auth(request)
        channel_id = _optional_positive_int(channel)
        rows = await (db.latest_stats(channel_id=channel_id, limit=_EXPORT_ROW_LIMIT))
        try:
            totals = await (db.kpis(channel_id))
            total_posts = int(totals.get("posts", 0) or 0)
        except Exception:  # noqa: BLE001 - export must stay available when counts fail
            total_posts = len(rows)
        truncated = total_posts > len(rows)
        scope = f"channel-{channel_id}" if channel_id else "all-active"
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "post_db_id", "message_id", "channel", "posted_at_utc", "text",
            "views", "reactions", "comments", "shares", "updated_at_utc",
        ])
        for r in rows:
            writer.writerow([
                _sanitize_csv_cell(r["id"]), _sanitize_csv_cell(r["message_id"]), _sanitize_channel_csv_cell(r["identifier"]), _sanitize_csv_cell(r["posted_at"]),
                _sanitize_csv_cell((r["text"] or "").replace("\n", " ")),
                _sanitize_csv_cell(r["views"]), _sanitize_csv_cell(r["reactions"]), _sanitize_csv_cell(r["comments"]), _sanitize_csv_cell(r["shares"]), _sanitize_csv_cell(r["updated_at"]),
            ])
        # Never silently claim a complete export after truncation: the stored
        # history scope, server cap and truncation state travel in headers.
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=tg-studio-export.csv",
                "X-Export-Scope": f"stored-history:{scope}",
                "X-Export-Limit": str(_EXPORT_ROW_LIMIT),
                "X-Export-Total": str(total_posts),
                "X-Export-Truncated": "true" if truncated else "false",
            },
        )

    @app.get("/export-comments.csv")
    async def export_comments_csv(request: Request, channel: str = ""):
        require_auth(request)
        channel_id = _optional_positive_int(channel)
        fetch = getattr(db, "all_comments")
        try:
            rows = await (fetch(channel_id=channel_id, limit=_EXPORT_ROW_LIMIT))
        except TypeError:
            rows = await (fetch(channel_id=channel_id))
            rows = rows[:_EXPORT_ROW_LIMIT]
        try:
            totals = await (db.kpis(channel_id))
            total_comments = int(totals.get("collected_comments", 0) or 0)
        except Exception:  # noqa: BLE001 - export must stay available when counts fail
            total_comments = len(rows)
        truncated = total_comments > len(rows)
        scope = f"channel-{channel_id}" if channel_id else "all-active"
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "comment_db_id", "post_db_id", "post_message_id", "channel",
            "comment_message_id", "discussion_chat_id", "posted_at_utc",
            "edited_at_utc", "sender_id", "sender_name", "sender_username",
            "text", "media_type", "reactions", "reply_to_message_id", "deleted",
        ])
        for row in rows:
            writer.writerow([
                _sanitize_csv_cell(row["id"]), _sanitize_csv_cell(row["post_id"]), _sanitize_csv_cell(row["post_message_id"]),
                _sanitize_channel_csv_cell(row["channel_identifier"]), _sanitize_csv_cell(row["telegram_message_id"]),
                _sanitize_csv_cell(row["discussion_chat_id"]), _sanitize_csv_cell(row["posted_at"]), _sanitize_csv_cell(row["edited_at"]),
                _sanitize_csv_cell(row["sender_id"]), _sanitize_csv_cell(row["sender_name"]), _sanitize_csv_cell(row["sender_username"]),
                _sanitize_csv_cell((row["text"] or "").replace("\n", " ")), _sanitize_csv_cell(row["media_type"]),
                _sanitize_csv_cell(row["reactions"]), _sanitize_csv_cell(row["reply_to_message_id"]), _sanitize_csv_cell(row["is_deleted"]),
            ])
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=tg-comments-export.csv",
                "X-Export-Scope": f"stored-history:{scope}",
                "X-Export-Limit": str(_EXPORT_ROW_LIMIT),
                "X-Export-Total": str(total_comments),
                "X-Export-Truncated": "true" if truncated else "false",
            },
        )

    return app
