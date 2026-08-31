from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import time
import zlib
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx

from app.config import Settings
from app.utils import slugify, strip_html

READ_STATE = "user/-/state/com.google/read"
STARRED_STATE = "user/-/state/com.google/starred"
READING_LIST_STREAM = "reading-list"
STREAM_PAGE_CACHE_LIMIT = 64
STREAM_CACHE_HTML_CHARS = 2_048
STREAM_CACHE_TEXT_CHARS = 512
STREAM_CACHE_STALE_GRACE_SECONDS = 300
ENTRY_CACHE_LIMIT = 512
ENTRY_CACHE_BYTES_LIMIT = 16 * 1024 * 1024
LOCAL_STATE_GRACE_SECONDS = 300
CURSOR_ANCHOR_CACHE_LIMIT = 128
STATE_SCAN_CHUNK_SIZE = 100
STATE_SCAN_REQUEST_LIMIT = 10
OVERLAY_CONTINUATION_PREFIX = "rk1."
OVERLAY_CONTINUATION_MAX_ITEMS = 100
OVERLAY_CONTINUATION_MAX_BYTES = 65_536

logger = logging.getLogger(__name__)


class FreshRSSError(RuntimeError):
    pass


@dataclass(frozen=True)
class FreshRSSGroup:
    name: str
    slug: str
    stream_id: str


@dataclass(frozen=True)
class FreshRSSFeed:
    token: str
    stream_id: str
    title: str
    feed_url: str | None
    site_url: str | None
    group_slugs: tuple[str, ...]


@dataclass(frozen=True)
class FreshRSSEntry:
    id: str
    title: str
    author: str | None
    url: str | None
    published_at: str | None
    summary_html: str | None
    summary_text: str
    content_html: str | None
    feed_title: str | None
    feed_site_url: str | None
    feed_token: str | None
    group_names: tuple[str, ...]
    is_starred: bool
    received_at: str | None = None
    is_read: bool = False


@dataclass(frozen=True)
class FreshRSSStreamPage:
    entries: list[FreshRSSEntry]
    continuation: str | None
    is_stale: bool = False
    entries_are_compact: bool = False


@dataclass(frozen=True)
class FreshRSSNavigation:
    groups: list[FreshRSSGroup]
    feeds: list[FreshRSSFeed]


@dataclass
class _StreamRefreshState:
    lock: threading.Lock
    users: int = 0


@dataclass(frozen=True)
class _OverlayContinuation:
    scope_kind: str
    scope_value: str | None
    upstream: str | None
    carried_entry_ids: tuple[str, ...]
    shown_entry_ids: tuple[str, ...]


def normalize_freshrss_api_url(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise FreshRSSError("FreshRSS API URL is required.")

    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        raise FreshRSSError("FreshRSS API URL must include the scheme and host.")

    path = parsed.path.rstrip("/")
    if path.endswith("/greader.php"):
        normalized_path = path
    elif path.endswith("/api"):
        normalized_path = f"{path}/greader.php"
    else:
        normalized_path = f"{path}/api/greader.php" if path else "/api/greader.php"
    return urlunsplit((parsed.scheme, parsed.netloc, normalized_path, "", ""))


def encode_feed_token(stream_id: str) -> str:
    raw = stream_id.encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_feed_token(token: str) -> str:
    padding = "=" * ((4 - len(token) % 4) % 4)
    return base64.urlsafe_b64decode(token + padding).decode("utf-8")


class FreshRSSClient:
    def __init__(
        self,
        settings: Settings,
        client_factory: Callable[[], Any] | None = None,
    ):
        if (
            not settings.freshrss_api_url
            or not settings.freshrss_username
            or not settings.freshrss_api_password
        ):
            raise FreshRSSError(
                "FreshRSS frontend mode requires FRESHRSS_API_URL, FRESHRSS_USERNAME, and FRESHRSS_API_PASSWORD."
            )

        self.settings = settings
        self.api_url = normalize_freshrss_api_url(settings.freshrss_api_url)
        self.username = settings.freshrss_username
        self.api_password = settings.freshrss_api_password
        self.client_factory = client_factory or self._default_client_factory
        self._shared_client = (
            None if client_factory is not None else self._default_client_factory()
        )
        self._lock = threading.Lock()
        self._state_update_lock = threading.Lock()
        self._navigation_refresh_lock = threading.Lock()
        self._stream_refresh_states: dict[
            tuple[str, str, str | None, bool], _StreamRefreshState
        ] = {}
        self._auth_token: str | None = None
        self._write_token: str | None = None
        self._navigation_cache: tuple[float, FreshRSSNavigation] | None = None
        self._navigation_retry_after = 0.0
        self._entry_cache: dict[str, tuple[float, FreshRSSEntry, int]] = {}
        self._entry_cache_bytes = 0
        self._stream_cache: dict[
            tuple[str, str | None, str | None, int, bool],
            tuple[float, FreshRSSStreamPage],
        ] = {}
        self._stream_retry_after: dict[
            tuple[str, str | None, str | None, int, bool], float
        ] = {}
        self._stream_cache_generation = 0
        self._local_state_overrides: dict[
            tuple[Literal["read", "starred"], str], tuple[bool, float | None]
        ] = {}
        self._cursor_anchors: dict[tuple[str, str], str] = {}

    def _default_client_factory(self) -> httpx.Client:
        return httpx.Client(
            follow_redirects=True,
            timeout=self.settings.http_timeout_seconds,
            headers={"User-Agent": self.settings.user_agent},
        )

    @contextmanager
    def _open_client(self):
        if self._shared_client is not None:
            yield self._shared_client
            return
        with self.client_factory() as client:
            yield client

    def close(self) -> None:
        if self._shared_client is not None:
            self._shared_client.close()

    @contextmanager
    def _stream_singleflight(
        self, key: tuple[str, str, str | None, bool]
    ):
        with self._lock:
            state = self._stream_refresh_states.get(key)
            if state is None:
                state = _StreamRefreshState(lock=threading.Lock())
                self._stream_refresh_states[key] = state
            state.users += 1
        try:
            with state.lock:
                yield
        finally:
            with self._lock:
                state.users -= 1
                if (
                    state.users == 0
                    and self._stream_refresh_states.get(key) is state
                ):
                    self._stream_refresh_states.pop(key, None)

    def list_navigation(self, force_refresh: bool = False) -> FreshRSSNavigation:
        now = time.monotonic()
        with self._lock:
            cached = self._navigation_cache
            retry_after = self._navigation_retry_after
            if not force_refresh and cached and (
                cached[0] > now or retry_after > now
            ):
                return cached[1]

        with self._navigation_refresh_lock:
            now = time.monotonic()
            with self._lock:
                cached = self._navigation_cache
                retry_after = self._navigation_retry_after
                if not force_refresh and cached and (
                    cached[0] > now or retry_after > now
                ):
                    return cached[1]
            try:
                payload = self._request_json(
                    "GET",
                    "/reader/api/0/subscription/list",
                    params={"output": "json"},
                )
                navigation = parse_navigation(payload)
            except (FreshRSSError, httpx.HTTPError, ValueError):
                if cached is None or force_refresh:
                    raise
                logger.warning(
                    "FreshRSS navigation refresh failed; serving the expired cache",
                    exc_info=True,
                )
                with self._lock:
                    self._navigation_retry_after = time.monotonic() + 5
                return cached[1]
            with self._lock:
                self._navigation_cache = (
                    time.monotonic() + self.settings.metadata_cache_seconds,
                    navigation,
                )
                self._navigation_retry_after = 0.0
            return navigation

    def get_stream(
        self,
        *,
        scope_kind: str,
        scope_value: str | None = None,
        continuation: str | None = None,
        limit: int = 15,
        include_read: bool = False,
    ) -> FreshRSSStreamPage:
        include_read = include_read or scope_kind == "starred"
        cache_key = (scope_kind, scope_value, continuation, limit, include_read)
        refresh_key = ("request", scope_kind, scope_value, include_read)
        now = time.monotonic()
        cache_generation = 0
        cached: tuple[float, FreshRSSStreamPage] | None = None
        if self.settings.stream_cache_seconds > 0:
            with self._lock:
                self._prune_stream_cache_locked(now)
                self._prune_stream_retry_after_locked(now)
                cache_generation = self._stream_cache_generation
                cached = self._stream_cache.get(cache_key)
                retry_after = self._stream_retry_after.get(cache_key, 0.0)
            if cached is not None and cached[0] > now:
                return cached[1]
            if cached is not None and retry_after > now:
                return replace(cached[1], is_stale=True)

        with self._stream_singleflight(refresh_key):
            now = time.monotonic()
            with self._lock:
                self._prune_stream_cache_locked(now)
                self._prune_stream_retry_after_locked(now)
                cache_generation = self._stream_cache_generation
                cached = self._stream_cache.get(cache_key)
                retry_after = self._stream_retry_after.get(cache_key, 0.0)
            if cached is not None and cached[0] > now:
                return cached[1]
            if cached is not None and retry_after > now:
                return replace(cached[1], is_stale=True)
            try:
                page = self._load_stream(
                    scope_kind=scope_kind,
                    scope_value=scope_value,
                    continuation=continuation,
                    limit=limit,
                    include_read=include_read,
                )
            except (FreshRSSError, httpx.HTTPError, ValueError):
                if cached is None:
                    raise
                logger.warning(
                    "FreshRSS stream refresh failed; serving the expired cache",
                    exc_info=True,
                )
                with self._lock:
                    if cache_key in self._stream_cache:
                        self._stream_retry_after[cache_key] = time.monotonic() + 5
                return replace(cached[1], is_stale=True)
            self._store_stream_cache(
                cache_key,
                page,
                expected_generation=cache_generation,
            )
            return page

    def _load_stream(
        self,
        *,
        scope_kind: str,
        scope_value: str | None,
        continuation: str | None,
        limit: int,
        include_read: bool,
    ) -> FreshRSSStreamPage:
        overlay = _decode_overlay_continuation(continuation)
        if continuation and continuation.startswith(OVERLAY_CONTINUATION_PREFIX):
            if overlay is None:
                raise FreshRSSError("Invalid local stream continuation.")
            if (
                overlay.scope_kind != scope_kind
                or overlay.scope_value != scope_value
            ):
                raise FreshRSSError("Stream continuation does not match this view.")
        upstream_continuation = overlay.upstream if overlay else continuation
        navigation = (
            self._cached_navigation_or_empty()
            if scope_kind in {"home", "starred"}
            else self.list_navigation()
        )
        stream_id = self._stream_id_for_scope(
            scope_kind,
            scope_value,
            navigation,
        )
        resolved_carries: list[FreshRSSEntry] = []
        deferred_carry_ids: tuple[str, ...] = ()
        if overlay:
            resolved_carries, deferred_carry_ids = self._resolve_overlay_entries(
                overlay.carried_entry_ids,
                limit=limit,
            )
        carried_entries = (
            self._filter_stream_entries(
                resolved_carries,
                scope_kind=scope_kind,
                scope_value=scope_value,
                navigation=navigation,
                include_read=include_read,
            )
            if overlay
            else []
        )
        if overlay and (deferred_carry_ids or len(carried_entries) >= limit):
            page = FreshRSSStreamPage(
                entries=carried_entries[:limit],
                continuation=_encode_overlay_continuation(
                    replace(
                        overlay,
                        carried_entry_ids=tuple(
                            entry.id for entry in carried_entries[limit:]
                        )
                        + deferred_carry_ids,
                    )
                ),
            )
            self._cache_entries(page.entries)
            return page

        fetch_limit = limit
        if overlay:
            fetch_limit = (
                limit - len(carried_entries) + len(overlay.shown_entry_ids)
            )
        starred_anchor_state = (
            self._cursor_outside_filtered_state(
                upstream_continuation,
                "starred",
                stream_id=stream_id,
            )
            if upstream_continuation and scope_kind == "starred"
            else False
        )
        if overlay and upstream_continuation is None:
            page = FreshRSSStreamPage(entries=[], continuation=None)
        elif upstream_continuation and not include_read:
            page = self._fetch_locally_filtered_continuation(
                stream_id=stream_id,
                navigation=navigation,
                continuation=upstream_continuation,
                limit=fetch_limit,
                state_kind="read",
            )
        elif (
            upstream_continuation
            and scope_kind == "starred"
            and starred_anchor_state is not False
        ):
            page = self._fetch_locally_filtered_continuation(
                stream_id=READING_LIST_STREAM,
                navigation=navigation,
                continuation=upstream_continuation,
                limit=fetch_limit,
                state_kind="starred",
            )
        else:
            page = self._fetch_stream_page(
                stream_id=stream_id,
                navigation=navigation,
                continuation=upstream_continuation,
                limit=fetch_limit,
                exclude_read=not include_read,
            )
        fetched_entry_ids = {entry.id for entry in page.entries}
        self._cache_entries(page.entries)
        page = self._overlay_stream_page(
            page,
            scope_kind=scope_kind,
            scope_value=scope_value,
            navigation=navigation,
            continuation=continuation,
            include_read=include_read,
            limit=limit,
        )
        if overlay:
            page = self._continue_overlay_page(
                page,
                overlay=overlay,
                carried_entries=carried_entries,
                fetched_entry_ids=fetched_entry_ids,
                limit=limit,
            )
        self._cache_entries(page.entries)
        return page

    def _resolve_overlay_entries(
        self,
        entry_ids: Iterable[str],
        *,
        limit: int,
    ) -> tuple[list[FreshRSSEntry], tuple[str, ...]]:
        ordered_ids = list(dict.fromkeys(entry_ids))
        selected_ids = ordered_ids[: max(1, limit)]
        deferred_ids = tuple(ordered_ids[len(selected_ids) :])
        entries_by_id: dict[str, FreshRSSEntry] = {}
        missing_ids: list[str] = []
        for entry_id in selected_ids:
            entry = self._get_cached_entry(entry_id)
            if entry is not None:
                entries_by_id[entry_id] = entry
            else:
                missing_ids.append(entry_id)
        if missing_ids:
            entries_by_id.update(self._fetch_overlay_entries(missing_ids))
        return (
            [
                entries_by_id[entry_id]
                for entry_id in selected_ids
                if entry_id in entries_by_id
            ],
            deferred_ids,
        )

    def _continue_overlay_page(
        self,
        page: FreshRSSStreamPage,
        *,
        overlay: _OverlayContinuation,
        carried_entries: list[FreshRSSEntry],
        fetched_entry_ids: set[str],
        limit: int,
    ) -> FreshRSSStreamPage:
        scheduled_ids = set(overlay.shown_entry_ids)
        merged = list(carried_entries)
        present_ids = {entry.id for entry in merged}
        for entry in page.entries:
            if entry.id in scheduled_ids or entry.id in present_ids:
                continue
            merged.append(entry)
            present_ids.add(entry.id)

        visible = merged[:limit]
        remaining = merged[limit:]
        remaining_scheduled_ids = tuple(
            entry_id
            for entry_id in overlay.shown_entry_ids
            if entry_id not in fetched_entry_ids
            and _entry_can_follow_continuation(entry_id, page.continuation)
        )
        continuation = _encode_overlay_continuation(
            replace(
                overlay,
                upstream=page.continuation,
                carried_entry_ids=tuple(entry.id for entry in remaining),
                shown_entry_ids=remaining_scheduled_ids,
            )
        )
        return replace(page, entries=visible, continuation=continuation)

    def _fetch_locally_filtered_continuation(
        self,
        *,
        stream_id: str,
        navigation: FreshRSSNavigation,
        continuation: str,
        limit: int,
        state_kind: Literal["read", "starred"],
    ) -> FreshRSSStreamPage:
        """Keep a mutable state filter from removing FreshRSS's cursor anchor."""
        entries: list[FreshRSSEntry] = []
        cursor: str | None = continuation
        seen = {continuation}
        request_count = 0
        numeric_cursor = continuation.isdigit()
        while (
            cursor
            and len(entries) < limit
            and request_count < STATE_SCAN_REQUEST_LIMIT
        ):
            remaining = limit - len(entries)
            request_limit = (
                remaining
                if request_count == 0 or not numeric_cursor
                else STATE_SCAN_CHUNK_SIZE
            )
            raw_page = self._fetch_stream_page(
                stream_id=stream_id,
                navigation=navigation,
                continuation=cursor,
                limit=request_limit,
                exclude_read=False,
            )
            request_count += 1
            raw_entries = self._overlay_entries(raw_page.entries)
            matching_entries = [
                entry
                for entry in raw_entries
                if (not entry.is_read if state_kind == "read" else entry.is_starred)
            ]
            for entry in matching_entries:
                entries.append(entry)
                if len(entries) == limit:
                    selected_cursor = (
                        _continuation_from_entry_id(entry.id)
                        if numeric_cursor
                        else raw_page.continuation
                    )
                    if selected_cursor is not None:
                        self._remember_cursor_anchor(
                            stream_id,
                            selected_cursor,
                            entry.id,
                        )
                        return FreshRSSStreamPage(
                            entries=entries,
                            continuation=selected_cursor,
                        )
                    return FreshRSSStreamPage(
                        entries=entries,
                        continuation=raw_page.continuation,
                    )

            next_cursor = raw_page.continuation
            if not next_cursor or next_cursor in seen:
                return FreshRSSStreamPage(entries=entries, continuation=None)
            seen.add(next_cursor)
            cursor = next_cursor
            numeric_cursor = numeric_cursor and next_cursor.isdigit()
        return FreshRSSStreamPage(entries=entries, continuation=cursor)

    def _cursor_outside_filtered_state(
        self,
        continuation: str,
        state_kind: Literal["read", "starred"],
        *,
        stream_id: str,
    ) -> bool | None:
        with self._lock:
            entry_id = self._cursor_anchors.get((stream_id, continuation))
        entry_id = entry_id or _entry_id_from_continuation(continuation)
        if entry_id is None:
            return None
        with self._lock:
            local_override = self._local_state_overrides.get((state_kind, entry_id))
        if local_override is not None:
            entry = self._get_cached_entry(entry_id)
            if entry is None:
                entry = self.get_entry(entry_id)
        elif state_kind == "starred":
            entry = self._fetch_entry(entry_id)
        else:
            entry = self._get_cached_entry(entry_id) or self.get_entry(entry_id)
        if entry is None:
            return None
        return entry.is_read if state_kind == "read" else not entry.is_starred

    def _remember_cursor_anchor(
        self,
        stream_id: str,
        continuation: str,
        entry_id: str,
    ) -> None:
        with self._lock:
            key = (stream_id, continuation)
            self._cursor_anchors.pop(key, None)
            self._cursor_anchors[key] = entry_id
            while len(self._cursor_anchors) > CURSOR_ANCHOR_CACHE_LIMIT:
                oldest = next(iter(self._cursor_anchors))
                self._cursor_anchors.pop(oldest, None)

    def _cached_navigation_or_empty(self) -> FreshRSSNavigation:
        with self._lock:
            cached = self._navigation_cache
        if cached is not None:
            return cached[1]
        return FreshRSSNavigation(groups=[], feeds=[])

    def _store_stream_cache(
        self,
        cache_key: tuple[str, str | None, str | None, int, bool],
        page: FreshRSSStreamPage,
        *,
        expected_generation: int,
    ) -> None:
        if self.settings.stream_cache_seconds <= 0:
            return
        expires_at = time.monotonic() + self.settings.stream_cache_seconds
        with self._lock:
            self._prune_stream_cache_locked(time.monotonic())
            self._prune_stream_retry_after_locked(time.monotonic())
            if expected_generation != self._stream_cache_generation:
                return
            self._stream_retry_after.pop(cache_key, None)
            if (
                len(self._stream_cache) >= STREAM_PAGE_CACHE_LIMIT
                and cache_key not in self._stream_cache
            ):
                oldest_key = min(
                    self._stream_cache,
                    key=lambda key: self._stream_cache[key][0],
                )
                self._stream_cache.pop(oldest_key, None)
                self._stream_retry_after.pop(oldest_key, None)
            self._stream_cache[cache_key] = (
                expires_at,
                replace(
                    page,
                    entries=[_stream_cache_entry(entry) for entry in page.entries],
                    entries_are_compact=True,
                ),
            )

    def _prune_stream_cache_locked(self, now: float) -> None:
        expired_keys = [
            key
            for key, (expires_at, _) in self._stream_cache.items()
            if expires_at + STREAM_CACHE_STALE_GRACE_SECONDS <= now
        ]
        for key in expired_keys:
            self._stream_cache.pop(key, None)
            self._stream_retry_after.pop(key, None)

    def _prune_stream_retry_after_locked(self, now: float) -> None:
        if not self._stream_retry_after:
            return
        self._stream_retry_after = {
            key: retry_after
            for key, retry_after in self._stream_retry_after.items()
            if retry_after > now and key in self._stream_cache
        }

    def get_entry(self, entry_id: str) -> FreshRSSEntry | None:
        cached = self._get_cached_entry(entry_id)
        if cached is not None:
            return cached
        return self._fetch_entry(entry_id)

    def _fetch_entry(self, entry_id: str) -> FreshRSSEntry | None:
        return self._fetch_entry_response(entry_id, suppress_http_errors=True)

    def _fetch_overlay_entries(
        self, entry_ids: Iterable[str]
    ) -> dict[str, FreshRSSEntry]:
        ids = list(dict.fromkeys(entry_ids))
        if not ids:
            return {}
        return self._fetch_entries_response(ids)

    def _fetch_entry_response(
        self,
        entry_id: str,
        *,
        suppress_http_errors: bool,
    ) -> FreshRSSEntry | None:
        try:
            entries = self._fetch_entries_response([entry_id])
        except httpx.HTTPError:
            if suppress_http_errors:
                return None
            raise
        return entries.get(entry_id)

    def _fetch_entries_response(
        self, entry_ids: list[str]
    ) -> dict[str, FreshRSSEntry]:
        navigation = self._cached_navigation_or_empty()
        payload = self._request_json(
            "POST",
            "/reader/api/0/stream/items/contents",
            data=[("output", "json"), *(("i", entry_id) for entry_id in entry_ids)],
        )
        items = payload.get("items") or []
        parsed = [
            entry
            for item in items
            if (entry := self._parse_entry(item, navigation)) is not None
        ]
        entries = self._overlay_entries(parsed)
        self._cache_entries(entries)
        requested_ids = set(entry_ids)
        return {entry.id: entry for entry in entries if entry.id in requested_ids}

    def get_latest_entry(self) -> FreshRSSEntry | None:
        navigation = self._cached_navigation_or_empty()
        page = self._fetch_stream_page(
            stream_id=READING_LIST_STREAM,
            navigation=navigation,
            continuation=None,
            limit=1,
            exclude_read=False,
        )
        page = replace(page, entries=self._overlay_entries(page.entries))
        self._cache_entries(page.entries)
        return page.entries[0] if page.entries else None

    def get_last_refreshed_at(self) -> str | None:
        parsed = urlsplit(self.api_url)
        base_path = parsed.path.removesuffix("/api/greader.php")
        fever_url = urlunsplit(
            (parsed.scheme, parsed.netloc, f"{base_path}/api/fever.php", "api", "")
        )
        api_key = hashlib.md5(
            f"{self.username}:{self.api_password}".encode(),
            usedforsecurity=False,
        ).hexdigest()
        with self._open_client() as client:
            response = client.request("POST", fever_url, data={"api_key": api_key})
        response.raise_for_status()
        payload = response.json()
        if int(payload.get("auth") or 0) != 1:
            raise FreshRSSError("FreshRSS Fever API authentication failed.")
        return _timestamp_to_iso(payload.get("last_refreshed_on_time"))

    def mark_read(self, entry_ids: Iterable[str]) -> None:
        ids = self.send_state("read", entry_ids, True)
        self.apply_local_state("read", ids, True)
        self.confirm_local_state("read", ids, True)

    def mark_unread(self, entry_ids: Iterable[str]) -> None:
        ids = self.send_state("read", entry_ids, False)
        self.apply_local_state("read", ids, False)
        self.confirm_local_state("read", ids, False)

    def mark_starred(self, entry_ids: Iterable[str]) -> None:
        ids = self.send_state("starred", entry_ids, True)
        self.apply_local_state("starred", ids, True)
        self.confirm_local_state("starred", ids, True)

    def mark_unstarred(self, entry_ids: Iterable[str]) -> None:
        ids = self.send_state("starred", entry_ids, False)
        self.apply_local_state("starred", ids, False)
        self.confirm_local_state("starred", ids, False)

    def send_state(
        self,
        kind: Literal["read", "starred"],
        entry_ids: Iterable[str],
        enabled: bool,
    ) -> list[str]:
        """Write state to FreshRSS without changing local caches."""
        if kind == "read":
            state = READ_STATE
        elif kind == "starred":
            state = STARRED_STATE
        else:
            raise ValueError(f"Unsupported FreshRSS state kind: {kind}")
        return self._edit_tags(
            entry_ids,
            add=state if enabled else None,
            remove=None if enabled else state,
        )

    def apply_local_state(
        self,
        kind: Literal["read", "starred"],
        entry_ids: Iterable[str],
        enabled: bool,
    ) -> None:
        """Change cached state without sending an upstream request."""
        ids = [
            str(entry_id).strip()
            for entry_id in entry_ids
            if str(entry_id).strip()
        ]
        with self._state_update_lock:
            with self._lock:
                for entry_id in ids:
                    self._local_state_overrides[(kind, entry_id)] = (
                        enabled,
                        None,
                    )
            if kind == "read":
                if enabled:
                    self._set_cached_read(ids, True)
                else:
                    self._set_cached_entry_read(ids, False)
                    self._clear_stream_cache()
                return
            if kind == "starred":
                self._set_cached_starred(ids, enabled)
                self._set_cached_stream_starred(ids, enabled)
                self._clear_stream_cache(scope_kind="starred")
                return
        raise ValueError(f"Unsupported FreshRSS state kind: {kind}")

    def confirm_local_state(
        self,
        kind: Literal["read", "starred"],
        entry_ids: Iterable[str],
        enabled: bool,
    ) -> None:
        """Keep an acknowledged state long enough to fence in-flight stale reads."""
        expires_at = time.monotonic() + LOCAL_STATE_GRACE_SECONDS
        with self._lock:
            for raw_entry_id in entry_ids:
                entry_id = str(raw_entry_id).strip()
                key = (kind, entry_id)
                current = self._local_state_overrides.get(key)
                if current is not None and current[0] == enabled:
                    self._local_state_overrides[key] = (enabled, expires_at)

    def _prune_local_state_overrides_locked(self, now: float) -> None:
        expired = [
            key
            for key, (_, expires_at) in self._local_state_overrides.items()
            if expires_at is not None and expires_at <= now
        ]
        for key in expired:
            self._local_state_overrides.pop(key, None)

    def _overlay_entry_locked(self, entry: FreshRSSEntry) -> FreshRSSEntry:
        read = self._local_state_overrides.get(("read", entry.id))
        starred = self._local_state_overrides.get(("starred", entry.id))
        return replace(
            entry,
            is_read=read[0] if read is not None else entry.is_read,
            is_starred=starred[0] if starred is not None else entry.is_starred,
        )

    def _overlay_entries(
        self, entries: Iterable[FreshRSSEntry]
    ) -> list[FreshRSSEntry]:
        now = time.monotonic()
        with self._lock:
            self._prune_local_state_overrides_locked(now)
            return [self._overlay_entry_locked(entry) for entry in entries]

    def _filter_stream_entries(
        self,
        entries: Iterable[FreshRSSEntry],
        *,
        scope_kind: str,
        scope_value: str | None,
        navigation: FreshRSSNavigation,
        include_read: bool,
    ) -> list[FreshRSSEntry]:
        return [
            entry
            for entry in self._overlay_entries(entries)
            if self._entry_belongs_to_scope(
                entry,
                scope_kind=scope_kind,
                scope_value=scope_value,
                navigation=navigation,
            )
            and (scope_kind != "starred" or entry.is_starred)
            and (include_read or not entry.is_read)
        ]

    def _overlay_stream_page(
        self,
        page: FreshRSSStreamPage,
        *,
        scope_kind: str,
        scope_value: str | None,
        navigation: FreshRSSNavigation,
        continuation: str | None,
        include_read: bool,
        limit: int,
    ) -> FreshRSSStreamPage:
        now = time.monotonic()
        entries = self._filter_stream_entries(
            page.entries,
            scope_kind=scope_kind,
            scope_value=scope_value,
            navigation=navigation,
            include_read=include_read,
        )
        candidate_entry_ids: list[str] = []
        resolved_missing: dict[str, FreshRSSEntry] = {}
        deferred_missing_ids: list[str] = []
        if continuation is None:
            present_ids = {entry.id for entry in entries}
            with self._lock:
                self._prune_local_state_overrides_locked(now)
                for (state_kind, entry_id), (
                    enabled,
                    _,
                ) in self._local_state_overrides.items():
                    if not self._override_adds_membership(
                        state_kind=state_kind,
                        enabled=enabled,
                        scope_kind=scope_kind,
                        include_read=include_read,
                    ):
                        continue
                    if entry_id in present_ids:
                        continue
                    if entry_id not in candidate_entry_ids:
                        candidate_entry_ids.append(entry_id)
                    if len(candidate_entry_ids) > OVERLAY_CONTINUATION_MAX_ITEMS:
                        raise FreshRSSError(
                            "Too many pending changes for one Kindle stream."
                        )
                missing_entry_ids = []
                for entry_id in candidate_entry_ids:
                    cached = self._entry_cache.get(entry_id)
                    if cached is None or cached[0] <= now:
                        missing_entry_ids.append(entry_id)
            chunk_size = max(1, min(limit, OVERLAY_CONTINUATION_MAX_ITEMS))
            while missing_entry_ids:
                available = self._filter_stream_entries(
                    resolved_missing.values(),
                    scope_kind=scope_kind,
                    scope_value=scope_value,
                    navigation=navigation,
                    include_read=include_read,
                )
                with self._lock:
                    cached_available = [
                        self._overlay_entry_locked(cached[1])
                        for entry_id in candidate_entry_ids
                        if (cached := self._entry_cache.get(entry_id)) is not None
                        and cached[0] > now
                    ]
                cached_available = self._filter_stream_entries(
                    cached_available,
                    scope_kind=scope_kind,
                    scope_value=scope_value,
                    navigation=navigation,
                    include_read=include_read,
                )
                available_ids = {
                    entry.id for entry in [*available, *cached_available]
                }
                if len(entries) + len(available_ids) >= limit:
                    break
                resolve_now = missing_entry_ids[:chunk_size]
                missing_entry_ids = missing_entry_ids[chunk_size:]
                resolved_missing.update(
                    self._fetch_overlay_entries(resolve_now)
                )
            deferred_missing_ids = missing_entry_ids

        with self._lock:
            self._prune_local_state_overrides_locked(now)
            additions: list[FreshRSSEntry] = []
            if continuation is None:
                present_ids = {entry.id for entry in entries}
                active_candidate_ids = {
                    entry_id
                    for (state_kind, entry_id), (
                        enabled,
                        _,
                    ) in self._local_state_overrides.items()
                    if entry_id in candidate_entry_ids
                    and self._override_adds_membership(
                        state_kind=state_kind,
                        enabled=enabled,
                        scope_kind=scope_kind,
                        include_read=include_read,
                    )
                }
                deferred_missing_ids = [
                    entry_id
                    for entry_id in deferred_missing_ids
                    if entry_id in active_candidate_ids and entry_id not in present_ids
                ]
                for entry_id in candidate_entry_ids:
                    if entry_id not in active_candidate_ids:
                        continue
                    if entry_id in present_ids:
                        continue
                    cached = self._entry_cache.get(entry_id)
                    if cached is not None and cached[0] > now:
                        candidate = self._overlay_entry_locked(cached[1])
                    else:
                        resolved = resolved_missing.get(entry_id)
                        if resolved is None:
                            continue
                        candidate = self._overlay_entry_locked(resolved)
                    if self._entry_belongs_to_scope(
                        candidate,
                        scope_kind=scope_kind,
                        scope_value=scope_value,
                        navigation=navigation,
                    ):
                        additions.append(candidate)
                        present_ids.add(entry_id)

        additions = [
            entry
            for entry in additions
            if (scope_kind != "starred" or entry.is_starred)
            and (include_read or not entry.is_read)
        ]
        if not additions and not deferred_missing_ids:
            return replace(page, entries=entries)
        merged = [*additions, *entries]
        visible = merged[:limit]
        carried_entry_ids = [
            *[entry.id for entry in merged[limit:]],
            *deferred_missing_ids,
        ]
        scheduled_entry_ids = [
            *[entry.id for entry in additions],
            *deferred_missing_ids,
        ]
        overlay = _OverlayContinuation(
            scope_kind=scope_kind,
            scope_value=scope_value,
            upstream=page.continuation,
            carried_entry_ids=tuple(dict.fromkeys(carried_entry_ids)),
            shown_entry_ids=tuple(
                entry_id
                for entry_id in dict.fromkeys(scheduled_entry_ids)
                if _entry_can_follow_continuation(entry_id, page.continuation)
            ),
        )
        return replace(
            page,
            entries=visible,
            continuation=_encode_overlay_continuation(overlay),
        )

    @staticmethod
    def _override_adds_membership(
        *,
        state_kind: Literal["read", "starred"],
        enabled: bool,
        scope_kind: str,
        include_read: bool,
    ) -> bool:
        return (
            scope_kind == "starred"
            and state_kind == "starred"
            and enabled
        ) or (
            scope_kind != "starred"
            and not include_read
            and state_kind == "read"
            and not enabled
        )

    @staticmethod
    def _entry_belongs_to_scope(
        entry: FreshRSSEntry,
        *,
        scope_kind: str,
        scope_value: str | None,
        navigation: FreshRSSNavigation,
    ) -> bool:
        if scope_kind in {"home", "starred"}:
            return True
        if scope_kind == "feed":
            return entry.feed_token == scope_value
        if scope_kind == "group":
            group = next(
                (candidate for candidate in navigation.groups if candidate.slug == scope_value),
                None,
            )
            if group is None:
                return False
            if group.name in entry.group_names:
                return True
            feed = next(
                (
                    candidate
                    for candidate in navigation.feeds
                    if candidate.token == entry.feed_token
                ),
                None,
            )
            return feed is not None and group.slug in feed.group_slugs
        return False

    def _cache_entries(self, entries: Iterable[FreshRSSEntry]) -> None:
        configured_ttl = self.settings.entry_cache_seconds
        if configured_ttl <= 0:
            return
        now = time.monotonic()
        expires_at = now + configured_ttl
        with self._lock:
            self._prune_local_state_overrides_locked(now)
            for entry_id, cached in list(self._entry_cache.items()):
                if cached[0] <= now:
                    self._drop_cached_entry_locked(entry_id)
            for entry in entries:
                entry = self._overlay_entry_locked(entry)
                self._drop_cached_entry_locked(entry.id)
                entry_bytes = _entry_cache_size(entry)
                self._entry_cache[entry.id] = (expires_at, entry, entry_bytes)
                self._entry_cache_bytes += entry_bytes
            while (
                len(self._entry_cache) > ENTRY_CACHE_LIMIT
                or self._entry_cache_bytes > ENTRY_CACHE_BYTES_LIMIT
            ):
                oldest_entry_id = next(iter(self._entry_cache))
                self._drop_cached_entry_locked(oldest_entry_id)

    def _drop_cached_entry_locked(self, entry_id: str) -> None:
        cached = self._entry_cache.pop(entry_id, None)
        if cached is not None:
            self._entry_cache_bytes -= cached[2]

    def _get_cached_entry(self, entry_id: str) -> FreshRSSEntry | None:
        now = time.monotonic()
        with self._lock:
            cached = self._entry_cache.get(entry_id)
            if cached is None:
                return None
            if cached[0] <= now:
                self._drop_cached_entry_locked(entry_id)
                return None
            self._entry_cache.pop(entry_id)
            entry = self._overlay_entry_locked(cached[1])
            self._entry_cache[entry_id] = (cached[0], entry, cached[2])
            return entry

    def _set_cached_starred(self, entry_ids: Iterable[str], is_starred: bool) -> None:
        ids = set(entry_ids)
        with self._lock:
            for entry_id in ids:
                cached = self._entry_cache.get(entry_id)
                if cached is not None:
                    self._entry_cache[entry_id] = (
                        cached[0],
                        replace(cached[1], is_starred=is_starred),
                        cached[2],
                    )

    def _set_cached_entry_read(self, entry_ids: Iterable[str], is_read: bool) -> None:
        ids = set(entry_ids)
        if not ids:
            return
        with self._lock:
            for entry_id in ids:
                cached = self._entry_cache.get(entry_id)
                if cached is not None:
                    self._entry_cache[entry_id] = (
                        cached[0],
                        replace(cached[1], is_read=is_read),
                        cached[2],
                    )

    def _set_cached_read(self, entry_ids: Iterable[str], is_read: bool) -> None:
        ids = set(entry_ids)
        if not ids:
            return
        self._set_cached_entry_read(ids, is_read)
        with self._lock:
            self._stream_cache_generation += 1
            for key, (expires_at, page) in list(self._stream_cache.items()):
                keeps_read = key[0] == "starred" or key[4]
                if is_read and not keeps_read:
                    entries = [entry for entry in page.entries if entry.id not in ids]
                else:
                    entries = [
                        replace(entry, is_read=is_read) if entry.id in ids else entry
                        for entry in page.entries
                    ]
                if entries != page.entries:
                    self._stream_cache[key] = (
                        expires_at,
                        replace(page, entries=entries),
                    )

    def _set_cached_stream_starred(
        self, entry_ids: Iterable[str], is_starred: bool
    ) -> None:
        ids = set(entry_ids)
        if not ids:
            return
        with self._lock:
            self._stream_cache_generation += 1
            for key, (expires_at, page) in list(self._stream_cache.items()):
                entries = [
                    replace(entry, is_starred=is_starred) if entry.id in ids else entry
                    for entry in page.entries
                ]
                self._stream_cache[key] = (
                    expires_at,
                    replace(page, entries=entries),
                )

    def _clear_stream_cache(self, *, scope_kind: str | None = None) -> None:
        with self._lock:
            self._stream_cache_generation += 1
            if scope_kind is None:
                self._stream_cache.clear()
                self._stream_retry_after.clear()
                return
            self._stream_cache = {
                key: value
                for key, value in self._stream_cache.items()
                if key[0] != scope_kind
            }
            self._stream_retry_after = {
                key: value
                for key, value in self._stream_retry_after.items()
                if key[0] != scope_kind
            }

    def get_group(self, slug: str) -> FreshRSSGroup | None:
        navigation = self.list_navigation()
        for group in navigation.groups:
            if group.slug == slug:
                return group
        return None

    def get_feed(self, token: str) -> FreshRSSFeed | None:
        navigation = self.list_navigation()
        for feed in navigation.feeds:
            if feed.token == token:
                return feed
        return None

    def _stream_id_for_scope(
        self,
        scope_kind: str,
        scope_value: str | None,
        navigation: FreshRSSNavigation,
    ) -> str:
        if scope_kind == "home":
            return READING_LIST_STREAM
        if scope_kind == "starred":
            return STARRED_STATE
        if scope_kind == "group":
            if not scope_value:
                raise FreshRSSError("Group scope requires a slug.")
            for group in navigation.groups:
                if group.slug == scope_value:
                    return group.stream_id
            raise FreshRSSError(f"Unknown FreshRSS group: {scope_value}")
        if scope_kind == "feed":
            if not scope_value:
                raise FreshRSSError("Feed scope requires a token.")
            for feed in navigation.feeds:
                if feed.token == scope_value:
                    return feed.stream_id
            raise FreshRSSError(f"Unknown FreshRSS feed: {scope_value}")
        raise FreshRSSError(f"Unsupported FreshRSS scope: {scope_kind}")

    def _edit_tags(
        self,
        entry_ids: Iterable[str],
        *,
        add: str | None = None,
        remove: str | None = None,
    ) -> list[str]:
        ids = [str(entry_id).strip() for entry_id in entry_ids if str(entry_id).strip()]
        if not ids:
            return []

        data: list[tuple[str, str]] = [("ac", "edit-tags")]
        if add:
            data.append(("a", add))
        if remove:
            data.append(("r", remove))
        for entry_id in ids:
            data.append(("i", entry_id))

        self._request(
            "POST",
            "/reader/api/0/edit-tag",
            data=data,
            require_write_token=True,
        )
        return ids

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        data: Any = None,
        require_write_token: bool = False,
    ) -> dict[str, Any]:
        response = self._request(
            method,
            path,
            params=params,
            data=data,
            require_write_token=require_write_token,
        )
        return response.json()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        data: Any = None,
        require_write_token: bool = False,
        retry: bool = True,
    ) -> httpx.Response:
        token = self._get_auth_token(force_refresh=False)
        headers = {"Authorization": f"GoogleLogin auth={token}"}
        request_data = list(data) if isinstance(data, list) else data
        if require_write_token:
            write_token = self._get_write_token(force_refresh=False)
            if isinstance(request_data, list):
                request_data = [*request_data, ("T", write_token)]
            else:
                request_data = {"T": write_token, **(request_data or {})}
        if isinstance(request_data, list):
            request_data = urlencode(request_data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        with self._open_client() as client:
            response = client.request(
                method,
                f"{self.api_url}{path}",
                params=params,
                data=request_data,
                headers=headers,
            )

        if response.status_code in {401, 403} and retry:
            with self._lock:
                self._auth_token = None
                if require_write_token:
                    self._write_token = None
            return self._request(
                method,
                path,
                params=params,
                data=data,
                require_write_token=require_write_token,
                retry=False,
            )

        response.raise_for_status()
        return response

    def _get_auth_token(self, *, force_refresh: bool) -> str:
        with self._lock:
            if self._auth_token and not force_refresh:
                return self._auth_token

        with self._open_client() as client:
            response = client.post(
                f"{self.api_url}/accounts/ClientLogin",
                data={"Email": self.username, "Passwd": self.api_password},
            )
        response.raise_for_status()
        values: dict[str, str] = {}
        for line in response.text.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
        auth_token = values.get("Auth") or values.get("SID")
        if not auth_token:
            raise FreshRSSError(
                "FreshRSS login succeeded but did not return an auth token."
            )
        with self._lock:
            self._auth_token = auth_token
        return auth_token

    def _get_write_token(self, *, force_refresh: bool) -> str:
        with self._lock:
            if self._write_token and not force_refresh:
                return self._write_token

        response = self._request("GET", "/reader/api/0/token", retry=not force_refresh)
        write_token = response.text.strip()
        if not write_token:
            raise FreshRSSError("FreshRSS did not return a write token.")
        with self._lock:
            self._write_token = write_token
        return write_token

    def _fetch_stream_page(
        self,
        *,
        stream_id: str,
        navigation: FreshRSSNavigation,
        continuation: str | None,
        limit: int,
        exclude_read: bool = True,
    ) -> FreshRSSStreamPage:
        params: dict[str, str] = {
            "output": "json",
            "n": str(limit),
        }
        if exclude_read and stream_id != STARRED_STATE:
            params["xt"] = READ_STATE
        if continuation:
            params["c"] = continuation
        payload = self._request_json(
            "GET",
            f"/reader/api/0/stream/contents/{quote(stream_id, safe='')}",
            params=params,
        )
        page = self._parse_stream_page(payload, navigation)
        if page.continuation and page.entries:
            self._remember_cursor_anchor(
                stream_id,
                page.continuation,
                page.entries[-1].id,
            )
        return page

    def _parse_stream_page(
        self, payload: dict[str, Any], navigation: FreshRSSNavigation
    ) -> FreshRSSStreamPage:
        entries: list[FreshRSSEntry] = []
        for raw_item in payload.get("items") or []:
            entry = self._parse_entry(raw_item, navigation)
            if entry is not None:
                entries.append(entry)
        continuation = payload.get("continuation")
        return FreshRSSStreamPage(
            entries=entries, continuation=str(continuation) if continuation else None
        )

    def _parse_entry(
        self, raw_item: dict[str, Any], navigation: FreshRSSNavigation
    ) -> FreshRSSEntry | None:
        entry_id = str(raw_item.get("id") or "").strip()
        if not entry_id:
            return None

        origin = raw_item.get("origin") or {}
        raw_feed_id = str(origin.get("streamId") or "").strip()
        normalized_feed_id = _normalize_feed_stream_id(raw_feed_id)
        feed = next(
            (
                candidate
                for candidate in navigation.feeds
                if candidate.stream_id == normalized_feed_id
            ),
            None,
        )
        categories = tuple(
            _label_name(raw_category_id)
            for raw_category_id in raw_item.get("categories") or []
            if _label_name(raw_category_id)
        )
        raw_category_ids = tuple(
            str(raw_category_id or "").strip()
            for raw_category_id in raw_item.get("categories") or []
        )
        summary_html = _content_field(raw_item, "summary")
        content_html = _content_field(raw_item, "content") or summary_html
        published_at = _timestamp_to_iso(
            raw_item.get("published")
            or raw_item.get("updated")
            or raw_item.get("crawlTimeMsec")
        )
        received_at = _timestamp_to_iso(
            raw_item.get("crawlTimeMsec") or raw_item.get("timestampUsec")
        )
        alternate = raw_item.get("alternate") or []
        url = None
        if alternate and isinstance(alternate[0], dict):
            url = str(alternate[0].get("href") or "").strip() or None

        return FreshRSSEntry(
            id=entry_id,
            title=str(raw_item.get("title") or "Untitled").strip() or "Untitled",
            author=str(raw_item.get("author") or "").strip() or None,
            url=url,
            published_at=published_at,
            summary_html=summary_html,
            summary_text=strip_html(content_html or summary_html),
            content_html=content_html,
            feed_title=feed.title
            if feed
            else str(origin.get("title") or "").strip() or None,
            feed_site_url=feed.site_url
            if feed
            else str(origin.get("htmlUrl") or "").strip() or None,
            feed_token=feed.token
            if feed
            else (
                encode_feed_token(normalized_feed_id) if normalized_feed_id else None
            ),
            group_names=categories,
            is_starred=STARRED_STATE in raw_category_ids,
            received_at=received_at,
            is_read=READ_STATE in raw_category_ids,
        )


def parse_navigation(payload: dict[str, Any]) -> FreshRSSNavigation:
    raw_groups: list[tuple[str, str]] = []
    feeds: list[FreshRSSFeed] = []
    slug_counts: dict[str, int] = {}
    slug_by_stream_id: dict[str, str] = {}

    for raw_feed in payload.get("subscriptions") or []:
        stream_id = _normalize_feed_stream_id(str(raw_feed.get("id") or "").strip())
        if not stream_id:
            continue
        title = str(raw_feed.get("title") or stream_id).strip() or stream_id
        categories = raw_feed.get("categories") or []
        group_slugs: list[str] = []
        for raw_category in categories:
            stream = str(raw_category.get("id") or "").strip()
            label = str(raw_category.get("label") or _label_name(stream) or "").strip()
            if not label or not stream:
                continue
            if stream not in slug_by_stream_id:
                base = slugify(label)
                suffix = slug_counts.get(base, 0)
                slug_counts[base] = suffix + 1
                slug = base if suffix == 0 else f"{base}-{suffix + 1}"
                slug_by_stream_id[stream] = slug
                raw_groups.append((stream, label))
            group_slugs.append(slug_by_stream_id[stream])

        feeds.append(
            FreshRSSFeed(
                token=encode_feed_token(stream_id),
                stream_id=stream_id,
                title=title,
                feed_url=str(raw_feed.get("url") or "").strip() or None,
                site_url=str(raw_feed.get("htmlUrl") or "").strip() or None,
                group_slugs=tuple(group_slugs),
            )
        )

    groups = [
        FreshRSSGroup(
            name=label, slug=slug_by_stream_id[stream_id], stream_id=stream_id
        )
        for stream_id, label in raw_groups
    ]
    groups.sort(key=lambda group: group.name.lower())
    feeds.sort(key=lambda feed: feed.title.lower())
    return FreshRSSNavigation(groups=groups, feeds=feeds)


def _content_field(raw_item: dict[str, Any], field_name: str) -> str | None:
    raw_value = raw_item.get(field_name)
    if isinstance(raw_value, dict):
        content = raw_value.get("content")
        return str(content).strip() or None if content is not None else None
    if raw_value is None:
        return None
    return str(raw_value).strip() or None


def _label_name(raw_category_id: Any) -> str | None:
    candidate = str(raw_category_id or "").strip()
    if "/label/" not in candidate:
        return None
    return candidate.split("/label/", 1)[1].strip() or None


def _timestamp_to_iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if (isinstance(value, str) and value.isdigit()) or isinstance(
            value, (int, float)
        ):
            numeric = int(value)
        else:
            return None
        while numeric > 10_000_000_000:
            numeric //= 1000
        return datetime.fromtimestamp(numeric, tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _normalize_feed_stream_id(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        return ""
    if candidate.startswith(("feed/", "user/-/", "http://", "https://")):
        return candidate
    return f"feed/{candidate}"


def _compact_overlay_entry_id(entry_id: str) -> str | int:
    prefix = "tag:google.com,2005:reader/item/"
    suffix = entry_id.removeprefix(prefix)
    if entry_id.startswith(prefix) and len(suffix) == 16:
        try:
            return int(suffix, 16)
        except ValueError:
            pass
    return entry_id


def _restore_overlay_entry_id(value: object) -> str | None:
    if isinstance(value, str):
        return value if 0 < len(value) <= 512 else None
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= 0xFFFFFFFFFFFFFFFF
    ):
        return f"tag:google.com,2005:reader/item/{value:016x}"
    return None


def _compact_overlay_entry_ids(entry_ids: tuple[str, ...]) -> object:
    values = [_compact_overlay_entry_id(entry_id) for entry_id in entry_ids]
    if values and all(isinstance(value, int) for value in values):
        numeric_values = [int(value) for value in values]
        return {
            "d": [
                numeric_values[0],
                *[
                    current - previous
                    for previous, current in zip(
                        numeric_values,
                        numeric_values[1:],
                    )
                ],
            ]
        }
    return values


def _restore_overlay_entry_ids(value: object) -> list[str] | None:
    raw_values: object = value
    if isinstance(value, dict):
        deltas = value.get("d")
        if (
            set(value) != {"d"}
            or not isinstance(deltas, list)
            or not deltas
            or len(deltas) > OVERLAY_CONTINUATION_MAX_ITEMS
            or not all(
                isinstance(delta, int) and not isinstance(delta, bool)
                for delta in deltas
            )
        ):
            return None
        numeric_values = [deltas[0]]
        for delta in deltas[1:]:
            numeric_values.append(numeric_values[-1] + delta)
        if not all(0 <= numeric_id <= 0xFFFFFFFFFFFFFFFF for numeric_id in numeric_values):
            return None
        raw_values = numeric_values
    if (
        not isinstance(raw_values, list)
        or len(raw_values) > OVERLAY_CONTINUATION_MAX_ITEMS
    ):
        return None
    restored = [_restore_overlay_entry_id(entry_id) for entry_id in raw_values]
    if any(entry_id is None for entry_id in restored):
        return None
    return [entry_id for entry_id in restored if entry_id is not None]


def _encode_overlay_continuation(state: _OverlayContinuation) -> str | None:
    carried_entry_ids = tuple(dict.fromkeys(state.carried_entry_ids))
    shown_entry_ids = tuple(dict.fromkeys(state.shown_entry_ids))
    if state.upstream is None and not carried_entry_ids:
        return None
    if not carried_entry_ids and not shown_entry_ids:
        return state.upstream
    if (
        len(carried_entry_ids) > OVERLAY_CONTINUATION_MAX_ITEMS
        or len(shown_entry_ids) > OVERLAY_CONTINUATION_MAX_ITEMS
    ):
        raise FreshRSSError("Local stream continuation is too large.")
    payload = {
        "k": state.scope_kind,
        "v": state.scope_value,
        "c": state.upstream,
        "p": _compact_overlay_entry_ids(carried_entry_ids),
        "s": _compact_overlay_entry_ids(shown_entry_ids),
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode()
    if len(raw) > OVERLAY_CONTINUATION_MAX_BYTES:
        raise FreshRSSError("Local stream continuation is too large.")
    packed = zlib.compress(raw, level=9)
    encoded = base64.urlsafe_b64encode(packed).decode("ascii").rstrip("=")
    return f"{OVERLAY_CONTINUATION_PREFIX}{encoded}"


def _decode_overlay_continuation(value: str | None) -> _OverlayContinuation | None:
    if not value or not value.startswith(OVERLAY_CONTINUATION_PREFIX):
        return None
    encoded = value.removeprefix(OVERLAY_CONTINUATION_PREFIX)
    if not encoded or len(encoded) > OVERLAY_CONTINUATION_MAX_BYTES * 2:
        return None
    padding = "=" * ((4 - len(encoded) % 4) % 4)
    try:
        packed = base64.b64decode(
            encoded + padding,
            altchars=b"-_",
            validate=True,
        )
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(packed, OVERLAY_CONTINUATION_MAX_BYTES + 1)
        if (
            len(raw) > OVERLAY_CONTINUATION_MAX_BYTES
            or decompressor.unconsumed_tail
            or not decompressor.eof
        ):
            return None
        payload = json.loads(raw.decode("ascii"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, zlib.error):
        return None
    if not isinstance(payload, dict):
        return None
    scope_kind = payload.get("k")
    scope_value = payload.get("v")
    upstream = payload.get("c")
    carried = payload.get("p")
    shown = payload.get("s")
    if not isinstance(scope_kind, str) or not scope_kind:
        return None
    if scope_value is not None and not isinstance(scope_value, str):
        return None
    if upstream is not None and not isinstance(upstream, str):
        return None
    restored_carried = _restore_overlay_entry_ids(carried)
    restored_shown = _restore_overlay_entry_ids(shown)
    if restored_carried is None or restored_shown is None:
        return None
    return _OverlayContinuation(
        scope_kind=scope_kind,
        scope_value=scope_value,
        upstream=upstream,
        carried_entry_ids=tuple(
            dict.fromkeys(
                entry_id for entry_id in restored_carried
            )
        ),
        shown_entry_ids=tuple(
            dict.fromkeys(
                entry_id for entry_id in restored_shown
            )
        ),
    )


def compact_overlay_continuation_for_history(
    value: str,
) -> str | list[object]:
    state = _decode_overlay_continuation(value)
    if state is None:
        return value
    return [
        "rk1",
        state.scope_kind,
        state.scope_value,
        state.upstream,
        list(state.carried_entry_ids),
        list(state.shown_entry_ids),
    ]


def restore_overlay_continuation_from_history(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if not isinstance(value, list) or len(value) != 6 or value[0] != "rk1":
        return None
    _, scope_kind, scope_value, upstream, carried, shown = value
    if not isinstance(scope_kind, str) or not scope_kind:
        return None
    if scope_value is not None and not isinstance(scope_value, str):
        return None
    if upstream is not None and not isinstance(upstream, str):
        return None
    if not isinstance(carried, list) or not isinstance(shown, list):
        return None
    if (
        len(carried) > OVERLAY_CONTINUATION_MAX_ITEMS
        or len(shown) > OVERLAY_CONTINUATION_MAX_ITEMS
    ):
        return None
    restored_carried = [_restore_overlay_entry_id(entry_id) for entry_id in carried]
    restored_shown = [_restore_overlay_entry_id(entry_id) for entry_id in shown]
    if any(entry_id is None for entry_id in [*restored_carried, *restored_shown]):
        return None
    try:
        return _encode_overlay_continuation(
            _OverlayContinuation(
                scope_kind=scope_kind,
                scope_value=scope_value,
                upstream=upstream,
                carried_entry_ids=tuple(
                    entry_id
                    for entry_id in restored_carried
                    if entry_id is not None
                ),
                shown_entry_ids=tuple(
                    entry_id
                    for entry_id in restored_shown
                    if entry_id is not None
                ),
            )
        )
    except FreshRSSError:
        return None


def _entry_id_from_continuation(continuation: str) -> str | None:
    try:
        numeric_id = int(continuation)
    except (TypeError, ValueError):
        return None
    if numeric_id < 0:
        return None
    return f"tag:google.com,2005:reader/item/{numeric_id:016x}"


def _continuation_from_entry_id(entry_id: str) -> str | None:
    prefix = "tag:google.com,2005:reader/item/"
    if not entry_id.startswith(prefix):
        return None
    try:
        return str(int(entry_id.removeprefix(prefix), 16))
    except ValueError:
        return None


def _entry_can_follow_continuation(
    entry_id: str,
    continuation: str | None,
) -> bool:
    if continuation is None:
        return False
    entry_cursor = _continuation_from_entry_id(entry_id)
    if entry_cursor is None or not continuation.isdigit():
        return True
    return int(entry_cursor) < int(continuation)


def _entry_cache_size(entry: FreshRSSEntry) -> int:
    text_values = (
        entry.id,
        entry.title,
        entry.author,
        entry.url,
        entry.published_at,
        entry.summary_html,
        entry.summary_text,
        entry.content_html,
        entry.feed_title,
        entry.feed_site_url,
        entry.feed_token,
        entry.received_at,
        *entry.group_names,
    )
    return sum(len(value) for value in text_values if value is not None)


def _stream_cache_entry(entry: FreshRSSEntry) -> FreshRSSEntry:
    return replace(
        entry,
        summary_html=(entry.summary_html or "")[:STREAM_CACHE_HTML_CHARS] or None,
        summary_text=entry.summary_text[:STREAM_CACHE_TEXT_CHARS],
        content_html=(entry.content_html or "")[:STREAM_CACHE_HTML_CHARS] or None,
    )
