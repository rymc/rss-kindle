import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode

from app.config import Settings
from app.freshrss import (
    READ_STATE,
    READING_LIST_STREAM,
    STARRED_STATE,
    STREAM_PAGE_CACHE_LIMIT,
    STREAM_SCAN_CACHE_ENTRY_BUDGET,
    FreshRSSClient,
    FreshRSSError,
    FreshRSSFeed,
    FreshRSSGroup,
    FreshRSSNavigation,
    FreshRSSStreamPage,
    decode_feed_token,
    parse_navigation,
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
        super().__init__(settings, client_factory=lambda: CapturingClient())
        self._navigation = navigation
        self.payloads = payloads
        self.calls: list[tuple[str, str | None]] = []
        self.request_params: list[dict[str, str]] = []

    def list_navigation(self, force_refresh: bool = False) -> FreshRSSNavigation:
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


def test_group_stream_merges_feeds_in_group_instead_of_label_stream():
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
        ("feed/1", None): {
            "items": [
                _item("entry-1a", "Older item", 100, "feed/1", "Feed One"),
                _item("entry-1b", "Oldest item", 50, "feed/1", "Feed One"),
            ],
            "continuation": None,
        },
        ("feed/2", None): {
            "items": [
                _item("entry-2a", "Newest item", 200, "feed/2", "Feed Two"),
                _item("entry-2b", "Middle item", 150, "feed/2", "Feed Two"),
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
        "Newest item",
        "Middle item",
    ]
    assert [entry.title for entry in second_page.entries] == [
        "Older item",
        "Oldest item",
    ]
    assert client.calls == [("feed/1", None), ("feed/2", None)]
    assert all(stream_id != "user/-/label/Blogs" for stream_id, _ in client.calls)


def test_home_stream_overfetches_and_sorts_by_published_date():
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
        "Actually newest",
        "Second newest",
    ]
    assert [entry.title for entry in next_page.entries] == [
        "Old but recently inserted",
        "Still old",
    ]
    assert client.calls == [("reading-list", None), ("reading-list", "page-2")]


def test_later_scan_response_does_not_reorder_articles_already_served():
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
    first_batch = [
        _item(
            f"first-{index:02d}",
            f"First batch {index:02d}",
            2_000 - index,
            "feed/1",
            "Feed One",
        )
        for index in range(50)
    ]
    later_batch = [
        _item(
            "later-newest",
            "Later response with a newer publication date",
            3_000,
            "feed/1",
            "Feed One",
        ),
        *[
            _item(
                f"later-{index:02d}",
                f"Later batch {index:02d}",
                1_000 - index,
                "feed/1",
                "Feed One",
            )
            for index in range(49)
        ],
    ]
    client = StubFreshRSSClient(
        settings,
        navigation,
        {
            (READING_LIST_STREAM, None): {
                "items": first_batch,
                "continuation": "page-2",
            },
            (READING_LIST_STREAM, "page-2"): {
                "items": later_batch,
                "continuation": None,
            },
        },
    )

    first_page = client.get_stream(scope_kind="home", limit=15)
    second_page = client.get_stream(
        scope_kind="home",
        continuation=first_page.continuation,
        limit=15,
    )

    assert [entry.id for entry in first_page.entries] == [
        f"first-{index:02d}" for index in range(15)
    ]
    assert [entry.id for entry in second_page.entries] == [
        f"first-{index:02d}" for index in range(15, 30)
    ]
    assert {entry.id for entry in first_page.entries}.isdisjoint(
        entry.id for entry in second_page.entries
    )
    assert client.calls == [
        (READING_LIST_STREAM, None),
        (READING_LIST_STREAM, "page-2"),
    ]


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


def test_scan_snapshots_stay_within_the_global_entry_budget():
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
    payloads = {
        (f"feed/{feed_index}", None): {
            "items": [
                _item(
                    f"entry-{feed_index}-{entry_index}",
                    f"Feed {feed_index} entry {entry_index}",
                    2_000 - entry_index,
                    f"feed/{feed_index}",
                    f"Feed {feed_index}",
                )
                for entry_index in range(50)
            ],
            "continuation": None,
        }
        for feed_index in range(11)
    }
    client = StubFreshRSSClient(settings, navigation, payloads)

    for feed_index in range(11):
        client._collect_entries_for_sorted_stream(
            stream_id=f"feed/{feed_index}",
            scan_limit=50,
            page_size=50,
            navigation=navigation,
            include_read=False,
        )

    retained_entries = sum(
        len(snapshot.entries) for snapshot in client._stream_scan_cache.values()
    )
    assert retained_entries <= STREAM_SCAN_CACHE_ENTRY_BUDGET


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
    assert second == first
    assert client.calls == [(READING_LIST_STREAM, None)]

    client._auth_token = "auth-token"
    client._write_token = "write-token"
    client.mark_read(["entry-1"])
    after_read = client.get_stream(scope_kind="home", limit=2)

    assert [entry.id for entry in after_read.entries] == ["entry-2"]
    assert client.calls == [(READING_LIST_STREAM, None)]


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
