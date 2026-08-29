from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

WAIT_UNTIL_VALUES = frozenset({"commit", "domcontentloaded", "load", "networkidle"})


class SourceBridgeError(RuntimeError):
    pass


class SourceNotConfiguredError(SourceBridgeError):
    pass


@dataclass(frozen=True)
class AuthProfile:
    name: str
    domains: tuple[str, ...]
    headers: dict[str, str] = field(default_factory=dict)
    cookie_header: str | None = None
    cookie_jar_path: Path | None = None
    browser_cdp_url: str | None = None
    browser_profile_path: Path | None = None
    browser_executable_path: Path | None = None
    browser_channel: str | None = None
    browser_headless: bool = False
    browser_launch_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceDefinition:
    source_id: str
    title: str
    start_urls: tuple[str, ...]
    link_selector: str = "a[href]"
    include_url_patterns: tuple[str, ...] = ()
    exclude_url_patterns: tuple[str, ...] = ()
    auth_profile: str | None = None
    max_items: int = 20
    refresh_seconds: int | None = None
    fetch_backend: str = "http"
    browser_wait_until: str = "domcontentloaded"
    browser_wait_for_selector: str | None = None
    browser_settle_seconds: float = 0.0
    discovery_browser_wait_until: str | None = None
    discovery_browser_wait_for_selector: str | None = None
    discovery_browser_settle_seconds: float | None = None


@dataclass(frozen=True)
class SourceCatalog:
    sources: dict[str, SourceDefinition]
    auth_profiles: dict[str, AuthProfile]

    @classmethod
    def load(cls, path: Path | None) -> SourceCatalog:
        if path is None:
            return cls(sources={}, auth_profiles={})
        if not path.exists():
            raise SourceBridgeError(f"Source bridge config not found: {path}")

        with path.open("rb") as handle:
            raw = tomllib.load(handle)
        auth_section = _table(
            raw.get("auth_profiles") or {}, field_name="auth_profiles"
        )
        source_section = _table(raw.get("sources") or {}, field_name="sources")
        auth_profiles = {
            name: _parse_auth_profile(
                name, _table(payload, field_name=f"auth_profiles.{name}"), path.parent
            )
            for name, payload in auth_section.items()
        }
        sources = {
            source_id: _parse_source(
                source_id,
                _table(payload, field_name=f"sources.{source_id}"),
                auth_profiles,
            )
            for source_id, payload in source_section.items()
        }
        return cls(sources=sources, auth_profiles=auth_profiles)


def _parse_auth_profile(
    name: str, payload: dict[str, Any], base_dir: Path
) -> AuthProfile:
    headers = _table(
        payload.get("headers") or {}, field_name=f"auth_profiles.{name}.headers"
    )
    return AuthProfile(
        name=name,
        domains=tuple(
            _string_list(
                payload.get("domains"), field_name=f"auth_profiles.{name}.domains"
            )
        ),
        headers={str(key): str(value) for key, value in headers.items()},
        cookie_header=_optional_string(payload.get("cookie_header")),
        cookie_jar_path=_optional_path(base_dir, payload.get("cookie_jar_path")),
        browser_cdp_url=_optional_string(payload.get("browser_cdp_url")),
        browser_profile_path=_optional_path(
            base_dir, payload.get("browser_profile_path")
        ),
        browser_executable_path=_optional_path(
            base_dir, payload.get("browser_executable_path")
        ),
        browser_channel=_optional_string(payload.get("browser_channel")),
        browser_headless=_boolean(
            payload.get("browser_headless"),
            field_name=f"auth_profiles.{name}.browser_headless",
        ),
        browser_launch_args=tuple(
            _string_list(
                payload.get("browser_launch_args"),
                field_name=f"auth_profiles.{name}.browser_launch_args",
            )
        ),
    )


def _parse_source(
    source_id: str,
    payload: dict[str, Any],
    auth_profiles: dict[str, AuthProfile],
) -> SourceDefinition:
    start_urls = payload.get("start_urls")
    if start_urls is None and payload.get("start_url"):
        start_urls = [payload["start_url"]]
    max_items = int(payload.get("max_items", 20))
    if max_items <= 0:
        raise SourceBridgeError(f"Source {source_id!r} max_items must be positive.")

    auth_profile = _optional_string(payload.get("auth_profile"))
    if auth_profile and auth_profile not in auth_profiles:
        raise SourceBridgeError(
            f"Source {source_id!r} references unknown auth profile {auth_profile!r}."
        )
    fetch_backend = (_optional_string(payload.get("fetch_backend")) or "http").lower()
    if fetch_backend not in {"http", "browser"}:
        raise SourceBridgeError(
            f"Source {source_id!r} fetch_backend must be 'http' or 'browser'."
        )

    return SourceDefinition(
        source_id=source_id,
        title=_required_string(
            payload.get("title"), field_name=f"sources.{source_id}.title"
        ),
        start_urls=tuple(
            _string_list(start_urls, field_name=f"sources.{source_id}.start_urls")
        ),
        link_selector=_optional_string(payload.get("link_selector")) or "a[href]",
        include_url_patterns=tuple(
            _string_list(
                payload.get("include_url_patterns"),
                field_name=f"sources.{source_id}.include_url_patterns",
            )
        ),
        exclude_url_patterns=tuple(
            _string_list(
                payload.get("exclude_url_patterns"),
                field_name=f"sources.{source_id}.exclude_url_patterns",
            )
        ),
        auth_profile=auth_profile,
        max_items=max_items,
        refresh_seconds=_optional_int(payload.get("refresh_seconds")),
        fetch_backend=fetch_backend,
        browser_wait_until=_wait_until(
            payload.get("browser_wait_until"),
            field_name=f"sources.{source_id}.browser_wait_until",
            default="domcontentloaded",
        ),
        browser_wait_for_selector=_optional_string(
            payload.get("browser_wait_for_selector")
        ),
        browser_settle_seconds=float(payload.get("browser_settle_seconds", 0.0) or 0.0),
        discovery_browser_wait_until=_wait_until(
            payload.get("discovery_browser_wait_until"),
            field_name=f"sources.{source_id}.discovery_browser_wait_until",
            default=None,
        ),
        discovery_browser_wait_for_selector=_optional_string(
            payload.get("discovery_browser_wait_for_selector")
        ),
        discovery_browser_settle_seconds=_optional_float(
            payload.get("discovery_browser_settle_seconds"),
            field_name=f"sources.{source_id}.discovery_browser_settle_seconds",
        ),
    )


def _table(value: Any, *, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SourceBridgeError(f"{field_name} must be a table.")
    return value


def _required_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceBridgeError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SourceBridgeError("Expected a string value in source bridge config.")
    return value.strip() or None


def _string_list(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        raise SourceBridgeError(f"{field_name} must be a string or array of strings.")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise SourceBridgeError(f"{field_name} entries must be non-empty strings.")
    return [item.strip() for item in value]


def _boolean(value: Any, *, field_name: str) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise SourceBridgeError(f"{field_name} must be a boolean.")


def _optional_float(value: Any, *, field_name: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise SourceBridgeError(f"{field_name} must be a number.") from exc


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


def _optional_path(base_dir: Path, value: Any) -> Path | None:
    raw_value = _optional_string(value)
    if raw_value is None:
        return None
    candidate = Path(raw_value).expanduser()
    return (candidate if candidate.is_absolute() else base_dir / candidate).resolve()


def _wait_until(value: Any, *, field_name: str, default: str | None) -> str | None:
    selected = _optional_string(value)
    if selected is None:
        return default
    selected = selected.lower()
    if selected not in WAIT_UNTIL_VALUES:
        choices = ", ".join(sorted(WAIT_UNTIL_VALUES))
        raise SourceBridgeError(f"{field_name} must be one of {choices}.")
    return selected
