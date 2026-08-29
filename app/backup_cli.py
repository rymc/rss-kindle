from __future__ import annotations

from app.backup_service import BackupService
from app.config import get_settings


def main() -> None:
    service = BackupService(get_settings())
    backup = service.create_backup()
    path = service.get_backup_path(backup.filename)
    print(path or backup.filename)


if __name__ == "__main__":
    main()
