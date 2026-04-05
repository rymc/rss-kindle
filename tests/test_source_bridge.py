from pathlib import Path
from datetime import timedelta

from app.config import Settings
from app.db import Database
from app.repository import Repository
from app.source_bridge import AuthProfile, SourceBridgeService, SourceCatalog, SourceDefinition
from app.utils import utc_now


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

    assert "<title>FT Front Page</title>" in xml
    assert "<title>Lead story</title>" in xml
    assert "content:encoded" in xml
    assert "World hub" not in xml
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
