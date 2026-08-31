import re
from dataclasses import dataclass, replace
from pathlib import Path

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.device_auth import DeviceAuthService
from app.freshrss import (
    FreshRSSEntry,
    FreshRSSFeed,
    FreshRSSGroup,
    FreshRSSNavigation,
    FreshRSSStreamPage,
    encode_feed_token,
)
from app.hacker_news import HackerNewsComment, HackerNewsDiscussion
from app.main import create_app
from app.repository import Repository

TEST_BASE_DIR = Path(__file__).resolve().parent.parent


class FakeFreshRSSClient:
    def __init__(
        self,
        navigation: FreshRSSNavigation,
        stream_pages: dict[tuple[str, str | None, str | None], FreshRSSStreamPage],
    ):
        self.navigation = navigation
        self.stream_pages = stream_pages
        self.entry_map = {
            entry.id: entry for page in stream_pages.values() for entry in page.entries
        }
        self.read_ids: set[str] = set()
        self.starred_ids: set[str] = set()
        self.read_calls: list[list[str]] = []
        self.unread_calls: list[list[str]] = []
        self.star_calls: list[list[str]] = []
        self.unstar_calls: list[list[str]] = []
        self.navigation_calls = 0
        self.stream_calls: list[tuple[str, str | None, str | None]] = []

    def list_navigation(self):
        self.navigation_calls += 1
        return self.navigation

    def get_stream(
        self,
        *,
        scope_kind: str,
        scope_value: str | None = None,
        continuation: str | None = None,
        limit: int = 15,
        include_read: bool = False,
    ):
        self.stream_calls.append((scope_kind, scope_value, continuation))
        page = self.stream_pages[(scope_kind, scope_value, continuation)]
        if scope_kind == "starred":
            entries = [
                replace(
                    entry,
                    is_starred=True,
                    is_read=entry.id in self.read_ids,
                )
                for entry in page.entries
                if entry.id in self.starred_ids
            ]
            return replace(page, entries=entries)
        visible_entries = [
            replace(
                entry,
                is_starred=entry.id in self.starred_ids,
                is_read=entry.id in self.read_ids,
            )
            for entry in page.entries
            if include_read or entry.id not in self.read_ids
        ]
        return replace(page, entries=visible_entries)

    def get_entry(self, entry_id: str):
        entry = self.entry_map.get(entry_id)
        if entry is None:
            return None
        return replace(
            entry,
            is_starred=entry_id in self.starred_ids,
            is_read=entry_id in self.read_ids,
        )

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


class FakeHackerNewsClient:
    def __init__(self, discussion: HackerNewsDiscussion):
        self.discussion = discussion
        self.calls: list[int] = []

    def get_discussion(self, item_id: int):
        self.calls.append(item_id)
        return self.discussion


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


def build_app(
    tmp_path: Path,
    *,
    hacker_news_client=None,
    **settings_overrides,
):
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
                make_entry(
                    freshrss_item_id("2"),
                    "Second story",
                    minute=2,
                    feed_token=feed_token,
                ),
                make_entry(
                    freshrss_item_id("1"),
                    "First story",
                    minute=1,
                    feed_token=feed_token,
                ),
            ],
            continuation="page-2",
        ),
        ("home", None, "page-2"): FreshRSSStreamPage(
            entries=[
                make_entry(
                    freshrss_item_id("4"),
                    "Fourth story",
                    minute=4,
                    feed_token=feed_token,
                ),
                make_entry(
                    freshrss_item_id("3"),
                    "Third story",
                    minute=3,
                    feed_token=feed_token,
                ),
            ],
            continuation=None,
        ),
        ("starred", None, None): FreshRSSStreamPage(
            entries=[
                make_entry(
                    freshrss_item_id("2"),
                    "Second story",
                    minute=2,
                    feed_token=feed_token,
                ),
                make_entry(
                    freshrss_item_id("1"),
                    "First story",
                    minute=1,
                    feed_token=feed_token,
                ),
            ],
            continuation=None,
        ),
        ("group", "tech", None): FreshRSSStreamPage(
            entries=[
                make_entry(
                    freshrss_item_id("2"),
                    "Second story",
                    minute=2,
                    feed_token=feed_token,
                )
            ],
            continuation=None,
        ),
        ("feed", feed_token, None): FreshRSSStreamPage(
            entries=[
                make_entry(
                    freshrss_item_id("2"),
                    "Second story",
                    minute=2,
                    feed_token=feed_token,
                ),
                make_entry(
                    freshrss_item_id("1"),
                    "First story",
                    minute=1,
                    feed_token=feed_token,
                ),
            ],
            continuation=None,
        ),
    }
    client = FakeFreshRSSClient(navigation, stream_pages)
    app = create_app(
        settings,
        repository=repository,
        freshrss_client=client,
        extractor=FakeExtractor(),
        hacker_news_client=hacker_news_client,
    )
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


def extract_text_link(html: str, link_text: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    link = next(
        (
            candidate
            for candidate in soup.find_all("a")
            if candidate.get_text(strip=True) == link_text
        ),
        None,
    )
    assert link is not None
    href = link.get("href")
    assert href
    return str(href)


def test_home_reads_from_freshrss_and_mark_read_hides_item(tmp_path: Path):
    client, freshrss, _ = build_app(tmp_path)

    response = client.get("/")
    assert response.status_code == 200
    assert "Second story" in response.text
    assert "First story" in response.text
    assert not any(
        label.get_text(strip=True) == "Tech"
        for label in BeautifulSoup(response.text, "html.parser").select(
            "[data-entry-card] .item-meta span"
        )
    )

    hide = client.post(
        f"/items/{freshrss_item_id('1')}/read",
        data={"next_path": "/", "csrf_token": ""},
        follow_redirects=True,
    )
    assert hide.status_code == 200
    assert "First story" not in hide.text
    assert freshrss.read_calls[-1] == [freshrss_item_id("1")]


def test_home_warns_when_freshrss_refresh_falls_back_to_cached_articles(
    tmp_path: Path,
):
    client, freshrss, _ = build_app(tmp_path)
    page_key = ("home", None, None)
    freshrss.stream_pages[page_key] = replace(
        freshrss.stream_pages[page_key], is_stale=True
    )

    response = client.get("/")

    assert response.status_code == 200
    assert "FreshRSS unavailable · showing cached articles" in response.text


def test_opening_an_article_does_not_mark_it_read_but_advancing_does(
    tmp_path: Path,
):
    client, freshrss, _ = build_app(tmp_path)

    stream = client.get("/")
    open_url = extract_text_link(stream.text, "First story")
    detail_url = open_url.rsplit("/", 1)[0]

    assert re.fullmatch(r"/read/g-1/[0-9a-f]{12}", open_url)
    assert "?" not in open_url

    detail = client.get(detail_url)
    assert detail.status_code == 200
    assert "Clean body for First story" in detail.text
    assert "Browse feeds" not in detail.text
    assert '<header class="site-header">' not in detail.text
    assert not freshrss.read_calls

    opened = client.get(open_url)
    assert opened.status_code == 200
    assert "Clean body for First story" in opened.text
    assert not freshrss.read_calls

    opened_soup = BeautifulSoup(opened.text, "html.parser")
    close_link = opened_soup.select_one("a.article-close-fixed")
    original_link = opened_soup.find("a", string="Open original")
    advance_form = opened_soup.select_one("form[data-article-advance-form]")
    end_cue = opened_soup.select_one("[data-article-end-cue]")
    assert close_link is not None
    assert str(close_link.get("href")) == "/#entry-g-1"
    assert original_link is not None
    assert "button-link" in original_link.get("class", [])
    stream_soup = BeautifulSoup(stream.text, "html.parser")
    assert stream_soup.select_one("#entry-g-1[data-entry-card]") is not None
    assert advance_form is not None
    assert end_cue is not None
    assert "End of article" in end_cue.get_text(" ", strip=True)
    assert "Fourth story" in end_cue.get_text(" ", strip=True)

    advance = client.post(
        str(advance_form["action"]),
        data={
            input_node.get("name"): input_node.get("value", "")
            for input_node in advance_form.find_all("input")
            if input_node.get("name")
        },
        follow_redirects=True,
    )
    assert advance.status_code == 200
    assert "Clean body for Fourth story" in advance.text
    assert freshrss.read_calls[-1] == [freshrss_item_id("1")]

    unread = client.post(
        f"/items/{freshrss_item_id('1')}/unread",
        data={"next_path": "/", "csrf_token": ""},
        follow_redirects=True,
    )
    assert unread.status_code == 200
    assert "First story" in unread.text
    assert freshrss.unread_calls[-1] == [freshrss_item_id("1")]


def test_read_visibility_toggle_shows_read_items_and_preserves_paging(
    tmp_path: Path,
):
    client, _, _ = build_app(tmp_path)
    entry_id = freshrss_item_id("1")
    client.post(
        f"/items/{entry_id}/read",
        data={"next_path": "/", "csrf_token": ""},
    )

    unread = client.get("/")
    assert "First story" not in unread.text
    unread_soup = BeautifulSoup(unread.text, "html.parser")
    show_read = unread_soup.select_one("a.read-visibility-toggle")
    assert show_read is not None
    assert show_read.get_text(strip=True) == "Show read"
    assert show_read.get("href") == "/?read=1"

    all_items = client.get("/?read=1")
    assert all_items.status_code == 200
    assert "First story" in all_items.text
    all_soup = BeautifulSoup(all_items.text, "html.parser")
    hide_read = all_soup.select_one("a.read-visibility-toggle.active")
    assert hide_read is not None
    assert hide_read.get_text(strip=True) == "Hide read"
    assert hide_read.get("href") == "/"
    assert all_soup.select_one("[data-paged-stream][data-include-read]") is not None

    read_card = next(
        card
        for card in all_soup.select("[data-entry-card]")
        if "First story" in card.get_text(" ", strip=True)
    )
    assert "is-read" in read_card.get("class", [])
    assert "Read" in read_card.get_text(" ", strip=True)
    assert read_card.select_one('[aria-label="Mark First story as unread"]') is not None

    controls = all_soup.select_one('[data-page-mode="stream"]')
    assert controls is not None
    assert "read=1" in str(controls.get("data-page-next-url"))


def test_direct_item_detail_does_not_fetch_unused_navigation(tmp_path: Path):
    client, freshrss, _ = build_app(tmp_path)

    response = client.get(f"/items/{freshrss_item_id('1')}")

    assert response.status_code == 200
    assert freshrss.navigation_calls == 0


def test_next_crosses_page_boundary_when_continuation_exists(tmp_path: Path):
    client, _, _ = build_app(tmp_path)

    stream = client.get("/")
    detail_url = extract_text_link(stream.text, "First story")
    detail = client.get(detail_url)

    assert detail.status_code == 200
    soup = BeautifulSoup(detail.text, "html.parser")
    controls = soup.select_one('.page-turn-rails[data-page-mode="article"]')
    assert controls is not None
    next_url = controls.get("data-page-next-url")
    assert next_url
    assert re.fullmatch(r"/read/g-4/[0-9a-f]{12}", str(next_url))
    assert "?" not in next_url
    assert soup.select_one(".article-previous, .article-next") is None

    next_detail = BeautifulSoup(client.get(str(next_url)).text, "html.parser")
    next_controls = next_detail.select_one(
        '.page-turn-rails[data-page-mode="article"]'
    )
    assert next_controls is not None
    assert next_controls.get("data-page-previous-url") == detail_url


def test_article_response_prewarms_the_next_article(
    tmp_path: Path,
    monkeypatch,
):
    prewarm_calls: list[list[str]] = []
    monkeypatch.setattr(
        FakeExtractor,
        "prewarm",
        lambda self, entries: prewarm_calls.append(
            [entry.id for entry in entries]
        ),
        raising=False,
    )
    client, freshrss, _ = build_app(tmp_path)
    stream = client.get("/")
    prewarm_calls.clear()
    freshrss.stream_calls.clear()

    client.get(extract_text_link(stream.text, "Second story"))

    assert prewarm_calls == [[freshrss_item_id("1")]]
    assert freshrss.stream_calls == [("home", None, "page-2")]


def test_page_boundary_prewarm_hydrates_a_compact_stream_entry(
    tmp_path: Path,
    monkeypatch,
):
    prewarm_calls: list[list[tuple[str, str | None]]] = []
    monkeypatch.setattr(
        FakeExtractor,
        "prewarm",
        lambda self, entries: prewarm_calls.append(
            [(entry.id, entry.content_html) for entry in entries]
        ),
        raising=False,
    )
    client, freshrss, _ = build_app(tmp_path)
    original_get_stream = freshrss.get_stream
    page_two_calls = 0

    def get_stream(**kwargs):
        nonlocal page_two_calls
        page = original_get_stream(**kwargs)
        if kwargs.get("continuation") != "page-2":
            return page
        page_two_calls += 1
        if page_two_calls == 1:
            return page
        return replace(
            page,
            entries=[
                replace(entry, content_html="<p>Truncated preview.</p>")
                for entry in page.entries
            ],
            entries_are_compact=True,
        )

    monkeypatch.setattr(freshrss, "get_stream", get_stream)
    stream = client.get("/")
    second_story_url = extract_text_link(stream.text, "Second story")
    first_story_url = extract_text_link(stream.text, "First story")

    client.get(second_story_url)
    prewarm_calls.clear()
    client.get(first_story_url)

    assert page_two_calls == 2
    assert prewarm_calls == [
        [
            (
                freshrss_item_id("4"),
                "<p>Summary for Fourth story</p>",
            )
        ]
    ]


def test_compact_cached_stream_does_not_replace_complete_article(tmp_path: Path):
    settings = build_settings(tmp_path, article_prewarm_count=1)
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    feed_token = encode_feed_token("feed/8")
    complete_marker = "The complete article keeps this final sentence."

    class CacheSensitiveExtractor(FakeExtractor):
        def __init__(self):
            self.article_html = (
                "<article><p>A complete article has enough useful text for the reader.</p>"
                f"<p>{complete_marker}</p></article>"
            )

        def prewarm(self, entries):
            self.article_html = entries[0].content_html or ""

        def ensure_extracted(self, entry: FreshRSSEntry):
            result = super().ensure_extracted(entry)
            return replace(result, html=self.article_html)

    entry = make_entry(
        freshrss_item_id("1"),
        "Cached story",
        minute=1,
        feed_token=feed_token,
        content_html="<p>Compact stream preview.</p>",
    )
    freshrss = FakeFreshRSSClient(
        FreshRSSNavigation(groups=[], feeds=[]),
        {
            ("home", None, None): FreshRSSStreamPage(
                entries=[entry],
                continuation=None,
                entries_are_compact=True,
            )
        },
    )
    app = create_app(
        settings,
        repository=repository,
        freshrss_client=freshrss,
        extractor=CacheSensitiveExtractor(),
    )
    client = TestClient(app)

    stream = client.get("/")
    article = client.get(extract_text_link(stream.text, "Cached story"))

    assert article.status_code == 200
    assert complete_marker in article.text


def test_group_and_feed_filters_use_freshrss_navigation(tmp_path: Path):
    client, _, feed_token = build_app(tmp_path)

    group_page = client.get("/groups/tech")
    assert group_page.status_code == 200
    assert "Second story" in group_page.text
    assert "Tech" in group_page.text
    group_soup = BeautifulSoup(group_page.text, "html.parser")
    selected_group = group_soup.select_one("a.category-picker-link.active")
    assert selected_group is not None
    assert selected_group.get_text(strip=True) == "Tech"
    assert str(selected_group.get("href")).endswith("/categories")

    group_article = client.get(extract_text_link(group_page.text, "Second story"))
    article_soup = BeautifulSoup(group_article.text, "html.parser")
    home_link = article_soup.select_one("a.article-home-fixed")
    close_link = article_soup.select_one("a.article-close-fixed")
    assert home_link is not None
    assert str(home_link.get("href")).endswith("/")
    assert close_link is not None
    assert str(close_link.get("href")) == "/groups/tech#entry-g-2"

    feed_page = client.get(f"/feeds/{feed_token}")
    assert feed_page.status_code == 200
    assert "Second story" in feed_page.text
    assert "MacRumors: Mac News and Rumors - Front Page" in feed_page.text

    categories_page = client.get("/categories")
    assert categories_page.status_code == 200
    category_soup = BeautifulSoup(categories_page.text, "html.parser")
    choices = {
        choice.get_text(strip=True): choice.get("href")
        for choice in category_soup.select("a.category-choice")
    }
    assert set(choices) == {"All articles", "Tech"}
    assert str(choices["All articles"]).endswith("/")
    assert str(choices["Tech"]).endswith("/groups/tech")
    assert category_soup.find("script", src=re.compile(r"/static/reader\.js")) is None

    feeds_page = client.get("/feeds")
    feeds_soup = BeautifulSoup(feeds_page.text, "html.parser")
    assert feeds_soup.find("script", src=re.compile(r"/static/reader\.js")) is None


def test_feed_picker_bounds_each_kindle_page(tmp_path: Path):
    client, freshrss, _ = build_app(tmp_path)
    freshrss.navigation = FreshRSSNavigation(
        groups=freshrss.navigation.groups,
        feeds=[
            FreshRSSFeed(
                token=f"feed-{index}",
                stream_id=f"feed/{index}",
                title=f"Feed {index:02d}",
                feed_url=f"https://example.com/{index}.xml",
                site_url=f"https://example.com/{index}",
                group_slugs=(),
            )
            for index in range(25)
        ],
    )

    first = BeautifulSoup(client.get("/feeds").text, "html.parser")
    second = BeautifulSoup(client.get("/feeds?page=2").text, "html.parser")
    last = BeautifulSoup(client.get("/feeds?page=3").text, "html.parser")

    assert len(first.select(".feed-card")) == 12
    assert first.select_one('a[href="/feeds?page=2"]') is not None
    assert len(second.select(".feed-card")) == 12
    assert second.select_one('a[href="/feeds?page=1"]') is not None
    assert len(last.select(".feed-card")) == 1
    assert "Feed 24" in last.get_text(" ", strip=True)


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


def test_starred_view_includes_saved_read_items(tmp_path: Path):
    client, _, _ = build_app(tmp_path)
    entry_id = freshrss_item_id("1")

    client.post(f"/items/{entry_id}/star", data={"next_path": "/", "csrf_token": ""})
    client.post(f"/items/{entry_id}/read", data={"next_path": "/", "csrf_token": ""})

    response = client.get("/starred")

    assert response.status_code == 200
    assert "First story" in response.text
    assert "Second story" not in response.text


def test_home_skips_unused_navigation_and_keeps_the_category_link(
    tmp_path: Path,
):
    client, freshrss, _ = build_app(tmp_path)

    response = client.get("/")

    assert response.status_code == 200
    assert "freshrss_stream;dur=" in response.headers["server-timing"]
    assert "total;dur=" in response.headers["server-timing"]
    assert response.headers["x-rss-kindle-version"]
    assert response.headers["cache-control"] == "private, no-cache"
    assert freshrss.navigation_calls == 0
    assert "Browse feeds" not in response.text
    soup = BeautifulSoup(response.text, "html.parser")
    category_link = soup.select_one("a.category-picker-link")
    assert category_link is not None
    assert category_link.get_text(strip=True) == "Categories"
    assert str(category_link.get("href")).endswith("/categories")
    assert soup.select_one("select") is None


def test_reader_page_revalidation_returns_no_html_body(tmp_path: Path):
    client, _, _ = build_app(tmp_path)

    first = client.get("/")
    etag = first.headers.get("etag")
    assert etag and etag.startswith('W/"')

    revalidated = client.get("/", headers={"If-None-Match": etag})

    assert revalidated.status_code == 304
    assert revalidated.content == b""
    assert revalidated.headers["etag"] == etag
    assert revalidated.headers["cache-control"] == "private, no-cache"


def test_reader_quick_action_skips_redirect_and_forms_keep_a_normal_fallback(
    tmp_path: Path,
):
    client, freshrss, _ = build_app(tmp_path)
    response = client.get("/")
    soup = BeautifulSoup(response.text, "html.parser")
    first_card = soup.select_one("[data-entry-card]")
    assert first_card is not None
    forms = first_card.find_all("form")
    assert len(forms) == 1

    star_button = first_card.select_one('[data-quick-action="star"]')
    read_button = first_card.select_one('[data-quick-action="read"]')
    assert star_button is not None
    assert read_button is not None
    assert read_button.get_text(strip=True) == "✓"
    assert read_button.get("title") == "Mark as read"
    star_form = star_button.find_parent("form")
    read_form = read_button.find_parent("form")
    assert star_form is not None
    assert read_form is not None
    assert star_form is read_form
    assert not star_button.has_attr("formaction")
    assert not read_button.has_attr("formaction")
    star_url = star_form.get("action")
    assert star_url
    payload = {
        input_node.get("name"): input_node.get("value", "")
        for input_node in star_form.find_all("input")
        if input_node.get("name")
    }
    payload["state_action"] = str(star_button.get("value"))

    quick_response = client.post(
        str(star_url),
        data=payload,
        headers={"X-RSS-Kindle-Action": "1"},
        follow_redirects=False,
    )

    assert quick_response.status_code == 204
    assert "location" not in quick_response.headers
    assert freshrss.star_calls
    assert star_form.has_attr("data-state-form")
    assert str(star_form.get("action", "")).endswith("/state")
    assert star_button.get("name") == "state_action"
    assert read_button.get("name") == "state_action"

    fallback_payload = {
        **payload,
        "state_action": str(read_button.get("value")),
    }
    fallback_response = client.post(
        str(star_url),
        data=fallback_payload,
        follow_redirects=False,
    )
    assert fallback_response.status_code == 303
    assert freshrss.read_calls


def test_stream_has_progressive_book_style_page_controls(tmp_path: Path):
    client, _, _ = build_app(tmp_path)

    response = client.get("/")
    soup = BeautifulSoup(response.text, "html.parser")
    menu = soup.select_one("details.reader-menu")
    controls = soup.select_one('.page-turn-rails[hidden][data-page-mode="stream"]')

    assert menu is not None
    assert not menu.has_attr("open")
    assert menu.select_one("summary") is not None
    assert menu.select_one("nav") is not None
    assert controls is not None
    assert [button.get("data-page-turn") for button in controls.find_all("button")] == [
        "-1",
        "1",
    ]
    assert controls.select_one('[aria-label="Newer articles"]') is not None
    assert controls.select_one('[aria-label="Older articles"]') is not None
    assert soup.select_one("[data-stream-page-status]") is not None
    assert soup.select_one('[data-paged-stream][data-stream-offset="0"]') is not None

    next_page_url = controls.get("data-page-next-url")
    assert next_page_url
    next_page = BeautifulSoup(client.get(str(next_page_url)).text, "html.parser")
    assert next_page.select_one(
        '[data-paged-stream][data-stream-offset="2"]'
    ) is not None


def test_pre_upgrade_numeric_cursor_restarts_the_stream_safely(tmp_path: Path):
    client, freshrss, _ = build_app(tmp_path)

    response = client.get(
        "/?c=sorted-offset%3A15",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert freshrss.stream_calls == []


def test_article_has_cached_script_and_progressive_page_controls(tmp_path: Path):
    client, _, _ = build_app(tmp_path)
    response = client.get(f"/items/{freshrss_item_id('1')}")
    soup = BeautifulSoup(response.text, "html.parser")

    script = soup.find("script", src=re.compile(r"/static/reader\.js\?v="))
    controls = soup.select_one('.page-turn-rails[hidden][data-page-mode="article"]')
    progress = soup.select_one('[data-reading-progress][role="progressbar"]')
    close_link = soup.select_one("a.article-close-fixed")

    assert script is not None
    assert controls is not None
    assert len(controls.find_all("button")) == 2
    assert progress is not None
    assert progress.get("aria-valuenow") == "0"
    assert close_link is not None
    assert str(close_link.get("href")).endswith("/#entry-g-1")
    assert "script-src 'self'" in response.headers["content-security-policy"]


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


def test_kindle_can_pair_once_and_use_a_long_lived_device_session(tmp_path: Path):
    auth_settings = {
        "app_auth_username": "reader",
        "app_auth_password": "secret-pass",
        "app_auth_secret": "test-secret",
        "app_secure_cookies": False,
    }
    client, _, _ = build_app(tmp_path, **auth_settings)
    settings = build_settings(tmp_path, **auth_settings)
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    DeviceAuthService(settings, repository).create_pairing_code(code="123456")

    activation_page = client.get("/activate")
    assert activation_page.status_code == 200
    assert "Pair this device" in activation_page.text

    paired = client.post(
        "/activate",
        data={"code": "123456", "device_name": "Test Kindle", "next_path": "/"},
        follow_redirects=False,
    )
    assert paired.status_code == 303
    assert settings.app_device_cookie_name in paired.headers["set-cookie"]

    home = client.get("/")
    assert home.status_code == 200
    assert "Second story" in home.text
    assert "Test Kindle" not in home.text

    second_use = TestClient(client.app)
    reused = second_use.post(
        "/activate",
        data={"code": "123456", "device_name": "Other Kindle", "next_path": "/"},
        follow_redirects=False,
    )
    assert reused.status_code == 401


def test_password_admin_can_pair_and_revoke_a_reader_device(tmp_path: Path):
    auth_settings = {
        "app_auth_username": "reader",
        "app_auth_password": "secret-pass",
        "app_auth_secret": "test-secret",
        "app_secure_cookies": False,
    }
    admin_client, _, _ = build_app(tmp_path, **auth_settings)

    blocked = admin_client.get("/dashboard", follow_redirects=False)
    assert blocked.status_code == 303
    assert blocked.headers["location"] == "/login?next=%2Fdashboard"

    admin_client.post(
        "/login",
        data={
            "username": "reader",
            "password": "secret-pass",
            "next_path": "/dashboard",
        },
        follow_redirects=False,
    )
    legacy = admin_client.get("/admin", follow_redirects=False)
    assert legacy.status_code == 303
    assert legacy.headers["location"] == "/dashboard"

    admin = admin_client.get("/dashboard")
    assert admin.status_code == 200
    assert "RSS Kindle Dashboard" in admin.text
    assert "Oldest feed refresh:" in admin.text
    assert "Latest article published:" in admin.text
    assert "Second story" in admin.text
    pairing_action, pairing_payload = extract_form(admin.text, "New pairing code")

    pairing = admin_client.post(pairing_action, data=pairing_payload)
    pairing_soup = BeautifulSoup(pairing.text, "html.parser")
    pairing_code = pairing_soup.select_one(".pairing-code")
    assert pairing_code is not None
    assert re.fullmatch(r"\d{6}", pairing_code.get_text(strip=True))

    kindle_client = TestClient(admin_client.app)
    activated = kindle_client.post(
        "/activate",
        data={
            "code": pairing_code.get_text(strip=True),
            "device_name": "Kitchen Kindle",
            "next_path": "/",
        },
        follow_redirects=False,
    )
    assert activated.status_code == 303
    assert kindle_client.get("/").status_code == 200
    assert (
        kindle_client.get("/dashboard", follow_redirects=False).headers["location"]
        == "/login?next=%2Fdashboard"
    )

    refreshed_admin = admin_client.get("/dashboard")
    assert "Kitchen Kindle" in refreshed_admin.text
    revoke_action, revoke_payload = extract_form(refreshed_admin.text, "Revoke")
    revoked = admin_client.post(revoke_action, data=revoke_payload)
    assert "Device access was revoked." in revoked.text
    assert kindle_client.get("/", follow_redirects=False).status_code == 303


def test_health_is_public_and_does_not_probe_dependencies(tmp_path: Path):
    client, _, _ = build_app(
        tmp_path,
        app_auth_username="reader",
        app_auth_password="secret-pass",
        app_auth_secret="test-secret",
        app_secure_cookies=False,
    )

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["version"]


def test_health_check_allows_loopback_when_trusted_hosts_are_set(tmp_path: Path):
    default_client, _, _ = build_app(
        tmp_path,
        app_allowed_hosts=("reader.example.com",),
    )
    health_client = TestClient(default_client.app, base_url="http://127.0.0.1:8000")

    assert health_client.get("/health").status_code == 200


def test_favicon_is_public_and_cacheable(tmp_path: Path):
    client, _, _ = build_app(
        tmp_path,
        app_auth_username="reader",
        app_auth_password="secret-pass",
        app_auth_secret="test-secret",
        app_secure_cookies=False,
    )

    response = client.get("/favicon.ico")

    assert response.status_code == 204
    assert "max-age=31536000" in response.headers["cache-control"]


def test_password_admin_can_create_and_download_backup(tmp_path: Path):
    client, _, _ = build_app(
        tmp_path,
        app_auth_username="reader",
        app_auth_password="secret-pass",
        app_auth_secret="test-secret",
        app_secure_cookies=False,
        backup_directory=tmp_path / "backups",
    )
    client.post(
        "/login",
        data={
            "username": "reader",
            "password": "secret-pass",
            "next_path": "/dashboard",
        },
        follow_redirects=False,
    )
    admin = client.get("/dashboard")
    backup_action, backup_payload = extract_form(admin.text, "Create backup")

    created = client.post(backup_action, data=backup_payload)

    assert created.status_code == 200
    assert "Backup created:" in created.text
    download_url = extract_link(
        created.text, r'href="([^"]*rss-kindle-backup-[^"]+\.zip)"'
    )
    downloaded = client.get(download_url)
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "application/zip"
    assert downloaded.content.startswith(b"PK")


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
    soup = BeautifulSoup(home.text, "html.parser")
    csrf_input = soup.find("input", attrs={"name": "csrf_token"})
    assert csrf_input is not None
    read_response = client.post(
        f"/items/{freshrss_item_id('1')}/read",
        data={"next_path": "/", "csrf_token": csrf_input.get("value", "")},
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
    app = create_app(
        settings,
        repository=repository,
        freshrss_client=freshrss,
        extractor=FakeExtractor(),
    )
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    discussion_link = next(
        link
        for link in soup.find_all("a")
        if link.get_text(strip=True) == "Read discussion"
    )
    assert discussion_link.get("href") == "/hacker-news/43849891?back=%2F"
    assert soup.select_one(".item-destination").get_text(strip=True) == "github.com"
    assert "<p>Comments</p>" not in response.text

    article_url = soup.select_one("[data-open-article]").get("href")
    article = client.get(article_url)
    article_soup = BeautifulSoup(article.text, "html.parser")
    assert article_soup.select_one(".article-meta-compact .item-source").get_text(
        strip=True
    ) == "Hacker News"
    assert article_soup.select_one(
        ".article-meta-compact .item-destination"
    ).get_text(strip=True) == "github.com"
    article_discussion = next(
        link
        for link in article_soup.find_all("a")
        if link.get_text(strip=True) == "Read discussion"
    )
    assert article_discussion.get("href").startswith(
        "/hacker-news/43849891?back=%2Fread%2F"
    )


def test_hacker_news_discussion_renders_thread_for_kindle(tmp_path: Path):
    discussion = HackerNewsDiscussion(
        id=43849891,
        title="Personal AI Development Environment",
        author="submitter",
        created_at="2026-03-29T10:21:00+00:00",
        score=314,
        comment_count=7,
        comments=(
            HackerNewsComment(
                id=10,
                author="alice",
                created_at="2026-03-29T10:22:00+00:00",
                html="<p>Top-level <strong>comment</strong>.</p>",
                depth=0,
                visual_depth=0,
                parent_author=None,
                is_deleted=False,
                is_dead=False,
            ),
            HackerNewsComment(
                id=11,
                author="bob",
                created_at="2026-03-29T10:23:00+00:00",
                html="<p>A nested reply.</p>",
                depth=1,
                visual_depth=1,
                parent_author="alice",
                is_deleted=False,
                is_dead=False,
            ),
        ),
        source_url="https://github.com/rbren/personal-ai-devbox",
        destination_host="github.com",
        permalink="https://news.ycombinator.com/item?id=43849891",
        is_partial=True,
    )
    hacker_news = FakeHackerNewsClient(discussion)
    client, _, _ = build_app(
        tmp_path,
        hacker_news_client=hacker_news,
    )

    response = client.get("/hacker-news/43849891?back=%2Fstarred")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-cache"
    soup = BeautifulSoup(response.text, "html.parser")
    assert soup.select_one("body.hacker-news-page") is not None
    assert soup.select_one(".hn-kicker") is None
    assert soup.select_one(".hn-story-title").get_text(strip=True) == discussion.title
    assert "submitter" in soup.select_one(".hn-story-meta").get_text(" ", strip=True)
    assert "314 pts" in soup.select_one(".hn-story-meta").get_text(" ", strip=True)
    assert "github.com" in soup.select_one(".hn-story-meta").get_text(" ", strip=True)
    assert [
        author.get_text(strip=True)
        for author in soup.select(".hn-comment-author")
    ] == [
        "alice",
        "bob",
    ]
    assert soup.select_one(".hn-comment-depth-1") is not None
    assert soup.select_one("#hn-comments-heading.visually-hidden") is not None
    assert "to alice" == soup.select_one(
        ".hn-comment-depth-1 .hn-comment-context"
    ).get_text(" ", strip=True)
    assert soup.select_one(".hn-comment-depth-0 .hn-comment-context") is None
    comment_time = soup.select_one(".hn-comment-depth-0 time")
    assert comment_time.get("datetime") == "2026-03-29T10:22:00+00:00"
    assert comment_time.get("title") == "2026-03-29 10:22 UTC"
    assert len(comment_time.get_text(strip=True)) <= 6
    assert "ago" not in comment_time.get_text(strip=True)
    assert "Top-level comment" in soup.select_one(
        ".hn-comment-body"
    ).get_text(" ", strip=True)
    assert soup.select_one(".hn-comment-footer") is None
    assert "View on HN" not in soup.get_text(" ", strip=True)
    assert soup.select_one(".hn-thread-note") is not None
    assert soup.select_one("[data-close-article]").get("href") == "/starred"
    controls = soup.select_one('[data-page-mode="article"]')
    assert controls is not None
    assert controls.get("aria-label") == "Discussion page controls"
    assert hacker_news.calls == [43849891]
