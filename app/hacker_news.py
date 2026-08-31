from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol, TypedDict, cast
from urllib.parse import urlparse

import httpx

from app.article_html import simplify_html_for_kindle
from app.config import Settings
from app.utils import hacker_news_external_host, strip_html

HACKER_NEWS_API_BASE_URL = "https://hacker-news.firebaseio.com/v0"
HACKER_NEWS_WEB_BASE_URL = "https://news.ycombinator.com"
HACKER_NEWS_CACHE_SECONDS = 300
HACKER_NEWS_CACHE_ITEMS = 32
HACKER_NEWS_MAX_COMMENTS = 100
HACKER_NEWS_FETCH_WORKERS = 8
HACKER_NEWS_FETCH_BATCH = 16
HACKER_NEWS_FETCH_BUDGET_SECONDS = 5
HACKER_NEWS_TIMEOUT_SECONDS = 5


class HackerNewsError(RuntimeError):
    pass


class HackerNewsItem(TypedDict, total=False):
    by: str
    dead: bool
    deleted: bool
    descendants: int
    id: int
    kids: list[int]
    score: int
    text: str
    time: int
    title: str
    type: str
    url: str


class HackerNewsResponse(Protocol):
    def raise_for_status(self) -> None: ...

    def json(self) -> object: ...


class HackerNewsHttpClient(Protocol):
    def get(self, url: str) -> HackerNewsResponse: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class HackerNewsComment:
    id: int
    author: str
    created_at: str | None
    html: str
    depth: int
    visual_depth: int
    parent_author: str | None
    reply_count: int
    permalink: str
    is_deleted: bool
    is_dead: bool


@dataclass(frozen=True)
class HackerNewsDiscussion:
    id: int
    title: str
    author: str
    created_at: str | None
    score: int | None
    comment_count: int
    comments: tuple[HackerNewsComment, ...]
    source_url: str | None
    destination_host: str | None
    permalink: str
    is_partial: bool
    is_stale: bool = False


class HackerNewsClient:
    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: Callable[[], HackerNewsHttpClient] | None = None,
        cache_seconds: int = HACKER_NEWS_CACHE_SECONDS,
        max_comments: int = HACKER_NEWS_MAX_COMMENTS,
        fetch_workers: int = HACKER_NEWS_FETCH_WORKERS,
        fetch_budget_seconds: float = HACKER_NEWS_FETCH_BUDGET_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._settings = settings
        self._client_factory = client_factory or self._default_client_factory
        self._cache_seconds = max(0, cache_seconds)
        self._max_comments = max(1, max_comments)
        self._fetch_workers = max(1, fetch_workers)
        self._fetch_budget_seconds = max(0.1, fetch_budget_seconds)
        self._clock = clock
        self._cache: OrderedDict[int, tuple[float, HackerNewsDiscussion]] = (
            OrderedDict()
        )
        self._cache_lock = threading.RLock()
        self._client_lock = threading.Lock()
        self._client: HackerNewsHttpClient | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._item_locks = tuple(threading.Lock() for _ in range(16))

    def _default_client_factory(self) -> httpx.Client:
        timeout_seconds = min(
            HACKER_NEWS_TIMEOUT_SECONDS,
            self._settings.http_timeout_seconds,
        )
        timeout = httpx.Timeout(
            timeout_seconds,
            connect=timeout_seconds,
        )
        return httpx.Client(
            timeout=timeout,
            headers={"User-Agent": self._settings.user_agent},
        )

    def get_discussion(self, item_id: int) -> HackerNewsDiscussion:
        if item_id <= 0:
            raise HackerNewsError("Invalid Hacker News item ID.")
        now = self._clock()
        cached = self._cached(item_id)
        if cached and cached[0] > now:
            return cached[1]

        lock = self._item_locks[item_id % len(self._item_locks)]
        with lock:
            now = self._clock()
            cached = self._cached(item_id)
            if cached and cached[0] > now:
                return cached[1]
            try:
                discussion = self._fetch_discussion(item_id)
            except (HackerNewsError, httpx.HTTPError):
                if cached:
                    stale = replace(cached[1], is_stale=True)
                    self._store(item_id, stale, now)
                    return stale
                raise
            self._store(item_id, discussion, now)
            return discussion

    def _cached(
        self, item_id: int
    ) -> tuple[float, HackerNewsDiscussion] | None:
        with self._cache_lock:
            cached = self._cache.get(item_id)
            if cached:
                self._cache.move_to_end(item_id)
            return cached

    def _store(
        self,
        item_id: int,
        discussion: HackerNewsDiscussion,
        now: float,
    ) -> None:
        with self._cache_lock:
            self._cache[item_id] = (now + self._cache_seconds, discussion)
            self._cache.move_to_end(item_id)
            while len(self._cache) > HACKER_NEWS_CACHE_ITEMS:
                self._cache.popitem(last=False)

    def _fetch_discussion(self, item_id: int) -> HackerNewsDiscussion:
        story = self._fetch_item(item_id)
        if story is None or story.get("type") not in {"story", "poll"}:
            raise HackerNewsError("Hacker News discussion not found.")

        records, fetch_incomplete = self._fetch_comment_records(_item_ids(story))
        comments = _flatten_comments(story, records)
        comment_count = _non_negative_int(story.get("descendants")) or len(comments)
        source_url = _http_url(story.get("url"))
        destination_host = hacker_news_external_host(source_url)
        return HackerNewsDiscussion(
            id=item_id,
            title=strip_html(_optional_text(story.get("title")))
            or "Hacker News discussion",
            author=_optional_text(story.get("by")) or "Unknown author",
            created_at=_timestamp_iso(story.get("time")),
            score=_non_negative_int(story.get("score")),
            comment_count=comment_count,
            comments=comments,
            source_url=source_url,
            destination_host=destination_host,
            permalink=f"{HACKER_NEWS_WEB_BASE_URL}/item?id={item_id}",
            is_partial=fetch_incomplete or len(comments) < comment_count,
        )

    def _fetch_comment_records(
        self, root_ids: tuple[int, ...]
    ) -> tuple[dict[int, HackerNewsItem], bool]:
        records: dict[int, HackerNewsItem] = {}
        frontier = list(root_ids)
        seen = set(frontier)
        fetch_failed = False
        started_at = self._clock()
        while frontier and len(records) < self._max_comments:
            if self._clock() - started_at >= self._fetch_budget_seconds:
                break
            remaining = self._max_comments - len(records)
            batch_size = min(HACKER_NEWS_FETCH_BATCH, remaining)
            batch = frontier[:batch_size]
            frontier = frontier[batch_size:]
            fetched = self._fetch_batch(batch)
            child_ids: list[int] = []
            for comment_id in batch:
                comment = fetched.get(comment_id)
                if comment is None:
                    fetch_failed = True
                    continue
                if comment.get("type") != "comment":
                    continue
                records[comment_id] = comment
                for child_id in _item_ids(comment):
                    if child_id not in seen:
                        seen.add(child_id)
                        child_ids.append(child_id)
            frontier = [*child_ids, *frontier]
        return records, fetch_failed or bool(frontier)

    def _fetch_batch(
        self, item_ids: list[int]
    ) -> dict[int, HackerNewsItem]:
        if not item_ids:
            return {}
        if len(item_ids) == 1:
            try:
                item = self._fetch_item(item_ids[0])
            except (HackerNewsError, httpx.HTTPError):
                return {}
            return {item_ids[0]: item} if item is not None else {}
        executor = self._get_executor()
        futures: dict[int, Future[HackerNewsItem | None]] = {
            item_id: executor.submit(self._fetch_item, item_id)
            for item_id in item_ids
        }
        fetched: dict[int, HackerNewsItem] = {}
        for item_id, future in futures.items():
            try:
                item = future.result()
            except (HackerNewsError, httpx.HTTPError):
                continue
            if item is not None:
                fetched[item_id] = item
        return fetched

    def _fetch_item(self, item_id: int) -> HackerNewsItem | None:
        response = self._get_client().get(
            f"{HACKER_NEWS_API_BASE_URL}/item/{item_id}.json"
        )
        response.raise_for_status()
        payload = response.json()
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise HackerNewsError("Hacker News returned an invalid item.")
        return cast(HackerNewsItem, payload)

    def _get_client(self) -> HackerNewsHttpClient:
        with self._client_lock:
            if self._client is None:
                self._client = self._client_factory()
            return self._client

    def _get_executor(self) -> ThreadPoolExecutor:
        with self._client_lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self._fetch_workers,
                    thread_name_prefix="hacker-news",
                )
            return self._executor

    def close(self) -> None:
        with self._client_lock:
            executor = self._executor
            client = self._client
            self._executor = None
            self._client = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        if client is not None:
            client.close()


def _flatten_comments(
    story: HackerNewsItem, records: dict[int, HackerNewsItem]
) -> tuple[HackerNewsComment, ...]:
    comments: list[HackerNewsComment] = []
    visited: set[int] = set()

    def visit(item_ids: tuple[int, ...], depth: int, parent_author: str | None) -> None:
        for comment_id in item_ids:
            if comment_id in visited:
                continue
            comment = records.get(comment_id)
            if comment is None:
                continue
            visited.add(comment_id)
            author = _optional_text(comment.get("by")) or "Deleted"
            deleted = bool(comment.get("deleted"))
            dead = bool(comment.get("dead"))
            raw_html = _optional_text(comment.get("text"))
            if deleted:
                content_html = "<p><em>Deleted comment.</em></p>"
            elif dead and not raw_html:
                content_html = "<p><em>Dead comment.</em></p>"
            else:
                content_html = simplify_html_for_kindle(raw_html)
            child_ids = _item_ids(comment)
            comments.append(
                HackerNewsComment(
                    id=comment_id,
                    author=author,
                    created_at=_timestamp_iso(comment.get("time")),
                    html=content_html or "<p><em>Empty comment.</em></p>",
                    depth=depth,
                    visual_depth=min(depth, 4),
                    parent_author=parent_author,
                    reply_count=len(child_ids),
                    permalink=(
                        f"{HACKER_NEWS_WEB_BASE_URL}/item?id={comment_id}"
                    ),
                    is_deleted=deleted,
                    is_dead=dead,
                )
            )
            visit(child_ids, depth + 1, author)

    visit(_item_ids(story), 0, None)
    return tuple(comments)


def _item_ids(item: HackerNewsItem) -> tuple[int, ...]:
    values = item.get("kids")
    if not isinstance(values, list):
        return ()
    return tuple(
        value
        for value in values
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    )


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _http_url(value: object) -> str | None:
    url = _optional_text(value)
    if url is None:
        return None
    try:
        parsed = urlparse(url)
        has_supported_origin = (
            parsed.scheme in {"http", "https"} and parsed.hostname is not None
        )
    except ValueError:
        return None
    return url if has_supported_origin else None


def _non_negative_int(value: object) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def _timestamp_iso(value: object) -> str | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None
