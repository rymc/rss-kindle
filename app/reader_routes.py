from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from fastapi import (
    APIRouter,
    BackgroundTasks,
    FastAPI,
    Form,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import RedirectResponse, Response

from app.article_html import simplify_html_for_kindle
from app.freshrss import FreshRSSEntry, FreshRSSError
from app.mutations import MutationKind
from app.reader_navigation import (
    ReadingContext,
    StreamRequest,
    StreamScope,
    article_key,
    entry_id_from_article_key,
    item_detail_url,
)
from app.utils import (
    compact_source_label,
    extract_hacker_news_comments_url,
    hacker_news_destination_host,
    hacker_news_item_id,
    is_comments_only_summary,
    truncate_text,
)
from app.web_runtime import (
    WebServices,
    action_response,
    build_template_context,
    current_relative_url,
    reader_template_response,
    record_timing,
    require_csrf,
)

FEEDS_PER_PAGE = 12


@dataclass(frozen=True)
class StreamItemView:
    id: str
    list_anchor: str
    title: str
    published_at: str | None
    summary_excerpt: str
    is_starred: bool
    open_url: str
    source_label: str
    destination_host: str | None
    comments_url: str | None
    summary_is_comments: bool
    is_read: bool


@dataclass(frozen=True)
class ArticleNavigation:
    previous_url: str | None
    next_url: str | None
    next_title: str | None
    next_entry: FreshRSSEntry | None


@dataclass(frozen=True)
class HackerNewsEntryView:
    destination_host: str | None
    discussion_url: str | None
    summary_is_discussion: bool


class ReaderController:
    def __init__(self, app: FastAPI, services: WebServices):
        self.app = app
        self.services = services
        self.router = APIRouter()
        self._register_routes()

    def _register_routes(self) -> None:
        self.router.add_api_route("/", self.home, methods=["GET"], name="home")
        self.router.add_api_route(
            "/starred",
            self.starred_view,
            methods=["GET"],
            name="starred_view",
        )
        self.router.add_api_route(
            "/groups/{slug}",
            self.group_view,
            methods=["GET"],
            name="group_view",
        )
        self.router.add_api_route(
            "/categories",
            self.categories_view,
            methods=["GET"],
            name="categories_view",
        )
        self.router.add_api_route(
            "/feeds", self.feeds_index, methods=["GET"], name="feeds_index"
        )
        self.router.add_api_route(
            "/feeds/{feed_id}",
            self.feed_view,
            methods=["GET"],
            name="feed_view",
        )
        self.router.add_api_route(
            "/read/{article_key}/{context_id}",
            self.read_article_with_context,
            methods=["GET"],
            name="read_article_with_context",
        )
        self.router.add_api_route(
            "/read/{article_key}",
            self.read_article,
            methods=["GET"],
            name="read_article",
        )
        self.router.add_api_route(
            "/items/{entry_id:path}",
            self.legacy_item_detail,
            methods=["GET"],
            name="item_detail",
            include_in_schema=False,
        )
        self.router.add_api_route(
            "/items/{entry_id:path}/state",
            self.change_item_state,
            methods=["POST"],
            name="change_item_state",
        )
        self.router.add_api_route(
            "/items/{entry_id:path}/read",
            self.mark_item_read,
            methods=["POST"],
            name="mark_item_read",
        )
        self.router.add_api_route(
            "/items/{entry_id:path}/unread",
            self.mark_item_unread,
            methods=["POST"],
            name="mark_item_unread",
        )
        self.router.add_api_route(
            "/items/{entry_id:path}/star",
            self.mark_item_starred,
            methods=["POST"],
            name="mark_item_starred",
        )
        self.router.add_api_route(
            "/items/{entry_id:path}/unstar",
            self.mark_item_unstarred,
            methods=["POST"],
            name="mark_item_unstarred",
        )

    def home(self, request: Request) -> Response:
        return self._render_stream(request, StreamScope("home"), page_title="Unread")

    def starred_view(self, request: Request) -> Response:
        return self._render_stream(
            request, StreamScope("starred"), page_title="Starred"
        )

    def group_view(self, request: Request, slug: str) -> Response:
        group = self.services.freshrss.get_group(slug)
        if group is None:
            raise HTTPException(status_code=404, detail="FreshRSS group not found")
        return self._render_stream(
            request,
            StreamScope("group", slug),
            page_title=group.name,
            active_group_slug=slug,
        )

    def categories_view(self, request: Request) -> Response:
        context = build_template_context(
            self.services,
            request,
            page_title="Categories",
            include_navigation=True,
        )
        return reader_template_response(
            self.services,
            request,
            name="categories.html",
            context=context,
        )

    def feeds_index(
        self,
        request: Request,
        feed: str | None = Query(default=None),
        page: int = Query(default=1, ge=1),
    ) -> Response:
        if feed:
            return RedirectResponse(
                self.app.url_path_for("feed_view", feed_id=feed), status_code=303
            )
        context = build_template_context(
            self.services,
            request,
            page_title="Feeds",
            include_feeds=True,
        )
        feeds = context["feeds"]
        page_count = max(1, (len(feeds) + FEEDS_PER_PAGE - 1) // FEEDS_PER_PAGE)
        page = min(page, page_count)
        page_start = (page - 1) * FEEDS_PER_PAGE
        feeds_path = str(self.app.url_path_for("feeds_index"))
        context.update(
            {
                "feeds": feeds[page_start : page_start + FEEDS_PER_PAGE],
                "feed_page": page,
                "feed_page_count": page_count,
                "newer_url": f"{feeds_path}?page={page - 1}" if page > 1 else None,
                "older_url": (
                    f"{feeds_path}?page={page + 1}" if page < page_count else None
                ),
            }
        )
        return reader_template_response(
            self.services,
            request,
            name="feeds.html",
            context=context,
        )

    def feed_view(self, request: Request, feed_id: str) -> Response:
        feed = self.services.freshrss.get_feed(feed_id)
        if feed is None:
            raise HTTPException(status_code=404, detail="FreshRSS feed not found")
        return self._render_stream(
            request,
            StreamScope("feed", feed_id),
            page_title=feed.title,
            active_feed_id=feed_id,
        )

    def read_article_with_context(
        self,
        request: Request,
        article_key: str,
        context_id: str,
    ) -> Response:
        entry_id = _validated_entry_id(article_key)
        return self._render_item(
            request,
            entry_id,
            context_id=context_id,
        )

    def read_article(
        self,
        request: Request,
        article_key: str,
    ) -> Response:
        return self._render_item(
            request,
            _validated_entry_id(article_key),
        )

    def legacy_item_detail(
        self,
        entry_id: str,
        context_token: str | None = Query(default=None, alias="ctx"),
    ) -> Response:
        reading_context = ReadingContext.decode(context_token)
        context_id = (
            self.services.repository.save_reading_context(context_token or "")
            if reading_context
            else None
        )
        return RedirectResponse(
            item_detail_url(self.app, entry_id, context_id),
            status_code=303,
        )

    def mark_item_read(
        self,
        request: Request,
        entry_id: str,
        next_path: str = Form("/"),
        csrf_token: str | None = Form(default=None),
    ) -> Response:
        return self._item_action(
            request,
            entry_id,
            next_path,
            csrf_token,
            state_kind="read",
            enabled=True,
            failure_message="Could not mark item as read",
        )

    def change_item_state(
        self,
        request: Request,
        entry_id: str,
        state_action: str = Form(...),
        next_path: str = Form("/"),
        csrf_token: str | None = Form(default=None),
    ) -> Response:
        operations: dict[str, tuple[MutationKind, bool, str]] = {
            "read": ("read", True, "Could not mark item as read"),
            "unread": ("read", False, "Could not mark item as unread"),
            "star": ("starred", True, "Could not star item"),
            "unstar": ("starred", False, "Could not unstar item"),
        }
        operation = operations.get(state_action)
        if operation is None:
            raise HTTPException(status_code=400, detail="Unknown item state action")
        state_kind, enabled, failure_message = operation
        return self._item_action(
            request,
            entry_id,
            next_path,
            csrf_token,
            state_kind=state_kind,
            enabled=enabled,
            failure_message=failure_message,
        )

    def mark_item_unread(
        self,
        request: Request,
        entry_id: str,
        next_path: str = Form("/"),
        csrf_token: str | None = Form(default=None),
    ) -> Response:
        return self._item_action(
            request,
            entry_id,
            next_path,
            csrf_token,
            state_kind="read",
            enabled=False,
            failure_message="Could not mark item as unread",
        )

    def mark_item_starred(
        self,
        request: Request,
        entry_id: str,
        next_path: str = Form("/"),
        csrf_token: str | None = Form(default=None),
    ) -> Response:
        return self._item_action(
            request,
            entry_id,
            next_path,
            csrf_token,
            state_kind="starred",
            enabled=True,
            failure_message="Could not star item",
        )

    def mark_item_unstarred(
        self,
        request: Request,
        entry_id: str,
        next_path: str = Form("/"),
        csrf_token: str | None = Form(default=None),
    ) -> Response:
        return self._item_action(
            request,
            entry_id,
            next_path,
            csrf_token,
            state_kind="starred",
            enabled=False,
            failure_message="Could not unstar item",
        )

    def _render_stream(
        self,
        request: Request,
        scope: StreamScope,
        *,
        page_title: str,
        active_group_slug: str | None = None,
        active_feed_id: str | None = None,
    ) -> Response:
        stream_request = StreamRequest.from_request(request, scope)
        if stream_request.has_legacy_cursor:
            return RedirectResponse(
                StreamRequest(
                    scope=scope,
                    include_read=stream_request.include_read,
                ).url(self.app),
                status_code=303,
            )
        started_at = time.perf_counter()
        try:
            page = self.services.freshrss.get_stream(
                scope_kind=scope.kind,
                scope_value=scope.value,
                continuation=stream_request.continuation,
                limit=self.services.settings.max_stream_items,
                include_read=stream_request.include_read,
            )
        except (
            FreshRSSError,
            httpx.HTTPError,
        ) as exc:  # pragma: no cover - runtime path
            raise HTTPException(
                status_code=503, detail=f"FreshRSS stream request failed: {exc}"
            ) from exc
        record_timing(request, "freshrss_stream", started_at)

        links = stream_request.page_links(self.app, page.continuation)
        reading_context = ReadingContext(
            entry_ids=tuple(entry.id for entry in page.entries),
            back_url=current_relative_url(request),
            next_page_url=links.older,
        )
        context_id = self.services.repository.save_reading_context(
            reading_context.encode()
        )
        stream_back_url = current_relative_url(request)
        items = [
            self._stream_item(entry, context_id, stream_back_url)
            for entry in page.entries
        ]
        document_title = (
            "All articles"
            if scope.kind == "home" and stream_request.include_read
            else page_title
        )
        context = build_template_context(
            self.services,
            request,
            page_title=document_title,
            active_group_slug=active_group_slug,
            active_feed_id=active_feed_id,
            reader_script=True,
        )
        context.update(
            {
                "items": items,
                "older_url": links.older,
                "newer_url": links.newer,
                "stream_offset": (
                    len(stream_request.history)
                    * self.services.settings.max_stream_items
                ),
                "include_read": stream_request.include_read,
                "read_toggle_url": (
                    stream_request.read_toggle_url(self.app)
                    if scope.kind != "starred"
                    else None
                ),
                "stream_context": page_title if scope.kind != "home" else None,
                "stream_is_stale": page.is_stale,
                "empty_message": (
                    "No saved items in this view."
                    if scope.kind == "starred"
                    else (
                        "No items in this view."
                        if stream_request.include_read
                        else "No unread items in this view."
                    )
                ),
            }
        )

        background_tasks = BackgroundTasks()
        prewarm = getattr(self.services.extractor, "prewarm", None)
        if (
            self.services.settings.article_prewarm_count > 0
            and page.entries
            and not page.entries_are_compact
            and callable(prewarm)
        ):
            background_tasks.add_task(
                prewarm,
                page.entries[: self.services.settings.article_prewarm_count],
            )
        return reader_template_response(
            self.services,
            request,
            name="index.html",
            context=context,
            background=background_tasks,
        )

    def _render_item(
        self,
        request: Request,
        entry_id: str,
        *,
        context_id: str | None = None,
    ) -> Response:
        reading_context = self._reading_context(context_id, entry_id)
        if reading_context is None:
            context_id = None

        started_at = time.perf_counter()
        entry = self.services.freshrss.get_entry(entry_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="FreshRSS entry not found")
        record_timing(request, "freshrss_entry", started_at)

        started_at = time.perf_counter()
        article = self.services.extractor.ensure_extracted(entry)
        record_timing(request, "article", started_at)
        source_label = compact_source_label(entry.feed_title, entry.feed_site_url)
        content_html = simplify_html_for_kindle(
            article.html,
            item_title=entry.title,
            source_label=source_label,
            feed_title=entry.feed_title,
            source_url=entry.url,
        )

        navigation = self._article_navigation(
            reading_context,
            entry_id,
            context_id,
        )
        back_url = (
            reading_context.back_url
            if reading_context
            else str(self.app.url_path_for("home"))
        )
        hacker_news = self._hacker_news_entry_view(
            entry,
            current_relative_url(request),
        )
        context = build_template_context(
            self.services,
            request,
            page_title=entry.title,
            active_feed_id=entry.feed_token,
            show_site_header=False,
            reader_script=True,
        )
        context.update(
            {
                "entry": entry,
                "content_html": content_html,
                "source_label": source_label,
                "destination_host": hacker_news.destination_host,
                "comments_url": hacker_news.discussion_url,
                "fallback_used": article.extraction_status == "failed",
                "error_message": article.error_message,
                "body_class": "article-page",
                "back_url": back_url,
                "return_url": f"{back_url}#entry-{article_key(entry.id)}",
                "previous_url": navigation.previous_url,
                "next_url": navigation.next_url,
                "next_title": navigation.next_title,
            }
        )
        background_tasks = BackgroundTasks()
        prewarm = getattr(self.services.extractor, "prewarm", None)
        if self.services.settings.article_prewarm_count > 0:
            if navigation.next_entry is not None and callable(prewarm):
                background_tasks.add_task(prewarm, [navigation.next_entry])
            if self._should_prime_next_page(reading_context, entry_id):
                background_tasks.add_task(
                    self._first_article,
                    reading_context.next_page_url,
                )
        return reader_template_response(
            self.services,
            request,
            name="item.html",
            context=context,
            background=background_tasks,
        )

    def _reading_context(
        self, context_id: str | None, entry_id: str
    ) -> ReadingContext | None:
        if not context_id:
            return None
        context = ReadingContext.decode(
            self.services.repository.get_reading_context(context_id)
        )
        return context if context and entry_id in context.entry_ids else None

    @staticmethod
    def _should_prime_next_page(
        context: ReadingContext | None,
        entry_id: str,
    ) -> bool:
        if (
            context is None
            or not context.next_page_url
            or len(context.entry_ids) < 2
        ):
            return False
        return context.entry_ids.index(entry_id) == len(context.entry_ids) - 2

    def _article_navigation(
        self,
        context: ReadingContext | None,
        entry_id: str,
        context_id: str | None,
    ) -> ArticleNavigation:
        if context is None:
            return ArticleNavigation(None, None, None, None)
        index = context.entry_ids.index(entry_id)
        previous_url = (
            item_detail_url(self.app, context.entry_ids[index - 1], context_id)
            if index > 0
            else context.previous_url
        )
        next_entry = None
        if index + 1 < len(context.entry_ids):
            next_entry_id = context.entry_ids[index + 1]
            next_url = item_detail_url(self.app, next_entry_id, context_id)
            next_entry = self._entry(next_entry_id)
            next_title = next_entry.title if next_entry else None
        else:
            next_url, next_title, next_entry = self._first_article(
                context.next_page_url,
                previous_url=item_detail_url(self.app, entry_id, context_id),
            )
        return ArticleNavigation(previous_url, next_url, next_title, next_entry)

    def _first_article(
        self,
        relative_url: str | None,
        *,
        previous_url: str | None = None,
    ) -> tuple[str | None, str | None, FreshRSSEntry | None]:
        if not relative_url:
            return None, None, None
        try:
            stream_request = StreamRequest.from_url(relative_url)
            if stream_request.has_legacy_cursor:
                stream_request = StreamRequest(
                    scope=stream_request.scope,
                    include_read=stream_request.include_read,
                )
            page = self.services.freshrss.get_stream(
                scope_kind=stream_request.scope.kind,
                scope_value=stream_request.scope.value,
                continuation=stream_request.continuation,
                limit=self.services.settings.max_stream_items,
                include_read=stream_request.include_read,
            )
        except (
            ValueError,
            FreshRSSError,
            httpx.HTTPError,
        ):  # pragma: no cover - runtime fallback
            return None, None, None
        if not page.entries:
            return None, None, None
        links = stream_request.page_links(self.app, page.continuation)
        reading_context = ReadingContext(
            entry_ids=tuple(entry.id for entry in page.entries),
            back_url=relative_url,
            next_page_url=links.older,
            previous_url=previous_url,
        )
        context_id = self.services.repository.save_reading_context(
            reading_context.encode()
        )
        first_entry = page.entries[0]
        prewarm_entry = first_entry
        if page.entries_are_compact:
            prewarm_entry = self._entry(first_entry.id)
            first_entry = prewarm_entry or first_entry
        return (
            item_detail_url(self.app, first_entry.id, context_id),
            first_entry.title,
            prewarm_entry,
        )

    def _entry(self, entry_id: str) -> FreshRSSEntry | None:
        try:
            return self.services.freshrss.get_entry(entry_id)
        except (FreshRSSError, httpx.HTTPError):
            return None

    def _stream_item(
        self,
        entry: FreshRSSEntry,
        context_id: str,
        back_url: str,
    ) -> StreamItemView:
        hacker_news = self._hacker_news_entry_view(entry, back_url)
        return StreamItemView(
            id=entry.id,
            list_anchor=f"entry-{article_key(entry.id)}",
            title=entry.title,
            published_at=entry.published_at,
            summary_excerpt=truncate_text(entry.summary_text, 160),
            is_starred=entry.is_starred,
            open_url=item_detail_url(self.app, entry.id, context_id),
            source_label=compact_source_label(entry.feed_title, entry.feed_site_url),
            destination_host=hacker_news.destination_host,
            comments_url=hacker_news.discussion_url,
            summary_is_comments=hacker_news.summary_is_discussion,
            is_read=entry.is_read,
        )

    def _hacker_news_entry_view(
        self,
        entry: FreshRSSEntry,
        back_url: str,
    ) -> HackerNewsEntryView:
        comments_url = extract_hacker_news_comments_url(
            summary_html=entry.summary_html,
            content_html=entry.content_html,
            entry_url=entry.url,
            feed_site_url=entry.feed_site_url,
        )
        return HackerNewsEntryView(
            destination_host=hacker_news_destination_host(
                entry.url, entry.feed_site_url
            ),
            discussion_url=self._hacker_news_reader_url(comments_url, back_url),
            summary_is_discussion=bool(comments_url)
            and is_comments_only_summary(entry.summary_text),
        )

    def _hacker_news_reader_url(
        self,
        comments_url: str | None,
        back_url: str,
    ) -> str | None:
        item_id = hacker_news_item_id(comments_url)
        if item_id is None:
            return comments_url
        path = self.app.url_path_for(
            "hacker_news_discussion",
            item_id=str(item_id),
        )
        return f"{path}?{urlencode({'back': back_url})}"

    def _item_action(
        self,
        request: Request,
        entry_id: str,
        next_path: str,
        csrf_token: str | None,
        *,
        state_kind: MutationKind,
        enabled: bool,
        failure_message: str,
        default: str = "/",
    ) -> Response:
        require_csrf(request, csrf_token)
        try:
            self.services.mutations.submit(
                entry_id,
                state_kind=state_kind,
                enabled=enabled,
            )
        except (
            FreshRSSError,
            httpx.HTTPError,
            sqlite3.Error,
        ) as exc:  # pragma: no cover - runtime path
            raise HTTPException(
                status_code=503, detail=f"{failure_message}: {exc}"
            ) from exc
        return action_response(request, next_path, default=default)


def _validated_entry_id(value: str) -> str:
    entry_id = entry_id_from_article_key(value)
    if entry_id is None:
        raise HTTPException(status_code=404, detail="Article not found")
    return entry_id
