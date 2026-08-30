import base64
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.source_main import create_app


class FakeSourceBridge:
    def __init__(self):
        self.schedule_calls: list[float] = []
        self.refresh_calls: list[str] = []

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

    def list_source_status(self):
        return [
            {
                "id": "ft-home",
                "title": "FT Front Page",
                "fetch_backend": "browser",
                "item_count": 12,
                "last_attempted_at": "2026-08-29T10:00:00+00:00",
                "last_successful_at": "2026-08-29T10:00:00+00:00",
                "last_error": None,
                "refreshing": False,
            }
        ]

    def schedule_refresh(self, source_id: str):
        self.refresh_calls.append(source_id)
        return True


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
    settings = Settings(
        **{**build_settings(tmp_path).__dict__, "source_bridge_access_token": "bridge-token"}
    )
    app = create_app(settings, source_bridge=FakeSourceBridge())
    client = TestClient(app)

    response = client.get(
        "/synthetic/ft-home.xml",
        headers={"X-Source-Bridge-Token": "bridge-token"},
    )

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
    assert health.json() == {"status": "ok"}

    blocked_status = client.get("/status")
    assert blocked_status.status_code == 401

    allowed_status = client.get("/status", headers={"X-Source-Bridge-Token": "bridge-token"})
    assert allowed_status.status_code == 200

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


def test_source_main_accepts_freshrss_http_basic_credentials(tmp_path: Path):
    settings = Settings(
        **{**build_settings(tmp_path).__dict__, "source_bridge_access_token": "bridge-token"}
    )
    app = create_app(settings, source_bridge=FakeSourceBridge())
    client = TestClient(app)
    credentials = base64.b64encode(b"source-bridge:bridge-token").decode("ascii")

    allowed_feed = client.get(
        "/synthetic/ft-home.xml",
        headers={"Authorization": f"Basic {credentials}"},
    )
    wrong_user = base64.b64encode(b"other:bridge-token").decode("ascii")
    blocked_feed = client.get(
        "/synthetic/ft-home.xml",
        headers={"Authorization": f"Basic {wrong_user}"},
    )
    malformed_feed = client.get(
        "/synthetic/ft-home.xml",
        headers={"Authorization": "Basic not-base64"},
    )

    assert allowed_feed.status_code == 200
    assert blocked_feed.status_code == 401
    assert malformed_feed.status_code == 401


def test_source_main_fails_closed_when_token_is_not_configured(tmp_path: Path):
    source_bridge = FakeSourceBridge()
    app = create_app(build_settings(tmp_path), source_bridge=source_bridge)
    client = TestClient(app)

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

    blocked_requests = [
        client.get("/sources"),
        client.get("/status"),
        client.post("/sources/ft-home/refresh"),
        client.get("/synthetic/ft-home.xml"),
        client.get(
            "/extract",
            params={"url": "https://www.ft.com/content/story-1"},
            headers={"X-Source-Bridge-Token": "unconfigured-token"},
        ),
    ]

    assert {response.status_code for response in blocked_requests} == {401}
    assert source_bridge.refresh_calls == []


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

    health_client = TestClient(app, base_url="http://127.0.0.1:8100")
    assert health_client.get("/health").status_code == 200

    blocked_client = TestClient(app, base_url="http://untrusted.internal:8100")
    blocked_extract = blocked_client.get(
        "/extract",
        params={"url": "https://www.ft.com/content/story-1"},
        headers={"X-Source-Bridge-Token": "bridge-token"},
    )
    assert blocked_extract.status_code == 400


def test_source_main_serves_extracted_article_json(tmp_path: Path):
    settings = Settings(
        **{**build_settings(tmp_path).__dict__, "source_bridge_access_token": "bridge-token"}
    )
    app = create_app(settings, source_bridge=FakeSourceBridge())
    client = TestClient(app)

    response = client.get(
        "/extract",
        params={"url": "https://www.ft.com/content/story-1", "title": "Lead story"},
        headers={"X-Source-Bridge-Token": "bridge-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["source_id"] == "ft-home"
    assert payload["article_url"] == "https://www.ft.com/content/story-1"
    assert "Body text." in payload["content_html"]


def test_source_main_reports_status_and_schedules_manual_refresh(tmp_path: Path):
    source_bridge = FakeSourceBridge()
    settings = Settings(
        **{**build_settings(tmp_path).__dict__, "source_bridge_access_token": "bridge-token"}
    )
    app = create_app(settings, source_bridge=source_bridge)
    client = TestClient(app)

    headers = {"X-Source-Bridge-Token": "bridge-token"}
    status = client.get("/status", headers=headers)
    refresh = client.post("/sources/ft-home/refresh", headers=headers)

    assert status.status_code == 200
    assert status.json()[0]["item_count"] == 12
    assert refresh.status_code == 202
    assert refresh.json() == {"source_id": "ft-home", "scheduled": True}
    assert source_bridge.refresh_calls == ["ft-home"]


def test_source_main_starts_prewarm_on_startup(tmp_path: Path):
    settings = Settings(
        **{
            **build_settings(tmp_path).__dict__,
            "source_bridge_access_token": "bridge-token",
            "source_bridge_prewarm_enabled": True,
            "source_bridge_prewarm_interval_seconds": 7,
        }
    )
    source_bridge = FakeSourceBridge()

    with TestClient(create_app(settings, source_bridge=source_bridge)):
        pass

    assert source_bridge.schedule_calls
    assert source_bridge.schedule_calls[0] == 7


def test_source_main_does_not_prewarm_without_access_token(tmp_path: Path):
    source_bridge = FakeSourceBridge()

    with TestClient(create_app(build_settings(tmp_path), source_bridge=source_bridge)):
        pass

    assert source_bridge.schedule_calls == []
