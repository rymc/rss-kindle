from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import timedelta

from app.db import Database
from app.utils import utc_now, utc_now_iso


@dataclass(frozen=True)
class CachedArticle:
    entry_id: str
    source_url: str
    extracted_html: str | None
    extraction_status: str
    error_message: str | None
    extracted_at: str
    source_fingerprint: str | None


@dataclass(frozen=True)
class SyntheticSourceState:
    source_id: str
    last_attempted_at: str
    last_successful_at: str | None
    last_error: str | None


@dataclass(frozen=True)
class SyntheticFeedItem:
    source_id: str
    item_id: str
    article_url: str
    title: str
    summary_text: str | None
    content_html: str | None
    published_at: str | None
    source_page_url: str
    sort_index: int
    discovered_at: str


@dataclass(frozen=True)
class ReaderDevice:
    device_id: str
    name: str
    created_at: str
    last_used_at: str
    expires_at: str
    revoked_at: str | None


@dataclass(frozen=True)
class ArticleCacheStats:
    total_count: int
    success_count: int
    failed_count: int
    database_bytes: int


class Repository:
    def __init__(self, database: Database):
        self.database = database

    def initialize(self) -> None:
        self.database.initialize()

    def set_pairing_code(
        self,
        *,
        code_hash: str,
        created_at: str,
        expires_at: str,
        attempts_remaining: int,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO reader_pairing (
                    singleton_id, code_hash, created_at, expires_at, attempts_remaining
                )
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(singleton_id) DO UPDATE SET
                    code_hash = excluded.code_hash,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at,
                    attempts_remaining = excluded.attempts_remaining
                """,
                (code_hash, created_at, expires_at, attempts_remaining),
            )
            connection.commit()

    def consume_pairing_code(self, *, code_hash: str, now: str) -> bool:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT code_hash, expires_at, attempts_remaining
                FROM reader_pairing
                WHERE singleton_id = 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return False
            if row["expires_at"] <= now or row["attempts_remaining"] <= 0:
                connection.execute("DELETE FROM reader_pairing WHERE singleton_id = 1")
                connection.commit()
                return False
            if not hmac.compare_digest(row["code_hash"], code_hash):
                attempts_remaining = row["attempts_remaining"] - 1
                if attempts_remaining <= 0:
                    connection.execute("DELETE FROM reader_pairing WHERE singleton_id = 1")
                else:
                    connection.execute(
                        "UPDATE reader_pairing SET attempts_remaining = ? WHERE singleton_id = 1",
                        (attempts_remaining,),
                    )
                connection.commit()
                return False
            connection.execute("DELETE FROM reader_pairing WHERE singleton_id = 1")
            connection.commit()
            return True

    def create_reader_device(
        self,
        *,
        device_id: str,
        name: str,
        token_hash: str,
        created_at: str,
        expires_at: str,
    ) -> ReaderDevice:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO reader_devices (
                    device_id, name, token_hash, created_at, last_used_at, expires_at, revoked_at
                )
                VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (device_id, name, token_hash, created_at, created_at, expires_at),
            )
            connection.commit()
        return ReaderDevice(
            device_id=device_id,
            name=name,
            created_at=created_at,
            last_used_at=created_at,
            expires_at=expires_at,
            revoked_at=None,
        )

    def get_reader_device_by_token_hash(self, token_hash: str, *, now: str) -> ReaderDevice | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT device_id, name, created_at, last_used_at, expires_at, revoked_at
                FROM reader_devices
                WHERE token_hash = ? AND revoked_at IS NULL AND expires_at > ?
                """,
                (token_hash, now),
            ).fetchone()
        return ReaderDevice(**dict(row)) if row is not None else None

    def touch_reader_device(self, device_id: str, *, used_at: str, older_than: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE reader_devices
                SET last_used_at = ?
                WHERE device_id = ? AND last_used_at < ?
                """,
                (used_at, device_id, older_than),
            )
            connection.commit()

    def list_reader_devices(self) -> list[ReaderDevice]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT device_id, name, created_at, last_used_at, expires_at, revoked_at
                FROM reader_devices
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [ReaderDevice(**dict(row)) for row in rows]

    def revoke_reader_device(self, device_id: str, *, revoked_at: str) -> bool:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE reader_devices
                SET revoked_at = ?
                WHERE device_id = ? AND revoked_at IS NULL
                """,
                (revoked_at, device_id),
            )
            connection.commit()
        return cursor.rowcount > 0

    def save_reading_context(self, payload: str) -> str:
        context_id = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
        saved_at_datetime = utc_now()
        saved_at = saved_at_datetime.isoformat()
        touch_before = (saved_at_datetime - timedelta(hours=1)).isoformat()
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT saved_at FROM reading_contexts WHERE context_id = ?",
                (context_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO reading_contexts (context_id, payload, saved_at)
                    VALUES (?, ?, ?)
                    """,
                    (context_id, payload, saved_at),
                )
                connection.execute(
                    """
                    DELETE FROM reading_contexts
                    WHERE context_id NOT IN (
                        SELECT context_id
                        FROM reading_contexts
                        ORDER BY saved_at DESC
                        LIMIT 256
                    )
                    """
                )
                connection.commit()
            elif str(existing["saved_at"]) < touch_before:
                connection.execute(
                    """
                    UPDATE reading_contexts
                    SET saved_at = ?
                    WHERE context_id = ?
                    """,
                    (saved_at, context_id),
                )
                connection.commit()
        return context_id

    def get_reading_context(self, context_id: str) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM reading_contexts WHERE context_id = ?",
                (context_id,),
            ).fetchone()
        return str(row["payload"]) if row is not None else None

    def get_article_cache_stats(self) -> ArticleCacheStats:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total_count,
                    SUM(CASE WHEN extraction_status = 'success' THEN 1 ELSE 0 END) AS success_count,
                    SUM(CASE WHEN extraction_status != 'success' THEN 1 ELSE 0 END) AS failed_count
                FROM article_cache
                """
            ).fetchone()
        return ArticleCacheStats(
            total_count=int(row["total_count"] or 0),
            success_count=int(row["success_count"] or 0),
            failed_count=int(row["failed_count"] or 0),
            database_bytes=self.database.path.stat().st_size if self.database.path.exists() else 0,
        )

    def get_cached_article(self, entry_id: str, source_url: str | None) -> CachedArticle | None:
        normalized_url = (source_url or "").strip()
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    entry_id,
                    source_url,
                    extracted_html,
                    extraction_status,
                    error_message,
                    extracted_at,
                    source_fingerprint
                FROM article_cache
                WHERE entry_id = ? AND source_url = ?
                """,
                (entry_id, normalized_url),
            ).fetchone()
        if row is None:
            return None
        return CachedArticle(**dict(row))

    def save_cached_article(
        self,
        entry_id: str,
        *,
        source_url: str | None,
        extracted_html: str | None,
        extraction_status: str,
        error_message: str | None = None,
        source_fingerprint: str | None = None,
    ) -> None:
        normalized_url = (source_url or "").strip()
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM article_cache WHERE entry_id = ? AND source_url != ?",
                (entry_id, normalized_url),
            )
            connection.execute(
                """
                INSERT INTO article_cache (
                    entry_id, source_url, extracted_html, extraction_status,
                    error_message, extracted_at,
                    source_fingerprint
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(entry_id, source_url) DO UPDATE SET
                    source_url = excluded.source_url,
                    extracted_html = excluded.extracted_html,
                    extracted_text = NULL,
                    extraction_status = excluded.extraction_status,
                    error_message = excluded.error_message,
                    extracted_at = excluded.extracted_at,
                    source_fingerprint = excluded.source_fingerprint
                """,
                (
                    entry_id,
                    normalized_url,
                    extracted_html,
                    extraction_status,
                    error_message,
                    utc_now_iso(),
                    source_fingerprint,
                ),
            )
            connection.commit()

    def get_synthetic_source_state(self, source_id: str) -> SyntheticSourceState | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    source_id,
                    last_attempted_at,
                    last_successful_at,
                    last_error
                FROM synthetic_source_state
                WHERE source_id = ?
                """,
                (source_id,),
            ).fetchone()
        if row is None:
            return None
        return SyntheticSourceState(**dict(row))

    def list_synthetic_feed_items(self, source_id: str, *, limit: int | None = None) -> list[SyntheticFeedItem]:
        query = """
            SELECT
                source_id,
                item_id,
                article_url,
                title,
                summary_text,
                content_html,
                published_at,
                source_page_url,
                sort_index,
                discovered_at
            FROM synthetic_feed_items
            WHERE source_id = ?
            ORDER BY sort_index ASC, discovered_at DESC
        """
        params: list[object] = [source_id]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        with self.database.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [SyntheticFeedItem(**dict(row)) for row in rows]

    def count_synthetic_feed_items(self, source_id: str) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS item_count FROM synthetic_feed_items WHERE source_id = ?",
                (source_id,),
            ).fetchone()
        return int(row["item_count"] or 0)

    def replace_synthetic_feed_items(self, source_id: str, items: list[SyntheticFeedItem]) -> None:
        refreshed_at = utc_now_iso()
        with self.database.connect() as connection:
            connection.execute("DELETE FROM synthetic_feed_items WHERE source_id = ?", (source_id,))
            connection.executemany(
                """
                INSERT INTO synthetic_feed_items (
                    source_id,
                    item_id,
                    article_url,
                    title,
                    summary_text,
                    content_html,
                    published_at,
                    source_page_url,
                    sort_index,
                    discovered_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.source_id,
                        item.item_id,
                        item.article_url,
                        item.title,
                        item.summary_text,
                        item.content_html,
                        item.published_at,
                        item.source_page_url,
                        item.sort_index,
                        item.discovered_at,
                    )
                    for item in items
                ],
            )
            connection.execute(
                """
                INSERT INTO synthetic_source_state (
                    source_id,
                    last_attempted_at,
                    last_successful_at,
                    last_error
                )
                VALUES (?, ?, ?, NULL)
                ON CONFLICT(source_id) DO UPDATE SET
                    last_attempted_at = excluded.last_attempted_at,
                    last_successful_at = excluded.last_successful_at,
                    last_error = NULL
                """,
                (source_id, refreshed_at, refreshed_at),
            )
            connection.commit()

    def mark_synthetic_source_failure(self, source_id: str, error_message: str) -> None:
        attempted_at = utc_now_iso()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO synthetic_source_state (
                    source_id,
                    last_attempted_at,
                    last_successful_at,
                    last_error
                )
                VALUES (?, ?, NULL, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    last_attempted_at = excluded.last_attempted_at,
                    last_error = excluded.last_error
                """,
                (source_id, attempted_at, error_message),
            )
            connection.commit()
