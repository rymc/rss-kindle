import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

from app.config import Settings
from app.content_service import ArticleExtractor
from app.db import Database
from app.freshrss import FreshRSSEntry
from app.repository import Repository

TEST_BASE_DIR = Path(__file__).resolve().parent.parent


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200, json_payload: dict | None = None):
        self.text = text
        self.status_code = status_code
        self._json_payload = json_payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if self._json_payload is None:
            raise ValueError("No JSON payload")
        return self._json_payload


class FakeClient:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.response


def fail_if_called():
    raise AssertionError("network fetch should not be used")


def build_settings(tmp_path: Path) -> Settings:
    return Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=tmp_path / "content.db",
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=15,
        metadata_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )


def build_entry() -> FreshRSSEntry:
    return FreshRSSEntry(
        id="entry-1",
        title="Substack Story",
        author="Writer",
        url="https://example.substack.com/p/story",
        published_at="2026-03-29T10:00:00+00:00",
        summary_html="<p>Read more</p>",
        summary_text="Read more",
        content_html="<p>Read more</p>",
        feed_title="Substack Feed",
        feed_site_url="https://example.com",
        feed_token="feed-token",
        group_names=("Tech",),
        is_starred=False,
    )


def test_substack_available_content_is_preferred(tmp_path: Path):
    repository = Repository(Database(tmp_path / "content.db"))
    repository.initialize()
    settings = build_settings(tmp_path)
    html = """
    <html>
      <body>
        <button>Read distraction-free on Substack</button>
        <div class="available-content">
          <div class="body markup">
            <p>Actual first paragraph.</p>
            <p>Actual second paragraph.</p>
          </div>
        </div>
      </body>
    </html>
    """
    extractor = ArticleExtractor(
        settings,
        repository,
        client_factory=lambda: FakeClient(FakeResponse(html)),
    )

    article = extractor.ensure_extracted(build_entry())

    assert article.extraction_status == "success"
    assert "Actual first paragraph." in article.html
    assert "Read distraction-free on Substack" not in article.html


def test_failed_extraction_is_cached_and_reused(tmp_path: Path):
    repository = Repository(Database(tmp_path / "content.db"))
    repository.initialize()
    settings = build_settings(tmp_path)
    html = """
    <html>
      <body>
        <div class="available-content"><div class="body markup"></div></div>
        <div class="paywall">This post is for paid subscribers</div>
      </body>
    </html>
    """
    extractor = ArticleExtractor(
        settings,
        repository,
        client_factory=lambda: FakeClient(FakeResponse(html)),
    )
    entry = build_entry()

    first = extractor.ensure_extracted(entry)
    second = extractor.ensure_extracted(entry)

    assert first.extraction_status == "failed"
    assert "paywalled" in (first.error_message or "").lower()
    assert second.extraction_status == "failed"
    assert repository.get_cached_article(entry.id, entry.url) is not None


def test_failed_extraction_retries_after_the_backoff_and_can_recover(
    tmp_path: Path,
):
    repository = Repository(Database(tmp_path / "content.db"))
    repository.initialize()
    settings = build_settings(tmp_path)
    failed_client = FakeClient(FakeResponse("", status_code=503))
    extractor = ArticleExtractor(
        settings,
        repository,
        client_factory=lambda: failed_client,
    )
    entry = build_entry()

    first = extractor.ensure_extracted(entry)
    second = extractor.ensure_extracted(entry)

    assert first.extraction_status == "failed"
    assert second.extraction_status == "failed"
    assert len(failed_client.calls) == 1

    with sqlite3.connect(repository.database.path) as connection:
        connection.execute(
            "UPDATE article_cache SET extracted_at = ? WHERE entry_id = ?",
            ("2020-01-01T00:00:00+00:00", entry.id),
        )
        connection.commit()
    recovered_html = """
    <html><body><article>
      <p>A recovered article paragraph with enough useful detail for the reader.</p>
      <p>A second paragraph confirms that the retry succeeded.</p>
    </article></body></html>
    """
    recovered_client = FakeClient(FakeResponse(recovered_html))
    recovered = ArticleExtractor(
        settings,
        repository,
        client_factory=lambda: recovered_client,
    ).ensure_extracted(entry)

    assert recovered.extraction_status == "success"
    assert "retry succeeded" in recovered.html
    assert len(recovered_client.calls) == 1


def test_concurrent_article_requests_share_one_extraction(tmp_path: Path):
    repository = Repository(Database(tmp_path / "content.db"))
    repository.initialize()
    settings = build_settings(tmp_path)
    fetch_started = threading.Event()
    release_fetch = threading.Event()

    class BlockingClient(FakeClient):
        def get(self, url, **kwargs):
            self.calls.append({"url": url, **kwargs})
            fetch_started.set()
            release_fetch.wait(timeout=2)
            return self.response

    client = BlockingClient(
        FakeResponse(
            "<html><body><article><p>A complete shared extraction paragraph with useful detail.</p>"
            "<p>A second paragraph makes this a meaningful article.</p></article></body></html>"
        )
    )
    extractor = ArticleExtractor(
        settings,
        repository,
        client_factory=lambda: client,
    )
    results = []

    first = threading.Thread(target=lambda: results.append(extractor.ensure_extracted(build_entry())))
    second = threading.Thread(target=lambda: results.append(extractor.ensure_extracted(build_entry())))
    first.start()
    assert fetch_started.wait(timeout=1)
    second.start()
    release_fetch.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert len(results) == 2
    assert all(article.extraction_status == "success" for article in results)
    assert len(client.calls) == 1


def test_successful_cache_hit_skips_feed_and_network_extraction(
    tmp_path: Path,
    monkeypatch,
):
    repository = Repository(Database(tmp_path / "content.db"))
    repository.initialize()
    settings = build_settings(tmp_path)
    entry = build_entry()
    initial_html = """
    <html><body><div class="available-content"><div class="body markup">
      <p>A complete cached paragraph with enough useful article text to remain valid.</p>
      <p>A second cached paragraph provides more detail for the reader.</p>
    </div></div></body></html>
    """
    ArticleExtractor(
        settings,
        repository,
        client_factory=lambda: FakeClient(FakeResponse(initial_html)),
    ).ensure_extracted(entry)
    extractor = ArticleExtractor(
        settings,
        repository,
        client_factory=fail_if_called,
    )
    monkeypatch.setattr(
        extractor,
        "_extract_feed_content",
        lambda _entry: (_ for _ in ()).throw(
            AssertionError("feed extraction should not run for a valid cache hit")
        ),
    )

    article = extractor.ensure_extracted(entry)

    assert article.extraction_status == "success"
    assert "complete cached paragraph" in article.html


def test_changed_feed_content_replaces_successful_cached_article(tmp_path: Path):
    repository = Repository(Database(tmp_path / "content.db"))
    repository.initialize()
    settings = build_settings(tmp_path)
    old_entry = replace(
        build_entry(),
        content_html=(
            "<article><p>The original full article has enough useful text for the reader.</p>"
            "<p>This second paragraph makes the feed content complete.</p></article>"
        ),
    )
    extractor = ArticleExtractor(settings, repository, client_factory=fail_if_called)
    original = extractor.ensure_extracted(old_entry)
    updated_entry = replace(
        old_entry,
        content_html=(
            "<article><p>The corrected full article now contains updated facts for the reader.</p>"
            "<p>This second paragraph confirms that the publisher changed the content.</p></article>"
        ),
    )

    corrected = extractor.ensure_extracted(updated_entry)

    assert "original full article" in original.html
    assert "corrected full article" in corrected.html
    assert "original full article" not in corrected.html


def test_promo_only_extraction_falls_back_instead_of_caching_success(tmp_path: Path):
    repository = Repository(Database(tmp_path / "content.db"))
    repository.initialize()
    settings = build_settings(tmp_path)
    html = """
    <html>
      <head><title>Edit PDFs Easily and Securely</title></head>
      <body>
        <article>
          <h1>Edit PDFs Easily and Securely</h1>
          <h3>BreezePDF PRO</h3>
          <p>Unlimited downloads, desktop app, CLI tools, PDF signing &amp; OCR — <strong>$12/mo</strong></p>
        </article>
      </body>
    </html>
    """
    extractor = ArticleExtractor(
        settings,
        repository,
        client_factory=lambda: FakeClient(FakeResponse(html)),
    )
    entry = FreshRSSEntry(
        id="entry-2",
        title="Show HN: BreezePDF – Free, in-browser PDF editor",
        author="Writer",
        url="https://breezepdf.com/?v=3",
        published_at="2026-03-29T10:00:00+00:00",
        summary_html="<p><a href=\"https://news.ycombinator.com/item?id=1\">Comments</a></p>",
        summary_text="Comments",
        content_html="<p><a href=\"https://news.ycombinator.com/item?id=1\">Comments</a></p>",
        feed_title="Hacker News",
        feed_site_url="https://news.ycombinator.com",
        feed_token="feed-token",
        group_names=("Tech",),
        is_starred=False,
    )

    article = extractor.ensure_extracted(entry)

    assert article.extraction_status == "failed"
    assert "too little meaningful article text" in (article.error_message or "").lower()
    assert "Open the source article" in article.html


def test_meaningful_feed_content_is_preferred_over_direct_fetch(tmp_path: Path):
    repository = Repository(Database(tmp_path / "content.db"))
    repository.initialize()
    settings = build_settings(tmp_path)
    entry = FreshRSSEntry(
        id="entry-3",
        title="FT synthetic story",
        author="Reporter",
        url="https://www.ft.com/content/example",
        published_at="2026-03-29T10:00:00+00:00",
        summary_html="<p>Short summary</p>",
        summary_text="Short summary",
        content_html="<article><h1>FT synthetic story</h1><p>First real paragraph with substantial detail about the story and what happened in the market today.</p><p>Second paragraph with more context and analysis for readers.</p></article>",
        feed_title="FT synthetic",
        feed_site_url="https://www.ft.com",
        feed_token="feed-token",
        group_names=("World",),
        is_starred=False,
    )
    extractor = ArticleExtractor(
        settings,
        repository,
        client_factory=fail_if_called,
    )

    article = extractor.ensure_extracted(entry)

    assert article.extraction_status == "success"
    assert "First real paragraph" in article.html
    assert article.error_message is None


def test_failed_cache_is_replaced_when_feed_content_becomes_meaningful(tmp_path: Path):
    repository = Repository(Database(tmp_path / "content.db"))
    repository.initialize()
    settings = build_settings(tmp_path)
    entry = FreshRSSEntry(
        id="entry-4",
        title="Recovered story",
        author="Reporter",
        url="https://www.ft.com/content/recovered",
        published_at="2026-03-29T10:00:00+00:00",
        summary_html="<p>Older teaser</p>",
        summary_text="Older teaser",
        content_html="<article><h1>Recovered story</h1><p>Fresh full text from the synthetic feed with enough detail to qualify as the article body.</p><p>More context in a second paragraph.</p></article>",
        feed_title="FT synthetic",
        feed_site_url="https://www.ft.com",
        feed_token="feed-token",
        group_names=("World",),
        is_starred=False,
    )
    repository.save_cached_article(
        entry.id,
        source_url=entry.url,
        extracted_html="<article><h1>Recovered story</h1><p>HTTP 403</p></article>",
        extraction_status="failed",
        error_message="HTTP 403",
    )
    extractor = ArticleExtractor(
        settings,
        repository,
        client_factory=fail_if_called,
    )

    article = extractor.ensure_extracted(entry)
    cached = repository.get_cached_article(entry.id, entry.url)

    assert article.extraction_status == "success"
    assert "Fresh full text from the synthetic feed" in article.html
    assert cached is not None
    assert cached.extraction_status == "success"
    assert "Fresh full text from the synthetic feed" in (cached.extracted_html or "")


def test_source_bridge_article_fetch_is_used_when_feed_only_has_teaser(tmp_path: Path):
    repository = Repository(Database(tmp_path / "content.db"))
    repository.initialize()
    settings = build_settings(tmp_path)
    settings = Settings(
        **{
            **settings.__dict__,
            "source_bridge_api_url": "http://source-bridge:8100",
            "source_bridge_access_token": "bridge-token",
        }
    )
    entry = FreshRSSEntry(
        id="entry-5",
        title="Bridge recovered story",
        author="Reporter",
        url="https://www.ft.com/content/recovered-by-bridge",
        published_at="2026-03-29T10:00:00+00:00",
        summary_html="<p>Short teaser</p>",
        summary_text="Short teaser",
        content_html="<p>Complete digital access to quality FT journalism with expert analysis from industry leaders. Pay a year upfront and save 20%.</p>",
        feed_title="FT synthetic",
        feed_site_url="https://www.ft.com",
        feed_token="feed-token",
        group_names=("World",),
        is_starred=False,
    )
    repository.save_cached_article(
        entry.id,
        source_url=entry.url,
        extracted_html="<article><h1>Bridge recovered story</h1><p>HTTP 403</p></article>",
        extraction_status="failed",
        error_message="HTTP 403",
    )
    bridge_response = FakeResponse(
        "",
        json_payload={
            "source_id": "ft-home",
            "article_url": entry.url,
            "title": entry.title,
            "content_html": "<article><h1>Bridge recovered story</h1><p>Recovered from the authenticated source bridge instead of the stale teaser content in FreshRSS.</p><p>Second paragraph with extra detail.</p></article>",
            "summary_text": "Recovered from the authenticated source bridge.",
            "published_at": "2026-03-30T12:00:00+00:00",
        },
    )
    extractor = ArticleExtractor(
        settings,
        repository,
        client_factory=fail_if_called,
        bridge_client_factory=lambda: FakeClient(bridge_response),
    )

    article = extractor.ensure_extracted(entry)

    assert article.extraction_status == "success"
    assert "Recovered from the authenticated source bridge" in article.html
    assert "Complete digital access to quality FT journalism" not in article.html


def test_source_bridge_token_is_forwarded_when_configured(tmp_path: Path):
    repository = Repository(Database(tmp_path / "content.db"))
    repository.initialize()
    settings = build_settings(tmp_path)
    settings = Settings(
        **{
            **settings.__dict__,
            "source_bridge_api_url": "http://source-bridge:8100",
            "source_bridge_access_token": "bridge-token",
        }
    )
    entry = FreshRSSEntry(
        id="entry-6",
        title="Bridge token story",
        author="Reporter",
        url="https://www.ft.com/content/token-story",
        published_at="2026-03-29T10:00:00+00:00",
        summary_html="<p>Short teaser</p>",
        summary_text="Short teaser",
        content_html="<p>Short teaser</p>",
        feed_title="FT synthetic",
        feed_site_url="https://www.ft.com",
        feed_token="feed-token",
        group_names=("World",),
        is_starred=False,
    )
    bridge_client = FakeClient(
        FakeResponse(
            "",
            json_payload={
                "source_id": "ft-home",
                "article_url": entry.url,
                "title": entry.title,
                "content_html": "<article><h1>Bridge token story</h1><p>Recovered body paragraph one with enough detail to count.</p><p>Recovered body paragraph two with more detail.</p></article>",
                "summary_text": "Recovered body",
                "published_at": "2026-03-30T12:00:00+00:00",
            },
        )
    )
    extractor = ArticleExtractor(
        settings,
        repository,
        client_factory=fail_if_called,
        bridge_client_factory=lambda: bridge_client,
    )

    article = extractor.ensure_extracted(entry)

    assert article.extraction_status == "success"
    assert bridge_client.calls
    assert bridge_client.calls[0]["headers"] == {"X-Source-Bridge-Token": "bridge-token"}


def test_default_source_bridge_client_uses_the_bridge_timeout(
    tmp_path: Path,
    monkeypatch,
):
    repository = Repository(Database(tmp_path / "content.db"))
    repository.initialize()
    settings = replace(build_settings(tmp_path), source_bridge_timeout_seconds=17)
    created_timeouts = []

    class CapturingHttpClient:
        def __init__(self, **kwargs):
            created_timeouts.append(kwargs["timeout"])

        def close(self):
            return None

    monkeypatch.setattr("app.content_service.httpx.Client", CapturingHttpClient)
    extractor = ArticleExtractor(settings, repository)

    try:
        assert created_timeouts[0] == settings.http_timeout_seconds
        assert created_timeouts[1].read == settings.source_bridge_timeout_seconds
    finally:
        extractor.close()
