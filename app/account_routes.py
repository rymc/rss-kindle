from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, Form, Query, Request
from fastapi.responses import RedirectResponse, Response

from app.auth import issue_session_token, sanitize_next_path, verify_password
from app.web_runtime import WebServices, request_session, require_csrf


class AccountController:
    def __init__(self, app: FastAPI, services: WebServices):
        self.app = app
        self.services = services
        self.router = APIRouter()
        self.router.add_api_route(
            "/login", self.login_page, methods=["GET"], name="login_page"
        )
        self.router.add_api_route(
            "/login", self.login_submit, methods=["POST"], name="login_submit"
        )
        self.router.add_api_route(
            "/activate", self.activate_page, methods=["GET"], name="activate_page"
        )
        self.router.add_api_route(
            "/activate",
            self.activate_submit,
            methods=["POST"],
            name="activate_submit",
        )
        self.router.add_api_route(
            "/logout", self.logout, methods=["POST"], name="logout"
        )

    def login_page(
        self,
        request: Request,
        next_path: str | None = Query(default=None, alias="next"),
    ) -> Response:
        if not self.services.login_required:
            return self._home_redirect()
        target = sanitize_next_path(next_path)
        session = request_session(request)
        password_session = bool(session and session.get("auth_type") == "password")
        device_can_continue = bool(session and not _target_is_admin(target))
        if password_session or device_can_continue:
            return RedirectResponse(target, status_code=303)
        return self._login_form(request, next_path=target)

    def login_submit(
        self,
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        next_path: str = Form("/"),
    ) -> Response:
        if not self.services.login_required:
            return self._home_redirect()
        target = sanitize_next_path(next_path)
        settings = self.services.settings
        valid_username = username == settings.app_auth_username
        valid_password = verify_password(settings.app_auth_password or "", password)
        if not valid_username or not valid_password:
            return self._login_form(
                request,
                next_path=target,
                error_message="Incorrect username or password.",
                status_code=401,
            )

        token = issue_session_token(
            username=settings.app_auth_username or username,
            secret=settings.app_auth_secret or "",
            max_age_seconds=settings.app_session_max_age_seconds,
        )
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            settings.app_session_cookie_name,
            token,
            max_age=settings.app_session_max_age_seconds,
            httponly=True,
            samesite="lax",
            secure=settings.app_secure_cookies,
            path="/",
        )
        return response

    def activate_page(
        self,
        request: Request,
        next_path: str | None = Query(default=None, alias="next"),
    ) -> Response:
        if not self.services.login_required or self.services.device_auth is None:
            return self._home_redirect()
        target = sanitize_next_path(next_path)
        if request_session(request):
            return RedirectResponse(target, status_code=303)
        return self._activation_form(request, next_path=target)

    def activate_submit(
        self,
        request: Request,
        code: str = Form(...),
        device_name: str | None = Form(default=None),
        next_path: str = Form("/"),
    ) -> Response:
        device_auth = self.services.device_auth
        if not self.services.login_required or device_auth is None:
            return self._home_redirect()
        target = sanitize_next_path(next_path)
        grant = device_auth.redeem_pairing_code(code, device_name=device_name)
        if grant is None:
            return self._activation_form(
                request,
                next_path=target,
                error_message="The pairing code is invalid or expired.",
                status_code=401,
            )

        settings = self.services.settings
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            settings.app_device_cookie_name,
            grant.token,
            max_age=settings.device_session_max_age_seconds,
            httponly=True,
            samesite="lax",
            secure=settings.app_secure_cookies,
            path="/",
        )
        return response

    def logout(
        self, request: Request, csrf_token: str | None = Form(default=None)
    ) -> Response:
        require_csrf(request, csrf_token)
        settings = self.services.settings
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(settings.app_session_cookie_name, path="/")
        response.delete_cookie(settings.app_device_cookie_name, path="/")
        return response

    def _home_redirect(self) -> RedirectResponse:
        return RedirectResponse(str(self.app.url_path_for("home")), status_code=303)

    def _login_form(
        self,
        request: Request,
        *,
        next_path: str,
        error_message: str | None = None,
        status_code: int = 200,
    ) -> Response:
        return self.services.templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "request": request,
                "page_title": "Sign in",
                "show_site_header": False,
                "next_path": next_path,
                "error_message": error_message,
            },
            status_code=status_code,
        )

    def _activation_form(
        self,
        request: Request,
        *,
        next_path: str,
        error_message: str | None = None,
        status_code: int = 200,
    ) -> Response:
        return self.services.templates.TemplateResponse(
            request=request,
            name="activate.html",
            context={
                "request": request,
                "page_title": "Pair this device",
                "show_site_header": False,
                "next_path": next_path,
                "error_message": error_message,
            },
            status_code=status_code,
        )


def _target_is_admin(path: str) -> bool:
    return urlsplit(path).path.startswith(("/admin", "/dashboard"))
