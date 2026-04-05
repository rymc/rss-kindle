from __future__ import annotations

import base64
import json
from dataclasses import asdict
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.auth import (
    auth_enabled,
    build_login_redirect,
    decode_session_token,
    issue_session_token,
    sanitize_next_path,
    validate_csrf,
    verify_password,
)
from app.config import Settings, get_settings
from app.content_service import ArticleExtractor
from app.db import Database
from app.freshrss import FreshRSSClient, FreshRSSEntry, FreshRSSError
from app.repository import Repository
from app.utils import (
    cleanup_kindle_article_html,
    compact_source_label,
    extract_hacker_news_comments_url,
    excerpt,
    format_relative_time,
    is_comments_only_summary,
    simplify_html_for_kindle,
)


def _current_relative_url(request: Request) -> str:
    return f"{request.url.path}?{request.url.query}" if request.url.query else request.url.path


def _set_security_headers(response: Response) -> Response:
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data: https:; style-src 'self'; form-action 'self'; base-uri 'self'; frame-ancestors 'none'; object-src 'none'")
    return response


def _request_session_payload(request: Request) -> dict[str, Any] | None:
    return getattr(request.state, "auth_session", None)


def _require_csrf(request: Request, submitted_token: str | None) -> None:
    if not getattr(request.state, "auth_enabled", False):
        return
    if not validate_csrf(_request_session_payload(request), submitted_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token.")


def _encode_token(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_token(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    padding = "=" * ((4 - len(token) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode(token + padding).decode("utf-8")
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _encode_cursor_stack(stack: list[str]) -> str | None:
    if not stack:
        return None
    return _encode_token({"stack": stack})


def _decode_cursor_stack(value: str | None) -> list[str]:
    payload = _decode_token(value)
    if not payload:
        return []
    stack = payload.get("stack")
    if not isinstance(stack, list):
        return []
    return [str(item) for item in stack]


def _build_scope_path(app: FastAPI, scope_kind: str, scope_value: str | None = None) -> str:
    if scope_kind == "home":
        return str(app.url_path_for("home"))
    if scope_kind == "group":
        if scope_value is None:
            raise ValueError("group scope requires a value")
        return str(app.url_path_for("group_view", slug=scope_value))
    if scope_kind == "feed":
        if scope_value is None:
            raise ValueError("feed scope requires a value")
        return str(app.url_path_for("feed_view", feed_id=scope_value))
    raise ValueError(f"Unsupported scope: {scope_kind}")


def _build_stream_url(
    app: FastAPI,
    scope_kind: str,
    scope_value: str | None = None,
    *,
    continuation: str | None = None,
    back_stack: list[str] | None = None,
) -> str:
    params: dict[str, str] = {}
    if continuation:
        params["c"] = continuation
    encoded_stack = _encode_cursor_stack(back_stack or [])
    if encoded_stack:
        params["b"] = encoded_stack
    path = _build_scope_path(app, scope_kind, scope_value)
    return f"{path}?{urlencode(params)}" if params else path


def _parse_scope_url(relative_url: str) -> tuple[str, str | None, str | None, list[str]]:
    parsed = urlsplit(relative_url)
    path = parsed.path or "/"
    query = parse_qs(parsed.query)
    continuation = query.get("c", [None])[0]
    back_stack = _decode_cursor_stack(query.get("b", [None])[0])

    if path == "/":
        return "home", None, continuation, back_stack
    if path.startswith("/groups/"):
        return "group", path.split("/groups/", 1)[1], continuation, back_stack
    if path.startswith("/feeds/"):
        return "feed", path.split("/feeds/", 1)[1], continuation, back_stack
    raise ValueError(f"Unsupported stream URL: {relative_url}")


def _build_pager_urls(
    app: FastAPI,
    scope_kind: str,
    scope_value: str | None,
    current_continuation: str | None,
    back_stack: list[str],
    next_continuation: str | None,
) -> dict[str, str | None]:
    newer_url = None
    if back_stack:
        previous_cursor = back_stack[-1] or None
        newer_url = _build_stream_url(
            app,
            scope_kind,
            scope_value,
            continuation=previous_cursor,
            back_stack=back_stack[:-1],
        )

    older_url = None
    if next_continuation:
        older_url = _build_stream_url(
            app,
            scope_kind,
            scope_value,
            continuation=next_continuation,
            back_stack=[*back_stack, current_continuation or ""],
        )
    return {"newer_url": newer_url, "older_url": older_url}


def _stream_context_token(*, entry_ids: list[str], back_url: str, next_page_url: str | None) -> str:
    return _encode_token(
        {
            "entry_ids": entry_ids,
            "back_url": back_url,
            "next_page_url": next_page_url,
        }
    )


def _decode_stream_context(token: str | None) -> dict[str, Any] | None:
    payload = _decode_token(token)
    if not payload:
        return None
    entry_ids = payload.get("entry_ids")
    back_url = payload.get("back_url")
    next_page_url = payload.get("next_page_url")
    if not isinstance(entry_ids, list) or not isinstance(back_url, str):
        return None
    return {
        "entry_ids": [str(entry_id) for entry_id in entry_ids],
        "back_url": back_url,
        "next_page_url": str(next_page_url) if next_page_url else None,
    }


def _item_detail_url(app: FastAPI, entry_id: str, ctx_token: str) -> str:
    return f"{app.url_path_for('item_detail', entry_id=entry_id)}?{urlencode({'ctx': ctx_token})}"


def _entry_to_template_item(
    app: FastAPI,
    entry: FreshRSSEntry,
    ctx_token: str,
    active_group_slug: str | None,
) -> dict[str, Any]:
    if active_group_slug and len(entry.group_names) == 1:
        group_display = ""
    else:
        group_display = ", ".join(entry.group_names)
    comments_url = extract_hacker_news_comments_url(
        summary_html=entry.summary_html,
        content_html=entry.content_html,
        entry_url=entry.url,
        feed_site_url=entry.feed_site_url,
    )
    return {
        **asdict(entry),
        "detail_url": _item_detail_url(app, entry.id, ctx_token),
        "source_label": compact_source_label(entry.feed_title, entry.feed_site_url),
        "group_display": group_display,
        "comments_url": comments_url,
        "summary_is_comments": bool(comments_url) and is_comments_only_summary(entry.summary_text),
    }


def _get_entry_or_404(freshrss_client: Any, entry_id: str) -> FreshRSSEntry:
    entry = freshrss_client.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="FreshRSS entry not found")
    return entry


def create_app(
    settings: Settings | None = None,
    *,
    repository: Repository | None = None,
    freshrss_client: Any | None = None,
    extractor: ArticleExtractor | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    repository = repository or Repository(Database(settings.database_path))
    repository.initialize()
    freshrss_client = freshrss_client or FreshRSSClient(settings)
    extractor = extractor or ArticleExtractor(settings, repository)

    app = FastAPI(title=settings.app_name)
    if settings.app_allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.app_allowed_hosts))
    templates = Jinja2Templates(directory=str(settings.base_dir / "app" / "templates"))
    templates.env.filters["format_relative_time"] = format_relative_time
    templates.env.filters["excerpt"] = excerpt
    app.mount("/static", StaticFiles(directory=str(settings.base_dir / "app" / "static")), name="static")

    login_required = auth_enabled(settings.app_auth_username, settings.app_auth_password)

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        session_payload = None
        if settings.app_auth_secret:
            session_payload = decode_session_token(
                request.cookies.get(settings.app_session_cookie_name),
                secret=settings.app_auth_secret,
            )
        request.state.auth_session = session_payload
        request.state.auth_enabled = login_required

        exempt_path = request.url.path == "/login" or request.url.path.startswith("/static/")
        if login_required and not exempt_path and session_payload is None:
            if request.method in {"GET", "HEAD"}:
                response = RedirectResponse(
                    build_login_redirect("/login", next_path=sanitize_next_path(_current_relative_url(request))),
                    status_code=303,
                )
            else:
                response = HTMLResponse("Authentication required.", status_code=401)
            return _set_security_headers(response)

        response = await call_next(request)
        return _set_security_headers(response)

    def build_context(
        request: Request,
        *,
        page_title: str,
        active_group_slug: str | None = None,
        active_feed_id: str | None = None,
        show_site_header: bool = True,
        show_feed_jump: bool = True,
    ) -> dict[str, Any]:
        try:
            navigation = freshrss_client.list_navigation()
        except FreshRSSError as exc:  # pragma: no cover - runtime path
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:  # pragma: no cover - runtime path
            raise HTTPException(status_code=503, detail=f"FreshRSS request failed: {exc}") from exc

        return {
            "request": request,
            "page_title": page_title,
            "groups": navigation.groups,
            "feeds": navigation.feeds,
            "active_group_slug": active_group_slug,
            "active_feed_id": active_feed_id,
            "current_path": _current_relative_url(request),
            "show_site_header": show_site_header,
            "show_feed_jump": show_feed_jump,
            "auth_enabled": login_required,
            "current_user": (_request_session_payload(request) or {}).get("sub"),
            "csrf_token": (_request_session_payload(request) or {}).get("csrf", ""),
        }

    def render_stream(
        request: Request,
        *,
        scope_kind: str,
        scope_value: str | None = None,
        page_title: str,
        active_group_slug: str | None = None,
        active_feed_id: str | None = None,
    ):
        continuation = request.query_params.get("c")
        back_stack = _decode_cursor_stack(request.query_params.get("b"))

        try:
            page = freshrss_client.get_stream(
                scope_kind=scope_kind,
                scope_value=scope_value,
                continuation=continuation,
                limit=settings.max_stream_items,
            )
        except FreshRSSError as exc:  # pragma: no cover - runtime path
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:  # pragma: no cover - runtime path
            raise HTTPException(status_code=503, detail=f"FreshRSS stream request failed: {exc}") from exc

        pager_urls = _build_pager_urls(
            app,
            scope_kind,
            scope_value,
            continuation,
            back_stack,
            page.continuation,
        )
        ctx_token = _stream_context_token(
            entry_ids=[entry.id for entry in page.entries],
            back_url=_current_relative_url(request),
            next_page_url=pager_urls["older_url"],
        )
        items = [
            _entry_to_template_item(app, entry, ctx_token, active_group_slug)
            for entry in page.entries
        ]

        context = build_context(
            request,
            page_title=page_title,
            active_group_slug=active_group_slug,
            active_feed_id=active_feed_id,
        )
        context.update(
            {
                "items": items,
                "older_url": pager_urls["older_url"],
                "newer_url": pager_urls["newer_url"],
            }
        )
        return templates.TemplateResponse(request=request, name="index.html", context=context)

    def build_cross_page_next_url(relative_url: str) -> str | None:
        try:
            scope_kind, scope_value, continuation, back_stack = _parse_scope_url(relative_url)
            page = freshrss_client.get_stream(
                scope_kind=scope_kind,
                scope_value=scope_value,
                continuation=continuation,
                limit=settings.max_stream_items,
            )
        except Exception:  # pragma: no cover - runtime fallback
            return None
        if not page.entries:
            return None
        pager_urls = _build_pager_urls(
            app,
            scope_kind,
            scope_value,
            continuation,
            back_stack,
            page.continuation,
        )
        ctx_token = _stream_context_token(
            entry_ids=[entry.id for entry in page.entries],
            back_url=relative_url,
            next_page_url=pager_urls["older_url"],
        )
        return _item_detail_url(app, page.entries[0].id, ctx_token)

    @app.get("/login")
    def login_page(request: Request, next: str | None = Query(default=None)):
        if not login_required:
            return RedirectResponse(str(app.url_path_for("home")), status_code=303)
        next_path = sanitize_next_path(next)
        if _request_session_payload(request):
            return RedirectResponse(next_path, status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "request": request,
                "page_title": "Sign in",
                "show_site_header": False,
                "show_feed_jump": False,
                "next_path": next_path,
                "error_message": None,
            },
        )

    @app.post("/login")
    def login_submit(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        next_path: str = Form("/"),
    ):
        if not login_required:
            return RedirectResponse(str(app.url_path_for("home")), status_code=303)
        target_path = sanitize_next_path(next_path)
        if username != settings.app_auth_username or not verify_password(settings.app_auth_password or "", password):
            response = templates.TemplateResponse(
                request=request,
                name="login.html",
                context={
                    "request": request,
                    "page_title": "Sign in",
                    "show_site_header": False,
                    "show_feed_jump": False,
                    "next_path": target_path,
                    "error_message": "Incorrect username or password.",
                },
                status_code=401,
            )
            return response

        token = issue_session_token(
            username=settings.app_auth_username or username,
            secret=settings.app_auth_secret or "",
            max_age_seconds=settings.app_session_max_age_seconds,
        )
        response = RedirectResponse(target_path, status_code=303)
        response.set_cookie(
            settings.app_session_cookie_name,
            token,
            max_age=settings.app_session_max_age_seconds,
            httponly=True,
            samesite="lax",
            secure=settings.app_secure_cookies,
            path="/",
        )
        return response

    @app.post("/logout")
    def logout(request: Request, csrf_token: str | None = Form(default=None)):
        _require_csrf(request, csrf_token)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(settings.app_session_cookie_name, path="/")
        return response

    @app.get("/")
    def home(request: Request):
        return render_stream(request, scope_kind="home", page_title="Unread")

    @app.get("/groups/{slug}")
    def group_view(request: Request, slug: str):
        group = freshrss_client.get_group(slug)
        if group is None:
            raise HTTPException(status_code=404, detail="FreshRSS group not found")
        return render_stream(
            request,
            scope_kind="group",
            scope_value=slug,
            page_title=group.name,
            active_group_slug=slug,
        )

    @app.get("/feeds")
    def feeds_index(request: Request, feed: str | None = Query(default=None)):
        if feed:
            return RedirectResponse(app.url_path_for("feed_view", feed_id=feed), status_code=303)
        context = build_context(request, page_title="Feeds")
        return templates.TemplateResponse(request=request, name="feeds.html", context=context)

    @app.get("/feeds/{feed_id}")
    def feed_view(request: Request, feed_id: str):
        feed = freshrss_client.get_feed(feed_id)
        if feed is None:
            raise HTTPException(status_code=404, detail="FreshRSS feed not found")
        return render_stream(
            request,
            scope_kind="feed",
            scope_value=feed_id,
            page_title=feed.title,
            active_feed_id=feed_id,
        )

    @app.get("/items/{entry_id:path}")
    def item_detail(request: Request, entry_id: str, ctx: str | None = None):
        stream_context = _decode_stream_context(ctx)
        entry = _get_entry_or_404(freshrss_client, entry_id)

        article = extractor.ensure_extracted(entry)
        content_html = simplify_html_for_kindle(article.html)
        content_html = cleanup_kindle_article_html(
            content_html,
            item_title=entry.title,
            source_label=compact_source_label(entry.feed_title, entry.feed_site_url),
            feed_title=entry.feed_title,
            source_url=entry.url,
        )

        back_url = stream_context["back_url"] if stream_context else str(app.url_path_for("home"))
        previous_item = None
        next_item = None
        if stream_context and entry_id in stream_context["entry_ids"]:
            entry_ids = stream_context["entry_ids"]
            index = entry_ids.index(entry_id)
            if index > 0:
                previous_entry_id = entry_ids[index - 1]
                previous_item = {
                    "entry_id": previous_entry_id,
                    "detail_url": _item_detail_url(app, previous_entry_id, ctx or ""),
                }
            if index + 1 < len(entry_ids):
                next_entry_id = entry_ids[index + 1]
                next_item = {
                    "entry_id": next_entry_id,
                    "detail_url": _item_detail_url(app, next_entry_id, ctx or ""),
                }
            elif stream_context["next_page_url"]:
                cross_page_url = build_cross_page_next_url(stream_context["next_page_url"])
                if cross_page_url:
                    next_entry_id = cross_page_url.split("/items/", 1)[1].split("?", 1)[0]
                    next_item = {
                        "entry_id": next_entry_id,
                        "detail_url": cross_page_url,
                    }

        context = build_context(request, page_title=entry.title, active_feed_id=entry.feed_token)
        context.update(
            {
                "entry": asdict(entry),
                "content_html": content_html,
                "source_label": compact_source_label(entry.feed_title, entry.feed_site_url),
                "fallback_used": article.extraction_status == "failed",
                "error_message": article.error_message,
                "back_url": back_url,
                "previous_item": previous_item,
                "next_item": next_item,
                "show_site_header": False,
                "show_feed_jump": False,
            }
        )
        return templates.TemplateResponse(request=request, name="item.html", context=context)

    @app.post("/items/{entry_id:path}/open")
    def open_item(
        request: Request,
        entry_id: str,
        next_path: str = Form(...),
        csrf_token: str | None = Form(default=None),
    ):
        _require_csrf(request, csrf_token)
        try:
            freshrss_client.mark_read([entry_id])
        except Exception as exc:  # pragma: no cover - runtime path
            raise HTTPException(status_code=503, detail=f"Could not mark item as read: {exc}") from exc
        return RedirectResponse(sanitize_next_path(next_path, default=str(app.url_path_for("home"))), status_code=303)

    @app.post("/items/{entry_id:path}/read")
    def mark_item_read(
        request: Request,
        entry_id: str,
        next_path: str = Form("/"),
        csrf_token: str | None = Form(default=None),
    ):
        _require_csrf(request, csrf_token)
        try:
            freshrss_client.mark_read([entry_id])
        except Exception as exc:  # pragma: no cover - runtime path
            raise HTTPException(status_code=503, detail=f"Could not mark item as read: {exc}") from exc
        return RedirectResponse(sanitize_next_path(next_path), status_code=303)

    @app.post("/items/{entry_id:path}/unread")
    def mark_item_unread(
        request: Request,
        entry_id: str,
        next_path: str = Form("/"),
        csrf_token: str | None = Form(default=None),
    ):
        _require_csrf(request, csrf_token)
        try:
            freshrss_client.mark_unread([entry_id])
        except Exception as exc:  # pragma: no cover - runtime path
            raise HTTPException(status_code=503, detail=f"Could not mark item as unread: {exc}") from exc
        return RedirectResponse(sanitize_next_path(next_path), status_code=303)

    @app.post("/items/{entry_id:path}/star")
    def mark_item_starred(
        request: Request,
        entry_id: str,
        next_path: str = Form("/"),
        csrf_token: str | None = Form(default=None),
    ):
        _require_csrf(request, csrf_token)
        try:
            freshrss_client.mark_starred([entry_id])
        except Exception as exc:  # pragma: no cover - runtime path
            raise HTTPException(status_code=503, detail=f"Could not star item: {exc}") from exc
        return RedirectResponse(sanitize_next_path(next_path), status_code=303)

    @app.post("/items/{entry_id:path}/unstar")
    def mark_item_unstarred(
        request: Request,
        entry_id: str,
        next_path: str = Form("/"),
        csrf_token: str | None = Form(default=None),
    ):
        _require_csrf(request, csrf_token)
        try:
            freshrss_client.mark_unstarred([entry_id])
        except Exception as exc:  # pragma: no cover - runtime path
            raise HTTPException(status_code=503, detail=f"Could not unstar item: {exc}") from exc
        return RedirectResponse(sanitize_next_path(next_path), status_code=303)

    return app
