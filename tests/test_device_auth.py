from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import Settings
from app.db import Database
from app.device_auth import DeviceAuthService
from app.repository import Repository

TEST_BASE_DIR = Path(__file__).resolve().parent.parent


def build_service(tmp_path: Path, *, attempts: int = 5) -> tuple[DeviceAuthService, Repository]:
    settings = Settings(
        app_name="RSS Kindle",
        base_dir=TEST_BASE_DIR,
        database_path=tmp_path / "devices.db",
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=15,
        metadata_cache_seconds=60,
        freshrss_api_url="https://rss.example.net/api/greader.php",
        freshrss_username="alice",
        freshrss_api_password="secret",
        app_auth_username="reader",
        app_auth_password="backup-password",
        app_auth_secret="pairing-secret",
        device_session_max_age_seconds=86400 * 365,
        pairing_code_ttl_seconds=600,
        pairing_code_attempts=attempts,
    )
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    return DeviceAuthService(settings, repository), repository


def test_pairing_code_creates_a_long_lived_revocable_device(tmp_path: Path):
    service, repository = build_service(tmp_path)
    now = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
    pairing = service.create_pairing_code(code="123456", now=now)

    grant = service.redeem_pairing_code("123 456", device_name="Kitchen Kindle", now=now)

    assert pairing.expires_at == (now + timedelta(minutes=10)).isoformat()
    assert grant is not None
    assert grant.device.name == "Kitchen Kindle"
    assert grant.device.expires_at == (now + timedelta(days=365)).isoformat()
    assert service.redeem_pairing_code("123456", now=now) is None

    session = service.authenticate(grant.token, now=now + timedelta(days=1))
    assert session is not None
    assert session.device.device_id == grant.device.device_id
    assert session.csrf_token

    assert repository.revoke_reader_device(grant.device.device_id, revoked_at=now.isoformat())
    assert service.authenticate(grant.token, now=now + timedelta(days=1)) is None


def test_pairing_code_expires_and_limits_guesses(tmp_path: Path):
    service, _ = build_service(tmp_path, attempts=2)
    now = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)

    service.create_pairing_code(code="654321", now=now)
    assert service.redeem_pairing_code("111111", now=now) is None
    assert service.redeem_pairing_code("222222", now=now) is None
    assert service.redeem_pairing_code("654321", now=now) is None

    service.create_pairing_code(code="654321", now=now)
    assert service.redeem_pairing_code("6543217", now=now) is None
    assert service.redeem_pairing_code("654321", now=now) is not None

    service.create_pairing_code(code="654321", now=now)
    assert service.redeem_pairing_code("654321", now=now + timedelta(minutes=11)) is None
