import sys

import pytest

from app.browser_fetch import PersistentBrowserClient, _build_playwright_cookies


def test_build_playwright_cookies_parses_cookie_header():
    cookies = _build_playwright_cookies(
        "https://www.ft.com/content/story-1",
        "session=abc123; flag; pref=two=parts",
    )

    assert cookies == [
        {"name": "session", "value": "abc123", "url": "https://www.ft.com/"},
        {"name": "pref", "value": "two=parts", "url": "https://www.ft.com/"},
    ]


class _FakeResponse:
    status = 200


class _FakePage:
    def __init__(self):
        self.default_timeout = None
        self.headers = None
        self.goto_calls: list[tuple[str, str | None, int | None]] = []
        self.selector_calls: list[tuple[str, int | None]] = []
        self.timeout_calls: list[int] = []
        self.closed = False

    def set_default_timeout(self, timeout: int) -> None:
        self.default_timeout = timeout

    def set_extra_http_headers(self, headers: dict[str, str]) -> None:
        self.headers = headers

    def goto(self, url: str, *, wait_until: str | None, timeout: int | None):
        self.goto_calls.append((url, wait_until, timeout))
        return _FakeResponse()

    def wait_for_selector(self, selector: str, *, timeout: int | None) -> None:
        self.selector_calls.append((selector, timeout))

    def wait_for_timeout(self, timeout: int) -> None:
        self.timeout_calls.append(timeout)

    def content(self) -> str:
        return "<html><body>ok</body></html>"

    def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self):
        self.pages: list[_FakePage] = []
        self.created_pages: list[_FakePage] = []
        self.cookies: list[dict[str, str]] = []
        self.closed = False

    def new_page(self) -> _FakePage:
        page = _FakePage()
        self.created_pages.append(page)
        return page

    def add_cookies(self, cookies: list[dict[str, str]]) -> None:
        self.cookies.extend(cookies)

    def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self, context: _FakeContext):
        self.contexts = [context]


class _FakeChromium:
    def __init__(self, context: _FakeContext):
        self.context = context
        self.connect_calls: list[tuple[str, int]] = []

    def connect_over_cdp(self, url: str, *, timeout: int):
        self.connect_calls.append((url, timeout))
        return _FakeBrowser(self.context)


class _FailingChromium(_FakeChromium):
    def connect_over_cdp(self, url: str, *, timeout: int):
        self.connect_calls.append((url, timeout))
        raise RuntimeError("CDP unavailable")


class _FakePlaywright:
    def __init__(self, chromium: _FakeChromium):
        self.chromium = chromium
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class _FakePlaywrightStarter:
    def __init__(self, playwright: _FakePlaywright):
        self.playwright = playwright

    def start(self) -> _FakePlaywright:
        return self.playwright


def test_persistent_browser_client_can_attach_over_cdp(monkeypatch):
    context = _FakeContext()
    chromium = _FakeChromium(context)
    playwright = _FakePlaywright(chromium)
    fake_module = type(
        "FakeSyncApiModule",
        (),
        {"sync_playwright": lambda: _FakePlaywrightStarter(playwright)},
    )
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)

    client = PersistentBrowserClient(
        profile_path=None,
        cdp_url="http://browser-cdp:9222",
        executable_path=None,
        channel=None,
        headless=True,
        launch_args=(),
        timeout_seconds=12,
        user_agent="ignored-in-cdp-mode",
        wait_until="load",
        wait_for_selector="main",
        settle_seconds=2,
    )

    with client as connected_client:
        response = connected_client.get(
            "https://www.ft.com/content/story-1",
            headers={"X-Test": "1"},
            cookie_header="session=abc123",
        )

    page = context.created_pages[0]
    assert response.text == "<html><body>ok</body></html>"
    assert chromium.connect_calls == [("http://browser-cdp:9222", 12000)]
    assert page.default_timeout == 12000
    assert page.headers == {"X-Test": "1"}
    assert page.goto_calls == [("https://www.ft.com/content/story-1", "load", 12000)]
    assert page.selector_calls == [("main", 12000)]
    assert page.timeout_calls == [2000]
    assert context.cookies == [{"name": "session", "value": "abc123", "url": "https://www.ft.com/"}]
    assert page.closed is True
    assert context.closed is False
    assert playwright.stopped is True


def test_persistent_browser_client_cleans_up_failed_start(monkeypatch):
    context = _FakeContext()
    chromium = _FailingChromium(context)
    playwright = _FakePlaywright(chromium)
    fake_module = type(
        "FakeSyncApiModule",
        (),
        {"sync_playwright": lambda: _FakePlaywrightStarter(playwright)},
    )
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)

    with pytest.raises(RuntimeError, match="CDP unavailable"):
        _build_cdp_client().__enter__()

    assert playwright.stopped is True


def _build_cdp_client() -> PersistentBrowserClient:
    return PersistentBrowserClient(
        profile_path=None,
        cdp_url="http://browser-cdp:9222",
        executable_path=None,
        channel=None,
        headless=True,
        launch_args=(),
        timeout_seconds=12,
        user_agent="ignored-in-cdp-mode",
        wait_until="load",
        wait_for_selector="main",
        settle_seconds=2,
    )
