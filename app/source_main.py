from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import Settings, get_settings
from app.db import Database
from app.repository import Repository
from app.source_bridge import SourceBridgeError, SourceBridgeService, SourceNotConfiguredError

logger = logging.getLogger(__name__)


def _get_submitted_bridge_token(request: Request) -> str | None:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip() or None
    return (
        request.headers.get("x-source-bridge-token")
        or request.query_params.get("access_token")
        or None
    )


def _require_bridge_token(request: Request, expected_token: str | None) -> None:
    if not expected_token:
        return
    if _get_submitted_bridge_token(request) != expected_token:
        raise HTTPException(status_code=401, detail="Missing or invalid source bridge token.")


def _source_bridge_allowed_hosts(settings: Settings) -> list[str]:
    allowed_hosts = list(settings.app_allowed_hosts)
    if settings.source_bridge_api_url:
        bridge_host = urlsplit(settings.source_bridge_api_url).hostname
        if bridge_host and bridge_host not in allowed_hosts:
            allowed_hosts.append(bridge_host)
    return allowed_hosts


def _run_source_prewarm_loop(
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


def create_app(
    settings: Settings | None = None,
    *,
    repository: Repository | None = None,
    source_bridge: SourceBridgeService | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    repository = repository or Repository(Database(settings.database_path))
    repository.initialize()
    source_bridge = source_bridge or SourceBridgeService(settings, repository)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        stop_event = threading.Event()
        prewarm_thread: threading.Thread | None = None

        if settings.source_bridge_prewarm_enabled and source_bridge.list_sources():
            interval_seconds = max(1, settings.source_bridge_prewarm_interval_seconds)
            try:
                source_bridge.schedule_stale_refreshes(lookahead_seconds=interval_seconds)
            except Exception:
                logger.warning("Source bridge prewarm startup refresh failed", exc_info=True)
            prewarm_thread = threading.Thread(
                target=_run_source_prewarm_loop,
                kwargs={
                    "source_bridge": source_bridge,
                    "interval_seconds": interval_seconds,
                    "stop_event": stop_event,
                },
                name="source-bridge-prewarm",
                daemon=True,
            )
            prewarm_thread.start()

        try:
            yield
        finally:
            stop_event.set()
            if prewarm_thread is not None:
                prewarm_thread.join(timeout=1)

    app = FastAPI(title=f"{settings.app_name} Source Bridge", lifespan=lifespan)
    allowed_hosts = _source_bridge_allowed_hosts(settings)
    if allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "source_count": len(source_bridge.list_sources())})

    @app.get("/sources")
    def sources(request: Request) -> list[dict[str, object]]:
        _require_bridge_token(request, settings.source_bridge_access_token)
        return [
            {
                "id": source.source_id,
                "title": source.title,
                "start_urls": list(source.start_urls),
                "fetch_backend": source.fetch_backend,
                "max_items": source.max_items,
            }
            for source in source_bridge.list_sources()
        ]

    @app.get("/synthetic/{source_id}.xml")
    def synthetic_feed(request: Request, source_id: str) -> Response:
        _require_bridge_token(request, settings.source_bridge_access_token)
        try:
            xml = source_bridge.build_feed(source_id)
        except SourceNotConfiguredError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SourceBridgeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return Response(content=xml, media_type="application/rss+xml")

    @app.get("/extract")
    def extract_article(
        request: Request,
        url: str = Query(...),
        title: str | None = Query(default=None),
    ) -> JSONResponse:
        _require_bridge_token(request, settings.source_bridge_access_token)
        try:
            article = source_bridge.extract_article(url, fallback_title=title)
        except SourceNotConfiguredError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SourceBridgeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse(
            {
                "source_id": article.source_id,
                "article_url": article.article_url,
                "title": article.title,
                "content_html": article.content_html,
                "summary_text": article.summary_text,
                "published_at": article.published_at,
            }
        )

    return app


app = create_app()
