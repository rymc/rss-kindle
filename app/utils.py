from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from bs4 import BeautifulSoup, NavigableString, Tag


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(candidate)
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError, IndexError):
        return None


def format_datetime(value: str | None) -> str:
    parsed = parse_datetime(value)
    if not parsed:
        return "Unknown date"
    return parsed.strftime("%Y-%m-%d %H:%M UTC")


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

    if seconds < 60 * 60:
        count = max(1, seconds // 60)
        label = "min"
    elif seconds < 60 * 60 * 24:
        count = max(1, seconds // (60 * 60))
        label = "hour"
    elif seconds < 60 * 60 * 24 * 7:
        count = max(1, seconds // (60 * 60 * 24))
        label = "day"
    elif seconds < 60 * 60 * 24 * 30:
        count = max(1, seconds // (60 * 60 * 24 * 7))
        label = "week"
    elif seconds < 60 * 60 * 24 * 365:
        count = max(1, seconds // (60 * 60 * 24 * 30))
        label = "month"
    else:
        count = max(1, seconds // (60 * 60 * 24 * 365))
        label = "year"

    if label == "min":
        label_text = "min" if count == 1 else "mins"
    else:
        label_text = label if count == 1 else f"{label}s"

    if future:
        return f"in {count} {label_text}"
    return f"{count} {label_text} ago"


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)


def compact_source_label(feed_title: str | None, site_url: str | None = None) -> str:
    if feed_title:
        candidate = feed_title.strip()
        for delimiter in (" - ", " | ", " — "):
            candidate = candidate.split(delimiter, 1)[0].strip()
        prefix = candidate.split(": ", 1)[0].strip()
        if prefix and len(prefix) >= 4:
            candidate = prefix
        if candidate:
            return candidate

    if site_url:
        hostname = urlparse(site_url).hostname or ""
        hostname = hostname.lower().removeprefix("www.")
        parts = [part for part in hostname.split(".") if part]
        if len(parts) >= 2:
            candidate = parts[-3] if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in {"co", "com", "org", "net"} else parts[-2]
            return " ".join(word.capitalize() for word in candidate.split("-"))

    return "Unknown source"


def excerpt(value: str | None, length: int = 280) -> str:
    text = strip_html(value)
    if len(text) <= length:
        return text
    return text[: max(0, length - 1)].rstrip() + "…"


def is_hacker_news_site(url: str | None) -> bool:
    if not url:
        return False
    hostname = (urlparse(url).hostname or "").lower()
    return hostname == "news.ycombinator.com"


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
        soup = BeautifulSoup(html, "html.parser")
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if _is_hacker_news_comments_url(href):
                return href

    return None


def is_comments_only_summary(value: str | None) -> bool:
    normalized = strip_html(value).strip().lower()
    return normalized in {"comment", "comments", "discuss", "discussion"}


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "group"


def stable_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8"), usedforsecurity=False).hexdigest()


def is_kindle_user_agent(user_agent: str | None) -> bool:
    if not user_agent:
        return False
    candidate = user_agent.lower()
    return "kindle" in candidate or "silk" in candidate


def parse_positive_int(value: str | None, default: int) -> int:
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def simplify_html_for_kindle(value: str | None) -> str:
    if not value:
        return ""

    soup = BeautifulSoup(value, "html.parser")
    remove_tags = {
        "script",
        "style",
        "iframe",
        "video",
        "audio",
        "source",
        "svg",
        "picture",
        "img",
        "form",
        "input",
        "button",
        "nav",
        "footer",
        "aside",
        "noscript",
        "figure",
        "figcaption",
    }
    allowed_tags = {
        "article",
        "h1",
        "h2",
        "h3",
        "h4",
        "p",
        "ul",
        "ol",
        "li",
        "blockquote",
        "pre",
        "code",
        "a",
        "strong",
        "em",
        "b",
        "i",
        "hr",
        "br",
    }
    unwrap_tags = {"html", "body", "main", "section", "header", "div", "span"}

    for tag_name in remove_tags:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    for tag in list(soup.find_all(True)):
        if tag.name in unwrap_tags:
            tag.unwrap()
            continue
        if tag.name not in allowed_tags:
            tag.unwrap()
            continue
        if tag.name == "a":
            href = tag.get("href")
            tag.attrs = {}
            if href:
                parsed_href = urlparse(href)
                if parsed_href.scheme in {"http", "https"}:
                    tag["href"] = href
        else:
            tag.attrs = {}

    for paragraph in list(soup.find_all("p")):
        if not paragraph.get_text(" ", strip=True):
            paragraph.decompose()

    simplified = str(soup).strip()
    return simplified


def cleanup_kindle_article_html(
    value: str | None,
    *,
    item_title: str,
    source_label: str | None = None,
    feed_title: str | None = None,
    source_url: str | None = None,
) -> str:
    if not value:
        return ""

    soup = BeautifulSoup(value, "html.parser")
    container = soup.find("article") or soup

    for node in list(container.children):
        if isinstance(node, NavigableString):
            if not node.strip():
                node.extract()
                continue
            break
        if not isinstance(node, Tag):
            continue
        if _is_empty_tag(node):
            node.decompose()
            continue
        if _should_strip_leading_article_node(
            node,
            item_title=item_title,
            source_label=source_label,
            feed_title=feed_title,
            source_url=source_url,
        ):
            node.decompose()
            continue
        break

    for paragraph in list(soup.find_all("p")):
        if not paragraph.get_text(" ", strip=True):
            paragraph.decompose()

    return str(soup).strip()


def _is_empty_tag(node: Tag) -> bool:
    if node.name == "br":
        return True
    return not node.get_text(" ", strip=True) and not node.find("a", href=True)


def _should_strip_leading_article_node(
    node: Tag,
    *,
    item_title: str,
    source_label: str | None,
    feed_title: str | None,
    source_url: str | None,
) -> bool:
    text = node.get_text(" ", strip=True)
    if not text:
        return True

    if node.name in {"h1", "h2", "h3", "h4"}:
        return True
    if _matches_title(text, item_title):
        return True
    if _matches_source_label(text, source_label, feed_title):
        return True
    if _looks_like_date_line(text):
        return True
    if _looks_like_byline(text):
        return True
    if _looks_like_source_link(node, text, source_url):
        return True
    if node.name in {"hr"}:
        return True
    return False


def _normalize_compare_text(value: str | None) -> str:
    if not value:
        return ""
    lowered = value.lower().replace("’", "'").replace("&nbsp;", " ")
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _matches_title(text: str, item_title: str) -> bool:
    candidate = _normalize_compare_text(text)
    title = _normalize_compare_text(item_title)
    if not candidate or not title:
        return False
    if candidate == title:
        return True
    if candidate.startswith(title) and len(candidate) - len(title) <= 24:
        return True
    if title.startswith(candidate) and len(title) - len(candidate) <= 24:
        return True
    return False


def _matches_source_label(text: str, source_label: str | None, feed_title: str | None) -> bool:
    candidate = _normalize_compare_text(text)
    if not candidate:
        return False
    candidate_words = len(candidate.split())
    if candidate_words > 12:
        return False

    for label in (source_label, feed_title):
        normalized_label = _normalize_compare_text(label)
        if not normalized_label:
            continue
        if candidate == normalized_label:
            return True
        if candidate.startswith(normalized_label) or normalized_label.startswith(candidate):
            return True
    return False


def _looks_like_date_line(text: str) -> bool:
    candidate = text.strip()
    if not candidate:
        return False
    if parse_datetime(candidate) is not None:
        return True

    lowered = candidate.lower()
    if len(candidate) <= 80:
        for prefix in ("published", "updated", "last updated"):
            if lowered.startswith(prefix):
                trimmed = candidate[len(prefix) :].lstrip(": ").strip()
                if parse_datetime(trimmed) is not None:
                    return True
        if re.search(r"\b\d{4}-\d{2}-\d{2}\b", candidate) and re.search(r"\b\d{1,2}:\d{2}\b", candidate):
            return True
    return False


def _looks_like_byline(text: str) -> bool:
    candidate = text.strip()
    lowered = candidate.lower()
    return len(candidate) <= 80 and (lowered.startswith("by ") or lowered.startswith("byline:"))


def _looks_like_source_link(node: Tag, text: str, source_url: str | None) -> bool:
    lowered = text.strip().lower()
    if lowered in {"source", "open the source article", "open original", "original", "source article"}:
        return True

    if node.name == "a":
        href = node.get("href")
        if source_url and href == source_url and len(text.strip()) <= 40:
            return True

    links = node.find_all("a", href=True)
    if len(links) == 1:
        href = links[0].get("href")
        link_text = links[0].get_text(" ", strip=True).lower()
        if link_text in {"source", "open the source article", "open original", "original", "source article"}:
            return True
        if source_url and href == source_url and len(text.strip()) <= 40:
            return True

    return False


def _is_hacker_news_comments_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and (parsed.hostname or "").lower() == "news.ycombinator.com" and parsed.path == "/item"
