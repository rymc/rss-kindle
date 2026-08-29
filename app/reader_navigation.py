from __future__ import annotations

import base64
import json
import re
import zlib
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import FastAPI, Request

G_READER_ENTRY_PREFIX = "tag:google.com,2005:reader/item/"
ScopeKind = Literal["home", "starred", "group", "feed"]


def _encode_token(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode()
    compressed = zlib.compress(raw, level=9)
    if len(compressed) + 1 < len(raw):
        return "z" + base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")
    return "j" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_token(token: str | None) -> dict[str, object] | None:
    if not token:
        return None
    encoding = token[0] if token[0] in {"j", "z"} else "j"
    encoded = token[1:] if token[0] in {"j", "z"} else token
    padding = "=" * ((4 - len(encoded) % 4) % 4)
    try:
        packed = base64.urlsafe_b64decode(encoded + padding)
        if encoding == "z":
            decompressor = zlib.decompressobj()
            raw_bytes = decompressor.decompress(packed, 32_769)
            if (
                len(raw_bytes) > 32_768
                or decompressor.unconsumed_tail
                or not decompressor.eof
            ):
                return None
        else:
            raw_bytes = packed
        payload = json.loads(raw_bytes.decode())
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, zlib.error):
        return None
    return payload if isinstance(payload, dict) else None


def _decode_cursor_stack(value: str | None) -> tuple[str, ...]:
    payload = _decode_token(value)
    stack = payload.get("stack") if payload else None
    if not isinstance(stack, list):
        return ()
    return tuple(str(item) for item in stack)


@dataclass(frozen=True)
class StreamScope:
    kind: ScopeKind
    value: str | None = None

    def __post_init__(self) -> None:
        requires_value = self.kind in {"group", "feed"}
        if requires_value != bool(self.value):
            raise ValueError(f"{self.kind} scope has an invalid value")

    def path(self, app: FastAPI) -> str:
        if self.kind == "home":
            return str(app.url_path_for("home"))
        if self.kind == "starred":
            return str(app.url_path_for("starred_view"))
        if self.kind == "group":
            return str(app.url_path_for("group_view", slug=self.value))
        return str(app.url_path_for("feed_view", feed_id=self.value))

    @classmethod
    def from_path(cls, path: str) -> StreamScope:
        if path == "/":
            return cls("home")
        if path == "/starred":
            return cls("starred")
        if path.startswith("/groups/"):
            return cls("group", path.removeprefix("/groups/"))
        if path.startswith("/feeds/"):
            return cls("feed", path.removeprefix("/feeds/"))
        raise ValueError(f"Unsupported stream path: {path}")


@dataclass(frozen=True)
class PageLinks:
    newer: str | None
    older: str | None


@dataclass(frozen=True)
class StreamRequest:
    scope: StreamScope
    continuation: str | None = None
    history: tuple[str, ...] = ()
    include_read: bool = False

    @classmethod
    def from_request(cls, request: Request, scope: StreamScope) -> StreamRequest:
        return cls(
            scope=scope,
            continuation=request.query_params.get("c"),
            history=_decode_cursor_stack(request.query_params.get("b")),
            include_read=request.query_params.get("read") == "1",
        )

    @classmethod
    def from_url(cls, relative_url: str) -> StreamRequest:
        parsed = urlsplit(relative_url)
        query = parse_qs(parsed.query)
        return cls(
            scope=StreamScope.from_path(parsed.path or "/"),
            continuation=query.get("c", [None])[0],
            history=_decode_cursor_stack(query.get("b", [None])[0]),
            include_read=query.get("read", [None])[0] == "1",
        )

    @property
    def offset(self) -> int:
        if not self.continuation:
            return 0
        prefix, separator, raw_offset = self.continuation.rpartition(":")
        if separator != ":" or prefix not in {"group-offset", "sorted-offset"}:
            return 0
        try:
            return max(0, int(raw_offset))
        except ValueError:
            return 0

    def url(self, app: FastAPI) -> str:
        params: dict[str, str] = {}
        if self.include_read:
            params["read"] = "1"
        if self.continuation:
            params["c"] = self.continuation
        if self.history:
            params["b"] = _encode_token({"stack": list(self.history)})
        path = self.scope.path(app)
        return f"{path}?{urlencode(params)}" if params else path

    def read_toggle_url(self, app: FastAPI) -> str:
        return StreamRequest(
            scope=self.scope,
            include_read=not self.include_read,
        ).url(app)

    def page_links(self, app: FastAPI, next_continuation: str | None) -> PageLinks:
        newer = None
        if self.history:
            newer = StreamRequest(
                scope=self.scope,
                continuation=self.history[-1] or None,
                history=self.history[:-1],
                include_read=self.include_read,
            ).url(app)

        older = None
        if next_continuation:
            older = StreamRequest(
                scope=self.scope,
                continuation=next_continuation,
                history=(*self.history, self.continuation or ""),
                include_read=self.include_read,
            ).url(app)
        return PageLinks(newer=newer, older=older)


@dataclass(frozen=True)
class ReadingContext:
    entry_ids: tuple[str, ...]
    back_url: str
    next_page_url: str | None

    def encode(self) -> str:
        return _encode_token(
            {
                "entry_ids": list(self.entry_ids),
                "back_url": self.back_url,
                "next_page_url": self.next_page_url,
            }
        )

    @classmethod
    def decode(cls, token: str | None) -> ReadingContext | None:
        payload = _decode_token(token)
        if not payload:
            return None
        entry_ids = payload.get("entry_ids")
        back_url = payload.get("back_url")
        next_page_url = payload.get("next_page_url")
        if not isinstance(entry_ids, list) or not isinstance(back_url, str):
            return None
        try:
            StreamRequest.from_url(back_url)
            if next_page_url:
                StreamRequest.from_url(str(next_page_url))
        except ValueError:
            return None
        return cls(
            entry_ids=tuple(str(entry_id) for entry_id in entry_ids),
            back_url=back_url,
            next_page_url=str(next_page_url) if next_page_url else None,
        )


def article_key(entry_id: str) -> str:
    if entry_id.startswith(G_READER_ENTRY_PREFIX):
        suffix = entry_id.removeprefix(G_READER_ENTRY_PREFIX)
        if re.fullmatch(r"[A-Za-z0-9_-]+", suffix):
            return f"g-{suffix}"
    encoded = base64.urlsafe_b64encode(entry_id.encode()).decode("ascii").rstrip("=")
    return f"b-{encoded}"


def entry_id_from_article_key(value: str) -> str | None:
    if value.startswith("g-") and re.fullmatch(r"[A-Za-z0-9_-]+", value[2:]):
        return f"{G_READER_ENTRY_PREFIX}{value[2:]}"
    if not value.startswith("b-"):
        return None
    encoded = value[2:]
    if not encoded or not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
        return None
    padding = "=" * ((4 - len(encoded) % 4) % 4)
    try:
        entry_id = base64.urlsafe_b64decode(encoded + padding).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    return entry_id if entry_id and len(entry_id) <= 4096 else None


def item_detail_url(app: FastAPI, entry_id: str, context_id: str | None = None) -> str:
    value = article_key(entry_id)
    if context_id:
        return str(
            app.url_path_for(
                "read_article_with_context",
                article_key=value,
                context_id=context_id,
            )
        )
    return str(app.url_path_for("read_article", article_key=value))
