from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from functools import lru_cache
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def parse_datetime(value: str | None) -> datetime | None:
    if not value or not (candidate := value.strip()):
        return None
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(candidate)
        except (TypeError, ValueError, IndexError):
            return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def format_datetime(value: str | None) -> str:
    parsed = parse_datetime(value)
    return parsed.strftime("%Y-%m-%d %H:%M UTC") if parsed else "Unknown date"


def format_relative_time(value: str | None, reference: datetime | None = None) -> str:
    parsed = parse_datetime(value)
    if not parsed:
        return "Unknown time"

    reference = reference.astimezone(UTC) if reference else utc_now()
    delta_seconds = int((reference - parsed).total_seconds())
    future = delta_seconds < 0
    seconds = abs(delta_seconds)
    if seconds < 60:
        return "just now"

    count, label = _relative_time_unit(seconds)
    label_text = (
        ("min" if count == 1 else "mins")
        if label == "min"
        else (label if count == 1 else f"{label}s")
    )
    return f"in {count} {label_text}" if future else f"{count} {label_text} ago"


def _relative_time_unit(seconds: int) -> tuple[int, str]:
    units = (
        (60 * 60, 60, "min"),
        (60 * 60 * 24, 60 * 60, "hour"),
        (60 * 60 * 24 * 7, 60 * 60 * 24, "day"),
        (60 * 60 * 24 * 30, 60 * 60 * 24 * 7, "week"),
        (60 * 60 * 24 * 365, 60 * 60 * 24 * 30, "month"),
    )
    for upper_bound, divisor, label in units:
        if seconds < upper_bound:
            return max(1, seconds // divisor), label
    return max(1, seconds // (60 * 60 * 24 * 365)), "year"


def strip_html(value: str | None) -> str:
    return (
        BeautifulSoup(value, "html.parser").get_text(" ", strip=True) if value else ""
    )


@lru_cache(maxsize=256)
def compact_source_label(feed_title: str | None, site_url: str | None = None) -> str:
    if feed_title and (candidate := _compact_feed_title(feed_title)):
        return candidate
    if site_url and (candidate := _hostname_label(site_url)):
        return candidate
    return "Unknown source"


def _compact_feed_title(feed_title: str) -> str:
    candidate = feed_title.strip()
    for delimiter in (" - ", " | ", " — "):
        candidate = candidate.split(delimiter, 1)[0].strip()
    prefix = candidate.split(": ", 1)[0].strip()
    return prefix if len(prefix) >= 4 else candidate


def _hostname_label(site_url: str) -> str:
    hostname = (urlparse(site_url).hostname or "").lower().removeprefix("www.")
    parts = [part for part in hostname.split(".") if part]
    if len(parts) < 2:
        return ""
    compound_suffix = (
        len(parts) >= 3
        and len(parts[-1]) == 2
        and parts[-2]
        in {
            "co",
            "com",
            "org",
            "net",
        }
    )
    candidate = parts[-3] if compound_suffix else parts[-2]
    return " ".join(word.capitalize() for word in candidate.split("-"))


def excerpt(value: str | None, length: int = 280) -> str:
    return truncate_text(strip_html(value), length)


def truncate_text(value: str | None, length: int = 280) -> str:
    text = value or ""
    if len(text) <= length:
        return text
    return text[: max(0, length - 1)].rstrip() + "…"


@lru_cache(maxsize=512)
def is_hacker_news_site(url: str | None) -> bool:
    return display_hostname(url) == "news.ycombinator.com"


@lru_cache(maxsize=512)
def display_hostname(url: str | None) -> str | None:
    if not url:
        return None
    try:
        hostname = (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return None
    return hostname or None


@lru_cache(maxsize=512)
def hacker_news_item_id(url: str | None) -> int | None:
    if not _is_hacker_news_comments_url(url):
        return None
    raw_id = parse_qs(urlparse(url or "").query).get("id", [""])[0]
    try:
        item_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    return item_id if item_id > 0 else None


@lru_cache(maxsize=512)
def hacker_news_external_host(url: str | None) -> str | None:
    hostname = display_hostname(url)
    return hostname if hostname and hostname != "news.ycombinator.com" else None


@lru_cache(maxsize=512)
def hacker_news_destination_host(
    entry_url: str | None,
    feed_site_url: str | None,
) -> str | None:
    if not is_hacker_news_site(feed_site_url) or not entry_url:
        return None
    return hacker_news_external_host(entry_url)


def extract_hacker_news_comments_url(
    *,
    summary_html: str | None,
    content_html: str | None,
    entry_url: str | None,
    feed_site_url: str | None,
) -> str | None:
    if not is_hacker_news_site(feed_site_url):
        return None
    if _is_hacker_news_comments_url(entry_url):
        return entry_url
    for html in (summary_html, content_html):
        if not html:
            continue
        for anchor in BeautifulSoup(html, "html.parser").find_all("a", href=True):
            href = anchor["href"].strip()
            if _is_hacker_news_comments_url(href):
                return href
    return None


@lru_cache(maxsize=64)
def is_comments_only_summary(value: str | None) -> bool:
    return strip_html(value).strip().lower() in {
        "comment",
        "comments",
        "discuss",
        "discussion",
    }


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "group"


def stable_hash(value: str) -> str:
    return hashlib.sha1(value.encode(), usedforsecurity=False).hexdigest()


def is_kindle_user_agent(user_agent: str | None) -> bool:
    candidate = (user_agent or "").lower()
    return "kindle" in candidate or "silk" in candidate


def parse_positive_int(value: str | None, default: int) -> int:
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _is_hacker_news_comments_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and display_hostname(url) == "news.ycombinator.com"
        and parsed.path == "/item"
    )
