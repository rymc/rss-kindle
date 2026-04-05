from __future__ import annotations

from dataclasses import dataclass

from app.db import Database
from app.utils import utc_now_iso


@dataclass(frozen=True)
class CachedArticle:
    entry_id: str
    source_url: str
    extracted_html: str | None
    extracted_text: str | None
    extraction_status: str
    error_message: str | None
    extracted_at: str


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


class Repository:
    def __init__(self, database: Database):
        self.database = database

    def initialize(self) -> None:
        self.database.initialize()

    def get_cached_article(self, entry_id: str, source_url: str | None) -> CachedArticle | None:
        normalized_url = (source_url or "").strip()
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    entry_id,
                    source_url,
                    extracted_html,
                    extracted_text,
                    extraction_status,
                    error_message,
                    extracted_at
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
        extracted_text: str | None,
        extraction_status: str,
        error_message: str | None = None,
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
                    entry_id, source_url, extracted_html, extracted_text,
                    extraction_status, error_message, extracted_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(entry_id, source_url) DO UPDATE SET
                    source_url = excluded.source_url,
                    extracted_html = excluded.extracted_html,
                    extracted_text = excluded.extracted_text,
                    extraction_status = excluded.extraction_status,
                    error_message = excluded.error_message,
                    extracted_at = excluded.extracted_at
                """,
                (
                    entry_id,
                    normalized_url,
                    extracted_html,
                    extracted_text,
                    extraction_status,
                    error_message,
                    utc_now_iso(),
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
