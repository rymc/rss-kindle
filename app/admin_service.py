from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Any
from urllib.parse import quote

import httpx

from app.backup_service import BackupService
from app.config import Settings
from app.repository import Repository
from app.utils import utc_now_iso


class AdminService:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        freshrss_client: Any,
        backup_service: BackupService,
    ):
        self.settings = settings
        self.repository = repository
        self.freshrss_client = freshrss_client
        self.backup_service = backup_service
        self.started_at = time.monotonic()
        self._bridge_client = (
            httpx.Client(
                base_url=settings.source_bridge_api_url,
                follow_redirects=True,
                timeout=min(settings.http_timeout_seconds, 5),
                headers={"User-Agent": settings.user_agent},
            )
            if settings.source_bridge_api_url
            else None
        )

    def close(self) -> None:
        if self._bridge_client is not None:
            self._bridge_client.close()

    def snapshot(self) -> dict[str, Any]:
        freshrss = self._freshrss_status()
        bridge = self._bridge_status()
        return {
            "uptime_seconds": int(time.monotonic() - self.started_at),
            "freshrss": freshrss,
            "bridge": bridge,
            "cache": asdict(self.repository.get_article_cache_stats()),
            "devices": [
                asdict(device) for device in self.repository.list_reader_devices()
            ],
            "backup": self.backup_service.status(),
        }

    def refresh_bridge_source(self, source_id: str) -> bool:
        if self._bridge_client is None:
            raise RuntimeError("Source bridge is not configured.")
        response = self._bridge_client.post(
            f"/sources/{quote(source_id, safe='')}/refresh",
            headers=self._bridge_headers(),
        )
        response.raise_for_status()
        payload = response.json()
        return bool(payload.get("scheduled"))

    def _freshrss_status(self) -> dict[str, Any]:
        started_at = time.perf_counter()
        try:
            navigation = self.freshrss_client.list_navigation()
        except Exception as exc:  # noqa: BLE001 - the dashboard reports dependency failures
            return self._freshrss_failure(started_at, exc)

        detail_errors: list[str] = []
        last_refreshed_at = self._optional_status_value(
            "refresh time",
            getattr(self.freshrss_client, "get_last_refreshed_at", None),
            detail_errors,
        )
        latest_entry = self._optional_status_value(
            "latest article",
            self._get_latest_entry,
            detail_errors,
        )
        latest_received_at = (
            getattr(latest_entry, "received_at", None) if latest_entry else None
        )
        return {
            "ok": True,
            "latency_ms": _elapsed_ms(started_at),
            "feed_count": len(navigation.feeds),
            "group_count": len(navigation.groups),
            "checked_at": utc_now_iso(),
            "last_refreshed_at": last_refreshed_at,
            "latest_article_at": latest_received_at
            or (latest_entry.published_at if latest_entry else None),
            "latest_article_time_kind": "received"
            if latest_received_at
            else "published",
            "latest_article_title": latest_entry.title if latest_entry else None,
            "detail_error": "; ".join(detail_errors) or None,
            "error": None,
        }

    def _bridge_status(self) -> dict[str, Any]:
        if self._bridge_client is None:
            return {"configured": False, "ok": None, "sources": [], "error": None}
        try:
            response = self._bridge_client.get(
                "/status", headers=self._bridge_headers()
            )
            response.raise_for_status()
            payload = response.json()
            sources = payload if isinstance(payload, list) else []
            return {"configured": True, "ok": True, "sources": sources, "error": None}
        except (httpx.HTTPError, ValueError) as exc:
            return {"configured": True, "ok": False, "sources": [], "error": str(exc)}

    def _get_latest_entry(self) -> Any:
        get_latest_entry = getattr(self.freshrss_client, "get_latest_entry", None)
        if callable(get_latest_entry):
            return get_latest_entry()
        latest_page = self.freshrss_client.get_stream(scope_kind="home", limit=1)
        return latest_page.entries[0] if latest_page.entries else None

    def _optional_status_value(
        self,
        label: str,
        operation: Callable[[], Any] | None,
        errors: list[str],
    ) -> Any:
        if not callable(operation):
            return None
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 - optional checks must not hide core status
            errors.append(f"{label}: {exc}")
            return None

    def _freshrss_failure(self, started_at: float, error: Exception) -> dict[str, Any]:
        return {
            "ok": False,
            "latency_ms": _elapsed_ms(started_at),
            "feed_count": 0,
            "group_count": 0,
            "checked_at": utc_now_iso(),
            "last_refreshed_at": None,
            "latest_article_at": None,
            "latest_article_time_kind": None,
            "latest_article_title": None,
            "detail_error": None,
            "error": str(error),
        }

    def _bridge_headers(self) -> dict[str, str] | None:
        if not self.settings.source_bridge_access_token:
            return None
        return {"X-Source-Bridge-Token": self.settings.source_bridge_access_token}


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 1)
