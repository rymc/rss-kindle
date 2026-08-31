from __future__ import annotations

import re
from functools import lru_cache
from urllib.parse import urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

from app.utils import parse_datetime

REMOVE_TAGS = frozenset(
    {
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
)
ALLOWED_TAGS = frozenset(
    {
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
)
UNWRAP_TAGS = frozenset({"html", "body", "main", "section", "header", "div", "span"})


def simplify_html_for_kindle(
    value: str | None,
    *,
    item_title: str | None = None,
    source_label: str | None = None,
    feed_title: str | None = None,
    source_url: str | None = None,
) -> str:
    if not value:
        return ""
    return _simplify_html_for_kindle_cached(
        value,
        item_title,
        source_label,
        feed_title,
        source_url,
    )


@lru_cache(maxsize=64)
def _simplify_html_for_kindle_cached(
    value: str,
    item_title: str | None,
    source_label: str | None,
    feed_title: str | None,
    source_url: str | None,
) -> str:
    soup = BeautifulSoup(value, "lxml")
    _replace_media_with_text(soup)
    for tag in soup.find_all(REMOVE_TAGS):
        tag.decompose()
    for tag in list(soup.find_all(True)):
        _sanitize_tag(tag)
    _remove_empty_paragraphs(soup)
    if item_title is not None:
        _cleanup_leading_article_nodes(
            soup,
            item_title=item_title,
            source_label=source_label,
            feed_title=feed_title,
            source_url=source_url,
        )
    return str(soup).strip()


def _replace_media_with_text(soup: BeautifulSoup) -> None:
    for figure in list(soup.find_all("figure")):
        caption = figure.find("figcaption")
        description = caption.get_text(" ", strip=True) if caption else ""
        if not description:
            image = figure.find("img")
            description = _useful_alt_text(image.get("alt") if image else None)
        replacement = _description_paragraph(soup, "Figure", description)
        if replacement is None:
            figure.decompose()
        else:
            figure.replace_with(replacement)

    for picture in list(soup.find_all("picture")):
        image = picture.find("img")
        replacement = _description_emphasis(
            soup,
            "Image",
            _useful_alt_text(image.get("alt") if image else None),
        )
        if replacement is None:
            picture.decompose()
        else:
            picture.replace_with(replacement)

    for image in list(soup.find_all("img")):
        replacement = _description_emphasis(
            soup,
            "Image",
            _useful_alt_text(image.get("alt")),
        )
        if replacement is None:
            image.decompose()
        else:
            image.replace_with(replacement)


def _description_paragraph(
    soup: BeautifulSoup, label: str, description: str
) -> Tag | None:
    if not description:
        return None
    paragraph = soup.new_tag("p")
    emphasis = soup.new_tag("em")
    emphasis.string = f"{label}: {description}"
    paragraph.append(emphasis)
    return paragraph


def _description_emphasis(
    soup: BeautifulSoup, label: str, description: str
) -> Tag | None:
    if not description:
        return None
    emphasis = soup.new_tag("em")
    emphasis.string = f"{label}: {description}"
    return emphasis


def _useful_alt_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    description = re.sub(r"\s+", " ", value).strip()
    normalized = description.lower().strip(". ")
    if normalized in {"", "image", "photo", "picture", "figure", "thumbnail"}:
        return ""
    if re.fullmatch(r"[^/\\]+\.(?:avif|gif|jpe?g|png|webp)", normalized):
        return ""
    return description[:500]


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
    _cleanup_leading_article_nodes(
        soup,
        item_title=item_title,
        source_label=source_label,
        feed_title=feed_title,
        source_url=source_url,
    )
    _remove_empty_paragraphs(soup)
    return str(soup).strip()


def _sanitize_tag(tag: Tag) -> None:
    if tag.name in UNWRAP_TAGS or tag.name not in ALLOWED_TAGS:
        tag.unwrap()
        return
    if tag.name != "a":
        tag.attrs = {}
        return
    href = tag.get("href")
    tag.attrs = {}
    if isinstance(href, str) and urlparse(href).scheme in {"http", "https"}:
        tag["href"] = href


def _remove_empty_paragraphs(soup: BeautifulSoup) -> None:
    for paragraph in list(soup.find_all("p")):
        if not paragraph.get_text(" ", strip=True):
            paragraph.decompose()


def _cleanup_leading_article_nodes(
    soup: BeautifulSoup,
    *,
    item_title: str,
    source_label: str | None,
    feed_title: str | None,
    source_url: str | None,
) -> None:
    container = soup.find("article") or soup
    for node in list(container.children):
        if isinstance(node, NavigableString):
            if not node.strip():
                node.extract()
                continue
            break
        if not isinstance(node, Tag):
            continue
        if _is_empty_tag(node) or _should_strip_leading_article_node(
            node,
            item_title=item_title,
            source_label=source_label,
            feed_title=feed_title,
            source_url=source_url,
        ):
            node.decompose()
            continue
        break


def _is_empty_tag(node: Tag) -> bool:
    return node.name == "br" or (
        not node.get_text(" ", strip=True) and not node.find("a", href=True)
    )


def _should_strip_leading_article_node(
    node: Tag,
    *,
    item_title: str,
    source_label: str | None,
    feed_title: str | None,
    source_url: str | None,
) -> bool:
    text = node.get_text(" ", strip=True)
    return (
        not text
        or node.name in {"h1", "h2", "h3", "h4", "hr"}
        or _matches_title(text, item_title)
        or _matches_source_label(text, source_label, feed_title)
        or _looks_like_date_line(text)
        or _looks_like_byline(text)
        or _looks_like_source_link(node, text, source_url)
    )


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
    return (
        candidate == title
        or (candidate.startswith(title) and len(candidate) - len(title) <= 24)
        or (title.startswith(candidate) and len(title) - len(candidate) <= 24)
    )


def _matches_source_label(
    text: str,
    source_label: str | None,
    feed_title: str | None,
) -> bool:
    candidate = _normalize_compare_text(text)
    if not candidate or len(candidate.split()) > 12:
        return False
    for label in (source_label, feed_title):
        normalized_label = _normalize_compare_text(label)
        if normalized_label and (
            candidate == normalized_label
            or candidate.startswith(normalized_label)
            or normalized_label.startswith(candidate)
        ):
            return True
    return False


def _looks_like_date_line(text: str) -> bool:
    candidate = text.strip()
    if not candidate:
        return False
    if parse_datetime(candidate) is not None:
        return True
    if len(candidate) > 80:
        return False
    lowered = candidate.lower()
    for prefix in ("published", "updated", "last updated"):
        if lowered.startswith(prefix):
            trimmed = candidate[len(prefix) :].lstrip(": ").strip()
            if parse_datetime(trimmed) is not None:
                return True
    return bool(
        re.search(r"\b\d{4}-\d{2}-\d{2}\b", candidate)
        and re.search(r"\b\d{1,2}:\d{2}\b", candidate)
    )


def _looks_like_byline(text: str) -> bool:
    candidate = text.strip()
    return len(candidate) <= 80 and candidate.lower().startswith(("by ", "byline:"))


def _looks_like_source_link(node: Tag, text: str, source_url: str | None) -> bool:
    source_labels = {
        "source",
        "open the source article",
        "open original",
        "original",
        "source article",
    }
    if text.strip().lower() in source_labels:
        return True
    if node.name == "a" and _is_short_source_url(node, text, source_url):
        return True
    links = node.find_all("a", href=True)
    if len(links) != 1:
        return False
    link = links[0]
    return link.get_text(
        " ", strip=True
    ).lower() in source_labels or _is_short_source_url(
        link,
        text,
        source_url,
    )


def _is_short_source_url(node: Tag, text: str, source_url: str | None) -> bool:
    return bool(
        source_url and node.get("href") == source_url and len(text.strip()) <= 40
    )
