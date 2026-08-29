from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True)
class BrowserResponse:
    text: str
    status_code: int = 200

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"Browser fetch failed with status {self.status_code}")


class PersistentBrowserClient:
    def __init__(
        self,
        *,
        profile_path: Path | None,
        cdp_url: str | None,
        executable_path: Path | None,
        channel: str | None,
        headless: bool,
        launch_args: tuple[str, ...],
        timeout_seconds: float,
        user_agent: str,
        wait_until: str,
        wait_for_selector: str | None,
        settle_seconds: float,
    ):
        self.profile_path = profile_path
        self.cdp_url = cdp_url
        self.executable_path = executable_path
        self.channel = channel
        self.headless = headless
        self.launch_args = launch_args
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.wait_until = wait_until
        self.wait_for_selector = wait_for_selector
        self.settle_seconds = settle_seconds
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._seeded_cookie_headers: dict[str, str] = {}
        self._owns_context = False
        self._close_page_on_exit = False

    def __enter__(self) -> Self:
        if self.profile_path is None and not self.cdp_url:
            raise RuntimeError(
                "browser_profile_path or browser_cdp_url is required for the browser backend"
            )

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Browser fetching requires the playwright package. Run `uv sync --extra dev`."
            ) from exc

        self._playwright = sync_playwright().start()
        if self.cdp_url:
            self._browser = self._playwright.chromium.connect_over_cdp(
                self.cdp_url,
                timeout=int(self.timeout_seconds * 1000),
            )
            if not self._browser.contexts:
                raise RuntimeError(
                    "CDP browser has no default context. Launch Chromium with a persistent user-data-dir."
                )
            self._context = self._browser.contexts[0]
            self._page = self._context.new_page()
            self._close_page_on_exit = True
            self._page.set_default_timeout(int(self.timeout_seconds * 1000))
            return self

        assert self.profile_path is not None
        self.profile_path.mkdir(parents=True, exist_ok=True)

        launch_kwargs: dict[str, object] = {
            "headless": self.headless,
            "user_agent": self.user_agent,
            "args": list(self.launch_args),
        }
        if self.executable_path is not None:
            launch_kwargs["executable_path"] = str(self.executable_path)
        elif self.channel:
            launch_kwargs["channel"] = self.channel

        self._context = self._playwright.chromium.launch_persistent_context(
            str(self.profile_path),
            **launch_kwargs,
        )
        self._owns_context = True
        self._page = (
            self._context.pages[0] if self._context.pages else self._context.new_page()
        )
        self._page.set_default_timeout(int(self.timeout_seconds * 1000))
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._page is not None and self._close_page_on_exit:
            self._page.close()
        if self._context is not None and self._owns_context:
            self._context.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._page = None
        self._context = None
        self._playwright = None
        self._browser = None
        self._seeded_cookie_headers = {}
        self._owns_context = False
        self._close_page_on_exit = False
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
    ) -> BrowserResponse:
        if self._page is None:
            raise RuntimeError("Browser client is not open")

        extra_headers = dict(headers or {})
        inline_cookie_header = cookie_header or extra_headers.pop("Cookie", None)

        if inline_cookie_header and self._context is not None:
            host = (urlsplit(url).hostname or "").lower()
            if host and self._seeded_cookie_headers.get(host) != inline_cookie_header:
                cookies = _build_playwright_cookies(url, inline_cookie_header)
                if cookies:
                    self._context.add_cookies(cookies)
                    self._seeded_cookie_headers[host] = inline_cookie_header

        self._page.set_extra_http_headers(extra_headers)

        selected_wait_until = wait_until or self.wait_until
        selected_wait_for_selector = (
            self.wait_for_selector if wait_for_selector is None else wait_for_selector
        )
        selected_settle_seconds = (
            self.settle_seconds if settle_seconds is None else settle_seconds
        )

        response = self._page.goto(
            url,
            wait_until=selected_wait_until,
            timeout=int(self.timeout_seconds * 1000),
        )
        if selected_wait_for_selector:
            self._page.wait_for_selector(
                selected_wait_for_selector, timeout=int(self.timeout_seconds * 1000)
            )
        if selected_settle_seconds > 0:
            self._page.wait_for_timeout(int(selected_settle_seconds * 1000))

        status_code = response.status if response is not None else 200
        return BrowserResponse(text=self._page.content(), status_code=status_code)


def _build_playwright_cookies(url: str, cookie_header: str) -> list[dict[str, str]]:
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        return []

    cookie_url = urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))
    cookies: list[dict[str, str]] = []
    for part in cookie_header.split(";"):
        chunk = part.strip()
        if not chunk:
            continue
        name, separator, value = chunk.partition("=")
        if not separator or not name:
            continue
        cookies.append(
            {
                "name": name.strip(),
                "value": value.strip(),
                "url": cookie_url,
            }
        )
    return cookies
