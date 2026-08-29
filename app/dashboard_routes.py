from __future__ import annotations

import sqlite3
import zipfile

import httpx
from fastapi import APIRouter, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from app import __version__
from app.auth import build_login_redirect
from app.utils import utc_now_iso
from app.web_runtime import (
    WebServices,
    current_relative_url,
    request_session,
    require_csrf,
)


class DashboardController:
    def __init__(self, app: FastAPI, services: WebServices):
        self.app = app
        self.services = services
        self.router = APIRouter()
        self.router.add_api_route(
            "/dashboard",
            self.dashboard_page,
            methods=["GET"],
            name="dashboard_page",
        )
        self.router.add_api_route(
            "/admin",
            self.legacy_admin_page,
            methods=["GET"],
            name="legacy_admin_page",
            include_in_schema=False,
        )
        self.router.add_api_route(
            "/dashboard/pairing",
            self.create_pairing,
            methods=["POST"],
            name="admin_create_pairing",
        )
        self.router.add_api_route(
            "/dashboard/devices/{device_id}/revoke",
            self.revoke_device,
            methods=["POST"],
            name="admin_revoke_device",
        )
        self.router.add_api_route(
            "/dashboard/bridge/{source_id}/refresh",
            self.refresh_bridge_source,
            methods=["POST"],
            name="admin_refresh_bridge_source",
        )
        self.router.add_api_route(
            "/dashboard/backups",
            self.create_backup,
            methods=["POST"],
            name="admin_create_backup",
        )
        self.router.add_api_route(
            "/dashboard/backups/{filename}",
            self.download_backup,
            methods=["GET"],
            name="admin_download_backup",
        )

    def dashboard_page(self, request: Request) -> Response:
        if not _is_admin(request):
            return self._login_redirect(request)
        return self._render(request)

    def legacy_admin_page(self) -> RedirectResponse:
        return RedirectResponse(
            str(self.app.url_path_for("dashboard_page")), status_code=303
        )

    def create_pairing(
        self,
        request: Request,
        csrf_token: str | None = Form(default=None),
    ) -> Response:
        self._authorize_action(request, csrf_token)
        if self.services.device_auth is None:
            raise HTTPException(
                status_code=409, detail="Device pairing is not configured."
            )
        pairing = self.services.device_auth.create_pairing_code()
        return self._render(
            request,
            pairing_code=pairing.code,
            pairing_expires_at=pairing.expires_at,
            notice="A new one-time pairing code is ready.",
        )

    def revoke_device(
        self,
        request: Request,
        device_id: str,
        csrf_token: str | None = Form(default=None),
    ) -> Response:
        self._authorize_action(request, csrf_token)
        revoked = self.services.repository.revoke_reader_device(
            device_id, revoked_at=utc_now_iso()
        )
        notice = (
            "Device access was revoked."
            if revoked
            else "Device was already revoked or was not found."
        )
        return self._render(request, notice=notice)

    def refresh_bridge_source(
        self,
        request: Request,
        source_id: str,
        csrf_token: str | None = Form(default=None),
    ) -> Response:
        self._authorize_action(request, csrf_token)
        try:
            scheduled = self.services.admin.refresh_bridge_source(source_id)
        except (httpx.HTTPError, RuntimeError) as exc:
            return self._render(request, error_message=f"Bridge refresh failed: {exc}")
        notice = (
            "Bridge refresh started."
            if scheduled
            else "This source is already refreshing."
        )
        return self._render(request, notice=notice)

    def create_backup(
        self,
        request: Request,
        csrf_token: str | None = Form(default=None),
    ) -> Response:
        self._authorize_action(request, csrf_token)
        try:
            backup = self.services.backup.create_backup()
        except (OSError, RuntimeError, sqlite3.Error, zipfile.BadZipFile) as exc:
            return self._render(request, error_message=f"Backup failed: {exc}")
        return self._render(request, notice=f"Backup created: {backup.filename}")

    def download_backup(self, request: Request, filename: str) -> Response:
        if not _is_admin(request):
            return self._login_redirect(request)
        path = self.services.backup.get_backup_path(filename)
        if path is None:
            raise HTTPException(status_code=404, detail="Backup not found.")
        return FileResponse(path, media_type="application/zip", filename=path.name)

    def _authorize_action(self, request: Request, csrf_token: str | None) -> None:
        if not _is_admin(request):
            raise HTTPException(
                status_code=403, detail="Password administrator access is required."
            )
        require_csrf(request, csrf_token)

    def _login_redirect(self, request: Request) -> RedirectResponse:
        return RedirectResponse(
            build_login_redirect("/login", next_path=current_relative_url(request)),
            status_code=303,
        )

    def _render(
        self,
        request: Request,
        *,
        pairing_code: str | None = None,
        pairing_expires_at: str | None = None,
        notice: str | None = None,
        error_message: str | None = None,
    ) -> Response:
        session = request_session(request) or {}
        return self.services.templates.TemplateResponse(
            request=request,
            name="admin.html",
            context={
                "request": request,
                "page_title": "Dashboard",
                "show_site_header": False,
                "admin_styles": True,
                "app_version": __version__,
                "csrf_token": session.get("csrf", ""),
                "status": self.services.admin.snapshot(),
                "pairing_code": pairing_code,
                "pairing_expires_at": pairing_expires_at,
                "notice": notice,
                "error_message": error_message,
            },
        )


def _is_admin(request: Request) -> bool:
    return bool(getattr(request.state, "is_admin", False))
