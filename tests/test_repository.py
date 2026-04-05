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

    assert tables == {"article_cache", "synthetic_source_state", "synthetic_feed_items"}
    backups = list(tmp_path.glob("rss.legacy-*.db"))
    assert len(backups) == 1
