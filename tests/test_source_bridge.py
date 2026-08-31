import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from app.config import Settings
from app.db import Database
from app.repository import Repository, SyntheticFeedItem
from app.source_bridge import (
    AuthProfile,
    SourceBridgeService,
    SourceCatalog,
    SourceDefinition,
)
from app.source_config import SourceBridgeError
from app.utils import stable_hash, utc_now


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class RecordingClient:
    def __init__(self, responses: dict[str, str | FakeResponse | Exception]):
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str], str | None]] = []
        self.wait_calls: list[tuple[str, str | None, str | None, float | None]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        cookie_header: str | None = None,
        wait_until: str | None = None,
        wait_for_selector: str | None = None,
        settle_seconds: float | None = None,
    ):
        self.calls.append((url, headers or {}, cookie_header))
        self.wait_calls.append((url, wait_until, wait_for_selector, settle_seconds))
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        if isinstance(response, FakeResponse):
            return response
        return FakeResponse(response)


class RecordingBrowserClient(RecordingClient):
    pass


class ClientConcurrencyTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self.first_entered = threading.Event()
        self.second_entered = threading.Event()
        self.release_first = threading.Event()
        self.entry_count = 0
        self.active_count = 0
        self.max_active_count = 0

    def enter(self) -> None:
        with self._lock:
            self.entry_count += 1
            entry_number = self.entry_count
            self.active_count += 1
            self.max_active_count = max(self.max_active_count, self.active_count)
        if entry_number == 1:
            self.first_entered.set()
            if not self.release_first.wait(timeout=3):
                self.exit()
                raise TimeoutError("test did not release the first client")
        else:
            self.second_entered.set()

    def exit(self) -> None:
        with self._lock:
            self.active_count -= 1


class CoordinatedClient(RecordingClient):
    def __init__(
        self,
        responses: dict[str, str | FakeResponse | Exception],
        tracker: ClientConcurrencyTracker,
    ):
        super().__init__(responses)
        self.tracker = tracker

    def __enter__(self):
        self.tracker.enter()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.tracker.exit()
        return False


def run_thread_operation(
    operation: Callable[[], object],
    errors: list[Exception],
    *,
    started: threading.Event | None = None,
) -> None:
    if started is not None:
        started.set()
    try:
        operation()
    except Exception as exc:  # noqa: BLE001 - surface worker failures in the test thread
        errors.append(exc)


def build_settings(tmp_path: Path, *, refresh_seconds: int = 900) -> Settings:
    return Settings(
        app_name="RSS Kindle",
        base_dir=tmp_path,
        database_path=tmp_path / "bridge.db",
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=15,
        metadata_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
        source_bridge_refresh_seconds=refresh_seconds,
    )


def build_catalog(cookie_header: str = "session=abc123") -> SourceCatalog:
    return SourceCatalog(
        auth_profiles={
            "ft": AuthProfile(
                name="ft",
                domains=("ft.com",),
                cookie_header=cookie_header,
                browser_profile_path=Path("/tmp/ft-profile"),
                browser_channel="chrome",
            )
        },
        sources={
            "ft-home": SourceDefinition(
                source_id="ft-home",
                title="FT Front Page",
                start_urls=("https://www.ft.com/",),
                include_url_patterns=(r"^https://www\.ft\.com/content/",),
                auth_profile="ft",
                max_items=2,
            )
        },
    )


def test_source_catalog_loads_relative_cookie_jar_paths(tmp_path: Path):
    cookie_jar = tmp_path / "ft.cookies"
    cookie_jar.write_text(".ft.com\tTRUE\t/\tTRUE\t0\tsession\tcookie-value\n", encoding="utf-8")
    config_path = tmp_path / "source-bridge.toml"
    config_path.write_text(
        """
[auth_profiles.ft]
domains = ["ft.com"]
cookie_jar_path = "ft.cookies"

[sources.ft-home]
title = "FT Front Page"
start_urls = ["https://www.ft.com/"]
include_url_patterns = ["^https://www\\\\.ft\\\\.com/content/"]
auth_profile = "ft"
max_items = 5
""".strip(),
        encoding="utf-8",
    )

    catalog = SourceCatalog.load(config_path)

    assert catalog.auth_profiles["ft"].cookie_jar_path == cookie_jar.resolve()
    assert catalog.sources["ft-home"].start_urls == ("https://www.ft.com/",)


def test_source_catalog_loads_relative_browser_profile_paths(tmp_path: Path):
    config_path = tmp_path / "source-bridge.toml"
    config_path.write_text(
        """
[auth_profiles.ft]
domains = ["ft.com"]
browser_profile_path = "profiles/ft"
browser_channel = "chrome"

[sources.ft-home]
title = "FT Front Page"
start_urls = ["https://www.ft.com/"]
include_url_patterns = ["^https://www\\\\.ft\\\\.com/content/"]
auth_profile = "ft"
fetch_backend = "browser"
browser_wait_until = "load"
browser_wait_for_selector = "main"
browser_settle_seconds = 3
discovery_browser_wait_until = "domcontentloaded"
discovery_browser_settle_seconds = 1
max_items = 5
""".strip(),
        encoding="utf-8",
    )

    catalog = SourceCatalog.load(config_path)

    assert catalog.auth_profiles["ft"].browser_profile_path == (tmp_path / "profiles" / "ft").resolve()
    assert catalog.sources["ft-home"].fetch_backend == "browser"
    assert catalog.sources["ft-home"].browser_wait_for_selector == "main"
    assert catalog.sources["ft-home"].discovery_browser_wait_until == "domcontentloaded"
    assert catalog.sources["ft-home"].discovery_browser_settle_seconds == 1.0


def test_source_catalog_loads_browser_cdp_url(tmp_path: Path):
    config_path = tmp_path / "source-bridge.toml"
    config_path.write_text(
        """
[auth_profiles.ft]
domains = ["ft.com"]
browser_cdp_url = "http://browser-cdp:9222"

[sources.ft-home]
title = "FT Front Page"
start_urls = ["https://www.ft.com/"]
include_url_patterns = ["^https://www\\\\.ft\\\\.com/content/"]
auth_profile = "ft"
fetch_backend = "browser"
max_items = 5
""".strip(),
        encoding="utf-8",
    )

    catalog = SourceCatalog.load(config_path)

    assert catalog.auth_profiles["ft"].browser_cdp_url == "http://browser-cdp:9222"
    assert catalog.auth_profiles["ft"].browser_profile_path is None


def test_source_bridge_generates_rss_from_ft_homepage_with_auth(tmp_path: Path):
    settings = build_settings(tmp_path)
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    homepage_html = """
    <html>
      <body>
        <a href="/content/story-1">Lead story</a>
        <a href="/world">World hub</a>
        <a href="https://www.ft.com/content/story-2">Second story</a>
      </body>
    </html>
    """
    story_html = """
    <html>
      <head>
        <title>Lead story</title>
        <meta property="article:published_time" content="2026-03-29T08:00:00Z" />
      </head>
      <body>
        <article>
          <p>This is the first paragraph of the lead story and it is long enough to count as real article content.</p>
          <p>This is the second paragraph of the lead story and it keeps the extracted body comfortably above the minimum threshold.</p>
        </article>
      </body>
    </html>
    """
    second_story_html = """
    <html>
      <head><title>Second story</title></head>
      <body>
        <article>
          <p>This is another substantial article body for the synthetic feed bridge to publish into RSS content.</p>
          <p>It should survive readability extraction and show up in content:encoded for FreshRSS.</p>
        </article>
      </body>
    </html>
    """
    client = RecordingClient(
        {
            "https://www.ft.com/": homepage_html,
            "https://www.ft.com/content/story-1": story_html,
            "https://www.ft.com/content/story-2": second_story_html,
        }
    )
    service = SourceBridgeService(
        settings,
        repository,
        catalog=build_catalog(),
        client_factory=lambda: client,
    )

    xml = service.build_feed("ft-home")
    status = service.list_source_status()[0]

    assert "<title>FT Front Page</title>" in xml
    assert "<title>Lead story</title>" in xml
    assert "content:encoded" in xml
    assert "World hub" not in xml
    assert status["latest_article_title"] == "Lead story"
    assert status["latest_article_at"] == "2026-03-29T08:00:00+00:00"
    assert all(call_headers.get("Cookie") == "session=abc123" for _, call_headers, _ in client.calls)


def test_source_bridge_serves_cached_items_when_refresh_later_fails(tmp_path: Path):
    settings = build_settings(tmp_path, refresh_seconds=0)
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    healthy_client = RecordingClient(
        {
            "https://www.ft.com/": '<html><body><a href="/content/story-1">Lead story</a></body></html>',
            "https://www.ft.com/content/story-1": """
                <html>
                  <head><title>Lead story</title></head>
                  <body><article><p>Long enough paragraph to remain in the cached synthetic feed body.</p><p>More body text here.</p></article></body>
                </html>
            """,
        }
    )
    service = SourceBridgeService(
        settings,
        repository,
        catalog=build_catalog(),
        client_factory=lambda: healthy_client,
    )

    first_xml = service.build_feed("ft-home")

    failing_client = RecordingClient({"https://www.ft.com/": RuntimeError("boom")})
    service.client_factory = lambda: failing_client
    scheduled_refreshes: list[str] = []
    service._schedule_background_refresh = lambda source_id: scheduled_refreshes.append(source_id)  # type: ignore[method-assign]
    second_xml = service.build_feed("ft-home")

    assert "Lead story" in first_xml
    assert second_xml == first_xml
    assert scheduled_refreshes == ["ft-home"]


def test_background_and_synchronous_refreshes_do_not_overlap(tmp_path: Path):
    settings = build_settings(tmp_path)
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    tracker = ClientConcurrencyTracker()
    responses = {
        "https://www.ft.com/": (
            '<html><body><a href="/content/story-1">Lead story</a></body></html>'
        ),
        "https://www.ft.com/content/story-1": """
            <html><head><title>Lead story</title></head><body><article>
              <p>This article has enough text for a coordinated source refresh.</p>
            </article></body></html>
        """,
    }
    service = SourceBridgeService(
        settings,
        repository,
        catalog=build_catalog(),
        client_factory=lambda: CoordinatedClient(responses, tracker),
    )
    errors: list[Exception] = []
    sync_started = threading.Event()
    sync_thread = threading.Thread(
        target=run_thread_operation,
        args=(lambda: service.refresh_source("ft-home"), errors),
        kwargs={"started": sync_started},
    )

    assert service.schedule_refresh("ft-home") is True
    first_entered = tracker.first_entered.wait(timeout=2)
    duplicate_was_rejected = service.schedule_refresh("ft-home") is False
    if first_entered:
        sync_thread.start()
        sync_started.wait(timeout=1)
        second_entered_while_blocked = tracker.second_entered.wait(timeout=0.2)
    else:
        second_entered_while_blocked = False
    tracker.release_first.set()
    if sync_thread.ident is not None:
        sync_thread.join(timeout=3)

    assert first_entered
    assert duplicate_was_rejected
    assert sync_started.is_set()
    assert not second_entered_while_blocked
    assert not sync_thread.is_alive()
    assert tracker.second_entered.wait(timeout=1)
    assert errors == []
    assert tracker.entry_count == 2
    assert tracker.max_active_count == 1


def test_source_bridge_reuses_unchanged_cached_articles(tmp_path: Path):
    settings = build_settings(tmp_path)
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    homepage_html = """
        <html><body>
          <a href="/content/story-1">Lead story</a>
          <a href="/content/story-2">Second story</a>
        </body></html>
    """
    first_client = RecordingClient(
        {
            "https://www.ft.com/": homepage_html,
            "https://www.ft.com/content/story-1": """
                <html><head><title>Lead story</title></head><body><article>
                  <p>The first cached story contains enough useful text for the feed, including context, evidence, and a clear explanation for the reader.</p>
                  <p>This second paragraph keeps the extracted article above the minimum content threshold.</p>
                </article></body></html>
            """,
            "https://www.ft.com/content/story-2": """
                <html><head><title>Second story</title></head><body><article>
                  <p>The second cached story contains text that must survive a later refresh, with enough detail for full-content extraction.</p>
                  <p>This second paragraph proves that the cached body remains useful when the source page has not changed.</p>
                </article></body></html>
            """,
        }
    )
    service = SourceBridgeService(
        settings,
        repository,
        catalog=build_catalog(),
        client_factory=lambda: first_client,
    )
    service.refresh_source("ft-home")

    refresh_client = RecordingClient(
        {
            "https://www.ft.com/": homepage_html,
            "https://www.ft.com/content/story-1": """
                <html><head><title>Lead story updated</title></head><body><article>
                  <p>The most recent story is rechecked and can receive corrected text, new context, and updated facts from the publisher.</p>
                  <p>This second paragraph keeps the refreshed article above the extraction threshold.</p>
                </article></body></html>
            """,
        }
    )
    service.client_factory = lambda: refresh_client

    items = service.refresh_source("ft-home")

    assert [url for url, _, _ in refresh_client.calls] == [
        "https://www.ft.com/",
        "https://www.ft.com/content/story-1",
    ]
    assert items[0].title == "Lead story updated"
    assert items[1].title == "Second story"
    assert "must survive" in (items[1].content_html or "")


def test_source_bridge_accepts_and_caches_an_empty_first_refresh(tmp_path: Path):
    settings = build_settings(tmp_path)
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    client = RecordingClient(
        {"https://www.ft.com/": "<html><body>No matching articles</body></html>"}
    )
    service = SourceBridgeService(
        settings,
        repository,
        catalog=build_catalog(),
        client_factory=lambda: client,
    )

    first_xml = service.build_feed("ft-home")
    second_xml = service.build_feed("ft-home")

    state = repository.get_synthetic_source_state("ft-home")
    assert first_xml == second_xml
    assert "<item>" not in first_xml
    assert client.calls == [("https://www.ft.com/", {"Cookie": "session=abc123"}, None)]
    assert state is not None
    assert state.last_successful_at is not None
    assert state.last_error is None


def test_source_bridge_throttles_a_second_cold_build_after_failure(tmp_path: Path):
    settings = build_settings(tmp_path, refresh_seconds=900)
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    client = RecordingClient({"https://www.ft.com/": RuntimeError("boom")})
    service = SourceBridgeService(
        settings,
        repository,
        catalog=build_catalog(),
        client_factory=lambda: client,
    )

    with pytest.raises(SourceBridgeError, match="boom"):
        service.build_feed("ft-home")

    second_xml = service.build_feed("ft-home")

    assert "<item>" not in second_xml
    assert [url for url, _, _ in client.calls] == ["https://www.ft.com/"]


def test_source_bridge_keeps_snapshot_when_discovery_is_empty(tmp_path: Path):
    settings = build_settings(tmp_path)
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    healthy_client = RecordingClient(
        {
            "https://www.ft.com/": '<html><body><a href="/content/story-1">Lead story</a></body></html>',
            "https://www.ft.com/content/story-1": """
                <html><head><title>Lead story</title></head><body><article>
                  <p>This cached story must remain available after an empty discovery response.</p>
                </article></body></html>
            """,
        }
    )
    service = SourceBridgeService(
        settings,
        repository,
        catalog=build_catalog(),
        client_factory=lambda: healthy_client,
    )
    original_items = service.refresh_source("ft-home")
    service.client_factory = lambda: RecordingClient(
        {"https://www.ft.com/": "<html><body>Sign in to continue</body></html>"}
    )

    with pytest.raises(SourceBridgeError, match="keeping the cached feed"):
        service.refresh_source("ft-home")

    retained_items = repository.list_synthetic_feed_items("ft-home")
    state = repository.get_synthetic_source_state("ft-home")
    assert retained_items == original_items
    assert state is not None
    assert state.last_successful_at is not None
    assert "keeping the cached feed" in (state.last_error or "")


def test_source_bridge_keeps_cached_body_when_revalidation_returns_a_login_page(
    tmp_path: Path,
):
    settings = build_settings(tmp_path)
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    homepage_html = '<html><body><a href="/content/story-1">Lead story</a></body></html>'
    healthy_client = RecordingClient(
        {
            "https://www.ft.com/": homepage_html,
            "https://www.ft.com/content/story-1": """
                <html><head><title>Lead story</title></head><body><article>
                  <p>The complete cached story has enough detail to remain useful to the reader.</p>
                  <p>This second paragraph keeps the full article above the extraction threshold.</p>
                </article></body></html>
            """,
        }
    )
    service = SourceBridgeService(
        settings,
        repository,
        catalog=build_catalog(),
        client_factory=lambda: healthy_client,
    )
    original_item = service.refresh_source("ft-home")[0]
    login_client = RecordingClient(
        {
            "https://www.ft.com/": homepage_html,
            "https://www.ft.com/content/story-1": (
                "<html><head><title>Sign in</title></head>"
                "<body>Please sign in to continue.</body></html>"
            ),
        }
    )
    service.client_factory = lambda: login_client

    retained_item = service.refresh_source("ft-home")[0]

    assert retained_item.title == original_item.title
    assert retained_item.content_html == original_item.content_html


def test_schedule_stale_refreshes_uses_lookahead_window(tmp_path: Path):
    settings = build_settings(tmp_path, refresh_seconds=900)
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    healthy_client = RecordingClient(
        {
            "https://www.ft.com/": '<html><body><a href="/content/story-1">Lead story</a></body></html>',
            "https://www.ft.com/content/story-1": """
                <html>
                  <head><title>Lead story</title></head>
                  <body><article><p>Long enough paragraph to remain in the cached synthetic feed body.</p><p>More body text here.</p></article></body>
                </html>
            """,
        }
    )
    service = SourceBridgeService(
        settings,
        repository,
        catalog=build_catalog(),
        client_factory=lambda: healthy_client,
    )
    service.build_feed("ft-home")

    due_soon_timestamp = (utc_now() - timedelta(seconds=850)).isoformat()
    with repository.database.connect() as connection:
        connection.execute(
            "UPDATE synthetic_source_state SET last_attempted_at = ? WHERE source_id = ?",
            (due_soon_timestamp, "ft-home"),
        )
        connection.commit()

    scheduled_refreshes: list[str] = []
    service._schedule_background_refresh = lambda source_id: scheduled_refreshes.append(source_id) or True  # type: ignore[method-assign]

    service.schedule_stale_refreshes(lookahead_seconds=60)

    assert scheduled_refreshes == ["ft-home"]


def test_probe_http_source_classifies_ft_access_and_subscribe_pages(tmp_path: Path):
    settings = build_settings(tmp_path)
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    client = RecordingClient(
        {
            "https://www.ft.com/": FakeResponse(
                """
                <html>
                  <head><title>Access Error</title></head>
                  <body>We detected potential misuse.</body>
                </html>
                """,
                status_code=403,
            ),
        }
    )
    service = SourceBridgeService(
        settings,
        repository,
        catalog=build_catalog(),
        client_factory=lambda: client,
    )

    results = service.probe_http_source("ft-home", limit=2)

    assert len(results) == 1
    assert results[0].stage == "start"
    assert results[0].page_state == "access_error"


def test_probe_http_source_detects_full_article_content(tmp_path: Path):
    settings = build_settings(tmp_path)
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    homepage_html = '<html><body><a href="/content/story-1">Lead story</a></body></html>'
    article_html = """
    <html>
      <head><title>Lead story</title></head>
      <body>
        <article>
          <p>This is a long first paragraph with enough detail to be treated as a real article body for probe purposes.</p>
          <p>This is a long second paragraph with enough detail to keep the extracted text above the full-content threshold in the probe classifier.</p>
          <p>This is a long third paragraph that makes the extracted body clearly substantial.</p>
          <p>This is a long fourth paragraph that removes any doubt that the probe saw a genuine article body rather than a teaser shell.</p>
        </article>
      </body>
    </html>
    """
    client = RecordingClient(
        {
            "https://www.ft.com/": homepage_html,
            "https://www.ft.com/content/story-1": article_html,
        }
    )
    service = SourceBridgeService(
        settings,
        repository,
        catalog=build_catalog(),
        client_factory=lambda: client,
    )

    results = service.probe_http_source("ft-home", limit=1)

    assert [result.stage for result in results] == ["start", "article"]
    assert results[1].page_state == "content"
    assert results[1].article_text_length >= 400


def test_probe_source_uses_configured_browser_backend(tmp_path: Path):
    settings = build_settings(tmp_path)
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    browser_client = RecordingBrowserClient(
        {
            "https://www.ft.com/": '<html><body><main><a href="/content/story-1">Lead story</a></main></body></html>',
            "https://www.ft.com/content/story-1": """
                <html>
                  <head><title>Lead story</title></head>
                  <body><main><article><p>This is a long first paragraph with enough detail to be treated as real article content in the configured backend probe.</p><p>This is a long second paragraph with enough detail to keep the extracted text above the content threshold for the configured backend probe.</p><p>This is a long third paragraph that makes the extracted body clearly substantial for the probe output.</p><p>This is a long fourth paragraph that removes any doubt that the probe saw a genuine article body rather than a teaser shell.</p></article></main></body>
                </html>
            """,
        }
    )
    browser_catalog = SourceCatalog(
        auth_profiles={
            "ft": AuthProfile(
                name="ft",
                domains=("ft.com",),
                cookie_header="session=abc123",
                browser_profile_path=tmp_path / "profiles" / "ft",
                browser_channel="chrome",
            )
        },
        sources={
            "ft-home": SourceDefinition(
                source_id="ft-home",
                title="FT Front Page",
                start_urls=("https://www.ft.com/",),
                include_url_patterns=(r"^https://www\.ft\.com/content/",),
                auth_profile="ft",
                fetch_backend="browser",
                browser_wait_until="load",
                browser_wait_for_selector="main",
                max_items=1,
            )
        },
    )
    service = SourceBridgeService(
        settings,
        repository,
        catalog=browser_catalog,
        browser_client_factory=lambda source, profile: browser_client,
    )

    results = service.probe_source("ft-home", limit=1)

    assert [result.stage for result in results] == ["start", "article"]
    assert results[1].page_state == "content"
    assert browser_client.calls == [
        ("https://www.ft.com/", {}, "session=abc123"),
        ("https://www.ft.com/", {}, "session=abc123"),
        ("https://www.ft.com/content/story-1", {}, "session=abc123"),
    ]


def test_source_bridge_uses_browser_backend_without_cookie_headers(tmp_path: Path):
    settings = build_settings(tmp_path)
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    browser_client = RecordingBrowserClient(
        {
            "https://www.ft.com/": '<html><body><main><a href="/content/story-1">Lead story</a></main></body></html>',
            "https://www.ft.com/content/story-1": """
                <html>
                  <head><title>Lead story</title></head>
                  <body><main><article><p>Long enough paragraph to remain in the synthetic feed body.</p><p>More body text here.</p></article></main></body>
                </html>
            """,
        }
    )
    browser_catalog = SourceCatalog(
        auth_profiles={
            "ft": AuthProfile(
                name="ft",
                domains=("ft.com",),
                cookie_header="session=abc123",
                browser_profile_path=tmp_path / "profiles" / "ft",
                browser_channel="chrome",
            )
        },
        sources={
            "ft-home": SourceDefinition(
                source_id="ft-home",
                title="FT Front Page",
                start_urls=("https://www.ft.com/",),
                include_url_patterns=(r"^https://www\.ft\.com/content/",),
                auth_profile="ft",
                fetch_backend="browser",
                browser_wait_until="load",
                browser_wait_for_selector="main",
                browser_settle_seconds=1,
                max_items=1,
            )
        },
    )
    service = SourceBridgeService(
        settings,
        repository,
        catalog=browser_catalog,
        browser_client_factory=lambda source, profile: browser_client,
    )

    xml = service.build_feed("ft-home")

    assert "<title>Lead story</title>" in xml
    assert browser_client.calls == [
        ("https://www.ft.com/", {}, "session=abc123"),
        ("https://www.ft.com/content/story-1", {}, "session=abc123"),
    ]


def test_browser_profile_serializes_refresh_and_article_extraction(tmp_path: Path):
    settings = build_settings(tmp_path)
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    base_catalog = build_catalog()
    browser_catalog = SourceCatalog(
        auth_profiles=base_catalog.auth_profiles,
        sources={
            "ft-home": replace(
                base_catalog.sources["ft-home"],
                fetch_backend="browser",
                max_items=1,
            )
        },
    )
    tracker = ClientConcurrencyTracker()
    responses = {
        "https://www.ft.com/": (
            '<html><body><a href="/content/story-1">Lead story</a></body></html>'
        ),
        "https://www.ft.com/content/story-1": """
            <html><head><title>Lead story</title></head><body><article>
              <p>This is a complete article used to verify that a refresh and an article extraction cannot use the same browser profile at the same time.</p>
              <p>The second paragraph keeps the extracted content above the required length.</p>
            </article></body></html>
        """,
    }
    service = SourceBridgeService(
        settings,
        repository,
        catalog=browser_catalog,
        browser_client_factory=lambda source, profile: CoordinatedClient(
            responses, tracker
        ),
        browser_concurrency=2,
    )
    errors: list[Exception] = []
    extracted_titles: list[str] = []
    extraction_started = threading.Event()
    refresh_thread = threading.Thread(
        target=run_thread_operation,
        args=(lambda: service.refresh_source("ft-home"), errors),
    )
    extraction_thread = threading.Thread(
        target=run_thread_operation,
        args=(
            lambda: extracted_titles.append(
                service.extract_article(
                    "https://www.ft.com/content/story-1"
                ).title
            ),
            errors,
        ),
        kwargs={"started": extraction_started},
    )

    refresh_thread.start()
    first_entered = tracker.first_entered.wait(timeout=2)
    if first_entered:
        extraction_thread.start()
        extraction_started.wait(timeout=1)
        second_entered_while_blocked = tracker.second_entered.wait(timeout=0.2)
    else:
        second_entered_while_blocked = False
    tracker.release_first.set()
    refresh_thread.join(timeout=3)
    if extraction_thread.ident is not None:
        extraction_thread.join(timeout=3)

    assert first_entered
    assert extraction_started.is_set()
    assert not second_entered_while_blocked
    assert not refresh_thread.is_alive()
    assert not extraction_thread.is_alive()
    assert errors == []
    assert extracted_titles == ["Lead story"]
    assert tracker.entry_count == 2
    assert tracker.max_active_count == 1


def test_browser_clients_respect_the_global_concurrency_limit(tmp_path: Path):
    settings = build_settings(tmp_path)
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    base_catalog = build_catalog()
    browser_catalog = SourceCatalog(
        auth_profiles={
            "ft": base_catalog.auth_profiles["ft"],
            "news": AuthProfile(
                name="news",
                domains=("news.example.com",),
                browser_profile_path=tmp_path / "profiles" / "news",
            ),
        },
        sources={
            "ft-home": replace(
                base_catalog.sources["ft-home"],
                fetch_backend="browser",
                max_items=1,
            ),
            "news-home": SourceDefinition(
                source_id="news-home",
                title="News Home",
                start_urls=("https://news.example.com/",),
                auth_profile="news",
                fetch_backend="browser",
                max_items=1,
            ),
        },
    )
    tracker = ClientConcurrencyTracker()

    def browser_client_factory(source, profile):
        return CoordinatedClient(
            {source.start_urls[0]: "<html><body>No links</body></html>"},
            tracker,
        )

    service = SourceBridgeService(
        settings,
        repository,
        catalog=browser_catalog,
        browser_client_factory=browser_client_factory,
        browser_concurrency=1,
    )
    errors: list[Exception] = []
    second_started = threading.Event()
    first_thread = threading.Thread(
        target=run_thread_operation,
        args=(lambda: service.discover_links("ft-home"), errors),
    )
    second_thread = threading.Thread(
        target=run_thread_operation,
        args=(lambda: service.discover_links("news-home"), errors),
        kwargs={"started": second_started},
    )

    first_thread.start()
    first_entered = tracker.first_entered.wait(timeout=2)
    if first_entered:
        second_thread.start()
        second_started.wait(timeout=1)
        second_entered_while_blocked = tracker.second_entered.wait(timeout=0.2)
    else:
        second_entered_while_blocked = False
    tracker.release_first.set()
    first_thread.join(timeout=3)
    if second_thread.ident is not None:
        second_thread.join(timeout=3)

    assert first_entered
    assert second_started.is_set()
    assert not second_entered_while_blocked
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    assert tracker.entry_count == 2
    assert tracker.max_active_count == 1


def test_discover_links_uses_browser_backend_client(tmp_path: Path):
    settings = build_settings(tmp_path)
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    browser_client = RecordingBrowserClient(
        {
            "https://www.ft.com/": '<html><body><main><a href="/content/story-1">Lead story</a></main></body></html>',
        }
    )
    browser_catalog = SourceCatalog(
        auth_profiles={
            "ft": AuthProfile(
                name="ft",
                domains=("ft.com",),
                cookie_header="session=abc123",
                browser_profile_path=tmp_path / "profiles" / "ft",
                browser_channel="chrome",
            )
        },
        sources={
            "ft-home": SourceDefinition(
                source_id="ft-home",
                title="FT Front Page",
                start_urls=("https://www.ft.com/",),
                include_url_patterns=(r"^https://www\.ft\.com/content/",),
                auth_profile="ft",
                fetch_backend="browser",
                browser_wait_until="load",
                browser_wait_for_selector="main",
                discovery_browser_wait_until="domcontentloaded",
                discovery_browser_wait_for_selector=None,
                discovery_browser_settle_seconds=1,
                max_items=5,
            )
        },
    )
    service = SourceBridgeService(
        settings,
        repository,
        catalog=browser_catalog,
        client_factory=lambda: (_ for _ in ()).throw(RuntimeError("http client should not be used")),
        browser_client_factory=lambda source, profile: browser_client,
    )

    links = service.discover_links("ft-home")

    assert [link.url for link in links] == ["https://www.ft.com/content/story-1"]
    assert browser_client.calls == [("https://www.ft.com/", {}, "session=abc123")]
    assert browser_client.wait_calls == [("https://www.ft.com/", "domcontentloaded", "", 1)]


def test_discover_links_accepts_browser_cdp_profile_without_profile_path(tmp_path: Path):
    settings = build_settings(tmp_path)
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    browser_client = RecordingBrowserClient(
        {
            "https://www.ft.com/": '<html><body><main><a href="/content/story-1">Lead story</a></main></body></html>',
        }
    )
    browser_catalog = SourceCatalog(
        auth_profiles={
            "ft": AuthProfile(
                name="ft",
                domains=("ft.com",),
                browser_cdp_url="http://browser-cdp:9222",
            )
        },
        sources={
            "ft-home": SourceDefinition(
                source_id="ft-home",
                title="FT Front Page",
                start_urls=("https://www.ft.com/",),
                include_url_patterns=(r"^https://www\.ft\.com/content/",),
                auth_profile="ft",
                fetch_backend="browser",
                max_items=5,
            )
        },
    )
    service = SourceBridgeService(
        settings,
        repository,
        catalog=browser_catalog,
        browser_client_factory=lambda source, profile: browser_client,
    )

    links = service.discover_links("ft-home")

    assert [link.url for link in links] == ["https://www.ft.com/content/story-1"]


def test_browser_backend_loads_cookie_header_from_cookie_file(tmp_path: Path):
    settings = build_settings(tmp_path)
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    cookie_file = tmp_path / "ft.cookie"
    cookie_file.write_text(".ft.com\tTRUE\t/\tTRUE\t0\tsession\tcookie-value\n", encoding="utf-8")
    browser_client = RecordingBrowserClient(
        {
            "https://www.ft.com/": '<html><body><main><a href="/content/story-1">Lead story</a></main></body></html>',
            "https://www.ft.com/content/story-1": """
                <html>
                  <head><title>Lead story</title></head>
                  <body><main><article><p>Long enough paragraph to remain in the synthetic feed body.</p><p>More body text here.</p></article></main></body>
                </html>
            """,
        }
    )
    browser_catalog = SourceCatalog(
        auth_profiles={
            "ft": AuthProfile(
                name="ft",
                domains=("ft.com",),
                cookie_jar_path=cookie_file,
                browser_profile_path=tmp_path / "profiles" / "ft",
                browser_channel="chrome",
            )
        },
        sources={
            "ft-home": SourceDefinition(
                source_id="ft-home",
                title="FT Front Page",
                start_urls=("https://www.ft.com/",),
                include_url_patterns=(r"^https://www\.ft\.com/content/",),
                auth_profile="ft",
                fetch_backend="browser",
                browser_wait_until="load",
                browser_wait_for_selector="main",
                max_items=1,
            )
        },
    )
    service = SourceBridgeService(
        settings,
        repository,
        catalog=browser_catalog,
        browser_client_factory=lambda source, profile: browser_client,
    )

    service.build_feed("ft-home")

    assert browser_client.calls == [
        ("https://www.ft.com/", {}, "session=cookie-value"),
        ("https://www.ft.com/content/story-1", {}, "session=cookie-value"),
    ]


def test_source_bridge_uses_distinct_discovery_and_article_browser_waits(tmp_path: Path):
    settings = build_settings(tmp_path)
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    browser_client = RecordingBrowserClient(
        {
            "https://www.ft.com/news-feed": '<html><body><a href="/content/story-1">Lead story</a></body></html>',
            "https://www.ft.com/content/story-1": """
                <html>
                  <head><title>Lead story</title></head>
                  <body><main><article><p>Long enough paragraph to remain in the synthetic feed body.</p><p>More body text here.</p></article></main></body>
                </html>
            """,
        }
    )
    browser_catalog = SourceCatalog(
        auth_profiles={
            "ft": AuthProfile(
                name="ft",
                domains=("ft.com",),
                cookie_header="session=abc123",
                browser_profile_path=tmp_path / "profiles" / "ft",
                browser_channel="chrome",
            )
        },
        sources={
            "ft-home": SourceDefinition(
                source_id="ft-home",
                title="FT Front Page",
                start_urls=("https://www.ft.com/news-feed",),
                include_url_patterns=(r"^https://www\.ft\.com/content/",),
                auth_profile="ft",
                fetch_backend="browser",
                browser_wait_until="load",
                browser_wait_for_selector="article, main",
                browser_settle_seconds=3,
                discovery_browser_wait_until="domcontentloaded",
                discovery_browser_wait_for_selector=None,
                discovery_browser_settle_seconds=1,
                max_items=1,
            )
        },
    )
    service = SourceBridgeService(
        settings,
        repository,
        catalog=browser_catalog,
        browser_client_factory=lambda source, profile: browser_client,
    )

    xml = service.build_feed("ft-home")

    assert "<title>Lead story</title>" in xml
    assert browser_client.wait_calls == [
        ("https://www.ft.com/news-feed", "domcontentloaded", "", 1),
        ("https://www.ft.com/content/story-1", "load", "article, main", 3),
    ]


def test_source_bridge_can_scope_discovery_to_page_body(tmp_path: Path):
    settings = build_settings(tmp_path)
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    client = RecordingClient(
        {
            "https://www.ft.com/news-feed": """
                <html>
                  <body>
                    <header>
                      <a href="/content/header-story">Header story</a>
                    </header>
                    <div id="site-content">
                      <a href="/content/body-story">Body story</a>
                    </div>
                  </body>
                </html>
            """,
        }
    )
    catalog = SourceCatalog(
        auth_profiles={
            "ft": AuthProfile(
                name="ft",
                domains=("ft.com",),
                cookie_header="session=abc123",
                browser_profile_path=tmp_path / "profiles" / "ft",
                browser_channel="chrome",
            )
        },
        sources={
            "ft-home": SourceDefinition(
                source_id="ft-home",
                title="FT Front Page",
                start_urls=("https://www.ft.com/news-feed",),
                link_selector="#site-content a[href]",
                include_url_patterns=(r"^https://www\.ft\.com/content/",),
                auth_profile="ft",
                max_items=5,
            )
        },
    )
    service = SourceBridgeService(
        settings,
        repository,
        catalog=catalog,
        client_factory=lambda: client,
    )

    links = service.discover_links("ft-home")

    assert [link.url for link in links] == ["https://www.ft.com/content/body-story"]


def test_source_bridge_can_extract_article_for_matching_url(tmp_path: Path):
    settings = build_settings(tmp_path)
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    client = RecordingClient(
        {
            "https://www.ft.com/content/story-1": """
                <html>
                  <head>
                    <title>Lead story</title>
                    <meta property="article:published_time" content="2026-03-30T12:00:00Z" />
                  </head>
                  <body>
                    <article>
                      <p>This is the first paragraph of the lead story and it is long enough to count as real article content.</p>
                      <p>This is the second paragraph of the lead story and it keeps the extracted body comfortably above the minimum threshold.</p>
                    </article>
                  </body>
                </html>
            """,
        }
    )
    service = SourceBridgeService(
        settings,
        repository,
        catalog=build_catalog(),
        client_factory=lambda: client,
    )

    article = service.extract_article("https://www.ft.com/content/story-1", fallback_title="Lead story")

    assert article.source_id == "ft-home"
    assert article.article_url == "https://www.ft.com/content/story-1"
    assert article.title == "Lead story"
    assert "content:encoded" not in article.content_html
    assert "first paragraph of the lead story" in article.content_html


def test_source_bridge_extracts_stored_full_content_without_fetching(tmp_path: Path):
    settings = build_settings(tmp_path)
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    article_url = "https://www.ft.com/content/story-1?edition=uk"
    repository.replace_synthetic_feed_items(
        "ft-home",
        [
            SyntheticFeedItem(
                source_id="ft-home",
                item_id=stable_hash(article_url),
                article_url=article_url,
                title="Stored lead story",
                summary_text="Stored summary",
                content_html="<article><p>Stored full article.</p></article>",
                published_at="2026-03-30T12:00:00+00:00",
                source_page_url="https://www.ft.com/",
                sort_index=0,
                discovered_at="2026-03-30T12:01:00+00:00",
            )
        ],
    )
    client = RecordingClient({})
    factory_calls: list[bool] = []

    def client_factory():
        factory_calls.append(True)
        return client

    service = SourceBridgeService(
        settings,
        repository,
        catalog=build_catalog(),
        client_factory=client_factory,
    )

    article = service.extract_article(f"{article_url}#reader")

    assert article.article_url == article_url
    assert article.title == "Stored lead story"
    assert article.content_html == "<article><p>Stored full article.</p></article>"
    assert factory_calls == []
    assert client.calls == []
