from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.repository import ReaderDevice, Repository


@dataclass(frozen=True)
class PairingCode:
    code: str
    expires_at: str


@dataclass(frozen=True)
class DeviceGrant:
    device: ReaderDevice
    token: str


@dataclass(frozen=True)
class DeviceSession:
    device: ReaderDevice
    csrf_token: str


class DeviceAuthService:
    def __init__(self, settings: Settings, repository: Repository):
        if not settings.app_auth_secret:
            raise RuntimeError("APP_AUTH_SECRET is required for device pairing.")
        self.settings = settings
        self.repository = repository
        self.secret = settings.app_auth_secret

    def create_pairing_code(
        self, *, code: str | None = None, now: datetime | None = None
    ) -> PairingCode:
        created_at = _as_utc(now)
        pairing_code = (
            _normalize_pairing_code(code)
            if code is not None
            else f"{secrets.randbelow(1_000_000):06d}"
        )
        if len(pairing_code) != 6:
            raise ValueError("Pairing codes must contain six digits.")
        expires_at = created_at + timedelta(
            seconds=self.settings.pairing_code_ttl_seconds
        )
        self.repository.set_pairing_code(
            code_hash=self._pairing_code_hash(pairing_code),
            created_at=created_at.isoformat(),
            expires_at=expires_at.isoformat(),
            attempts_remaining=self.settings.pairing_code_attempts,
        )
        return PairingCode(code=pairing_code, expires_at=expires_at.isoformat())

    def redeem_pairing_code(
        self,
        code: str,
        *,
        device_name: str | None = None,
        now: datetime | None = None,
    ) -> DeviceGrant | None:
        used_at = _as_utc(now)
        normalized_code = _normalize_pairing_code(code)
        if len(normalized_code) != 6:
            return None
        if not self.repository.consume_pairing_code(
            code_hash=self._pairing_code_hash(normalized_code),
            now=used_at.isoformat(),
        ):
            return None

        raw_token = secrets.token_urlsafe(32)
        expires_at = used_at + timedelta(
            seconds=self.settings.device_session_max_age_seconds
        )
        device = self.repository.create_reader_device(
            device_id=secrets.token_hex(12),
            name=_normalize_device_name(device_name),
            token_hash=_token_hash(raw_token),
            created_at=used_at.isoformat(),
            expires_at=expires_at.isoformat(),
        )
        return DeviceGrant(device=device, token=raw_token)

    def authenticate(
        self, token: str | None, *, now: datetime | None = None
    ) -> DeviceSession | None:
        if not token:
            return None
        used_at = _as_utc(now)
        device = self.repository.get_reader_device_by_token_hash(
            _token_hash(token),
            now=used_at.isoformat(),
        )
        if device is None:
            return None
        older_than = (used_at - timedelta(hours=24)).isoformat()
        if device.last_used_at < older_than:
            self.repository.touch_reader_device(
                device.device_id,
                used_at=used_at.isoformat(),
                older_than=older_than,
            )
        csrf_token = hmac.new(
            self.secret.encode("utf-8"),
            f"device-csrf:{token}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return DeviceSession(device=device, csrf_token=csrf_token)

    def _pairing_code_hash(self, code: str) -> str:
        return hmac.new(
            self.secret.encode("utf-8"),
            f"pairing-code:{code}".encode(),
            hashlib.sha256,
        ).hexdigest()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _normalize_pairing_code(code: str) -> str:
    return "".join(character for character in code if character.isdigit())


def _normalize_device_name(name: str | None) -> str:
    normalized = " ".join((name or "").split()).strip()
    return normalized[:80] or "Kindle"


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(tz=UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
