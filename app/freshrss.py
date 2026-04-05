from __future__ import annotations

import base64
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Iterable
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx

from app.config import Settings
from app.utils import slugify, strip_html

READ_STATE = "user/-/state/com.google/read"
STARRED_STATE = "user/-/state/com.google/starred"
READING_LIST_STREAM = "reading-list"
GROUP_CONTINUATION_PREFIX = "group-offset:"
SORTED_CONTINUATION_PREFIX = "sorted-offset:"


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


@dataclass(frozen=True)
class FreshRSSStreamPage:
    entries: list[FreshRSSEntry]
    continuation: str | None


@dataclass(frozen=True)
class FreshRSSNavigation:
    groups: list[FreshRSSGroup]
    feeds: list[FreshRSSFeed]


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
        if not settings.freshrss_api_url or not settings.freshrss_username or not settings.freshrss_api_password:
            raise FreshRSSError(
                "FreshRSS frontend mode requires FRESHRSS_API_URL, FRESHRSS_USERNAME, and FRESHRSS_API_PASSWORD."
            )

        self.settings = settings
        self.api_url = normalize_freshrss_api_url(settings.freshrss_api_url)
        self.username = settings.freshrss_username
        self.api_password = settings.freshrss_api_password
        self.client_factory = client_factory or self._default_client_factory
        self._lock = threading.Lock()
        self._auth_token: str | None = None
        self._write_token: str | None = None
        self._navigation_cache: tuple[float, FreshRSSNavigation] | None = None

    def _default_client_factory(self) -> httpx.Client:
        return httpx.Client(
            follow_redirects=True,
            timeout=self.settings.http_timeout_seconds,
            headers={"User-Agent": self.settings.user_agent},
        )

    def list_navigation(self, force_refresh: bool = False) -> FreshRSSNavigation:
        with self._lock:
            cached = self._navigation_cache
            if not force_refresh and cached and cached[0] > time.monotonic():
                return cached[1]

        payload = self._request_json(
            "GET",
            "/reader/api/0/subscription/list",
            params={"output": "json"},
        )
        navigation = parse_navigation(payload)
        with self._lock:
            self._navigation_cache = (time.monotonic() + self.settings.metadata_cache_seconds, navigation)
        return navigation

    def get_stream(
        self,
        *,
        scope_kind: str,
        scope_value: str | None = None,
        continuation: str | None = None,
        limit: int = 15,
    ) -> FreshRSSStreamPage:
        navigation = self.list_navigation()
        if scope_kind == "group":
            return self._get_group_stream(
                scope_value=scope_value,
                continuation=continuation,
                limit=limit,
                navigation=navigation,
            )
        return self._get_sorted_stream(
            scope_kind=scope_kind,
            scope_value=scope_value,
            continuation=continuation,
            limit=limit,
            navigation=navigation,
        )

    def get_entry(self, entry_id: str) -> FreshRSSEntry | None:
        navigation = self.list_navigation()
        params = {"output": "json", "i": entry_id}
        for method, include_token in (("GET", False), ("POST", False), ("POST", True)):
            try:
                payload = self._request_json(
                    method,
                    "/reader/api/0/stream/items/contents",
                    params=params if method == "GET" else None,
                    data=params if method == "POST" else None,
                    require_write_token=include_token,
                )
            except httpx.HTTPError:
                continue
            items = payload.get("items") or []
            if not items:
                return None
            return self._parse_entry(items[0], navigation)
        return None

    def mark_read(self, entry_ids: Iterable[str]) -> None:
        self._edit_tags(entry_ids, add=READ_STATE)

    def mark_unread(self, entry_ids: Iterable[str]) -> None:
        self._edit_tags(entry_ids, remove=READ_STATE)

    def mark_starred(self, entry_ids: Iterable[str]) -> None:
        self._edit_tags(entry_ids, add=STARRED_STATE)

    def mark_unstarred(self, entry_ids: Iterable[str]) -> None:
        self._edit_tags(entry_ids, remove=STARRED_STATE)

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

    def _get_group_stream(
        self,
        *,
        scope_value: str | None,
        continuation: str | None,
        limit: int,
        navigation: FreshRSSNavigation,
    ) -> FreshRSSStreamPage:
        if not scope_value:
            raise FreshRSSError("Group scope requires a slug.")

        group_feeds = [feed for feed in navigation.feeds if scope_value in feed.group_slugs]
        if not group_feeds:
            raise FreshRSSError(f"Unknown FreshRSS group: {scope_value}")

        offset = _decode_group_offset(continuation)
        desired_count = offset + limit + 1
        merged_entries: dict[str, FreshRSSEntry] = {}

        for feed in group_feeds:
            for entry in self._collect_feed_entries_for_group(
                feed=feed,
                desired_count=desired_count,
                page_size=limit,
                navigation=navigation,
            ):
                merged_entries[entry.id] = entry

        ordered_entries = sorted(merged_entries.values(), key=_entry_sort_key, reverse=True)
        page_entries = ordered_entries[offset : offset + limit]
        next_offset = offset + limit
        next_continuation = _encode_group_offset(next_offset) if len(ordered_entries) > next_offset else None
        return FreshRSSStreamPage(entries=page_entries, continuation=next_continuation)

    def _get_sorted_stream(
        self,
        *,
        scope_kind: str,
        scope_value: str | None,
        continuation: str | None,
        limit: int,
        navigation: FreshRSSNavigation,
    ) -> FreshRSSStreamPage:
        stream_id = self._stream_id_for_scope(scope_kind, scope_value, navigation)
        offset = _decode_sorted_offset(continuation)
        desired_count = offset + limit + 1
        scan_limit = _sorted_scan_limit(desired_count)
        entries = self._collect_entries_for_sorted_stream(
            stream_id=stream_id,
            scan_limit=scan_limit,
            page_size=max(limit, 50),
            navigation=navigation,
        )
        ordered_entries = sorted(entries, key=_entry_sort_key, reverse=True)
        page_entries = ordered_entries[offset : offset + limit]
        next_offset = offset + limit
        next_continuation = _encode_sorted_offset(next_offset) if len(ordered_entries) > next_offset else None
        return FreshRSSStreamPage(entries=page_entries, continuation=next_continuation)

    def _collect_feed_entries_for_group(
        self,
        *,
        feed: FreshRSSFeed,
        desired_count: int,
        page_size: int,
        navigation: FreshRSSNavigation,
    ) -> list[FreshRSSEntry]:
        scan_limit = _sorted_scan_limit(desired_count)
        entries = self._collect_entries_for_sorted_stream(
            stream_id=feed.stream_id,
            scan_limit=scan_limit,
            page_size=max(page_size, 50),
            navigation=navigation,
        )
        ordered_entries = sorted(entries, key=_entry_sort_key, reverse=True)
        return ordered_entries[:desired_count]

    def _collect_entries_for_sorted_stream(
        self,
        *,
        stream_id: str,
        scan_limit: int,
        page_size: int,
        navigation: FreshRSSNavigation,
    ) -> list[FreshRSSEntry]:
        entries: list[FreshRSSEntry] = []
        seen_ids: set[str] = set()
        continuation: str | None = None

        while len(entries) < scan_limit:
            page = self._fetch_stream_page(
                stream_id=stream_id,
                navigation=navigation,
                continuation=continuation,
                limit=page_size,
            )
            if not page.entries:
                break
            for entry in page.entries:
                if entry.id in seen_ids:
                    continue
                seen_ids.add(entry.id)
                entries.append(entry)
                if len(entries) >= scan_limit:
                    break
            if not page.continuation:
                break
            continuation = page.continuation

        return entries[:scan_limit]

    def _stream_id_for_scope(
        self,
        scope_kind: str,
        scope_value: str | None,
        navigation: FreshRSSNavigation,
    ) -> str:
        if scope_kind == "home":
            return READING_LIST_STREAM
        if scope_kind == "feed":
            if not scope_value:
                raise FreshRSSError("Feed scope requires a token.")
            for feed in navigation.feeds:
                if feed.token == scope_value:
                    return feed.stream_id
            raise FreshRSSError(f"Unknown FreshRSS feed: {scope_value}")
        raise FreshRSSError(f"Unsupported FreshRSS scope: {scope_kind}")

    def _edit_tags(self, entry_ids: Iterable[str], *, add: str | None = None, remove: str | None = None) -> None:
        ids = [str(entry_id).strip() for entry_id in entry_ids if str(entry_id).strip()]
        if not ids:
            return

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

        with self.client_factory() as client:
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

        with self.client_factory() as client:
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
            raise FreshRSSError("FreshRSS login succeeded but did not return an auth token.")
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
    ) -> FreshRSSStreamPage:
        params: dict[str, str] = {
            "output": "json",
            "n": str(limit),
            "xt": READ_STATE,
        }
        if continuation:
            params["c"] = continuation
        payload = self._request_json(
            "GET",
            f"/reader/api/0/stream/contents/{quote(stream_id, safe='')}",
            params=params,
        )
        return self._parse_stream_page(payload, navigation)

    def _parse_stream_page(self, payload: dict[str, Any], navigation: FreshRSSNavigation) -> FreshRSSStreamPage:
        entries: list[FreshRSSEntry] = []
        for raw_item in payload.get("items") or []:
            entry = self._parse_entry(raw_item, navigation)
            if entry is not None:
                entries.append(entry)
        continuation = payload.get("continuation")
        return FreshRSSStreamPage(entries=entries, continuation=str(continuation) if continuation else None)

    def _parse_entry(self, raw_item: dict[str, Any], navigation: FreshRSSNavigation) -> FreshRSSEntry | None:
        entry_id = str(raw_item.get("id") or "").strip()
        if not entry_id:
            return None

        origin = raw_item.get("origin") or {}
        raw_feed_id = str(origin.get("streamId") or "").strip()
        normalized_feed_id = _normalize_feed_stream_id(raw_feed_id)
        feed = next((candidate for candidate in navigation.feeds if candidate.stream_id == normalized_feed_id), None)
        categories = tuple(
            _label_name(raw_category_id)
            for raw_category_id in raw_item.get("categories") or []
            if _label_name(raw_category_id)
        )
        raw_category_ids = tuple(str(raw_category_id or "").strip() for raw_category_id in raw_item.get("categories") or [])
        summary_html = _content_field(raw_item, "summary")
        content_html = _content_field(raw_item, "content") or summary_html
        published_at = _timestamp_to_iso(
            raw_item.get("published") or raw_item.get("updated") or raw_item.get("crawlTimeMsec")
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
            feed_title=feed.title if feed else str(origin.get("title") or "").strip() or None,
            feed_site_url=feed.site_url if feed else str(origin.get("htmlUrl") or "").strip() or None,
            feed_token=feed.token if feed else (encode_feed_token(normalized_feed_id) if normalized_feed_id else None),
            group_names=categories,
            is_starred=STARRED_STATE in raw_category_ids,
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
        FreshRSSGroup(name=label, slug=slug_by_stream_id[stream_id], stream_id=stream_id)
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
        if isinstance(value, str) and value.isdigit():
            numeric = int(value)
        elif isinstance(value, (int, float)):
            numeric = int(value)
        else:
            return None
        if numeric > 10_000_000_000:
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


def _entry_sort_key(entry: FreshRSSEntry) -> tuple[str, str]:
    return (entry.published_at or "", entry.id)


def _encode_group_offset(offset: int) -> str:
    return f"{GROUP_CONTINUATION_PREFIX}{max(offset, 0)}"


def _decode_group_offset(value: str | None) -> int:
    if not value:
        return 0
    if not value.startswith(GROUP_CONTINUATION_PREFIX):
        raise FreshRSSError(f"Invalid group continuation token: {value}")
    try:
        return max(int(value.removeprefix(GROUP_CONTINUATION_PREFIX)), 0)
    except ValueError as exc:
        raise FreshRSSError(f"Invalid group continuation token: {value}") from exc


def _encode_sorted_offset(offset: int) -> str:
    return f"{SORTED_CONTINUATION_PREFIX}{max(offset, 0)}"


def _decode_sorted_offset(value: str | None) -> int:
    if not value:
        return 0
    if not value.startswith(SORTED_CONTINUATION_PREFIX):
        raise FreshRSSError(f"Invalid sorted continuation token: {value}")
    try:
        return max(int(value.removeprefix(SORTED_CONTINUATION_PREFIX)), 0)
    except ValueError as exc:
        raise FreshRSSError(f"Invalid sorted continuation token: {value}") from exc


def _sorted_scan_limit(desired_count: int) -> int:
    return min(max(desired_count * 10, 150), 1000)
