from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.source_main import create_app


class FakeSourceBridge:
    def __init__(self):
        self.schedule_calls: list[float] = []

    def list_sources(self):
        return [{"id": "ft-home"}]

    def build_feed(self, source_id: str) -> str:
        if source_id != "ft-home":
            raise RuntimeError("missing source")
        return "<?xml version='1.0'?><rss version='2.0'><channel><title>FT Front Page</title></channel></rss>"

    def extract_article(self, url: str, *, fallback_title: str | None = None):
        if url != "https://www.ft.com/content/story-1":
            raise RuntimeError("missing article")

        class Article:
            source_id = "ft-home"
            article_url = url
            title = fallback_title or "Lead story"
            content_html = "<article><h1>Lead story</h1><p>Body text.</p></article>"
            summary_text = "Body text."
            published_at = "2026-03-30T12:00:00+00:00"

        return Article()

    def schedule_stale_refreshes(self, *, lookahead_seconds: float = 0.0):
        self.schedule_calls.append(lookahead_seconds)
        return []


def build_settings(tmp_path: Path) -> Settings:
    return Settings(
        app_name="RSS Kindle",
        base_dir=tmp_path,
        database_path=tmp_path / "source-main.db",
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=15,
        metadata_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )


def test_source_main_serves_synthetic_feed(tmp_path: Path):
    app = create_app(build_settings(tmp_path), source_bridge=FakeSourceBridge())
    client = TestClient(app)

    response = client.get("/synthetic/ft-home.xml")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/rss+xml")
    assert "FT Front Page" in response.text


def test_source_main_requires_token_when_configured(tmp_path: Path):
    settings = Settings(
        **{**build_settings(tmp_path).__dict__, "source_bridge_access_token": "bridge-token"}
    )
    app = create_app(settings, source_bridge=FakeSourceBridge())
    client = TestClient(app)

    health = client.get("/health")
    assert health.status_code == 200

    blocked_feed = client.get("/synthetic/ft-home.xml")
    assert blocked_feed.status_code == 401

    allowed_feed = client.get(
        "/synthetic/ft-home.xml",
        headers={"X-Source-Bridge-Token": "bridge-token"},
    )
    assert allowed_feed.status_code == 200

    blocked_extract = client.get("/extract", params={"url": "https://www.ft.com/content/story-1"})
    assert blocked_extract.status_code == 401

    allowed_extract = client.get(
        "/extract",
        params={"url": "https://www.ft.com/content/story-1", "access_token": "bridge-token"},
    )
    assert allowed_extract.status_code == 200


def test_source_main_allows_configured_internal_bridge_host(tmp_path: Path):
    settings = Settings(
        **{
            **build_settings(tmp_path).__dict__,
            "app_allowed_hosts": ("reader.example.com",),
            "source_bridge_api_url": "http://source-bridge:8100",
            "source_bridge_access_token": "bridge-token",
        }
    )
    app = create_app(settings, source_bridge=FakeSourceBridge())
    allowed_client = TestClient(app, base_url="http://source-bridge:8100")

    allowed_extract = allowed_client.get(
        "/extract",
        params={"url": "https://www.ft.com/content/story-1"},
        headers={"X-Source-Bridge-Token": "bridge-token"},
    )
    assert allowed_extract.status_code == 200

    blocked_client = TestClient(app, base_url="http://untrusted.internal:8100")
    blocked_extract = blocked_client.get(
        "/extract",
        params={"url": "https://www.ft.com/content/story-1"},
        headers={"X-Source-Bridge-Token": "bridge-token"},
    )
    assert blocked_extract.status_code == 400


def test_source_main_serves_extracted_article_json(tmp_path: Path):
    app = create_app(build_settings(tmp_path), source_bridge=FakeSourceBridge())
    client = TestClient(app)

    response = client.get(
        "/extract",
        params={"url": "https://www.ft.com/content/story-1", "title": "Lead story"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["source_id"] == "ft-home"
    assert payload["article_url"] == "https://www.ft.com/content/story-1"
    assert "Body text." in payload["content_html"]


def test_source_main_starts_prewarm_on_startup(tmp_path: Path):
    settings = Settings(
        **{
            **build_settings(tmp_path).__dict__,
            "source_bridge_prewarm_enabled": True,
            "source_bridge_prewarm_interval_seconds": 7,
        }
    )
    source_bridge = FakeSourceBridge()

    with TestClient(create_app(settings, source_bridge=source_bridge)):
        pass

    assert source_bridge.schedule_calls
    assert source_bridge.schedule_calls[0] == 7
