from __future__ import annotations

import time

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from app.auth import sanitize_next_path
from app.hacker_news import HackerNewsError
from app.web_runtime import (
    WebServices,
    build_template_context,
    reader_template_response,
    record_timing,
)


class HackerNewsController:
    def __init__(self, services: WebServices):
        self.services = services
        self.router = APIRouter()
        self.router.add_api_route(
            "/hacker-news/{item_id}",
            self.discussion,
            methods=["GET"],
            name="hacker_news_discussion",
        )

    def discussion(
        self,
        request: Request,
        item_id: int,
        back: str = Query("/"),
    ) -> Response:
        started_at = time.perf_counter()
        try:
            discussion = self.services.hacker_news.get_discussion(item_id)
        except (HackerNewsError, httpx.HTTPError) as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Hacker News request failed: {exc}",
            ) from exc
        record_timing(request, "hacker_news", started_at)

        context = build_template_context(
            self.services,
            request,
            page_title=discussion.title,
            show_site_header=False,
            reader_script=True,
        )
        context.update(
            {
                "discussion": discussion,
                "body_class": "article-page hacker-news-page",
                "extra_styles": ("hacker_news.css",),
                "return_url": sanitize_next_path(back),
            }
        )
        return reader_template_response(
            self.services,
            request,
            name="hacker_news.html",
            context=context,
        )
