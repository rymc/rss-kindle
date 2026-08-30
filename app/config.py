from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    app_name: str
    base_dir: Path
    database_path: Path
    http_timeout_seconds: float
    user_agent: str
    max_stream_items: int
    metadata_cache_seconds: int
    freshrss_api_url: str | None
    freshrss_username: str | None
    freshrss_api_password: str | None
    entry_cache_seconds: int = 300
    stream_cache_seconds: int = 60
    article_prewarm_count: int = 2
    app_auth_username: str | None = None
    app_auth_password: str | None = None
    app_auth_secret: str | None = None
    app_session_cookie_name: str = "rss_kindle_session"
    app_session_max_age_seconds: int = 86400 * 30
    app_device_cookie_name: str = "rss_kindle_device"
    device_session_max_age_seconds: int = 86400 * 365
    pairing_code_ttl_seconds: int = 600
    pairing_code_attempts: int = 5
    app_secure_cookies: bool = True
    app_allowed_hosts: tuple[str, ...] = ()
    source_bridge_api_url: str | None = None
    source_bridge_access_token: str | None = None
    source_bridge_timeout_seconds: float = 60
    source_bridge_config_path: Path | None = None
    source_bridge_refresh_seconds: int = 900
    source_bridge_prewarm_enabled: bool = True
    source_bridge_prewarm_interval_seconds: int = 60
    backup_directory: Path | None = None
    backup_retention_count: int = 7


def _resolve_optional_path(base_dir: Path, raw_value: str | None) -> Path | None:
    if not raw_value:
        return None
    candidate = Path(raw_value).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate.resolve()


def _env_flag(name: str, default: bool) -> bool:
    raw_value = (os.getenv(name) or "").strip().lower()
    if not raw_value:
        return default
    return raw_value in {"1", "true", "yes", "on"}


def _env_csv(name: str) -> tuple[str, ...]:
    raw_value = (os.getenv(name) or "").strip()
    if not raw_value:
        return ()
    return tuple(part.strip() for part in raw_value.split(",") if part.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    base_dir = Path(os.getenv("APP_BASE_DIR", Path(__file__).resolve().parent.parent)).resolve()
    database_path = Path(os.getenv("DATABASE_PATH", base_dir / "data" / "rss_kindle.db"))
    source_bridge_config_path = _resolve_optional_path(
        base_dir,
        (os.getenv("SOURCE_BRIDGE_CONFIG_PATH") or "").strip() or None,
    )
    settings = Settings(
        app_name=os.getenv("APP_NAME", "RSS Kindle"),
        base_dir=base_dir,
        database_path=database_path,
        http_timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS", "20")),
        user_agent=os.getenv(
            "USER_AGENT",
            "rss-kindle/0.2 (+https://example.invalid; self-hosted personal reader)",
        ),
        max_stream_items=int(os.getenv("MAX_STREAM_ITEMS", "15")),
        metadata_cache_seconds=int(os.getenv("METADATA_CACHE_SECONDS", "60")),
        entry_cache_seconds=max(0, int(os.getenv("ENTRY_CACHE_SECONDS", "300"))),
        stream_cache_seconds=max(0, int(os.getenv("STREAM_CACHE_SECONDS", "60"))),
        article_prewarm_count=max(0, int(os.getenv("ARTICLE_PREWARM_COUNT", "2"))),
        freshrss_api_url=(os.getenv("FRESHRSS_API_URL") or "").strip() or None,
        freshrss_username=(os.getenv("FRESHRSS_USERNAME") or "").strip() or None,
        freshrss_api_password=(os.getenv("FRESHRSS_API_PASSWORD") or "").strip() or None,
        app_auth_username=(os.getenv("APP_AUTH_USERNAME") or "").strip() or None,
        app_auth_password=(os.getenv("APP_AUTH_PASSWORD") or "").strip() or None,
        app_auth_secret=(os.getenv("APP_AUTH_SECRET") or "").strip() or None,
        app_session_cookie_name=(os.getenv("APP_SESSION_COOKIE_NAME") or "").strip() or "rss_kindle_session",
        app_session_max_age_seconds=max(300, int(os.getenv("APP_SESSION_MAX_AGE_SECONDS", str(86400 * 30)))),
        app_device_cookie_name=(os.getenv("APP_DEVICE_COOKIE_NAME") or "").strip() or "rss_kindle_device",
        device_session_max_age_seconds=max(
            86400,
            int(os.getenv("DEVICE_SESSION_MAX_AGE_SECONDS", str(86400 * 365))),
        ),
        pairing_code_ttl_seconds=max(60, int(os.getenv("PAIRING_CODE_TTL_SECONDS", "600"))),
        pairing_code_attempts=max(1, int(os.getenv("PAIRING_CODE_ATTEMPTS", "5"))),
        app_secure_cookies=_env_flag("APP_SECURE_COOKIES", True),
        app_allowed_hosts=_env_csv("APP_ALLOWED_HOSTS"),
        source_bridge_api_url=(os.getenv("SOURCE_BRIDGE_API_URL") or "").strip().rstrip("/") or None,
        source_bridge_access_token=(os.getenv("SOURCE_BRIDGE_ACCESS_TOKEN") or "").strip() or None,
        source_bridge_timeout_seconds=max(
            1, float(os.getenv("SOURCE_BRIDGE_TIMEOUT_SECONDS", "60"))
        ),
        source_bridge_config_path=source_bridge_config_path,
        source_bridge_refresh_seconds=int(os.getenv("SOURCE_BRIDGE_REFRESH_SECONDS", "900")),
        source_bridge_prewarm_enabled=_env_flag("SOURCE_BRIDGE_PREWARM_ENABLED", True),
        source_bridge_prewarm_interval_seconds=max(1, int(os.getenv("SOURCE_BRIDGE_PREWARM_INTERVAL_SECONDS", "60"))),
        backup_directory=_resolve_optional_path(
            base_dir,
            (os.getenv("BACKUP_DIRECTORY") or "data/backups").strip(),
        ),
        backup_retention_count=max(1, int(os.getenv("BACKUP_RETENTION_COUNT", "7"))),
    )
    if bool(settings.app_auth_username) != bool(settings.app_auth_password):
        raise RuntimeError("APP_AUTH_USERNAME and APP_AUTH_PASSWORD must be set together.")
    if (settings.app_auth_username or settings.app_auth_password) and not settings.app_auth_secret:
        raise RuntimeError("APP_AUTH_SECRET is required when app auth is enabled.")
    return settings
