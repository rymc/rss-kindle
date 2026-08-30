from __future__ import annotations

import hashlib
import html
import logging
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from readability import Document

from app.article_html import simplify_html_for_kindle
from app.config import Settings
from app.freshrss import FreshRSSEntry
from app.repository import CachedArticle, Repository
from app.utils import compact_source_label, strip_html

logger = logging.getLogger(__name__)


ExtractionStatus = Literal["success", "failed"]


@dataclass(frozen=True)
class ExtractedArticle:
    html: str
    text: str
    extraction_status: ExtractionStatus
    error_message: str | None


class ArticleExtractor:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        client_factory: Callable[[], Any] | None = None,
        bridge_client_factory: Callable[[], Any] | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.client_factory = client_factory or self._default_client_factory
        self.bridge_client_factory = (
            bridge_client_factory or self._default_bridge_client_factory
        )
        self._shared_client = (
            None if client_factory is not None else self._default_client_factory()
        )
        self._shared_bridge_client = (
            None
            if bridge_client_factory is not None
            else self._default_client_factory()
        )

    def _default_client_factory(self) -> httpx.Client:
        return httpx.Client(
            follow_redirects=True,
            timeout=self.settings.http_timeout_seconds,
            headers={"User-Agent": self.settings.user_agent},
        )

    def _default_bridge_client_factory(self) -> httpx.Client:
        timeout = httpx.Timeout(
            self.settings.source_bridge_timeout_seconds,
            connect=min(5, self.settings.http_timeout_seconds),
        )
        return httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": self.settings.user_agent},
        )

    @contextmanager
    def _open_client(self, *, bridge: bool = False):
        shared_client = self._shared_bridge_client if bridge else self._shared_client
        client_factory = self.bridge_client_factory if bridge else self.client_factory
        if shared_client is not None:
            yield shared_client
            return
        with client_factory() as client:
            yield client

    def close(self) -> None:
        if self._shared_client is not None:
            self._shared_client.close()
        if self._shared_bridge_client is not None:
            self._shared_bridge_client.close()

    def prewarm(self, entries: list[FreshRSSEntry]) -> None:
        for entry in entries:
            try:
                article = self.ensure_extracted(entry)
                simplify_html_for_kindle(
                    article.html,
                    item_title=entry.title,
                    source_label=compact_source_label(
                        entry.feed_title, entry.feed_site_url
                    ),
                    feed_title=entry.feed_title,
                    source_url=entry.url,
                )
            except Exception:
                logger.warning("Article prewarm failed for %s", entry.id, exc_info=True)

    def ensure_extracted(self, entry: FreshRSSEntry) -> ExtractedArticle:
        cached = self.repository.get_cached_article(entry.id, entry.url)
        source_fingerprint = self._source_fingerprint(entry)
        if (
            cached
            and cached.extraction_status == "success"
            and cached.source_fingerprint == source_fingerprint
        ):
            return self._from_cache(cached)
        feed_content = self._extract_feed_content(entry)
        if feed_content:
            return self._save(entry, feed_content, source_fingerprint)
        if bridge_content := self._extract_via_source_bridge(entry):
            return self._save(entry, bridge_content, source_fingerprint)
        if (
            cached
            and cached.extraction_status != "success"
            and cached.source_fingerprint == source_fingerprint
        ):
            return self._from_cache(cached)
        return self._save(
            entry,
            self._extract_from_source(entry),
            source_fingerprint,
        )

    def _extract_from_source(self, entry: FreshRSSEntry) -> ExtractedArticle:
        if not entry.url:
            return self._failure(entry, "Item has no source URL for extraction.")
        try:
            with self._open_client() as client:
                response = client.get(entry.url)
                response.raise_for_status()
            return self._extract_content(entry, response.text)
        except Exception as exc:  # noqa: BLE001 - extraction failures become cached fallback content
            return self._failure(entry, str(exc))

    def _save(
        self,
        entry: FreshRSSEntry,
        article: ExtractedArticle,
        source_fingerprint: str,
    ) -> ExtractedArticle:
        self.repository.save_cached_article(
            entry.id,
            source_url=entry.url,
            extracted_html=article.html,
            extraction_status=article.extraction_status,
            error_message=article.error_message,
            source_fingerprint=source_fingerprint,
        )
        return article

    def _from_cache(self, cached: CachedArticle) -> ExtractedArticle:
        status: ExtractionStatus = (
            "success" if cached.extraction_status == "success" else "failed"
        )
        return ExtractedArticle(
            html=cached.extracted_html or "",
            text="",
            extraction_status=status,
            error_message=cached.error_message,
        )

    def _failure(self, entry: FreshRSSEntry, reason: str) -> ExtractedArticle:
        fallback_html, fallback_text = self._build_failure_content(entry, reason)
        return ExtractedArticle(
            html=fallback_html,
            text=fallback_text,
            extraction_status="failed",
            error_message=reason,
        )

    def _extract_content(
        self, entry: FreshRSSEntry, page_html: str
    ) -> ExtractedArticle:
        if self._is_substack_url(entry.url):
            substack_result = self._extract_substack_content(entry, page_html)
            if substack_result is not None:
                return substack_result

        document = Document(page_html)
        article_html = document.summary(html_partial=True).strip()
        if not article_html:
            raise ValueError("Readability returned no article content.")
        title = document.short_title() or entry.title
        wrapped_html = self._wrap_article(title, article_html)
        if not self._is_meaningful_extraction(entry, wrapped_html):
            raise ValueError("Extractor returned too little meaningful article text.")
        return ExtractedArticle(wrapped_html, strip_html(wrapped_html), "success", None)

    def _extract_substack_content(
        self,
        entry: FreshRSSEntry,
        page_html: str,
    ) -> ExtractedArticle | None:
        soup = BeautifulSoup(page_html, "html.parser")
        body = soup.select_one(".available-content .body.markup")
        if body is not None:
            for node in body.select("script, style"):
                node.decompose()
            body_html = body.decode_contents().strip()
            if strip_html(body_html):
                wrapped_html = self._wrap_article(entry.title, body_html)
                return ExtractedArticle(
                    wrapped_html, strip_html(wrapped_html), "success", None
                )

        if soup.select_one('[data-testid="paywall"], .paywall'):
            message = (
                "This Substack post is paywalled, so the server cannot extract the article body "
                "without access to the publisher account."
            )
            fallback_html, fallback_text = self._build_failure_content(entry, message)
            return ExtractedArticle(fallback_html, fallback_text, "failed", message)

        return None

    def _extract_feed_content(self, entry: FreshRSSEntry) -> ExtractedArticle | None:
        feed_html = entry.content_html or entry.summary_html
        if not feed_html:
            return None

        candidate_html = feed_html.strip()
        if not candidate_html:
            return None

        if "<article" not in candidate_html.lower():
            candidate_html = self._wrap_article(entry.title, candidate_html)

        if not self._is_meaningful_extraction(entry, candidate_html):
            return None

        return ExtractedArticle(
            html=candidate_html,
            text=strip_html(candidate_html),
            extraction_status="success",
            error_message=None,
        )

    def _extract_via_source_bridge(
        self, entry: FreshRSSEntry
    ) -> ExtractedArticle | None:
        if (
            not self.settings.source_bridge_api_url
            or not self.settings.source_bridge_access_token
            or not entry.url
        ):
            return None

        headers = {
            "X-Source-Bridge-Token": self.settings.source_bridge_access_token
        }

        try:
            with self._open_client(bridge=True) as client:
                response = client.get(
                    f"{self.settings.source_bridge_api_url}/extract",
                    params={"url": entry.url, "title": entry.title},
                    headers=headers,
                )
                response.raise_for_status()
        except Exception:  # noqa: BLE001 - the bridge is an optional extraction source
            return None

        try:
            payload = response.json()
        except ValueError:
            return None

        content_html = payload.get("content_html")
        if not isinstance(content_html, str) or not content_html.strip():
            return None

        candidate_html = content_html.strip()
        if not self._is_meaningful_extraction(entry, candidate_html):
            return None

        return ExtractedArticle(
            html=candidate_html,
            text=strip_html(candidate_html),
            extraction_status="success",
            error_message=None,
        )

    def _wrap_article(self, title: str, body_html: str) -> str:
        return f"<article><h1>{html.escape(title)}</h1>{body_html}</article>"

    def _is_substack_url(self, url: str | None) -> bool:
        if not url:
            return False
        hostname = urlparse(url).hostname or ""
        return hostname.endswith("substack.com") or "substack" in hostname

    def _is_meaningful_extraction(
        self, entry: FreshRSSEntry, wrapped_html: str
    ) -> bool:
        soup = BeautifulSoup(wrapped_html, "html.parser")
        article = soup.find("article") or soup
        text = strip_html(str(article))
        body_text = text.replace(entry.title, "", 1).strip()
        if not body_text:
            return False
        if "Read distraction-free on Substack" in body_text and len(body_text) < 120:
            return False
        content_blocks = [
            tag.get_text(" ", strip=True)
            for tag in article.find_all(["p", "li", "blockquote", "pre"])
            if tag.get_text(" ", strip=True)
        ]
        if not content_blocks:
            return False

        total_block_text = sum(len(block) for block in content_blocks)
        combined_block_text = " ".join(content_blocks)
        if total_block_text < 280 and self._looks_like_marketing_copy(
            combined_block_text
        ):
            return False
        if any(len(block) >= 120 for block in content_blocks):
            return True
        if len(content_blocks) >= 2 and total_block_text >= 140:
            return True
        return total_block_text >= 90

    @staticmethod
    def _source_fingerprint(entry: FreshRSSEntry) -> str:
        digest = hashlib.sha256()
        for value in (
            entry.title,
            entry.url,
            entry.summary_html,
            entry.content_html,
        ):
            encoded = (value or "").encode()
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        return digest.hexdigest()

    def _looks_like_marketing_copy(self, text: str) -> bool:
        lowered = text.lower()
        marker_hits = sum(
            1
            for marker in (
                " pro",
                "$",
                "desktop app",
                "cli",
                "ocr",
                "download",
                "downloads",
                "pricing",
                "free trial",
                "sign up",
                "subscribe",
                "subscribe to read",
                "subscribe to continue",
                "complete digital access",
                "pay a year upfront",
                "start your trial",
            )
            if marker in lowered
        )
        return marker_hits >= 2

    def _build_failure_content(
        self, entry: FreshRSSEntry, reason: str
    ) -> tuple[str, str]:
        parts = [
            "<article>",
            f"<h1>{html.escape(entry.title)}</h1>",
            f"<p>{html.escape(reason)}</p>",
        ]
        fallback_html = entry.content_html or entry.summary_html
        if fallback_html:
            parts.append(fallback_html)
        elif (
            entry.summary_text
            and entry.summary_text not in {"...", "Read more"}
            and len(entry.summary_text) > 20
        ):
            parts.append("<h2>Feed excerpt</h2>")
            parts.append(f"<p>{html.escape(entry.summary_text)}</p>")
        if entry.url:
            safe_url = html.escape(entry.url, quote=True)
            parts.append(
                f'<p><a href="{safe_url}" target="_blank" rel="noreferrer">Open the source article</a></p>'
            )
        parts.append("</article>")
        wrapped_html = "".join(parts)
        return wrapped_html, strip_html(wrapped_html)
