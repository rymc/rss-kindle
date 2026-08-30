from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import __version__
from app.account_routes import AccountController
from app.admin_service import AdminService
from app.auth import auth_enabled
from app.backup_service import BackupService
from app.config import Settings, get_settings
from app.content_service import ArticleExtractor
from app.dashboard_routes import DashboardController
from app.db import Database
from app.device_auth import DeviceAuthService
from app.freshrss import FreshRSSClient
from app.reader_routes import ReaderController
from app.repository import Repository
from app.web_runtime import (
    ArticleService,
    FreshRSSService,
    WebServices,
    allowed_reader_hosts,
    build_templates,
    close_services,
    install_request_middleware,
)


def create_app(
    settings: Settings | None = None,
    *,
    repository: Repository | None = None,
    freshrss_client: FreshRSSService | None = None,
    extractor: ArticleService | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    repository = repository or Repository(Database(settings.database_path))
    repository.initialize()
    freshrss = freshrss_client or FreshRSSClient(settings)
    article_service = extractor or ArticleExtractor(settings, repository)
    backup = BackupService(settings)
    admin = AdminService(settings, repository, freshrss, backup)
    login_required = auth_enabled(
        settings.app_auth_username, settings.app_auth_password
    )
    device_auth = DeviceAuthService(settings, repository) if login_required else None
    templates = build_templates(settings)

    services = WebServices(
        settings=settings,
        repository=repository,
        freshrss=freshrss,
        extractor=article_service,
        backup=backup,
        admin=admin,
        device_auth=device_auth,
        templates=templates,
        login_required=login_required,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        close_services(services)

    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)
    if settings.app_allowed_hosts:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=allowed_reader_hosts(settings.app_allowed_hosts),
        )

    static_directory = settings.base_dir / "app" / "static"
    app.mount("/static", StaticFiles(directory=str(static_directory)), name="static")
    install_request_middleware(app, services)

    app.add_api_route("/health", health, methods=["GET"], name="health")
    app.add_api_route(
        "/favicon.ico",
        favicon,
        methods=["GET"],
        name="favicon",
        include_in_schema=False,
    )
    app.include_router(ReaderController(app, services).router)
    app.include_router(AccountController(app, services).router)
    app.include_router(DashboardController(app, services).router)
    return app


def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "version": __version__})


def favicon() -> Response:
    return Response(
        status_code=204,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
