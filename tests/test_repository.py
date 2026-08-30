import sqlite3
from pathlib import Path

from app.db import Database
from app.repository import Repository


def test_initialize_replaces_legacy_schema_with_cache_schema(tmp_path: Path):
    database_path = tmp_path / "rss.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE feeds (id INTEGER PRIMARY KEY, title TEXT)")
        connection.commit()

    repository = Repository(Database(database_path))
    repository.initialize()

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if not row[0].startswith("sqlite_")
        }

    assert {"article_cache", "synthetic_source_state", "synthetic_feed_items"} <= tables
    assert "feeds" not in tables
    backups = list(tmp_path.glob("rss.legacy-*.db"))
    assert len(backups) == 1


def test_reading_context_has_a_short_stable_id_and_round_trips(tmp_path: Path):
    repository = Repository(Database(tmp_path / "rss.db"))
    repository.initialize()

    first_id = repository.save_reading_context("encoded reading context")
    second_id = repository.save_reading_context("encoded reading context")

    assert first_id == second_id
    assert len(first_id) == 12
    assert repository.get_reading_context(first_id) == "encoded reading context"
    assert repository.get_reading_context("missing") is None

    with sqlite3.connect(repository.database.path) as connection:
        connection.execute(
            "UPDATE reading_contexts SET saved_at = ? WHERE context_id = ?",
            ("2020-01-01T00:00:00+00:00", first_id),
        )
        connection.commit()
    repository.save_reading_context("encoded reading context")
    with sqlite3.connect(repository.database.path) as connection:
        refreshed_at = connection.execute(
            "SELECT saved_at FROM reading_contexts WHERE context_id = ?",
            (first_id,),
        ).fetchone()[0]
    assert refreshed_at > "2020-01-01T00:00:00+00:00"


def test_initialize_adds_article_fingerprint_without_losing_cache(tmp_path: Path):
    database_path = tmp_path / "rss.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE article_cache (
                entry_id TEXT NOT NULL,
                source_url TEXT NOT NULL,
                extracted_html TEXT,
                extracted_text TEXT,
                extraction_status TEXT NOT NULL,
                error_message TEXT,
                extracted_at TEXT NOT NULL,
                PRIMARY KEY (entry_id, source_url)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO article_cache VALUES (
                'entry-1', 'https://example.com/1', '<p>Body</p>', 'Body',
                'success', NULL, '2026-08-30T10:00:00+00:00'
            )
            """
        )

    repository = Repository(Database(database_path))
    repository.initialize()

    cached = repository.get_cached_article("entry-1", "https://example.com/1")
    assert cached is not None
    assert cached.extracted_html == "<p>Body</p>"
    assert cached.source_fingerprint is None
