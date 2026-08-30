from __future__ import annotations

import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS article_cache (
    entry_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    extracted_html TEXT,
    extracted_text TEXT,
    extraction_status TEXT NOT NULL,
    error_message TEXT,
    extracted_at TEXT NOT NULL,
    source_fingerprint TEXT,
    PRIMARY KEY (entry_id, source_url)
);

CREATE INDEX IF NOT EXISTS idx_article_cache_extracted_at ON article_cache(extracted_at DESC);

CREATE TABLE IF NOT EXISTS synthetic_source_state (
    source_id TEXT PRIMARY KEY,
    last_attempted_at TEXT NOT NULL,
    last_successful_at TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS synthetic_feed_items (
    source_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    article_url TEXT NOT NULL,
    title TEXT NOT NULL,
    summary_text TEXT,
    content_html TEXT,
    published_at TEXT,
    source_page_url TEXT NOT NULL,
    sort_index INTEGER NOT NULL,
    discovered_at TEXT NOT NULL,
    PRIMARY KEY (source_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_synthetic_feed_items_source_sort
ON synthetic_feed_items(source_id, sort_index ASC, discovered_at DESC);

CREATE TABLE IF NOT EXISTS reader_pairing (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    code_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts_remaining INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reader_devices (
    device_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_reader_devices_token_hash
ON reader_devices(token_hash);

CREATE TABLE IF NOT EXISTS reading_contexts (
    context_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    saved_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reading_contexts_saved_at
ON reading_contexts(saved_at DESC);
"""

CURRENT_TABLES = {
    "article_cache",
    "synthetic_source_state",
    "synthetic_feed_items",
    "reader_pairing",
    "reader_devices",
    "reading_contexts",
}


class Database:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._reset_if_legacy_schema()
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._add_missing_columns(connection)
            connection.commit()

    @staticmethod
    def _add_missing_columns(connection: sqlite3.Connection) -> None:
        article_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(article_cache)")
        }
        if "source_fingerprint" not in article_columns:
            connection.execute(
                "ALTER TABLE article_cache ADD COLUMN source_fingerprint TEXT"
            )

    def _reset_if_legacy_schema(self) -> None:
        if not self.path.exists():
            return

        with sqlite3.connect(self.path) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                if not row[0].startswith("sqlite_")
            }

        if not tables or tables <= CURRENT_TABLES:
            return

        timestamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
        backup_path = self.path.with_name(f"{self.path.stem}.legacy-{timestamp}{self.path.suffix}")
        shutil.copy2(self.path, backup_path)
        self.path.unlink()
        for suffix in ("-wal", "-shm"):
            sidecar = self.path.with_name(self.path.name + suffix)
            if sidecar.exists():
                sidecar.unlink()
