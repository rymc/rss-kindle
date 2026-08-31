from __future__ import annotations

import hashlib
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app import __version__
from app.admin_service import AdminService
from app.auth import (
    build_login_redirect,
    decode_session_token,
    sanitize_next_path,
    validate_csrf,
)
from app.backup_service import BackupService
from app.config import Settings
from app.content_service import ExtractedArticle
from app.device_auth import DeviceAuthService
from app.freshrss import (
    FreshRSSEntry,
    FreshRSSError,
    FreshRSSFeed,
    FreshRSSGroup,
    FreshRSSNavigation,
    FreshRSSStreamPage,
)
from app.hacker_news import HackerNewsDiscussion
from app.mutations import MutationService
from app.repository import Repository
from app.utils import format_datetime, format_relative_time


class FreshRSSService(Protocol):
    def list_navigation(self) -> FreshRSSNavigation: ...

    def get_stream(
        self,
        *,
        scope_kind: str,
        scope_value: str | None = None,
        continuation: str | None = None,
        limit: int = 15,
        include_read: bool = False,
    ) -> FreshRSSStreamPage: ...

    def get_group(self, slug: str) -> FreshRSSGroup | None: ...

    def get_feed(self, token: str) -> FreshRSSFeed | None: ...

    def get_entry(self, entry_id: str) -> FreshRSSEntry | None: ...

    def mark_read(self, entry_ids: Iterable[str]) -> None: ...

    def mark_unread(self, entry_ids: Iterable[str]) -> None: ...

    def mark_starred(self, entry_ids: Iterable[str]) -> None: ...

    def mark_unstarred(self, entry_ids: Iterable[str]) -> None: ...


class ArticleService(Protocol):
    def ensure_extracted(self, entry: FreshRSSEntry) -> ExtractedArticle: ...


class HackerNewsService(Protocol):
    def get_discussion(self, item_id: int) -> HackerNewsDiscussion: ...


@dataclass(frozen=True)
class WebServices:
    settings: Settings
    repository: Repository
    freshrss: FreshRSSService
    mutations: MutationService
    extractor: ArticleService
    hacker_news: HackerNewsService
    backup: BackupService
    admin: AdminService
    device_auth: DeviceAuthService | None
    templates: Jinja2Templates
    login_required: bool


def build_templates(settings: Settings) -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(settings.base_dir / "app" / "templates"))
    templates.env.filters["format_datetime"] = format_datetime
    templates.env.filters["format_relative_time"] = format_relative_time
    templates.env.globals["static_versions"] = _static_asset_versions(
        settings.base_dir / "app" / "static"
    )
    return templates


def current_relative_url(request: Request) -> str:
    return (
        f"{request.url.path}?{request.url.query}"
        if request.url.query
        else request.url.path
    )


def request_session(request: Request) -> dict[str, Any] | None:
    return getattr(request.state, "auth_session", None)


def require_csrf(request: Request, submitted_token: str | None) -> None:
    if not getattr(request.state, "auth_enabled", False):
        return
    if not validate_csrf(request_session(request), submitted_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token.")


def record_timing(request: Request, name: str, started_at: float) -> None:
    timings = getattr(request.state, "server_timings", None)
    if timings is not None:
        timings.append((name, (time.perf_counter() - started_at) * 1000))


def build_template_context(
    services: WebServices,
    request: Request,
    *,
    page_title: str,
    active_group_slug: str | None = None,
    active_feed_id: str | None = None,
    show_site_header: bool = True,
    include_navigation: bool = False,
    include_feeds: bool = False,
    reader_script: bool = False,
) -> dict[str, Any]:
    groups: list[FreshRSSGroup] = []
    feeds: list[FreshRSSFeed] = []
    if show_site_header and (
        include_navigation or include_feeds or active_group_slug is not None
    ):
        try:
            navigation = services.freshrss.list_navigation()
        except (
            FreshRSSError,
            httpx.HTTPError,
        ) as exc:  # pragma: no cover - runtime path
            raise HTTPException(
                status_code=503, detail=f"FreshRSS request failed: {exc}"
            ) from exc
        groups = navigation.groups
        if include_feeds:
            feeds = navigation.feeds

    session = request_session(request) or {}
    active_group_name = next(
        (group.name for group in groups if group.slug == active_group_slug),
        None,
    )
    return {
        "request": request,
        "page_title": page_title,
        "groups": groups,
        "feeds": feeds,
        "active_group_slug": active_group_slug,
        "active_group_name": active_group_name,
        "active_feed_id": active_feed_id,
        "current_path": current_relative_url(request),
        "show_site_header": show_site_header,
        "auth_enabled": services.login_required,
        "current_user": session.get("sub"),
        "csrf_token": session.get("csrf", ""),
        "is_admin": getattr(request.state, "is_admin", False),
        "reader_script": reader_script,
    }


def action_response(
    request: Request, next_path: str, *, default: str = "/"
) -> Response:
    if request.headers.get("x-rss-kindle-action") == "1":
        return Response(status_code=204)
    return RedirectResponse(
        sanitize_next_path(next_path, default=default), status_code=303
    )


def reader_template_response(
    services: WebServices,
    request: Request,
    *,
    name: str,
    context: dict[str, Any],
    background: Any = None,
) -> Response:
    response = services.templates.TemplateResponse(
        request=request,
        name=name,
        context=context,
        background=background,
    )
    body = getattr(response, "body", b"")
    etag = f'W/"{hashlib.sha256(body).hexdigest()[:24]}"'
    validators = {
        value.strip()
        for value in request.headers.get("if-none-match", "").split(",")
        if value.strip()
    }
    if etag in validators or "*" in validators:
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": "private, no-cache"},
        )
    response.headers["ETag"] = etag
    return response


def install_request_middleware(app: FastAPI, services: WebServices) -> None:
    @app.middleware("http")
    async def reader_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        started_at = time.perf_counter()
        request.state.server_timings = []
        path = request.url.path
        asset_or_health = path in {"/health", "/favicon.ico"} or path.startswith(
            "/static/"
        )

        session = None
        if services.settings.app_auth_secret:
            session = decode_session_token(
                request.cookies.get(services.settings.app_session_cookie_name),
                secret=services.settings.app_auth_secret,
            )
        if session is None and services.device_auth is not None and not asset_or_health:
            device_session = services.device_auth.authenticate(
                request.cookies.get(services.settings.app_device_cookie_name)
            )
            if device_session is not None:
                session = {
                    "sub": device_session.device.name,
                    "csrf": device_session.csrf_token,
                    "exp": device_session.device.expires_at,
                    "auth_type": "device",
                    "device_id": device_session.device.device_id,
                }

        request.state.auth_session = session
        request.state.auth_enabled = services.login_required
        request.state.is_admin = bool(
            session and session.get("auth_type") == "password"
        )

        auth_exempt = path in {
            "/login",
            "/activate",
            "/health",
            "/favicon.ico",
        } or path.startswith("/static/")
        if services.login_required and not auth_exempt and session is None:
            if request.method in {"GET", "HEAD"}:
                next_path = sanitize_next_path(current_relative_url(request))
                response = RedirectResponse(
                    build_login_redirect("/login", next_path=next_path),
                    status_code=303,
                )
            else:
                response = HTMLResponse("Authentication required.", status_code=401)
            return set_security_headers(response)

        response = await call_next(request)
        timings = [
            f"{name};dur={duration_ms:.1f}"
            for name, duration_ms in request.state.server_timings
        ]
        timings.append(f"total;dur={(time.perf_counter() - started_at) * 1000:.1f}")
        response.headers["Server-Timing"] = ", ".join(timings)
        response.headers["X-RSS-Kindle-Version"] = __version__
        return set_security_headers(
            response,
            static_asset=path.startswith("/static/"),
            history_cache=(
                request.method in {"GET", "HEAD"}
                and response.status_code in {200, 304}
                and _is_reader_page(path)
            ),
        )


def set_security_headers(
    response: Response,
    *,
    static_asset: bool = False,
    history_cache: bool = False,
) -> Response:
    if static_asset:
        cache_control = "public, max-age=31536000, immutable"
    elif history_cache:
        cache_control = "private, no-cache"
    else:
        cache_control = "no-store"
    response.headers.setdefault("Cache-Control", cache_control)
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: https:; style-src 'self'; "
        "script-src 'self'; form-action 'self'; base-uri 'self'; frame-ancestors 'none'; "
        "object-src 'none'",
    )
    return response


def _is_reader_page(path: str) -> bool:
    return (
        path == "/"
        or path in {"/starred", "/categories", "/feeds"}
        or path.startswith(
            ("/groups/", "/feeds/", "/read/", "/items/", "/hacker-news/")
        )
    )


def allowed_reader_hosts(configured_hosts: tuple[str, ...]) -> list[str]:
    allowed_hosts = list(configured_hosts)
    for host in ("127.0.0.1", "localhost"):
        if host not in allowed_hosts:
            allowed_hosts.append(host)
    return allowed_hosts


def close_services(services: WebServices) -> None:
    for service in (
        services.mutations,
        services.admin,
        services.extractor,
        services.hacker_news,
        services.freshrss,
    ):
        close = getattr(service, "close", None)
        if callable(close):
            close()


def _static_asset_versions(static_directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        for path in sorted(static_directory.iterdir())
        if path.is_file()
    }
