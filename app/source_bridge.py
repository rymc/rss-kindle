from __future__ import annotations

import html
import logging
import re
import threading
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from readability import Document

from app.browser_fetch import PersistentBrowserClient
from app.config import Settings
from app.repository import Repository, SyntheticFeedItem
from app.source_config import (
    AuthProfile,
    SourceBridgeError,
    SourceCatalog,
    SourceDefinition,
    SourceNotConfiguredError,
)
from app.utils import (
    excerpt,
    parse_datetime,
    stable_hash,
    strip_html,
    utc_now,
    utc_now_iso,
)

CONTENT_NAMESPACE = "http://purl.org/rss/1.0/modules/content/"
ET.register_namespace("content", CONTENT_NAMESPACE)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscoveredLink:
    url: str
    title: str | None
    source_page_url: str


@dataclass(frozen=True)
class HttpProbeResult:
    stage: str
    url: str
    status_code: int
    page_state: str
    title: str | None
    article_text_length: int
    excerpt: str


@dataclass(frozen=True)
class ExtractedSourceArticle:
    source_id: str
    article_url: str
    title: str
    content_html: str
    summary_text: str | None
    published_at: str | None


class SourceBridgeService:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        *,
        catalog: SourceCatalog | None = None,
        client_factory: Callable[[], Any] | None = None,
        browser_client_factory: Callable[[SourceDefinition, AuthProfile], Any]
        | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.catalog = catalog or SourceCatalog.load(settings.source_bridge_config_path)
        self.client_factory = client_factory or self._default_client_factory
        self.browser_client_factory = (
            browser_client_factory or self._default_browser_client_factory
        )
        self._active_background_refreshes: set[str] = set()
        self._active_background_refreshes_lock = threading.Lock()

    def _default_client_factory(self) -> httpx.Client:
        return httpx.Client(
            follow_redirects=True,
            timeout=self.settings.http_timeout_seconds,
            headers={"User-Agent": self.settings.user_agent},
        )

    def _default_browser_client_factory(
        self, source: SourceDefinition, profile: AuthProfile
    ) -> PersistentBrowserClient:
        return PersistentBrowserClient(
            profile_path=profile.browser_profile_path,
            cdp_url=profile.browser_cdp_url,
            executable_path=profile.browser_executable_path,
            channel=profile.browser_channel,
            headless=profile.browser_headless,
            launch_args=profile.browser_launch_args,
            timeout_seconds=self.settings.http_timeout_seconds,
            user_agent=self.settings.user_agent,
            wait_until=source.browser_wait_until,
            wait_for_selector=source.browser_wait_for_selector,
            settle_seconds=source.browser_settle_seconds,
        )

    def list_sources(self) -> list[SourceDefinition]:
        return sorted(
            self.catalog.sources.values(), key=lambda source: source.source_id
        )

    def get_source(self, source_id: str) -> SourceDefinition:
        try:
            return self.catalog.sources[source_id]
        except KeyError as exc:
            raise SourceNotConfiguredError(
                f"Unknown synthetic source: {source_id}"
            ) from exc

    def list_source_status(self) -> list[dict[str, object]]:
        with self._active_background_refreshes_lock:
            active_refreshes = set(self._active_background_refreshes)
        statuses: list[dict[str, object]] = []
        for source in self.list_sources():
            state = self.repository.get_synthetic_source_state(source.source_id)
            items = self.repository.list_synthetic_feed_items(source.source_id, limit=1)
            latest_item = items[0] if items else None
            statuses.append(
                {
                    "id": source.source_id,
                    "title": source.title,
                    "fetch_backend": source.fetch_backend,
                    "item_count": self.repository.count_synthetic_feed_items(
                        source.source_id
                    ),
                    "last_attempted_at": state.last_attempted_at if state else None,
                    "last_successful_at": state.last_successful_at if state else None,
                    "latest_article_at": (
                        (latest_item.published_at or latest_item.discovered_at)
                        if latest_item
                        else None
                    ),
                    "latest_article_title": latest_item.title if latest_item else None,
                    "last_error": state.last_error if state else None,
                    "refreshing": source.source_id in active_refreshes,
                }
            )
        return statuses

    def schedule_refresh(self, source_id: str) -> bool:
        self.get_source(source_id)
        return self._schedule_background_refresh(source_id)

    def build_feed(self, source_id: str) -> str:
        source = self.get_source(source_id)
        items = self.repository.list_synthetic_feed_items(
            source_id, limit=source.max_items
        )
        if self._source_needs_refresh(source, items):
            if items:
                self._schedule_background_refresh(source_id)
            else:
                try:
                    items = self.refresh_source(source_id)
                except Exception as exc:
                    if not items:
                        raise SourceBridgeError(str(exc)) from exc
        return self._render_rss(source, items)

    def _schedule_background_refresh(self, source_id: str) -> bool:
        with self._active_background_refreshes_lock:
            if source_id in self._active_background_refreshes:
                return False
            self._active_background_refreshes.add(source_id)

        thread = threading.Thread(
            target=self._run_background_refresh,
            args=(source_id,),
            name=f"source-bridge-refresh-{source_id}",
            daemon=True,
        )
        thread.start()
        return True

    def _run_background_refresh(self, source_id: str) -> None:
        try:
            self.refresh_source(source_id)
        except Exception:
            logger.warning(
                "Background synthetic source refresh failed for %s",
                source_id,
                exc_info=True,
            )
        finally:
            with self._active_background_refreshes_lock:
                self._active_background_refreshes.discard(source_id)

    def refresh_source(self, source_id: str) -> list[SyntheticFeedItem]:
        source = self.get_source(source_id)
        snapshot_time = utc_now_iso()
        try:
            with self._open_client(source) as client:
                candidates = self._discover_links(client, source)
                items = self._build_items(
                    client, source, candidates, snapshot_time=snapshot_time
                )
        except Exception as exc:
            self.repository.mark_synthetic_source_failure(source_id, str(exc))
            raise

        self.repository.replace_synthetic_feed_items(source_id, items)
        return self.repository.list_synthetic_feed_items(
            source_id, limit=source.max_items
        )

    def discover_links(self, source_id: str) -> list[DiscoveredLink]:
        source = self.get_source(source_id)
        with self._open_client(source) as client:
            return self._discover_links(client, source)

    def probe_http_source(
        self, source_id: str, *, limit: int = 5
    ) -> list[HttpProbeResult]:
        return self.probe_source(source_id, limit=limit, backend="http")

    def probe_source(
        self, source_id: str, *, limit: int = 5, backend: str | None = None
    ) -> list[HttpProbeResult]:
        source = self.get_source(source_id)
        selected_backend = backend or source.fetch_backend
        results: list[HttpProbeResult] = []
        client_context = (
            self._open_client(source)
            if selected_backend == "browser"
            else self.client_factory()
        )
        with client_context as client:
            for start_url in source.start_urls:
                response = self._fetch_response(
                    client,
                    start_url,
                    source,
                    backend=selected_backend,
                    stage="discovery",
                )
                results.append(
                    _build_http_probe_result(
                        "start", start_url, response.status_code, response.text
                    )
                )

            try:
                candidates = self._discover_links(client, source)
            except Exception:  # noqa: BLE001 - probes must return the partial diagnostic result
                return results

            for candidate in candidates[: max(0, limit)]:
                response = self._fetch_response(
                    client,
                    candidate.url,
                    source,
                    backend=selected_backend,
                    stage="article",
                )
                results.append(
                    _build_http_probe_result(
                        "article", candidate.url, response.status_code, response.text
                    )
                )
        return results

    def schedule_stale_refreshes(self, *, lookahead_seconds: float = 0.0) -> list[str]:
        scheduled_source_ids: list[str] = []
        for source in self.list_sources():
            cached_items = self.repository.list_synthetic_feed_items(
                source.source_id, limit=source.max_items
            )
            if self._source_needs_refresh(
                source, cached_items, lookahead_seconds=lookahead_seconds
            ) and self._schedule_background_refresh(source.source_id):
                scheduled_source_ids.append(source.source_id)
        return scheduled_source_ids

    def extract_article(
        self, article_url: str, *, fallback_title: str | None = None
    ) -> ExtractedSourceArticle:
        source = self._match_source_for_article_url(article_url)
        if source is None:
            raise SourceNotConfiguredError(
                f"No synthetic source matches article URL: {article_url}"
            )

        with self._open_client(source) as client:
            page_html = self._fetch_text(client, article_url, source)
        title, content_html, summary_text, published_at = _extract_article_payload(
            page_html, fallback_title, article_url
        )
        if not content_html:
            raise SourceBridgeError(
                f"Could not extract article content for: {article_url}"
            )
        return ExtractedSourceArticle(
            source_id=source.source_id,
            article_url=article_url,
            title=title,
            content_html=content_html,
            summary_text=summary_text,
            published_at=published_at,
        )

    def _source_needs_refresh(
        self,
        source: SourceDefinition,
        cached_items: list[SyntheticFeedItem],
        *,
        lookahead_seconds: float = 0.0,
    ) -> bool:
        state = self.repository.get_synthetic_source_state(source.source_id)
        if state is None:
            return True
        reference_time = parse_datetime(state.last_attempted_at)
        if reference_time is None:
            return True
        refresh_seconds = (
            source.refresh_seconds or self.settings.source_bridge_refresh_seconds
        )
        if refresh_seconds <= 0:
            return True
        age_seconds = (utc_now() - reference_time).total_seconds()
        if age_seconds + max(0.0, lookahead_seconds) >= refresh_seconds:
            return True
        return not cached_items

    def _match_source_for_article_url(
        self, article_url: str
    ) -> SourceDefinition | None:
        normalized_url = _normalize_url(article_url)
        if not normalized_url:
            return None

        hostname = (urlsplit(normalized_url).hostname or "").lower()
        ranked_matches: list[tuple[int, SourceDefinition]] = []
        for source in self.list_sources():
            source_hosts = {
                (urlsplit(start_url).hostname or "").lower()
                for start_url in source.start_urls
                if urlsplit(start_url).hostname
            }
            if source_hosts and hostname not in source_hosts:
                continue
            if any(
                re.search(pattern, normalized_url)
                for pattern in source.exclude_url_patterns
            ):
                continue
            if source.include_url_patterns:
                if not any(
                    re.search(pattern, normalized_url)
                    for pattern in source.include_url_patterns
                ):
                    continue
                ranked_matches.append((2, source))
                continue
            ranked_matches.append((1, source))

        if not ranked_matches:
            return None
        ranked_matches.sort(key=lambda item: (-item[0], item[1].source_id))
        return ranked_matches[0][1]

    def _open_client(self, source: SourceDefinition) -> Any:
        if source.fetch_backend == "browser":
            profile = self._get_browser_profile(source)
            return self.browser_client_factory(source, profile)
        return self.client_factory()

    def _get_browser_profile(self, source: SourceDefinition) -> AuthProfile:
        if not source.auth_profile:
            raise SourceBridgeError(
                f"Source {source.source_id!r} uses the browser backend but has no auth_profile."
            )
        profile = self.catalog.auth_profiles.get(source.auth_profile)
        if profile is None:
            raise SourceBridgeError(
                f"Source {source.source_id!r} references unknown auth profile {source.auth_profile!r}."
            )
        if profile.browser_profile_path is None and profile.browser_cdp_url is None:
            raise SourceBridgeError(
                f"Source {source.source_id!r} uses the browser backend but auth profile {profile.name!r} has neither browser_profile_path nor browser_cdp_url."
            )
        return profile

    def _discover_links(
        self, client: Any, source: SourceDefinition
    ) -> list[DiscoveredLink]:
        seen_urls: set[str] = set()
        discovered: list[DiscoveredLink] = []
        allowed_hosts = {
            (urlsplit(start_url).hostname or "").lower()
            for start_url in source.start_urls
            if urlsplit(start_url).hostname
        }

        for start_url in source.start_urls:
            html_text = self._fetch_text(client, start_url, source, stage="discovery")
            soup = BeautifulSoup(html_text, "html.parser")
            for anchor in soup.select(source.link_selector):
                href = anchor.get("href")
                if not href:
                    continue
                absolute_url = _normalize_url(urljoin(start_url, href))
                if not absolute_url:
                    continue
                hostname = (urlsplit(absolute_url).hostname or "").lower()
                if allowed_hosts and hostname not in allowed_hosts:
                    continue
                if source.include_url_patterns and not any(
                    re.search(pattern, absolute_url)
                    for pattern in source.include_url_patterns
                ):
                    continue
                if any(
                    re.search(pattern, absolute_url)
                    for pattern in source.exclude_url_patterns
                ):
                    continue
                if absolute_url in seen_urls:
                    continue
                seen_urls.add(absolute_url)
                discovered.append(
                    DiscoveredLink(
                        url=absolute_url,
                        title=_clean_text(anchor.get_text(" ", strip=True)),
                        source_page_url=start_url,
                    )
                )
                if len(discovered) >= max(source.max_items * 3, source.max_items):
                    return discovered
        return discovered

    def _build_items(
        self,
        client: Any,
        source: SourceDefinition,
        candidates: list[DiscoveredLink],
        *,
        snapshot_time: str,
    ) -> list[SyntheticFeedItem]:
        items: list[SyntheticFeedItem] = []
        for candidate in candidates:
            if len(items) >= source.max_items:
                break
            item = self._build_item_from_candidate(
                client,
                source,
                candidate,
                sort_index=len(items),
                snapshot_time=snapshot_time,
            )
            if item is not None:
                items.append(item)
        return items

    def _build_item_from_candidate(
        self,
        client: Any,
        source: SourceDefinition,
        candidate: DiscoveredLink,
        *,
        sort_index: int,
        snapshot_time: str,
    ) -> SyntheticFeedItem | None:
        try:
            page_html = self._fetch_text(client, candidate.url, source)
            title, content_html, summary_text, published_at = _extract_article_payload(
                page_html, candidate.title, candidate.url
            )
        except Exception:  # noqa: BLE001 - one bad article must not discard the feed snapshot
            fallback_title = candidate.title or candidate.url
            return SyntheticFeedItem(
                source_id=source.source_id,
                item_id=stable_hash(candidate.url),
                article_url=candidate.url,
                title=fallback_title,
                summary_text=None,
                content_html=None,
                published_at=None,
                source_page_url=candidate.source_page_url,
                sort_index=sort_index,
                discovered_at=snapshot_time,
            )

        return SyntheticFeedItem(
            source_id=source.source_id,
            item_id=stable_hash(candidate.url),
            article_url=candidate.url,
            title=title,
            summary_text=summary_text,
            content_html=content_html,
            published_at=published_at,
            source_page_url=candidate.source_page_url,
            sort_index=sort_index,
            discovered_at=snapshot_time,
        )

    def _fetch_text(
        self,
        client: Any,
        url: str,
        source: SourceDefinition,
        *,
        backend: str | None = None,
        stage: str = "article",
    ) -> str:
        response = self._fetch_response(
            client, url, source, backend=backend, stage=stage
        )
        response.raise_for_status()
        return response.text

    def _fetch_response(
        self,
        client: Any,
        url: str,
        source: SourceDefinition,
        *,
        backend: str | None = None,
        stage: str = "article",
    ) -> Any:
        selected_backend = backend or source.fetch_backend
        headers = self._build_request_headers(url, source.auth_profile)
        cookie_header = self._build_cookie_header(url, source.auth_profile)
        if selected_backend == "browser":
            return client.get(
                url,
                headers=headers,
                cookie_header=cookie_header,
                **self._browser_request_options(source, stage=stage),
            )

        request_headers = dict(headers)
        if cookie_header:
            request_headers["Cookie"] = cookie_header
        return client.get(url, headers=request_headers)

    def _build_request_headers(
        self, url: str, auth_profile_name: str | None
    ) -> dict[str, str]:
        if not auth_profile_name:
            return {}
        profile = self.catalog.auth_profiles.get(auth_profile_name)
        if profile is None:
            return {}

        hostname = (urlsplit(url).hostname or "").lower()
        if not any(_hostname_matches(hostname, domain) for domain in profile.domains):
            return {}

        return dict(profile.headers)

    def _build_cookie_header(
        self, url: str, auth_profile_name: str | None
    ) -> str | None:
        if not auth_profile_name:
            return None
        profile = self.catalog.auth_profiles.get(auth_profile_name)
        if profile is None:
            return None

        hostname = (urlsplit(url).hostname or "").lower()
        if not any(_hostname_matches(hostname, domain) for domain in profile.domains):
            return None

        if profile.cookie_header:
            return profile.cookie_header
        if profile.cookie_jar_path:
            return _load_cookie_header(profile.cookie_jar_path, hostname)
        return None

    def _browser_request_options(
        self, source: SourceDefinition, *, stage: str
    ) -> dict[str, Any]:
        if stage == "discovery":
            has_discovery_overrides = any(
                value is not None
                for value in (
                    source.discovery_browser_wait_until,
                    source.discovery_browser_wait_for_selector,
                    source.discovery_browser_settle_seconds,
                )
            )
            return {
                "wait_until": source.discovery_browser_wait_until
                or source.browser_wait_until,
                "wait_for_selector": (
                    source.discovery_browser_wait_for_selector
                    if source.discovery_browser_wait_for_selector is not None
                    else (
                        ""
                        if has_discovery_overrides
                        else source.browser_wait_for_selector
                    )
                ),
                "settle_seconds": (
                    source.discovery_browser_settle_seconds
                    if source.discovery_browser_settle_seconds is not None
                    else source.browser_settle_seconds
                ),
            }
        return {
            "wait_until": source.browser_wait_until,
            "wait_for_selector": source.browser_wait_for_selector,
            "settle_seconds": source.browser_settle_seconds,
        }

    def _render_rss(
        self, source: SourceDefinition, items: list[SyntheticFeedItem]
    ) -> str:
        root = ET.Element("rss", attrib={"version": "2.0"})
        channel = ET.SubElement(root, "channel")
        ET.SubElement(channel, "title").text = source.title
        ET.SubElement(channel, "link").text = source.start_urls[0]
        ET.SubElement(
            channel, "description"
        ).text = f"Synthetic feed generated from {source.start_urls[0]}"
        ET.SubElement(channel, "lastBuildDate").text = format_datetime(utc_now())

        for item in items:
            item_element = ET.SubElement(channel, "item")
            ET.SubElement(item_element, "title").text = item.title
            ET.SubElement(item_element, "link").text = item.article_url
            ET.SubElement(
                item_element, "guid", attrib={"isPermaLink": "false"}
            ).text = f"{source.source_id}:{item.item_id}"
            ET.SubElement(item_element, "description").text = item.summary_text or ""

            published_at = parse_datetime(item.published_at or item.discovered_at)
            if published_at is not None:
                ET.SubElement(item_element, "pubDate").text = format_datetime(
                    published_at
                )

            if item.content_html:
                ET.SubElement(
                    item_element, f"{{{CONTENT_NAMESPACE}}}encoded"
                ).text = item.content_html

        xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        return xml_bytes.decode("utf-8")


def _normalize_url(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, "")
    )


def _hostname_matches(hostname: str, domain: str) -> bool:
    normalized_domain = domain.lower().lstrip(".")
    return hostname == normalized_domain or hostname.endswith(f".{normalized_domain}")


def _load_cookie_header(path: Path, hostname: str) -> str | None:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return None

    cookie_lines = [
        line
        for line in raw.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not cookie_lines:
        return None
    if "\t" not in cookie_lines[0]:
        return raw

    cookie_pairs: list[str] = []
    for line in cookie_lines:
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, include_subdomains, _path, _secure, _expires, name, value = parts[:7]
        normalized_domain = domain.lower().lstrip(".")
        if hostname == normalized_domain or (
            include_subdomains.upper() == "TRUE"
            and hostname.endswith(f".{normalized_domain}")
        ):
            cookie_pairs.append(f"{name}={value}")
    return "; ".join(cookie_pairs) or None


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _extract_article_payload(
    page_html: str, fallback_title: str | None, article_url: str
) -> tuple[str, str | None, str | None, str | None]:
    soup = BeautifulSoup(page_html, "html.parser")
    document = Document(page_html)

    title = (
        _clean_text(_meta_content(soup, property_name="og:title"))
        or _clean_text(document.short_title())
        or _clean_text(soup.title.string if soup.title else None)
        or fallback_title
        or article_url
    )

    article_html = document.summary(html_partial=True).strip()
    content_html = None
    summary_text = None
    if article_html and len(strip_html(article_html)) >= 120:
        content_html = f"<article><h1>{html.escape(title)}</h1>{article_html}</article>"
        summary_text = excerpt(content_html, 320)
    else:
        meta_description = _clean_text(_meta_content(soup, name="description"))
        if meta_description:
            summary_text = excerpt(meta_description, 320)

    published_at = _extract_published_at(soup)
    return title, content_html, summary_text, published_at


def _meta_content(
    soup: BeautifulSoup, *, property_name: str | None = None, name: str | None = None
) -> str | None:
    if property_name:
        tag = soup.find("meta", attrs={"property": property_name})
        if tag and tag.get("content"):
            return str(tag["content"])
    if name:
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return str(tag["content"])
    return None


def _extract_published_at(soup: BeautifulSoup) -> str | None:
    candidates = [
        _meta_content(soup, property_name="article:published_time"),
        _meta_content(soup, property_name="og:article:published_time"),
        _meta_content(soup, name="article:published_time"),
        _meta_content(soup, name="parsely-pub-date"),
        _meta_content(soup, name="date"),
    ]
    time_tag = soup.find("time")
    if time_tag and time_tag.get("datetime"):
        candidates.append(str(time_tag["datetime"]))

    for candidate in candidates:
        parsed = parse_datetime(candidate)
        if parsed is not None:
            return parsed.isoformat()
    return None


def _build_http_probe_result(
    stage: str, url: str, status_code: int, page_html: str
) -> HttpProbeResult:
    soup = BeautifulSoup(page_html, "html.parser")
    document = Document(page_html)
    title = _clean_text(soup.title.string if soup.title and soup.title.string else None)
    article_text = strip_html(document.summary(html_partial=True).strip())
    body_text = " ".join(soup.get_text(" ", strip=True).split())
    page_state = _classify_page_state(status_code, title, body_text, article_text)
    excerpt_text = excerpt(body_text, 220)
    return HttpProbeResult(
        stage=stage,
        url=url,
        status_code=status_code,
        page_state=page_state,
        title=title,
        article_text_length=len(article_text),
        excerpt=excerpt_text,
    )


def _classify_page_state(
    status_code: int, title: str | None, body_text: str, article_text: str
) -> str:
    lowered_title = (title or "").lower()
    lowered_body = body_text.lower()

    if status_code >= 400 and (
        "access error" in lowered_title or "potential misuse" in lowered_body
    ):
        return "access_error"
    if "access error" in lowered_title or "potential misuse" in lowered_body:
        return "access_error"
    if "subscribe to read" in lowered_title:
        return "subscribe_wall"
    if "go further with an ft subscription" in lowered_body:
        return "subscribe_wall"
    if "try the ft digital edition" in lowered_body and "subscribe now" in lowered_body:
        return "subscribe_wall"
    if len(article_text) >= 400:
        return "content"
    if len(article_text) >= 120:
        return "partial_content"
    return "unknown"
