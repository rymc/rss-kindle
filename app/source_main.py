from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import Settings, get_settings
from app.db import Database
from app.repository import Repository
from app.source_bridge import SourceBridgeService
from app.source_routes import SourceBridgeController

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    repository: Repository | None = None,
    source_bridge: SourceBridgeService | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    if not settings.source_bridge_access_token:
        raise RuntimeError(
            "SOURCE_BRIDGE_ACCESS_TOKEN is required to start the source bridge."
        )
    repository = repository or Repository(Database(settings.database_path))
    repository.initialize()
    source_bridge = source_bridge or SourceBridgeService(settings, repository)

    app = FastAPI(
        title=f"{settings.app_name} Source Bridge",
        lifespan=_lifespan(settings, source_bridge),
    )
    allowed_hosts = _allowed_hosts(settings)
    if allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    app.include_router(SourceBridgeController(settings, source_bridge).router)
    return app


def _lifespan(settings: Settings, source_bridge: SourceBridgeService):
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        stop_event = threading.Event()
        prewarm_thread = _start_prewarm(settings, source_bridge, stop_event)
        try:
            yield
        finally:
            stop_event.set()
            if prewarm_thread is not None:
                prewarm_thread.join(timeout=1)

    return lifespan


def _start_prewarm(
    settings: Settings,
    source_bridge: SourceBridgeService,
    stop_event: threading.Event,
) -> threading.Thread | None:
    if (
        not settings.source_bridge_access_token
        or not settings.source_bridge_prewarm_enabled
        or not source_bridge.list_sources()
    ):
        return None
    interval_seconds = max(1, settings.source_bridge_prewarm_interval_seconds)
    try:
        source_bridge.schedule_stale_refreshes(lookahead_seconds=interval_seconds)
    except Exception:
        logger.warning("Source bridge prewarm startup refresh failed", exc_info=True)
    thread = threading.Thread(
        target=_run_prewarm_loop,
        kwargs={
            "source_bridge": source_bridge,
            "interval_seconds": interval_seconds,
            "stop_event": stop_event,
        },
        name="source-bridge-prewarm",
        daemon=True,
    )
    thread.start()
    return thread


def _run_prewarm_loop(
    source_bridge: SourceBridgeService,
    *,
    interval_seconds: int,
    stop_event: threading.Event,
) -> None:
    while not stop_event.wait(interval_seconds):
        try:
            source_bridge.schedule_stale_refreshes(lookahead_seconds=interval_seconds)
        except Exception:
            logger.warning("Source bridge prewarm loop failed", exc_info=True)


def _allowed_hosts(settings: Settings) -> list[str]:
    allowed_hosts = list(settings.app_allowed_hosts)
    if settings.source_bridge_api_url:
        bridge_host = urlsplit(settings.source_bridge_api_url).hostname
        if bridge_host and bridge_host not in allowed_hosts:
            allowed_hosts.append(bridge_host)
    if allowed_hosts:
        for host in ("source-bridge", "127.0.0.1", "localhost"):
            if host not in allowed_hosts:
                allowed_hosts.append(host)
    return allowed_hosts
