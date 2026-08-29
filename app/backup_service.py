from __future__ import annotations

import json
import sqlite3
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from app import __version__
from app.config import Settings

BACKUP_PREFIX = "rss-kindle-backup-"
BACKUP_SUFFIX = ".zip"


@dataclass(frozen=True)
class BackupArchive:
    filename: str
    created_at: str
    size_bytes: int


class BackupService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.backup_directory = settings.backup_directory or settings.database_path.parent / "backups"

    def create_backup(self, *, now: datetime | None = None) -> BackupArchive:
        created_at = _as_utc(now)
        self.backup_directory.mkdir(parents=True, exist_ok=True)
        timestamp = created_at.strftime("%Y%m%d-%H%M%S-%f")
        filename = f"{BACKUP_PREFIX}{timestamp}{BACKUP_SUFFIX}"
        archive_path = self.backup_directory / filename
        pending_archive_path = archive_path.with_suffix(".tmp")

        try:
            with tempfile.TemporaryDirectory(prefix="rss-kindle-backup-") as temp_directory:
                temp_path = Path(temp_directory)
                included_files: list[str] = []

                reader_snapshot = temp_path / "rss_kindle.db"
                _backup_sqlite(self.settings.database_path, reader_snapshot)
                included_files.append("data/rss_kindle.db")

                bridge_database = self.settings.database_path.with_name("source_bridge.db")
                bridge_snapshot = temp_path / "source_bridge.db"
                if bridge_database.exists() and bridge_database != self.settings.database_path:
                    _backup_sqlite(bridge_database, bridge_snapshot)
                    included_files.append("data/source_bridge.db")

                manifest = {
                    "format_version": 1,
                    "app_version": __version__,
                    "created_at": created_at.isoformat(),
                    "included_files": included_files,
                    "note": "FreshRSS data, private configuration, and deployment secrets are not included.",
                }

                with zipfile.ZipFile(pending_archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.write(reader_snapshot, "data/rss_kindle.db")
                    if bridge_snapshot.exists():
                        archive.write(bridge_snapshot, "data/source_bridge.db")
                    archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")

                with zipfile.ZipFile(pending_archive_path) as archive:
                    if archive.testzip() is not None:
                        raise RuntimeError("Backup archive verification failed.")
                pending_archive_path.replace(archive_path)
        finally:
            if pending_archive_path.exists():
                pending_archive_path.unlink()

        self._apply_retention()
        return self._describe(archive_path)

    def list_backups(self) -> list[BackupArchive]:
        if not self.backup_directory.exists():
            return []
        paths = sorted(
            self.backup_directory.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return [self._describe(path) for path in paths if path.is_file()]

    def get_backup_path(self, filename: str) -> Path | None:
        if Path(filename).name != filename:
            return None
        if not filename.startswith(BACKUP_PREFIX) or not filename.endswith(BACKUP_SUFFIX):
            return None
        path = self.backup_directory / filename
        return path if path.is_file() else None

    def status(self) -> dict[str, object]:
        backups = self.list_backups()
        return {
            "directory": str(self.backup_directory),
            "retention_count": self.settings.backup_retention_count,
            "backups": [asdict(backup) for backup in backups],
            "latest": asdict(backups[0]) if backups else None,
        }

    def _apply_retention(self) -> None:
        backups = self.list_backups()
        for backup in backups[self.settings.backup_retention_count :]:
            path = self.get_backup_path(backup.filename)
            if path is not None:
                path.unlink()

    def _describe(self, path: Path) -> BackupArchive:
        modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
        return BackupArchive(
            filename=path.name,
            created_at=modified_at,
            size_bytes=path.stat().st_size,
        )


def _backup_sqlite(source_path: Path, destination_path: Path) -> None:
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {source_path}")
    with sqlite3.connect(source_path) as source, sqlite3.connect(destination_path) as destination:
        source.backup(destination)


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(tz=UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
