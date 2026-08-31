from __future__ import annotations

import base64
import json
import re
import zlib
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import FastAPI, Request

from app.freshrss import (
    compact_overlay_continuation_for_history,
    restore_overlay_continuation_from_history,
)

G_READER_ENTRY_PREFIX = "tag:google.com,2005:reader/item/"
ScopeKind = Literal["home", "starred", "group", "feed"]
CURSOR_STACK_MAX_BYTES = 128 * 1024
CURSOR_STACK_MAX_ITEMS = 4_096
CURSOR_STACK_MAX_EXPANDED_IDS = 16_384
CURSOR_STACK_MAX_COMPACT_ITEMS = 512
CURSOR_STACK_MAX_RESTORED_ID_REFERENCES = 16_384
READING_CONTEXT_MAX_BYTES = 256 * 1024
READING_CONTEXT_MAX_ITEMS = 512


def _encode_token(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode()
    compressed = zlib.compress(raw, level=9)
    if len(compressed) + 1 < len(raw):
        return "z" + base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")
    return "j" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_token(
    token: str | None,
    *,
    max_bytes: int = 32_768,
) -> dict[str, object] | None:
    if not token:
        return None
    encoding = token[0] if token[0] in {"j", "z"} else "j"
    encoded = token[1:] if token[0] in {"j", "z"} else token
    if len(encoded) > max_bytes * 2:
        return None
    padding = "=" * ((4 - len(encoded) % 4) % 4)
    try:
        packed = base64.urlsafe_b64decode(encoded + padding)
        if encoding == "z":
            decompressor = zlib.decompressobj()
            raw_bytes = decompressor.decompress(packed, max_bytes + 1)
            if (
                len(raw_bytes) > max_bytes
                or decompressor.unconsumed_tail
                or not decompressor.eof
            ):
                return None
        else:
            raw_bytes = packed
            if len(raw_bytes) > max_bytes:
                return None
        payload = json.loads(raw_bytes.decode())
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, zlib.error):
        return None
    return payload if isinstance(payload, dict) else None


def _encode_cursor_stack(stack: tuple[str, ...]) -> str:
    compacted = [
        compact_overlay_continuation_for_history(cursor) for cursor in stack
    ]
    if all(isinstance(cursor, str) for cursor in compacted):
        return _encode_token({"stack": compacted})

    entry_ids: list[object] = []
    entry_id_indexes: dict[str, int] = {}
    id_lists: list[object] = []
    id_list_indexes: dict[tuple[int, ...], int] = {}

    def intern_ids(values: object) -> int:
        assert isinstance(values, list)
        indexes: list[int] = []
        for entry_id in values:
            assert isinstance(entry_id, str)
            index = entry_id_indexes.get(entry_id)
            if index is None:
                index = len(entry_ids)
                entry_id_indexes[entry_id] = index
                suffix = entry_id.removeprefix(G_READER_ENTRY_PREFIX)
                if (
                    entry_id.startswith(G_READER_ENTRY_PREFIX)
                    and len(suffix) == 16
                ):
                    try:
                        entry_ids.append(int(suffix, 16))
                    except ValueError:
                        entry_ids.append(entry_id)
                else:
                    entry_ids.append(entry_id)
            indexes.append(index)
        key = tuple(indexes)
        list_index = id_list_indexes.get(key)
        if list_index is None:
            list_index = len(id_lists)
            id_list_indexes[key] = list_index
            representation: object = indexes
            for existing, existing_index in id_list_indexes.items():
                if existing == key or len(existing) < len(key):
                    continue
                offset = len(existing) - len(key)
                if existing[offset:] == key:
                    representation = ["s", existing_index, offset]
                    break
            id_lists.append(representation)
        return list_index

    items: list[object] = []
    for cursor in compacted:
        if isinstance(cursor, str):
            items.append(cursor)
            continue
        items.append(
            [
                "r",
                cursor[1],
                cursor[2],
                cursor[3],
                intern_ids(cursor[4]),
                intern_ids(cursor[5]),
            ]
        )
    return _encode_token({"stack2": [entry_ids, id_lists, items]})


def _decode_compact_cursor_stack(payload: dict[str, object]) -> tuple[str, ...] | None:
    packed = payload.get("stack2")
    if not isinstance(packed, list) or len(packed) != 3:
        return None
    entry_ids, id_lists, items = packed
    if (
        not isinstance(entry_ids, list)
        or not isinstance(id_lists, list)
        or not isinstance(items, list)
        or len(entry_ids) > CURSOR_STACK_MAX_ITEMS
        or len(id_lists) > CURSOR_STACK_MAX_ITEMS * 2
        or len(items) > CURSOR_STACK_MAX_COMPACT_ITEMS
    ):
        return None
    resolved_entry_ids: list[str] = []
    for entry_id in entry_ids:
        if isinstance(entry_id, str) and 0 < len(entry_id) <= 512:
            resolved_entry_ids.append(entry_id)
            continue
        if (
            isinstance(entry_id, int)
            and not isinstance(entry_id, bool)
            and 0 <= entry_id <= 0xFFFFFFFFFFFFFFFF
        ):
            resolved_entry_ids.append(
                f"{G_READER_ENTRY_PREFIX}{entry_id:016x}"
            )
            continue
        return None
    resolved_lists: list[list[str]] = []
    expanded_id_count = 0
    for indexes in id_lists:
        if (
            isinstance(indexes, list)
            and len(indexes) == 3
            and indexes[0] == "s"
        ):
            source_index, offset = indexes[1], indexes[2]
            if (
                not isinstance(source_index, int)
                or isinstance(source_index, bool)
                or not 0 <= source_index < len(resolved_lists)
                or not isinstance(offset, int)
                or isinstance(offset, bool)
                or not 0 <= offset <= len(resolved_lists[source_index])
            ):
                return None
            expanded_size = len(resolved_lists[source_index]) - offset
            if expanded_id_count + expanded_size > CURSOR_STACK_MAX_EXPANDED_IDS:
                return None
            expanded_id_count += expanded_size
            resolved_lists.append(resolved_lists[source_index][offset:])
            continue
        if (
            not isinstance(indexes, list)
            or len(indexes) > CURSOR_STACK_MAX_ITEMS
            or not all(
                isinstance(index, int)
                and not isinstance(index, bool)
                and 0 <= index < len(resolved_entry_ids)
                for index in indexes
            )
        ):
            return None
        if expanded_id_count + len(indexes) > CURSOR_STACK_MAX_EXPANDED_IDS:
            return None
        expanded_id_count += len(indexes)
        resolved_lists.append([resolved_entry_ids[index] for index in indexes])

    restored: list[str] = []
    restored_id_references = 0
    for item in items:
        if isinstance(item, str):
            restored.append(item)
            continue
        if (
            not isinstance(item, list)
            or len(item) != 6
            or item[0] != "r"
            or not isinstance(item[4], int)
            or isinstance(item[4], bool)
            or not isinstance(item[5], int)
            or isinstance(item[5], bool)
            or not 0 <= item[4] < len(resolved_lists)
            or not 0 <= item[5] < len(resolved_lists)
        ):
            return None
        restored_id_references += len(resolved_lists[item[4]]) + len(
            resolved_lists[item[5]]
        )
        if (
            restored_id_references
            > CURSOR_STACK_MAX_RESTORED_ID_REFERENCES
        ):
            return None
        cursor = restore_overlay_continuation_from_history(
            [
                "rk1",
                item[1],
                item[2],
                item[3],
                resolved_lists[item[4]],
                resolved_lists[item[5]],
            ]
        )
        if cursor is None:
            return None
        restored.append(cursor)
    return tuple(restored)


def _decode_cursor_stack(value: str | None) -> tuple[str, ...]:
    payload = _decode_token(value, max_bytes=CURSOR_STACK_MAX_BYTES)
    compact = _decode_compact_cursor_stack(payload) if payload else None
    if compact is not None:
        return compact
    stack = payload.get("stack") if payload else None
    if (
        not isinstance(stack, list)
        or len(stack) > CURSOR_STACK_MAX_ITEMS
        or not all(
            isinstance(cursor, str) and len(cursor) <= CURSOR_STACK_MAX_BYTES
            for cursor in stack
        )
    ):
        return ()
    restored = [restore_overlay_continuation_from_history(item) for item in stack]
    if any(cursor is None for cursor in restored):
        return ()
    return tuple(cursor for cursor in restored if cursor is not None)


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

    @property
    def has_legacy_cursor(self) -> bool:
        if not self.continuation:
            return False
        prefix, separator, raw_offset = self.continuation.rpartition(":")
        return (
            separator == ":"
            and prefix in {"group-offset", "sorted-offset"}
            and raw_offset.isdigit()
        )

    def url(self, app: FastAPI) -> str:
        params: dict[str, str] = {}
        if self.include_read:
            params["read"] = "1"
        if self.continuation:
            params["c"] = self.continuation
        if self.history:
            params["b"] = _encode_cursor_stack(self.history)
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
    previous_url: str | None = None

    def encode(self) -> str:
        return _encode_token(
            {
                "entry_ids": list(self.entry_ids),
                "back_url": self.back_url,
                "next_page_url": self.next_page_url,
                "previous_url": self.previous_url,
            }
        )

    @classmethod
    def decode(cls, token: str | None) -> ReadingContext | None:
        payload = _decode_token(token, max_bytes=READING_CONTEXT_MAX_BYTES)
        if not payload:
            return None
        entry_ids = payload.get("entry_ids")
        back_url = payload.get("back_url")
        next_page_url = payload.get("next_page_url")
        previous_url = payload.get("previous_url")
        if (
            not isinstance(entry_ids, list)
            or len(entry_ids) > READING_CONTEXT_MAX_ITEMS
            or not all(
                isinstance(entry_id, str) and 0 < len(entry_id) <= 512
                for entry_id in entry_ids
            )
            or not isinstance(back_url, str)
            or len(back_url) > CURSOR_STACK_MAX_BYTES
            or (
                next_page_url is not None
                and (
                    not isinstance(next_page_url, str)
                    or len(next_page_url) > CURSOR_STACK_MAX_BYTES
                )
            )
            or (
                previous_url is not None
                and (
                    not isinstance(previous_url, str)
                    or len(previous_url) > 2_048
                )
            )
        ):
            return None
        try:
            StreamRequest.from_url(back_url)
            if next_page_url:
                StreamRequest.from_url(str(next_page_url))
        except ValueError:
            return None
        if previous_url is not None:
            parsed_previous = urlsplit(str(previous_url))
            if (
                parsed_previous.scheme
                or parsed_previous.netloc
                or not parsed_previous.path.startswith("/read/")
            ):
                return None
        return cls(
            entry_ids=tuple(str(entry_id) for entry_id in entry_ids),
            back_url=back_url,
            next_page_url=str(next_page_url) if next_page_url else None,
            previous_url=str(previous_url) if previous_url else None,
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
