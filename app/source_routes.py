from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from app.config import Settings
from app.source_bridge import (
    SourceBridgeError,
    SourceBridgeService,
    SourceNotConfiguredError,
)


class SourceBridgeController:
    def __init__(self, settings: Settings, source_bridge: SourceBridgeService):
        self.settings = settings
        self.source_bridge = source_bridge
        self.router = APIRouter()
        self.router.add_api_route(
            "/health", self.health, methods=["GET"], name="health"
        )
        self.router.add_api_route(
            "/sources", self.sources, methods=["GET"], name="sources"
        )
        self.router.add_api_route(
            "/status", self.status, methods=["GET"], name="status"
        )
        self.router.add_api_route(
            "/sources/{source_id}/refresh",
            self.refresh_source,
            methods=["POST"],
            name="refresh_source",
            status_code=202,
        )
        self.router.add_api_route(
            "/synthetic/{source_id}.xml",
            self.synthetic_feed,
            methods=["GET"],
            name="synthetic_feed",
        )
        self.router.add_api_route(
            "/extract",
            self.extract_article,
            methods=["GET"],
            name="extract_article",
        )

    def health(self) -> JSONResponse:
        return JSONResponse(
            {"status": "ok", "source_count": len(self.source_bridge.list_sources())}
        )

    def sources(self, request: Request) -> list[dict[str, object]]:
        self._authorize(request)
        return [
            {
                "id": source.source_id,
                "title": source.title,
                "start_urls": list(source.start_urls),
                "fetch_backend": source.fetch_backend,
                "max_items": source.max_items,
            }
            for source in self.source_bridge.list_sources()
        ]

    def status(self, request: Request) -> list[dict[str, object]]:
        self._authorize(request)
        return self.source_bridge.list_source_status()

    def refresh_source(self, request: Request, source_id: str) -> JSONResponse:
        self._authorize(request)
        try:
            scheduled = self.source_bridge.schedule_refresh(source_id)
        except SourceNotConfiguredError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return JSONResponse(
            {"source_id": source_id, "scheduled": scheduled},
            status_code=202,
        )

    def synthetic_feed(self, request: Request, source_id: str) -> Response:
        self._authorize(request)
        try:
            xml = self.source_bridge.build_feed(source_id)
        except SourceNotConfiguredError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SourceBridgeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return Response(content=xml, media_type="application/rss+xml")

    def extract_article(
        self,
        request: Request,
        url: str = Query(...),
        title: str | None = Query(default=None),
    ) -> JSONResponse:
        self._authorize(request)
        try:
            article = self.source_bridge.extract_article(url, fallback_title=title)
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

    def _authorize(self, request: Request) -> None:
        expected = self.settings.source_bridge_access_token
        if not expected:
            return
        submitted = _submitted_token(request)
        if submitted is None or not hmac.compare_digest(submitted, expected):
            raise HTTPException(
                status_code=401,
                detail="Missing or invalid source bridge token.",
            )


def _submitted_token(request: Request) -> str | None:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip() or None
    return request.headers.get("x-source-bridge-token") or request.query_params.get(
        "access_token"
    )
