import json
import sqlite3
import zipfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.backup_service import BackupService
from app.config import Settings
from app.db import Database
from app.repository import Repository

TEST_BASE_DIR = Path(__file__).resolve().parent.parent


def build_settings(tmp_path: Path, *, retention: int = 2) -> Settings:
    return Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=tmp_path / "data" / "rss_kindle.db",
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=15,
        metadata_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
        backup_directory=tmp_path / "backups",
        backup_retention_count=retention,
    )


def test_backup_is_consistent_downloadable_and_retained(tmp_path: Path):
    bridge_config = tmp_path / "source-bridge.toml"
    bridge_config.write_text('cookie_header = "private-cookie"\n')
    settings = replace(build_settings(tmp_path), source_bridge_config_path=bridge_config)
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    repository.save_cached_article(
        "entry-1",
        source_url="https://example.com/story",
        extracted_html="<article><p>Saved body</p></article>",
        extraction_status="success",
    )
    bridge_database = settings.database_path.with_name("source_bridge.db")
    with sqlite3.connect(bridge_database) as connection:
        connection.execute("CREATE TABLE bridge_marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO bridge_marker VALUES ('saved bridge state')")
        connection.commit()
    service = BackupService(settings)
    now = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)

    first = service.create_backup(now=now)
    service.create_backup(now=now + timedelta(seconds=1))
    third = service.create_backup(now=now + timedelta(seconds=2))

    backups = service.list_backups()
    assert len(backups) == 2
    assert backups[0].filename == third.filename
    assert first.filename not in {backup.filename for backup in backups}
    assert service.get_backup_path("../rss-kindle-backup-stolen.zip") is None

    archive_path = service.get_backup_path(third.filename)
    assert archive_path is not None
    with zipfile.ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == {
            "data/rss_kindle.db",
            "data/source_bridge.db",
            "manifest.json",
        }
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format_version"] == 1
        assert "private configuration" in manifest["note"]
        assert all(b"private-cookie" not in archive.read(name) for name in archive.namelist())
        archive.extract("data/rss_kindle.db", tmp_path / "restored")
        archive.extract("data/source_bridge.db", tmp_path / "restored")

    restored_database = tmp_path / "restored" / "data" / "rss_kindle.db"
    with sqlite3.connect(restored_database) as connection:
        row = connection.execute(
            "SELECT extracted_html FROM article_cache WHERE entry_id = 'entry-1'"
        ).fetchone()
    assert row == ("<article><p>Saved body</p></article>",)

    restored_bridge_database = tmp_path / "restored" / "data" / "source_bridge.db"
    with sqlite3.connect(restored_bridge_database) as connection:
        row = connection.execute("SELECT value FROM bridge_marker").fetchone()
    assert row == ("saved bridge state",)
