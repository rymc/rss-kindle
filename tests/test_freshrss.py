import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest
from fastapi import FastAPI

from app.config import MAX_STREAM_ITEMS_LIMIT, Settings
from app.freshrss import (
    ENTRY_CACHE_LIMIT,
    ENTRY_CACHE_BYTES_LIMIT,
    OVERLAY_CONTINUATION_MAX_ITEMS,
    READ_STATE,
    READING_LIST_STREAM,
    STARRED_STATE,
    STREAM_CACHE_HTML_CHARS,
    STREAM_PAGE_CACHE_LIMIT,
    FreshRSSClient,
    FreshRSSError,
    FreshRSSFeed,
    FreshRSSGroup,
    FreshRSSNavigation,
    FreshRSSStreamPage,
    decode_feed_token,
    parse_navigation,
    restore_overlay_continuation_from_history,
)
from app.reader_navigation import (
    CURSOR_STACK_MAX_BYTES,
    CURSOR_STACK_MAX_EXPANDED_IDS,
    CURSOR_STACK_MAX_ITEMS,
    CURSOR_STACK_MAX_RESTORED_ID_REFERENCES,
    ReadingContext,
    StreamRequest,
    StreamScope,
    _decode_compact_cursor_stack,
    _decode_cursor_stack,
    _encode_token,
)

TEST_BASE_DIR = Path(__file__).resolve().parent.parent


def test_parse_navigation_normalizes_numeric_feed_ids():
    navigation = parse_navigation(
        {
            "subscriptions": [
                {
                    "id": "8",
                    "title": "MacRumors: Mac News and Rumors - Front Page",
                    "url": "http://feeds.macrumors.com/MacRumors-Front",
                    "htmlUrl": "https://www.macrumors.com",
                    "categories": [{"id": "user/-/label/Tech", "label": "Tech"}],
                }
            ]
        }
    )

    assert len(navigation.feeds) == 1
    feed = navigation.feeds[0]
    assert feed.stream_id == "feed/8"
    assert decode_feed_token(feed.token) == "feed/8"
    assert feed.group_slugs == ("tech",)
    assert navigation.groups[0].slug == "tech"


class FakeResponse:
    def __init__(self):
        self.status_code = 200

    def raise_for_status(self):
        return None


class CapturingClient:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def request(self, method, url, params=None, data=None, headers=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "data": data,
                "headers": headers or {},
            }
        )
        return FakeResponse()


class FeverResponse(FakeResponse):
    def json(self):
        return {
            "auth": 1,
            "last_refreshed_on_time": 1_700_000_000,
        }


class FeverClient(CapturingClient):
    def request(self, method, url, params=None, data=None, headers=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "data": data,
                "headers": headers or {},
            }
        )
        return FeverResponse()


def test_fever_status_reports_the_last_freshrss_refresh():
    capturing = FeverClient()
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=15,
        metadata_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    client = FreshRSSClient(settings, client_factory=lambda: capturing)

    refreshed_at = client.get_last_refreshed_at()

    assert refreshed_at == "2023-11-14T22:13:20+00:00"
    assert capturing.calls[0]["method"] == "POST"
    assert capturing.calls[0]["url"] == "https://rss.example.net/api/fever.php?api"
    assert len(capturing.calls[0]["data"]["api_key"]) == 32


def test_mark_read_urlencodes_repeated_ids_for_httpx():
    capturing = CapturingClient()
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=15,
        metadata_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    client = FreshRSSClient(settings, client_factory=lambda: capturing)
    client._auth_token = "auth-token"
    client._write_token = "write-token"

    client.mark_read(["item-1", "item-2"])

    assert len(capturing.calls) == 1
    call = capturing.calls[0]
    assert call["method"] == "POST"
    assert call["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert (
        call["data"]
        == urlencode(
            [
                ("ac", "edit-tags"),
                ("a", READ_STATE),
                ("i", "item-1"),
                ("i", "item-2"),
                ("T", "write-token"),
            ]
        ).encode()
    )


def test_mark_starred_urlencodes_repeated_ids_for_httpx():
    capturing = CapturingClient()
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=15,
        metadata_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    client = FreshRSSClient(settings, client_factory=lambda: capturing)
    client._auth_token = "auth-token"
    client._write_token = "write-token"

    client.mark_starred(["item-1", "item-2"])

    assert len(capturing.calls) == 1
    call = capturing.calls[0]
    assert call["method"] == "POST"
    assert call["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert (
        call["data"]
        == urlencode(
            [
                ("ac", "edit-tags"),
                ("a", STARRED_STATE),
                ("i", "item-1"),
                ("i", "item-2"),
                ("T", "write-token"),
            ]
        ).encode()
    )


class StubFreshRSSClient(FreshRSSClient):
    def __init__(
        self,
        settings: Settings,
        navigation: FreshRSSNavigation,
        payloads: dict[tuple[str, str | None], dict],
    ):
        transport = CapturingClient()
        super().__init__(settings, client_factory=lambda: transport)
        self.transport = transport
        self._navigation = navigation
        self.payloads = payloads
        self.calls: list[tuple[str, str | None]] = []
        self.request_params: list[dict[str, str]] = []
        self.navigation_calls = 0

    def list_navigation(self, force_refresh: bool = False) -> FreshRSSNavigation:
        self.navigation_calls += 1
        return self._navigation

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params=None,
        data=None,
        require_write_token: bool = False,
    ):
        stream_id = path.rsplit("/", 1)[1]
        stream_id = __import__("urllib.parse").parse.unquote(stream_id)
        continuation = None if params is None else params.get("c")
        self.calls.append((stream_id, continuation))
        self.request_params.append(dict(params or {}))
        return self.payloads[(stream_id, continuation)]


def _item(
    item_id: str, title: str, published: int, feed_stream: str, feed_title: str
) -> dict:
    return {
        "id": item_id,
        "title": title,
        "published": published,
        "origin": {
            "streamId": feed_stream,
            "title": feed_title,
            "htmlUrl": f"https://example.com/{feed_title}",
        },
        "alternate": [{"href": f"https://example.com/{item_id}"}],
        "summary": {"content": f"<p>{title}</p>"},
    }


def _numeric_item(
    numeric_id: int,
    *,
    starred: bool = False,
    read: bool = False,
) -> dict:
    item = _item(
        f"tag:google.com,2005:reader/item/{numeric_id:016x}",
        f"Entry {numeric_id}",
        numeric_id,
        "feed/1",
        "Feed One",
    )
    item["categories"] = [
        state
        for state, enabled in ((STARRED_STATE, starred), (READ_STATE, read))
        if enabled
    ]
    return item


def test_latest_entry_includes_read_items_and_reports_received_time():
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=15,
        metadata_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    raw_entry = _item("item-1", "Latest story", 1_699_999_000, "feed/8", "Example")
    raw_entry["crawlTimeMsec"] = 1_700_000_000_000
    client = StubFreshRSSClient(
        settings,
        FreshRSSNavigation(groups=[], feeds=[]),
        {(READING_LIST_STREAM, None): {"items": [raw_entry]}},
    )

    entry = client.get_latest_entry()

    assert entry is not None
    assert entry.title == "Latest story"
    assert entry.received_at == "2023-11-14T22:13:20+00:00"
    assert "xt" not in client.request_params[0]


def test_stream_can_include_read_items_and_reports_their_state():
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=15,
        metadata_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    raw_entry = _item("item-1", "Read story", 100, "feed/8", "Example")
    raw_entry["categories"] = [READ_STATE]
    client = StubFreshRSSClient(
        settings,
        FreshRSSNavigation(groups=[], feeds=[]),
        {(READING_LIST_STREAM, None): {"items": [raw_entry]}},
    )

    page = client.get_stream(scope_kind="home", include_read=True)

    assert [entry.title for entry in page.entries] == ["Read story"]
    assert page.entries[0].is_read is True
    assert "xt" not in client.request_params[0]


def test_group_stream_uses_one_label_request_and_native_continuation():
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=2,
        metadata_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    navigation = FreshRSSNavigation(
        groups=[
            FreshRSSGroup(name="Blogs", slug="blogs", stream_id="user/-/label/Blogs")
        ],
        feeds=[
            FreshRSSFeed(
                token="feed-1",
                stream_id="feed/1",
                title="Feed One",
                feed_url="https://example.com/feed-1.xml",
                site_url="https://example.com/feed-1",
                group_slugs=("blogs",),
            ),
            FreshRSSFeed(
                token="feed-2",
                stream_id="feed/2",
                title="Feed Two",
                feed_url="https://example.com/feed-2.xml",
                site_url="https://example.com/feed-2",
                group_slugs=("blogs",),
            ),
        ],
    )
    payloads = {
        ("user/-/label/Blogs", None): {
            "items": [
                _item("entry-2a", "First received", 100, "feed/2", "Feed Two"),
                _item("entry-1a", "Second received", 300, "feed/1", "Feed One"),
            ],
            "continuation": "opaque-group-cursor",
        },
        ("user/-/label/Blogs", "opaque-group-cursor"): {
            "items": [
                _item("entry-2b", "Third received", 200, "feed/2", "Feed Two"),
                _item("entry-1b", "Fourth received", 400, "feed/1", "Feed One"),
            ],
            "continuation": None,
        },
    }
    client = StubFreshRSSClient(settings, navigation, payloads)

    first_page = client.get_stream(scope_kind="group", scope_value="blogs", limit=2)
    second_page = client.get_stream(
        scope_kind="group",
        scope_value="blogs",
        continuation=first_page.continuation,
        limit=2,
    )

    assert [entry.title for entry in first_page.entries] == [
        "First received",
        "Second received",
    ]
    assert first_page.continuation == "opaque-group-cursor"
    assert [entry.title for entry in second_page.entries] == [
        "Third received",
        "Fourth received",
    ]
    assert client.calls == [
        ("user/-/label/Blogs", None),
        ("user/-/label/Blogs", "opaque-group-cursor"),
    ]


def test_home_stream_preserves_native_order_and_continuation():
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=2,
        metadata_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    navigation = FreshRSSNavigation(
        groups=[],
        feeds=[
            FreshRSSFeed(
                token="feed-1",
                stream_id="feed/1",
                title="Feed One",
                feed_url="https://example.com/feed-1.xml",
                site_url="https://example.com/feed-1",
                group_slugs=(),
            )
        ],
    )
    payloads = {
        ("reading-list", None): {
            "items": [
                _item(
                    "entry-1", "Old but recently inserted", 100, "feed/1", "Feed One"
                ),
                _item("entry-2", "Still old", 90, "feed/1", "Feed One"),
            ],
            "continuation": "page-2",
        },
        ("reading-list", "page-2"): {
            "items": [
                _item("entry-3", "Actually newest", 300, "feed/1", "Feed One"),
                _item("entry-4", "Second newest", 250, "feed/1", "Feed One"),
            ],
            "continuation": None,
        },
    }
    client = StubFreshRSSClient(settings, navigation, payloads)

    page = client.get_stream(scope_kind="home", limit=2)
    next_page = client.get_stream(
        scope_kind="home",
        continuation=page.continuation,
        limit=2,
    )

    assert [entry.title for entry in page.entries] == [
        "Old but recently inserted",
        "Still old",
    ]
    assert page.continuation == "page-2"
    assert [entry.title for entry in next_page.entries] == [
        "Actually newest",
        "Second newest",
    ]
    assert client.calls == [("reading-list", None), ("reading-list", "page-2")]
    assert client.navigation_calls == 0


def test_marking_a_page_read_does_not_shift_the_next_page():
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=15,
        metadata_cache_seconds=60,
        stream_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    navigation = FreshRSSNavigation(groups=[], feeds=[])
    batches = [
        [
            _item(
                f"entry-{index:02d}",
                f"Entry {index:02d}",
                2_000 - index,
                "feed/1",
                "Feed One",
            )
            for index in range(start, start + 15)
        ]
        for start in range(0, 60, 15)
    ]
    payloads = {
        (READING_LIST_STREAM, None): {
            "items": batches[0],
            "continuation": "cursor-15",
        },
        (READING_LIST_STREAM, "cursor-15"): {
            "items": batches[1],
            "continuation": "cursor-30",
        },
        (READING_LIST_STREAM, "cursor-30"): {
            "items": batches[2],
            "continuation": "cursor-45",
        },
        (READING_LIST_STREAM, "cursor-45"): {
            "items": batches[3],
            "continuation": None,
        },
    }
    client = StubFreshRSSClient(settings, navigation, payloads)

    first_page = client.get_stream(scope_kind="home", limit=15)
    client.apply_local_state(
        "read",
        [entry.id for entry in first_page.entries[:-1]],
        True,
    )
    second_page = client.get_stream(
        scope_kind="home",
        continuation=first_page.continuation,
        limit=15,
    )

    assert [entry.id for entry in first_page.entries] == [
        f"entry-{index:02d}" for index in range(15)
    ]
    assert first_page.continuation == "cursor-15"
    assert [entry.id for entry in second_page.entries] == [
        f"entry-{index:02d}" for index in range(15, 30)
    ]
    assert client.calls == [
        (READING_LIST_STREAM, None),
        (READING_LIST_STREAM, "cursor-15"),
    ]
    assert "xt" not in client.request_params[1]
    assert client.request_params[1]["n"] == "15"


def test_read_cursor_anchor_can_leave_the_unread_view_without_skipping():
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=3,
        metadata_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    payloads = {
        (READING_LIST_STREAM, None): {
            "items": [_numeric_item(value) for value in (38, 37, 36)],
            "continuation": "36",
        },
        (READING_LIST_STREAM, "36"): {
            "items": [
                _numeric_item(35),
                _numeric_item(34, read=True),
                _numeric_item(33),
            ],
            "continuation": "33",
        },
        (READING_LIST_STREAM, "33"): {
            "items": [_numeric_item(32)],
            "continuation": "32",
        },
    }
    client = StubFreshRSSClient(
        settings,
        FreshRSSNavigation(groups=[], feeds=[]),
        payloads,
    )

    first = client.get_stream(scope_kind="home", limit=3)
    client.apply_local_state("read", [first.entries[-1].id], True)
    second = client.get_stream(
        scope_kind="home",
        continuation=first.continuation,
        limit=3,
    )

    assert [entry.title for entry in second.entries] == [
        "Entry 35",
        "Entry 33",
        "Entry 32",
    ]
    assert client.calls[1:] == [
        (READING_LIST_STREAM, "36"),
        (READING_LIST_STREAM, "33"),
    ]
    assert all("xt" not in params for params in client.request_params[1:])


def test_starred_cursor_anchor_can_be_unstarred_without_skipping():
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=3,
        metadata_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    payloads = {
        (STARRED_STATE, None): {
            "items": [_numeric_item(value, starred=True) for value in (38, 37, 36)],
            "continuation": "36",
        },
        (READING_LIST_STREAM, "36"): {
            "items": [
                _numeric_item(35, starred=True),
                _numeric_item(34),
                _numeric_item(33, starred=True),
            ],
            "continuation": "33",
        },
        (READING_LIST_STREAM, "33"): {
            "items": [_numeric_item(32, starred=True)],
            "continuation": "32",
        },
    }
    client = StubFreshRSSClient(
        settings,
        FreshRSSNavigation(groups=[], feeds=[]),
        payloads,
    )

    first = client.get_stream(scope_kind="starred", limit=3)
    client.apply_local_state("starred", [first.entries[-1].id], False)
    second = client.get_stream(
        scope_kind="starred",
        continuation=first.continuation,
        limit=3,
    )

    assert [entry.title for entry in second.entries] == [
        "Entry 35",
        "Entry 33",
        "Entry 32",
    ]
    assert client.calls[1:] == [
        (READING_LIST_STREAM, "36"),
        (READING_LIST_STREAM, "33"),
    ]


def test_opaque_cursor_uses_its_remembered_anchor_after_unstar():
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=2,
        metadata_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    anchor = _item("opaque-anchor", "Anchor", 3, "feed/1", "Feed One")
    anchor["categories"] = [STARRED_STATE]
    next_item = _item("opaque-next", "Next", 2, "feed/1", "Feed One")
    next_item["categories"] = [STARRED_STATE]
    client = StubFreshRSSClient(
        settings,
        FreshRSSNavigation(groups=[], feeds=[]),
        {
            (STARRED_STATE, None): {
                "items": [anchor],
                "continuation": "opaque-cursor",
            },
            (READING_LIST_STREAM, "opaque-cursor"): {
                "items": [next_item],
                "continuation": None,
            },
        },
    )

    first = client.get_stream(scope_kind="starred", limit=2)
    client.apply_local_state("starred", [first.entries[-1].id], False)
    second = client.get_stream(
        scope_kind="starred",
        continuation=first.continuation,
        limit=2,
    )

    assert [entry.id for entry in second.entries] == ["opaque-next"]
    assert client.calls[-1] == (READING_LIST_STREAM, "opaque-cursor")


def test_opaque_cursor_anchor_is_isolated_between_streams(monkeypatch):
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=2,
        metadata_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    starred_anchor = _item(
        "starred-anchor",
        "Starred anchor",
        3,
        "feed/1",
        "Feed One",
    )
    starred_anchor["categories"] = [STARRED_STATE]
    home_anchor = _item("home-anchor", "Home anchor", 2, "feed/1", "Feed One")
    next_starred = _item(
        "next-starred",
        "Next starred",
        1,
        "feed/1",
        "Feed One",
    )
    next_starred["categories"] = [STARRED_STATE]
    client = StubFreshRSSClient(
        settings,
        FreshRSSNavigation(groups=[], feeds=[]),
        {
            (STARRED_STATE, None): {
                "items": [starred_anchor],
                "continuation": "shared-cursor",
            },
            (READING_LIST_STREAM, None): {
                "items": [home_anchor],
                "continuation": "shared-cursor",
            },
            (READING_LIST_STREAM, "shared-cursor"): {
                "items": [next_starred],
                "continuation": None,
            },
            (STARRED_STATE, "shared-cursor"): {
                "items": [],
                "continuation": None,
            },
        },
    )

    first_starred = client.get_stream(scope_kind="starred", limit=2)
    home = client.get_stream(scope_kind="home", limit=2)
    monkeypatch.setattr(client, "_fetch_entry", lambda entry_id: home.entries[-1])
    client.apply_local_state("starred", [first_starred.entries[-1].id], False)
    second_starred = client.get_stream(
        scope_kind="starred",
        continuation=first_starred.continuation,
        limit=2,
    )

    assert [entry.id for entry in second_starred.entries] == ["next-starred"]
    assert client.calls[-1] == (READING_LIST_STREAM, "shared-cursor")


def test_starred_cursor_rechecks_anchor_state_changed_by_another_client(
    monkeypatch,
):
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=2,
        metadata_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    client = StubFreshRSSClient(
        settings,
        FreshRSSNavigation(groups=[], feeds=[]),
        {
            (STARRED_STATE, None): {
                "items": [_numeric_item(36, starred=True)],
                "continuation": "36",
            },
            (READING_LIST_STREAM, "36"): {
                "items": [_numeric_item(35, starred=True)],
                "continuation": None,
            },
        },
    )
    first = client.get_stream(scope_kind="starred", limit=2)
    state_checks = []

    def refresh_anchor(entry_id):
        state_checks.append(entry_id)
        return replace(first.entries[-1], is_starred=False)

    monkeypatch.setattr(client, "_fetch_entry", refresh_anchor)
    second = client.get_stream(
        scope_kind="starred",
        continuation=first.continuation,
        limit=2,
    )

    assert state_checks == [first.entries[-1].id]
    assert [entry.title for entry in second.entries] == ["Entry 35"]
    assert client.calls[-1] == (READING_LIST_STREAM, "36")


def test_sparse_state_recovery_has_a_request_bound():
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=15,
        metadata_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    payloads = {
        (READING_LIST_STREAM, None): {
            "items": [_numeric_item(10_000)],
            "continuation": "10000",
        }
    }
    cursor = 10_000
    for _ in range(10):
        next_cursor = cursor - 100
        payloads[(READING_LIST_STREAM, str(cursor))] = {
            "items": [
                _numeric_item(value, read=value != cursor - 1)
                for value in range(cursor - 1, next_cursor - 1, -1)
            ],
            "continuation": str(next_cursor),
        }
        cursor = next_cursor
    client = StubFreshRSSClient(
        settings,
        FreshRSSNavigation(groups=[], feeds=[]),
        payloads,
    )

    first = client.get_stream(scope_kind="home", limit=15)
    client.apply_local_state("read", [first.entries[-1].id], True)
    recovered = client.get_stream(
        scope_kind="home",
        continuation=first.continuation,
        limit=15,
    )

    assert len(recovered.entries) == 10
    assert recovered.continuation == "9000"
    assert len(client.calls) == 11
    assert client.request_params[1]["n"] == "15"
    assert all(params["n"] == "100" for params in client.request_params[2:])


def test_pending_star_is_visible_before_freshrss_returns_the_new_membership():
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=3,
        metadata_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    payloads = {
        (READING_LIST_STREAM, None): {
            "items": [_numeric_item(3)],
            "continuation": None,
        },
        (STARRED_STATE, None): {"items": [], "continuation": None},
    }
    client = StubFreshRSSClient(
        settings,
        FreshRSSNavigation(groups=[], feeds=[]),
        payloads,
    )

    entry = client.get_stream(scope_kind="home", limit=3).entries[0]
    client.apply_local_state("starred", [entry.id], True)
    starred = client.get_stream(scope_kind="starred", limit=3)

    assert [item.id for item in starred.entries] == [entry.id]
    assert starred.entries[0].is_starred is True


def test_unrelated_local_state_does_not_change_home_page_size():
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=1,
        metadata_cache_seconds=60,
        stream_cache_seconds=0,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    payloads = {
        (READING_LIST_STREAM, None): {
            "items": [_numeric_item(3)],
            "continuation": None,
        }
    }
    client = StubFreshRSSClient(
        settings,
        FreshRSSNavigation(groups=[], feeds=[]),
        payloads,
    )
    old_entry = client.get_stream(scope_kind="home", limit=1).entries[0]
    client.apply_local_state("starred", [old_entry.id], True)
    payloads[(READING_LIST_STREAM, None)] = {
        "items": [_numeric_item(2)],
        "continuation": None,
    }

    refreshed = client.get_stream(scope_kind="home", limit=1)

    assert [entry.title for entry in refreshed.entries] == ["Entry 2"]


def test_pending_membership_is_restored_after_the_entry_cache_is_lost(monkeypatch):
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=15,
        metadata_cache_seconds=60,
        entry_cache_seconds=0,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    client = FreshRSSClient(settings, client_factory=lambda: CapturingClient())
    raw_items = [_numeric_item(value) for value in range(30, 0, -1)]
    entry_ids = [item["id"] for item in raw_items]
    recovery_calls = []
    monkeypatch.setattr(
        client,
        "_fetch_stream_page",
        lambda **kwargs: FreshRSSStreamPage(entries=[], continuation=None),
    )

    def request_json(method, path, **kwargs):
        recovery_calls.append(kwargs["data"])
        return {"items": raw_items}

    monkeypatch.setattr(client, "_request_json", request_json)

    client.apply_local_state("starred", entry_ids, True)
    first = client.get_stream(scope_kind="starred", limit=15)
    assert first.continuation is not None
    second = client.get_stream(
        scope_kind="starred",
        continuation=first.continuation,
        limit=15,
    )

    first_ids = [entry.id for entry in first.entries]
    second_ids = [entry.id for entry in second.entries]
    assert first_ids + second_ids == entry_ids
    assert all(entry.is_starred for entry in [*first.entries, *second.entries])
    assert len(recovery_calls) == 2
    assert all(
        len([value for key, value in call if key == "i"]) == 15
        for call in recovery_calls
    )


def test_deferred_pending_membership_keeps_the_requested_feed_scope(monkeypatch):
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=15,
        metadata_cache_seconds=60,
        entry_cache_seconds=0,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    navigation = FreshRSSNavigation(
        groups=[],
        feeds=[
            FreshRSSFeed(
                token="feed-one",
                stream_id="feed/1",
                title="Feed One",
                feed_url=None,
                site_url=None,
                group_slugs=(),
            ),
            FreshRSSFeed(
                token="feed-two",
                stream_id="feed/2",
                title="Feed Two",
                feed_url=None,
                site_url=None,
                group_slugs=(),
            ),
        ],
    )
    other_feed_items = [
        _item(f"other-{index}", f"Other {index}", index, "feed/1", "Feed One")
        for index in range(16)
    ]
    requested_item = _item(
        "requested-entry",
        "Requested entry",
        100,
        "feed/2",
        "Feed Two",
    )
    raw_items = [*other_feed_items, requested_item]
    for item in raw_items:
        item["categories"] = [READ_STATE]
    raw_by_id = {item["id"]: item for item in raw_items}
    client = FreshRSSClient(settings, client_factory=lambda: CapturingClient())
    recovery_calls = []
    monkeypatch.setattr(client, "list_navigation", lambda: navigation)
    monkeypatch.setattr(client, "_cached_navigation_or_empty", lambda: navigation)
    monkeypatch.setattr(
        client,
        "_fetch_stream_page",
        lambda **kwargs: FreshRSSStreamPage(entries=[], continuation=None),
    )

    def request_json(method, path, **kwargs):
        requested_ids = [value for key, value in kwargs["data"] if key == "i"]
        recovery_calls.append(requested_ids)
        return {
            "items": [
                raw_by_id[entry_id]
                for entry_id in requested_ids
                if entry_id in raw_by_id
            ]
        }

    monkeypatch.setattr(client, "_request_json", request_json)
    client.apply_local_state("read", list(raw_by_id), False)

    first = client.get_stream(
        scope_kind="feed",
        scope_value="feed-two",
        limit=15,
    )

    assert [entry.id for entry in first.entries] == ["requested-entry"]
    assert first.continuation is None
    assert len(recovery_calls) == 2
    assert len(recovery_calls[0]) == 15
    assert recovery_calls[1] == ["other-15", "requested-entry"]


def test_pending_membership_fills_an_empty_page_up_to_its_limit(monkeypatch):
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=3,
        metadata_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    client = FreshRSSClient(settings, client_factory=lambda: CapturingClient())
    navigation = FreshRSSNavigation(groups=[], feeds=[])
    entries = [
        client._parse_entry(_numeric_item(numeric_id), navigation)
        for numeric_id in range(1, 5)
    ]
    assert all(entry is not None for entry in entries)
    resolved_entries = [entry for entry in entries if entry is not None]
    client._cache_entries(resolved_entries)
    client.apply_local_state(
        "starred",
        [entry.id for entry in resolved_entries],
        True,
    )
    monkeypatch.setattr(
        client,
        "_fetch_stream_page",
        lambda **kwargs: FreshRSSStreamPage(entries=[], continuation=None),
    )

    page = client.get_stream(scope_kind="starred", limit=3)

    assert len(page.entries) == 3
    assert all(entry.is_starred for entry in page.entries)


def test_pending_membership_fails_before_it_can_be_silently_omitted(monkeypatch):
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=15,
        metadata_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    client = FreshRSSClient(settings, client_factory=lambda: CapturingClient())
    navigation = FreshRSSNavigation(groups=[], feeds=[])
    parsed = [
        client._parse_entry(_numeric_item(value), navigation)
        for value in range(OVERLAY_CONTINUATION_MAX_ITEMS + 1)
    ]
    entries = [entry for entry in parsed if entry is not None]
    client._cache_entries(entries)
    client.apply_local_state("starred", [entry.id for entry in entries], True)
    monkeypatch.setattr(
        client,
        "_fetch_stream_page",
        lambda **kwargs: FreshRSSStreamPage(entries=[], continuation=None),
    )

    with pytest.raises(FreshRSSError, match="Too many pending changes"):
        client.get_stream(scope_kind="starred", limit=15)


def test_pending_membership_does_not_duplicate_across_adjacent_pages(monkeypatch):
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=3,
        metadata_cache_seconds=60,
        stream_cache_seconds=0,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    client = StubFreshRSSClient(
        settings,
        FreshRSSNavigation(groups=[], feeds=[]),
        {
            (READING_LIST_STREAM, None): {
                "items": [_numeric_item(70)],
                "continuation": None,
            },
            (STARRED_STATE, None): {
                "items": [
                    _numeric_item(value, starred=True)
                    for value in (100, 90, 80)
                ],
                "continuation": "80",
            },
            (STARRED_STATE, "80"): {
                "items": [
                    _numeric_item(value, starred=True)
                    for value in (70, 60, 50)
                ],
                "continuation": "50",
            },
        },
    )

    pending_entry = client.get_stream(scope_kind="home", limit=3).entries[0]
    client.apply_local_state("starred", [pending_entry.id], True)
    first = client.get_stream(scope_kind="starred", limit=3)
    carried_entry_id = _numeric_item(80)["id"]
    carried_entry = client._get_cached_entry(carried_entry_id)
    assert carried_entry is not None
    with client._lock:
        client._entry_cache.clear()
        client._entry_cache_bytes = 0

    def fail_carried_entries(entry_ids):
        raise httpx.ConnectError(
            "FreshRSS is offline",
            request=httpx.Request("POST", "https://rss.example.net"),
        )

    monkeypatch.setattr(client, "_fetch_overlay_entries", fail_carried_entries)
    with pytest.raises(httpx.HTTPError):
        client.get_stream(
            scope_kind="starred",
            continuation=first.continuation,
            limit=3,
        )
    monkeypatch.setattr(
        client,
        "_fetch_overlay_entries",
        lambda entry_ids: {
            carried_entry_id: carried_entry
        }
        if carried_entry_id in entry_ids
        else {},
    )
    monkeypatch.setattr(client, "_fetch_entry", lambda entry_id: first.entries[-1])
    second = client.get_stream(
        scope_kind="starred",
        continuation=first.continuation,
        limit=3,
    )

    first_ids = {entry.id for entry in first.entries}
    second_ids = {entry.id for entry in second.entries}
    assert first_ids.isdisjoint(second_ids)
    assert pending_entry.id in first_ids
    assert pending_entry.id not in second_ids
    assert [entry.title for entry in first.entries] == [
        "Entry 70",
        "Entry 100",
        "Entry 90",
    ]
    assert [entry.title for entry in second.entries] == [
        "Entry 80",
        "Entry 60",
        "Entry 50",
    ]
    assert second.continuation == "50"


def test_newer_pending_membership_returns_to_native_pagination(monkeypatch):
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=3,
        metadata_cache_seconds=60,
        stream_cache_seconds=0,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    client = StubFreshRSSClient(
        settings,
        FreshRSSNavigation(groups=[], feeds=[]),
        {
            (READING_LIST_STREAM, None): {
                "items": [_numeric_item(110)],
                "continuation": None,
            },
            (STARRED_STATE, None): {
                "items": [
                    _numeric_item(value, starred=True)
                    for value in (100, 90, 80)
                ],
                "continuation": "80",
            },
            (STARRED_STATE, "80"): {
                "items": [
                    _numeric_item(value, starred=True)
                    for value in (70, 60)
                ],
                "continuation": "60",
            },
        },
    )

    pending = client.get_stream(scope_kind="home", limit=3).entries[0]
    client.apply_local_state("starred", [pending.id], True)
    first = client.get_stream(scope_kind="starred", limit=3)
    monkeypatch.setattr(client, "_fetch_entry", lambda entry_id: first.entries[-1])
    second = client.get_stream(
        scope_kind="starred",
        continuation=first.continuation,
        limit=3,
    )

    assert [entry.title for entry in first.entries] == [
        "Entry 110",
        "Entry 100",
        "Entry 90",
    ]
    assert [entry.title for entry in second.entries] == [
        "Entry 80",
        "Entry 70",
        "Entry 60",
    ]
    assert second.continuation == "60"
    assert client.request_params[-1]["n"] == "2"


def test_large_pending_overlay_does_not_drop_displaced_entries():
    page_size = 100
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=page_size,
        metadata_cache_seconds=60,
        stream_cache_seconds=0,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    pending_items = [_numeric_item(value) for value in range(100, 0, -1)]
    native_items = [
        _numeric_item(value, starred=True)
        for value in range(300, 200, -1)
    ]
    client = StubFreshRSSClient(
        settings,
        FreshRSSNavigation(groups=[], feeds=[]),
        {
            (READING_LIST_STREAM, None): {
                "items": pending_items,
                "continuation": None,
            },
            (STARRED_STATE, None): {
                "items": native_items,
                "continuation": "201",
            },
        },
    )

    pending = client.get_stream(scope_kind="home", limit=page_size).entries
    client.apply_local_state(
        "starred",
        [entry.id for entry in pending],
        True,
    )
    first = client.get_stream(scope_kind="starred", limit=page_size)
    native_by_id = {item["id"]: item for item in native_items}
    recovery_calls = []
    original_request_json = client._request_json

    def request_json(method, path, **kwargs):
        if path == "/reader/api/0/stream/items/contents":
            recovery_calls.append(kwargs["data"])
            requested_ids = [
                value for key, value in kwargs["data"] if key == "i"
            ]
            return {
                "items": [
                    native_by_id[entry_id]
                    for entry_id in requested_ids
                    if entry_id in native_by_id
                ]
            }
        return original_request_json(method, path, **kwargs)

    with client._lock:
        client._entry_cache.clear()
        client._entry_cache_bytes = 0
    client._request_json = request_json
    second = client.get_stream(
        scope_kind="starred",
        continuation=first.continuation,
        limit=page_size,
    )

    first_ids = {entry.id for entry in first.entries}
    second_ids = {entry.id for entry in second.entries}
    assert len(first_ids) == page_size
    assert len(second_ids) == page_size
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == {
        item["id"] for item in [*pending_items, *native_items]
    }
    assert len(recovery_calls) == 1
    assert [value for key, value in recovery_calls[0] if key == "i"] == [
        item["id"] for item in native_items
    ]


def test_large_overlay_history_stays_within_a_kindle_safe_url():
    page_size = 15
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=page_size,
        metadata_cache_seconds=60,
        stream_cache_seconds=0,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    navigation = FreshRSSNavigation(groups=[], feeds=[])
    client = StubFreshRSSClient(
        settings,
        navigation,
        {
            (STARRED_STATE, None): {
                "items": [
                    _numeric_item(value, starred=True)
                    for value in range(1_000, 985, -1)
                ],
                "continuation": "986",
            }
        },
    )
    pending = [
        client._parse_entry(_numeric_item(value), navigation)
        for value in range(OVERLAY_CONTINUATION_MAX_ITEMS, 0, -1)
    ]
    pending_entries = [entry for entry in pending if entry is not None]
    client._cache_entries(pending_entries)
    client.apply_local_state(
        "starred",
        [entry.id for entry in pending_entries],
        True,
    )

    page = client.get_stream(scope_kind="starred", limit=page_size)
    continuations = []
    for _ in range(6):
        assert page.continuation is not None
        continuations.append(page.continuation)
        page = client.get_stream(
            scope_kind="starred",
            continuation=page.continuation,
            limit=page_size,
        )

    app = FastAPI()
    app.add_api_route("/starred", lambda: {}, name="starred_view")
    request = StreamRequest(
        scope=StreamScope("starred"),
        continuation=continuations[-1],
        history=("", *continuations[:-1]),
    )
    url = request.url(app)
    restored = StreamRequest.from_url(url)

    assert len(url) < 4_096
    assert restored.continuation == request.continuation
    assert restored.history == request.history


def test_maximum_overlay_history_stays_within_a_kindle_safe_url():
    page_size = 15
    entry_ids = [
        _numeric_item(value)["id"]
        for value in (
            (1 << 62) - index * 1_000_003
            for index in range(OVERLAY_CONTINUATION_MAX_ITEMS)
        )
    ]
    continuations = [
        restore_overlay_continuation_from_history(
            [
                "rk1",
                "starred",
                None,
                "1000",
                entry_ids[offset:],
                entry_ids,
            ]
        )
        for offset in range(0, len(entry_ids), page_size)
    ]
    assert all(continuation is not None for continuation in continuations)
    resolved_continuations = [
        continuation for continuation in continuations if continuation is not None
    ]

    app = FastAPI()
    app.add_api_route("/starred", lambda: {}, name="starred_view")
    request = StreamRequest(
        scope=StreamScope("starred"),
        continuation=resolved_continuations[-1],
        history=("", *resolved_continuations[:-1]),
    )
    url = request.url(app)
    restored = StreamRequest.from_url(url)

    assert len(url) < 4_096
    assert restored.continuation == request.continuation
    assert restored.history == request.history


def test_cursor_history_rejects_excessive_expansion():
    indexes = list(range(CURSOR_STACK_MAX_ITEMS))
    suffix_references = [
        ["s", 0, 0]
        for _ in range(CURSOR_STACK_MAX_EXPANDED_IDS // len(indexes))
    ]
    payload = {
        "stack2": [
            indexes,
            [indexes, *suffix_references],
            [],
        ]
    }

    assert _decode_compact_cursor_stack(payload) is None

    compact_lists = [list(range(100)), list(range(100, 200))]
    repeated_item = ["r", "starred", None, "1000", 0, 1]
    repeated_items = [
        repeated_item
        for _ in range(CURSOR_STACK_MAX_RESTORED_ID_REFERENCES // 200 + 1)
    ]
    reused_payload = {
        "stack2": [list(range(200)), compact_lists, repeated_items]
    }

    assert _decode_compact_cursor_stack(reused_payload) is None


def test_legacy_cursor_history_rejects_too_many_pages():
    token = _encode_token({"stack": [""] * (CURSOR_STACK_MAX_ITEMS + 1)})

    assert _decode_cursor_stack(token) == ()
    assert _decode_cursor_stack("j" + "a" * (CURSOR_STACK_MAX_BYTES * 2 + 1)) == ()


def test_large_server_reading_context_round_trips():
    entry_ids = tuple(
        _numeric_item((1 << 62) - index * 1_000_003)["id"]
        for index in range(512)
    )
    deep_history = "a" * 40_000
    context = ReadingContext(
        entry_ids=entry_ids,
        back_url=f"/starred?b={deep_history}",
        next_page_url=f"/starred?c=100&b={deep_history}",
    )

    assert ReadingContext.decode(context.encode()) == context


def test_settings_cap_stream_pages_to_a_kindle_safe_size():
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=512,
        metadata_cache_seconds=60,
        freshrss_api_url=None,
        freshrss_username=None,
        freshrss_api_password=None,
    )

    assert settings.max_stream_items == MAX_STREAM_ITEMS_LIMIT


def test_in_flight_stream_fetch_cannot_overwrite_a_local_state_change(monkeypatch):
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=3,
        metadata_cache_seconds=60,
        stream_cache_seconds=60,
        entry_cache_seconds=21_600,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    client = FreshRSSClient(settings, client_factory=lambda: CapturingClient())
    fetch_started = threading.Event()
    release_fetch = threading.Event()
    entry_id = "tag:google.com,2005:reader/item/0000000000000003"
    stale_entry = client._parse_entry(
        _numeric_item(3),
        FreshRSSNavigation(groups=[], feeds=[]),
    )
    assert stale_entry is not None

    def fetch_stream_page(**kwargs):
        fetch_started.set()
        assert release_fetch.wait(timeout=2)
        return FreshRSSStreamPage(entries=[stale_entry], continuation=None)

    monkeypatch.setattr(client, "_fetch_stream_page", fetch_stream_page)
    with ThreadPoolExecutor(max_workers=1) as executor:
        fetching = executor.submit(client.get_stream, scope_kind="home", limit=3)
        assert fetch_started.wait(timeout=2)
        client.apply_local_state("starred", [entry_id], True)
        release_fetch.set()
        page = fetching.result(timeout=2)

    assert page.entries[0].is_starred is True
    assert client.get_entry(entry_id).is_starred is True


def test_restored_local_state_overrides_an_uncached_entry_response(monkeypatch):
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=3,
        metadata_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    client = FreshRSSClient(settings, client_factory=lambda: CapturingClient())
    raw_item = _numeric_item(3, starred=True)
    entry_id = raw_item["id"]
    monkeypatch.setattr(
        client,
        "_request_json",
        lambda *args, **kwargs: {"items": [raw_item]},
    )

    client.apply_local_state("starred", [entry_id], False)
    entry = client.get_entry(entry_id)

    assert entry is not None
    assert entry.is_starred is False


def test_unconfirmed_local_state_remains_authoritative_until_acknowledged(monkeypatch):
    monotonic_time = [100.0]
    monkeypatch.setattr("app.freshrss.time.monotonic", lambda: monotonic_time[0])
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=3,
        metadata_cache_seconds=60,
        entry_cache_seconds=300,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    client = FreshRSSClient(settings, client_factory=lambda: CapturingClient())
    raw_item = _numeric_item(3)
    entry_id = raw_item["id"]
    monkeypatch.setattr(
        client,
        "_request_json",
        lambda *args, **kwargs: {"items": [raw_item]},
    )

    client.apply_local_state("starred", [entry_id], True)
    monotonic_time[0] += 86_400
    assert client.get_entry(entry_id).is_starred is True

    client.confirm_local_state("starred", [entry_id], True)
    with client._lock:
        client._entry_cache.clear()
        client._entry_cache_bytes = 0
    monotonic_time[0] += 301

    assert client.get_entry(entry_id).is_starred is False


def test_unrelated_stream_refreshes_can_run_concurrently(monkeypatch):
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=15,
        metadata_cache_seconds=60,
        stream_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    client = FreshRSSClient(settings, client_factory=lambda: CapturingClient())
    both_refreshes_started = threading.Barrier(2)

    def load_stream(**kwargs):
        both_refreshes_started.wait(timeout=1)
        return FreshRSSStreamPage(entries=[], continuation=None)

    monkeypatch.setattr(client, "_load_stream", load_stream)

    with ThreadPoolExecutor(max_workers=2) as executor:
        home = executor.submit(client.get_stream, scope_kind="home")
        starred = executor.submit(client.get_stream, scope_kind="starred")
        assert home.result(timeout=2).entries == []
        assert starred.result(timeout=2).entries == []
    assert client._stream_refresh_states == {}


def test_same_stream_refreshes_share_one_in_flight_load(monkeypatch):
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=15,
        metadata_cache_seconds=60,
        stream_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    client = FreshRSSClient(settings, client_factory=lambda: CapturingClient())
    callers_ready = threading.Barrier(3)
    first_load_started = threading.Event()
    second_load_started = threading.Event()
    release_load = threading.Event()
    load_count = 0
    load_count_lock = threading.Lock()

    def load_stream(**kwargs):
        nonlocal load_count
        with load_count_lock:
            load_count += 1
            if load_count == 1:
                first_load_started.set()
            else:
                second_load_started.set()
        assert release_load.wait(timeout=2)
        return FreshRSSStreamPage(entries=[], continuation=None)

    def request_home():
        callers_ready.wait(timeout=2)
        return client.get_stream(scope_kind="home")

    monkeypatch.setattr(client, "_load_stream", load_stream)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(request_home)
        second = executor.submit(request_home)
        callers_ready.wait(timeout=2)
        assert first_load_started.wait(timeout=2)
        assert not second_load_started.wait(timeout=0.1)
        release_load.set()
        assert first.result(timeout=2).entries == []
        assert second.result(timeout=2).entries == []

    assert load_count == 1
    assert client._stream_refresh_states == {}


def test_page_cache_eviction_removes_matching_retry_metadata(monkeypatch):
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=1,
        metadata_cache_seconds=60,
        stream_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    client = FreshRSSClient(settings, client_factory=lambda: CapturingClient())
    monkeypatch.setattr(
        client,
        "_load_stream",
        lambda **kwargs: FreshRSSStreamPage(entries=[], continuation=None),
    )

    for index in range(STREAM_PAGE_CACHE_LIMIT):
        client.get_stream(scope_kind="feed", scope_value=f"feed-{index}", limit=1)

    oldest_key = ("feed", "feed-0", None, 1, False)
    client._stream_retry_after[oldest_key] = float("inf")
    client.get_stream(scope_kind="feed", scope_value="feed-overflow", limit=1)

    assert oldest_key not in client._stream_cache
    assert oldest_key not in client._stream_retry_after


def test_stream_entries_are_reused_for_detail_requests():
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=2,
        metadata_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
        entry_cache_seconds=300,
    )
    navigation = FreshRSSNavigation(
        groups=[],
        feeds=[
            FreshRSSFeed(
                token="feed-1",
                stream_id="feed/1",
                title="Feed One",
                feed_url="https://example.com/feed.xml",
                site_url="https://example.com",
                group_slugs=(),
            )
        ],
    )
    payloads = {
        ("reading-list", None): {
            "items": [_item("entry-1", "Cached story", 100, "feed/1", "Feed One")],
            "continuation": None,
        },
    }
    client = StubFreshRSSClient(settings, navigation, payloads)

    client.get_stream(scope_kind="home", limit=2)
    calls_after_stream = list(client.calls)
    entry = client.get_entry("entry-1")

    assert entry is not None
    assert entry.title == "Cached story"
    assert client.calls == calls_after_stream


def test_uncached_entry_uses_one_plain_post_without_loading_navigation(monkeypatch):
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=15,
        metadata_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    client = FreshRSSClient(settings, client_factory=lambda: CapturingClient())
    calls = []

    def request_json(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {
            "items": [
                _item("entry-1", "Direct story", 100, "feed/1", "Feed One")
            ]
        }

    def unexpected_navigation(*args, **kwargs):
        raise AssertionError("get_entry must not load subscriptions")

    monkeypatch.setattr(client, "_request_json", request_json)
    monkeypatch.setattr(client, "list_navigation", unexpected_navigation)

    entry = client.get_entry("entry-1")

    assert entry is not None
    assert entry.title == "Direct story"
    assert calls == [
        (
            "POST",
            "/reader/api/0/stream/items/contents",
            {"data": [("output", "json"), ("i", "entry-1")]},
        )
    ]


def test_remote_and_local_state_updates_are_independent():
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=2,
        metadata_cache_seconds=60,
        stream_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    payloads = {
        (READING_LIST_STREAM, None): {
            "items": [
                _item("entry-1", "First", 200, "feed/1", "Feed One"),
                _item("entry-2", "Second", 100, "feed/1", "Feed One"),
            ],
            "continuation": None,
        }
    }
    client = StubFreshRSSClient(
        settings,
        FreshRSSNavigation(groups=[], feeds=[]),
        payloads,
    )
    client.get_stream(scope_kind="home", limit=2)

    client.apply_local_state("starred", ["entry-2"], True)
    client.apply_local_state("read", ["entry-1"], True)

    assert client.transport.calls == []
    assert client._get_cached_entry("entry-1").is_read is True
    assert client._get_cached_entry("entry-2").is_starred is True
    assert [
        entry.id for entry in client.get_stream(scope_kind="home", limit=2).entries
    ] == ["entry-2"]

    client._auth_token = "auth-token"
    client._write_token = "write-token"
    sent_ids = client.send_state("read", ["entry-2"], True)

    assert sent_ids == ["entry-2"]
    assert len(client.transport.calls) == 1
    assert client._get_cached_entry("entry-2").is_read is False
    assert [
        entry.id for entry in client.get_stream(scope_kind="home", limit=2).entries
    ] == ["entry-2"]


def test_entry_cache_uses_lru_eviction_and_the_configured_ttl(monkeypatch):
    monotonic_time = [100.0]
    monkeypatch.setattr("app.freshrss.time.monotonic", lambda: monotonic_time[0])
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=ENTRY_CACHE_LIMIT,
        metadata_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
        entry_cache_seconds=5,
    )
    payloads = {
        (READING_LIST_STREAM, None): {
            "items": [
                _item(
                    f"entry-{index}",
                    f"Entry {index}",
                    2_000 - index,
                    "feed/1",
                    "Feed One",
                )
                for index in range(ENTRY_CACHE_LIMIT)
            ],
            "continuation": "next-page",
        },
        (READING_LIST_STREAM, "next-page"): {
            "items": [
                _item(
                    f"entry-{ENTRY_CACHE_LIMIT}",
                    f"Entry {ENTRY_CACHE_LIMIT}",
                    1_000,
                    "feed/1",
                    "Feed One",
                )
            ],
            "continuation": None,
        },
    }
    client = StubFreshRSSClient(
        settings,
        FreshRSSNavigation(groups=[], feeds=[]),
        payloads,
    )

    first_page = client.get_stream(scope_kind="home", limit=ENTRY_CACHE_LIMIT)

    assert len(client._entry_cache) == ENTRY_CACHE_LIMIT
    assert client._get_cached_entry("entry-0") is not None
    client.get_stream(
        scope_kind="home",
        continuation=first_page.continuation,
        limit=1,
    )
    assert len(client._entry_cache) == ENTRY_CACHE_LIMIT
    assert client._get_cached_entry("entry-1") is None
    assert client._get_cached_entry("entry-0") is not None
    monotonic_time[0] += 4
    assert client._get_cached_entry(f"entry-{ENTRY_CACHE_LIMIT}") is not None
    monotonic_time[0] += 2
    assert client._get_cached_entry(f"entry-{ENTRY_CACHE_LIMIT}") is None


def test_entry_cache_has_a_byte_bound_for_full_content_feeds():
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=15,
        metadata_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    client = FreshRSSClient(settings, client_factory=lambda: CapturingClient())
    navigation = FreshRSSNavigation(groups=[], feeds=[])
    entries = []
    body = "x" * (ENTRY_CACHE_BYTES_LIMIT // 4 + 1)
    for numeric_id in range(1, 6):
        entry = client._parse_entry(_numeric_item(numeric_id), navigation)
        assert entry is not None
        entries.append(replace(entry, content_html=body))

    client._cache_entries(entries)

    assert client._entry_cache_bytes <= ENTRY_CACHE_BYTES_LIMIT
    assert client._get_cached_entry(entries[0].id) is None
    assert client._get_cached_entry(entries[-1].id) is not None


def test_stream_cache_avoids_refetch_and_removes_items_marked_read():
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=2,
        metadata_cache_seconds=60,
        stream_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    navigation = FreshRSSNavigation(groups=[], feeds=[])
    payloads = {
        (READING_LIST_STREAM, None): {
            "items": [
                _item("entry-1", "Newest", 200, "feed/1", "Feed One"),
                _item("entry-2", "Older", 100, "feed/1", "Feed One"),
            ],
            "continuation": None,
        }
    }
    client = StubFreshRSSClient(settings, navigation, payloads)

    first = client.get_stream(scope_kind="home", limit=2)
    second = client.get_stream(scope_kind="home", limit=2)

    assert [entry.id for entry in first.entries] == ["entry-1", "entry-2"]
    assert [entry.id for entry in second.entries] == ["entry-1", "entry-2"]
    assert first.entries_are_compact is False
    assert second.entries_are_compact is True
    assert client.calls == [(READING_LIST_STREAM, None)]

    client._auth_token = "auth-token"
    client._write_token = "write-token"
    client.mark_read(["entry-1"])
    after_read = client.get_stream(scope_kind="home", limit=2)

    assert [entry.id for entry in after_read.entries] == ["entry-2"]
    assert client.calls == [(READING_LIST_STREAM, None)]


def test_stream_cache_bounds_content_and_prunes_old_stale_pages(monkeypatch):
    monotonic_time = [100.0]
    monkeypatch.setattr("app.freshrss.time.monotonic", lambda: monotonic_time[0])
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=1,
        metadata_cache_seconds=60,
        stream_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    raw_item = _numeric_item(3)
    raw_item["content"] = {"content": "x" * 100_000}
    client = StubFreshRSSClient(
        settings,
        FreshRSSNavigation(groups=[], feeds=[]),
        {
            (READING_LIST_STREAM, None): {
                "items": [raw_item],
                "continuation": None,
            },
            (STARRED_STATE, None): {"items": [], "continuation": None},
        },
    )

    client.get_stream(scope_kind="home", limit=1)
    home_key = ("home", None, None, 1, False)
    cached_entry = client._stream_cache[home_key][1].entries[0]
    assert len(cached_entry.content_html or "") == STREAM_CACHE_HTML_CHARS

    monotonic_time[0] = 461.0
    client.get_stream(scope_kind="starred", limit=1)

    assert home_key not in client._stream_cache


def test_expired_stream_cache_refreshes_before_returning_and_falls_back_on_error(
    monkeypatch,
):
    monotonic_time = [100.0]
    monkeypatch.setattr("app.freshrss.time.monotonic", lambda: monotonic_time[0])
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=1,
        metadata_cache_seconds=60,
        stream_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    navigation = FreshRSSNavigation(groups=[], feeds=[])
    payloads = {
        (READING_LIST_STREAM, None): {
            "items": [_item("entry-1", "First", 100, "feed/1", "Feed One")],
            "continuation": None,
        }
    }
    client = StubFreshRSSClient(settings, navigation, payloads)
    first = client.get_stream(scope_kind="home", limit=1)
    assert [entry.id for entry in first.entries] == ["entry-1"]

    payloads[(READING_LIST_STREAM, None)] = {
        "items": [_item("entry-2", "Second", 200, "feed/1", "Feed One")],
        "continuation": None,
    }
    monotonic_time[0] = 161.0
    fresh = client.get_stream(scope_kind="home", limit=1)

    assert [entry.id for entry in fresh.entries] == ["entry-2"]
    assert fresh.is_stale is False
    assert client.calls == [
        (READING_LIST_STREAM, None),
        (READING_LIST_STREAM, None),
    ]

    refresh_attempts = []

    def fail_refresh(**kwargs):
        refresh_attempts.append(kwargs)
        raise FreshRSSError("FreshRSS is unavailable")

    monotonic_time[0] = 222.0
    monkeypatch.setattr(client, "_load_stream", fail_refresh)
    stale = client.get_stream(scope_kind="home", limit=1)

    assert refresh_attempts
    assert [entry.id for entry in stale.entries] == ["entry-2"]
    assert stale.is_stale is True

    monotonic_time[0] = 223.0
    backed_off = client.get_stream(scope_kind="home", limit=1)

    assert backed_off.is_stale is True
    assert len(refresh_attempts) == 1


def test_expired_navigation_cache_is_reused_during_a_bounded_failure_backoff(
    monkeypatch,
):
    monotonic_time = [100.0]
    monkeypatch.setattr("app.freshrss.time.monotonic", lambda: monotonic_time[0])
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=Path("/tmp/rss-kindle-test.db"),
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=15,
        metadata_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    client = FreshRSSClient(settings, client_factory=lambda: CapturingClient())
    attempts = []

    def navigation_request(*args, **kwargs):
        attempts.append((args, kwargs))
        if len(attempts) > 1:
            raise FreshRSSError("FreshRSS is unavailable")
        return {
            "subscriptions": [
                {
                    "id": "feed/1",
                    "title": "Feed One",
                    "url": "https://example.com/feed.xml",
                    "htmlUrl": "https://example.com",
                    "categories": [],
                }
            ]
        }

    monkeypatch.setattr(client, "_request_json", navigation_request)

    first = client.list_navigation()
    monotonic_time[0] = 161.0
    stale = client.list_navigation()
    monotonic_time[0] = 162.0
    backed_off = client.list_navigation()

    assert first.feeds[0].title == "Feed One"
    assert stale == first
    assert backed_off == first
    assert len(attempts) == 2
