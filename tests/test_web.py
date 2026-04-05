import re
from dataclasses import dataclass, replace
from pathlib import Path

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.freshrss import FreshRSSEntry, FreshRSSFeed, FreshRSSGroup, FreshRSSNavigation, FreshRSSStreamPage, encode_feed_token
from app.main import create_app
from app.repository import Repository

TEST_BASE_DIR = Path(__file__).resolve().parent.parent


class FakeFreshRSSClient:
    def __init__(self, navigation: FreshRSSNavigation, stream_pages: dict[tuple[str, str | None, str | None], FreshRSSStreamPage]):
        self.navigation = navigation
        self.stream_pages = stream_pages
        self.entry_map = {
            entry.id: entry
            for page in stream_pages.values()
            for entry in page.entries
        }
        self.read_ids: set[str] = set()
        self.starred_ids: set[str] = set()
        self.read_calls: list[list[str]] = []
        self.unread_calls: list[list[str]] = []
        self.star_calls: list[list[str]] = []
        self.unstar_calls: list[list[str]] = []

    def list_navigation(self):
        return self.navigation

    def get_stream(self, *, scope_kind: str, scope_value: str | None = None, continuation: str | None = None, limit: int = 15):
        page = self.stream_pages[(scope_kind, scope_value, continuation)]
        unread_entries = [
            replace(entry, is_starred=entry.id in self.starred_ids)
            for entry in page.entries
            if entry.id not in self.read_ids
        ]
        return FreshRSSStreamPage(entries=unread_entries, continuation=page.continuation)

    def get_entry(self, entry_id: str):
        entry = self.entry_map.get(entry_id)
        if entry is None:
            return None
        return replace(entry, is_starred=entry_id in self.starred_ids)

    def mark_read(self, entry_ids):
        values = [str(entry_id) for entry_id in entry_ids]
        self.read_calls.append(values)
        self.read_ids.update(values)

    def mark_unread(self, entry_ids):
        values = [str(entry_id) for entry_id in entry_ids]
        self.unread_calls.append(values)
        for entry_id in values:
            self.read_ids.discard(entry_id)

    def mark_starred(self, entry_ids):
        values = [str(entry_id) for entry_id in entry_ids]
        self.star_calls.append(values)
        self.starred_ids.update(values)

    def mark_unstarred(self, entry_ids):
        values = [str(entry_id) for entry_id in entry_ids]
        self.unstar_calls.append(values)
        for entry_id in values:
            self.starred_ids.discard(entry_id)

    def get_group(self, slug: str):
        for group in self.navigation.groups:
            if group.slug == slug:
                return group
        return None

    def get_feed(self, token: str):
        for feed in self.navigation.feeds:
            if feed.token == token:
                return feed
        return None


class FakeExtractor:
    def ensure_extracted(self, entry: FreshRSSEntry):
        html = f"<article><h1>{entry.title}</h1><p>Clean body for {entry.title}</p></article>"
        text = f"Clean body for {entry.title}"

        @dataclass(frozen=True)
        class Result:
            html: str
            text: str
            extraction_status: str
            error_message: str | None

        return Result(
            html=html,
            text=text,
            extraction_status="success",
            error_message=None,
        )


def build_settings(tmp_path: Path, **overrides) -> Settings:
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=tmp_path / "web.db",
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=2,
        metadata_cache_seconds=0,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
    )
    if overrides:
        settings = Settings(**{**settings.__dict__, **overrides})
    return settings


def make_entry(
    entry_id: str,
    title: str,
    *,
    minute: int,
    feed_token: str,
    url: str | None = None,
    summary_html: str | None = None,
    summary_text: str | None = None,
    content_html: str | None = None,
    feed_title: str = "MacRumors: Mac News and Rumors - Front Page",
    feed_site_url: str = "https://www.macrumors.com",
    group_names: tuple[str, ...] = ("Tech",),
    is_starred: bool = False,
) -> FreshRSSEntry:
    default_summary = f"Summary for {title}"
    return FreshRSSEntry(
        id=entry_id,
        title=title,
        author="Writer",
        url=url or f"https://example.com/{entry_id}",
        published_at=f"2026-03-29T10:{minute:02d}:00+00:00",
        summary_html=summary_html or f"<p>{default_summary}</p>",
        summary_text=summary_text or default_summary,
        content_html=content_html or f"<p>{default_summary}</p>",
        feed_title=feed_title,
        feed_site_url=feed_site_url,
        feed_token=feed_token,
        group_names=group_names,
        is_starred=is_starred,
    )


def freshrss_item_id(suffix: str) -> str:
    return f"tag:google.com,2005:reader/item/{suffix}"


def build_app(tmp_path: Path, **settings_overrides):
    settings = build_settings(tmp_path, **settings_overrides)
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    feed_token = encode_feed_token("feed/8")
    navigation = FreshRSSNavigation(
        groups=[FreshRSSGroup(name="Tech", slug="tech", stream_id="user/-/label/Tech")],
        feeds=[
            FreshRSSFeed(
                token=feed_token,
                stream_id="feed/8",
                title="MacRumors: Mac News and Rumors - Front Page",
                feed_url="http://feeds.macrumors.com/MacRumors-Front",
                site_url="https://www.macrumors.com",
                group_slugs=("tech",),
            )
        ],
    )
    stream_pages = {
        ("home", None, None): FreshRSSStreamPage(
            entries=[
                make_entry(freshrss_item_id("2"), "Second story", minute=2, feed_token=feed_token),
                make_entry(freshrss_item_id("1"), "First story", minute=1, feed_token=feed_token),
            ],
            continuation="page-2",
        ),
        ("home", None, "page-2"): FreshRSSStreamPage(
            entries=[
                make_entry(freshrss_item_id("4"), "Fourth story", minute=4, feed_token=feed_token),
                make_entry(freshrss_item_id("3"), "Third story", minute=3, feed_token=feed_token),
            ],
            continuation=None,
        ),
        ("group", "tech", None): FreshRSSStreamPage(
            entries=[make_entry(freshrss_item_id("2"), "Second story", minute=2, feed_token=feed_token)],
            continuation=None,
        ),
        ("feed", feed_token, None): FreshRSSStreamPage(
            entries=[
                make_entry(freshrss_item_id("2"), "Second story", minute=2, feed_token=feed_token),
                make_entry(freshrss_item_id("1"), "First story", minute=1, feed_token=feed_token),
            ],
            continuation=None,
        ),
    }
    client = FakeFreshRSSClient(navigation, stream_pages)
    app = create_app(settings, repository=repository, freshrss_client=client, extractor=FakeExtractor())
    return TestClient(app), client, feed_token


def extract_link(html: str, pattern: str) -> str:
    match = re.search(pattern, html)
    assert match is not None
    return match.group(1)


def extract_form(html: str, button_text: str) -> tuple[str, dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    for form in soup.find_all("form"):
        button = form.find("button")
        if button is None or button.get_text(strip=True) != button_text:
            continue
        action = form.get("action")
        assert action
        payload = {
            input_tag.get("name", ""): input_tag.get("value", "")
            for input_tag in form.find_all("input")
            if input_tag.get("name")
        }
        return action, payload
    raise AssertionError(f"Could not find form button {button_text!r}")


def test_home_reads_from_freshrss_and_mark_read_hides_item(tmp_path: Path):
    client, freshrss, _ = build_app(tmp_path)

    response = client.get("/")
    assert response.status_code == 200
    assert "Second story" in response.text
    assert "First story" in response.text

    hide = client.post(
        f"/items/{freshrss_item_id('1')}/read",
        data={"next_path": "/", "csrf_token": ""},
        follow_redirects=True,
    )
    assert hide.status_code == 200
    assert "First story" not in hide.text
    assert freshrss.read_calls[-1] == [freshrss_item_id("1")]


def test_direct_item_detail_is_safe_but_open_action_marks_read(tmp_path: Path):
    client, freshrss, _ = build_app(tmp_path)

    stream = client.get("/")
    open_action, open_payload = extract_form(stream.text, "First story")
    detail_url = open_payload["next_path"]

    detail = client.get(detail_url)
    assert detail.status_code == 200
    assert "Clean body for First story" in detail.text
    assert "Browse feeds" not in detail.text
    assert '<header class="site-header">' not in detail.text
    assert not freshrss.read_calls

    opened = client.post(open_action, data=open_payload, follow_redirects=True)
    assert opened.status_code == 200
    assert "Clean body for First story" in opened.text
    assert freshrss.read_calls[-1] == [freshrss_item_id("1")]

    unread = client.post(
        f"/items/{freshrss_item_id('1')}/unread",
        data={"next_path": "/", "csrf_token": ""},
        follow_redirects=True,
    )
    assert unread.status_code == 200
    assert "First story" in unread.text
    assert freshrss.unread_calls[-1] == [freshrss_item_id("1")]


def test_next_crosses_page_boundary_when_continuation_exists(tmp_path: Path):
    client, _, _ = build_app(tmp_path)

    stream = client.get("/")
    _, open_payload = extract_form(stream.text, "First story")
    detail_url = open_payload["next_path"]
    detail = client.get(detail_url)

    assert detail.status_code == 200
    assert "Next" in detail.text
    _, next_payload = extract_form(detail.text, "Next")
    assert f"/items/{freshrss_item_id('4')}?ctx=" in next_payload["next_path"]


def test_group_and_feed_filters_use_freshrss_navigation(tmp_path: Path):
    client, _, feed_token = build_app(tmp_path)

    group_page = client.get("/groups/tech")
    assert group_page.status_code == 200
    assert "Second story" in group_page.text
    assert "Tech" in group_page.text

    feed_page = client.get(f"/feeds/{feed_token}")
    assert feed_page.status_code == 200
    assert "Second story" in feed_page.text
    assert "MacRumors: Mac News and Rumors - Front Page" in feed_page.text


def test_removed_routes_are_gone(tmp_path: Path):
    client, _, _ = build_app(tmp_path)

    assert client.get("/archive").status_code == 404
    assert client.get("/admin/import").status_code == 404
    assert client.post("/admin/refresh").status_code == 404
    assert client.get("/debug/client").status_code == 404


def test_star_and_unstar_sync_to_freshrss(tmp_path: Path):
    client, freshrss, _ = build_app(tmp_path)

    star = client.post(
        f"/items/{freshrss_item_id('1')}/star",
        data={"next_path": "/", "csrf_token": ""},
        follow_redirects=True,
    )
    assert star.status_code == 200
    assert freshrss.star_calls[-1] == [freshrss_item_id("1")]

    detail = client.get(f"/items/{freshrss_item_id('1')}")
    assert "Unstar" in detail.text

    unstar = client.post(
        f"/items/{freshrss_item_id('1')}/unstar",
        data={"next_path": "/", "csrf_token": ""},
        follow_redirects=True,
    )
    assert unstar.status_code == 200
    assert freshrss.unstar_calls[-1] == [freshrss_item_id("1")]


def test_auth_redirects_until_login_then_allows_reader_access(tmp_path: Path):
    client, _, _ = build_app(
        tmp_path,
        app_auth_username="reader",
        app_auth_password="secret-pass",
        app_auth_secret="test-secret",
        app_secure_cookies=False,
    )

    redirected = client.get("/", follow_redirects=False)
    assert redirected.status_code == 303
    assert redirected.headers["location"] == "/login?next=%2F"

    login_page = client.get("/login")
    assert login_page.status_code == 200
    assert "Sign in" in login_page.text

    failed = client.post(
        "/login",
        data={"username": "reader", "password": "wrong", "next_path": "/"},
        follow_redirects=False,
    )
    assert failed.status_code == 401
    assert "Incorrect username or password." in failed.text

    success = client.post(
        "/login",
        data={"username": "reader", "password": "secret-pass", "next_path": "/"},
        follow_redirects=False,
    )
    assert success.status_code == 303
    assert success.headers["location"] == "/"

    home = client.get("/")
    assert home.status_code == 200
    assert "Second story" in home.text


def test_auth_redirect_preserves_full_return_url(tmp_path: Path):
    client, _, _ = build_app(
        tmp_path,
        app_auth_username="reader",
        app_auth_password="secret-pass",
        app_auth_secret="test-secret",
        app_secure_cookies=False,
    )

    redirected = client.get("/?c=abc&b=def", follow_redirects=False)
    assert redirected.status_code == 303
    assert redirected.headers["location"] == "/login?next=%2F%3Fc%3Dabc%26b%3Ddef"

    login_page = client.get(redirected.headers["location"])
    login_action, login_payload = extract_form(login_page.text, "Sign in")
    assert login_action.endswith("/login")
    assert login_payload["next_path"] == "/?c=abc&b=def"

    success = client.post(
        login_action,
        data={
            **login_payload,
            "username": "reader",
            "password": "secret-pass",
        },
        follow_redirects=False,
    )
    assert success.status_code == 303
    assert success.headers["location"] == "/?c=abc&b=def"


def test_auth_enabled_routes_require_valid_csrf_token(tmp_path: Path):
    client, freshrss, _ = build_app(
        tmp_path,
        app_auth_username="reader",
        app_auth_password="secret-pass",
        app_auth_secret="test-secret",
        app_secure_cookies=False,
    )

    client.post(
        "/login",
        data={"username": "reader", "password": "secret-pass", "next_path": "/"},
        follow_redirects=False,
    )

    forbidden = client.post(
        f"/items/{freshrss_item_id('1')}/read",
        data={"next_path": "/"},
        follow_redirects=False,
    )
    assert forbidden.status_code == 403

    home = client.get("/")
    _, open_payload = extract_form(home.text, "First story")
    read_response = client.post(
        f"/items/{freshrss_item_id('1')}/read",
        data={"next_path": "/", "csrf_token": open_payload["csrf_token"]},
        follow_redirects=False,
    )
    assert read_response.status_code == 303
    assert freshrss.read_calls[-1] == [freshrss_item_id("1")]


def test_home_renders_hacker_news_comments_preview_as_comment_link(tmp_path: Path):
    settings = build_settings(tmp_path)
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    feed_token = encode_feed_token("feed/131")
    navigation = FreshRSSNavigation(
        groups=[FreshRSSGroup(name="Tech", slug="tech", stream_id="user/-/label/Tech")],
        feeds=[
            FreshRSSFeed(
                token=feed_token,
                stream_id="feed/131",
                title="Hacker News",
                feed_url="https://news.ycombinator.com/rss",
                site_url="https://news.ycombinator.com/",
                group_slugs=("tech",),
            )
        ],
    )
    comments_url = "https://news.ycombinator.com/item?id=43849891"
    stream_pages = {
        ("home", None, None): FreshRSSStreamPage(
            entries=[
                make_entry(
                    freshrss_item_id("hn-1"),
                    "Personal AI Development Environment",
                    minute=21,
                    feed_token=feed_token,
                    url="https://github.com/rbren/personal-ai-devbox",
                    summary_html=f'<p><a href="{comments_url}">Comments</a></p>',
                    summary_text="Comments",
                    content_html=f'<p><a href="{comments_url}">Comments</a></p>',
                    feed_title="Hacker News",
                    feed_site_url="https://news.ycombinator.com/",
                )
            ],
            continuation=None,
        )
    }
    freshrss = FakeFreshRSSClient(navigation, stream_pages)
    app = create_app(settings, repository=repository, freshrss_client=freshrss, extractor=FakeExtractor())
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert f'href="{comments_url}"' in response.text
    assert '<p>Comments</p>' not in response.text
