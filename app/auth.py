from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any
from urllib.parse import urlencode, urlsplit


def auth_enabled(username: str | None, password: str | None) -> bool:
    return bool(username and password)


def verify_password(expected_password: str, submitted_password: str) -> bool:
    return hmac.compare_digest(expected_password.encode("utf-8"), submitted_password.encode("utf-8"))


def issue_session_token(
    *,
    username: str,
    secret: str,
    max_age_seconds: int,
    csrf_token: str | None = None,
) -> str:
    payload = {
        "sub": username,
        "csrf": csrf_token or secrets.token_urlsafe(24),
        "exp": int(time.time()) + max_age_seconds,
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return f"{_urlsafe_encode(raw)}.{_urlsafe_encode(signature)}"


def decode_session_token(token: str | None, *, secret: str) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None
    payload_part, signature_part = token.split(".", 1)
    try:
        raw_payload = _urlsafe_decode(payload_part)
        submitted_signature = _urlsafe_decode(signature_part)
    except ValueError:
        return None

    expected_signature = hmac.new(secret.encode("utf-8"), raw_payload, hashlib.sha256).digest()
    if not hmac.compare_digest(expected_signature, submitted_signature):
        return None

    try:
        payload = json.loads(raw_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None

    username = payload.get("sub")
    csrf_token = payload.get("csrf")
    expires_at = payload.get("exp")
    if not isinstance(username, str) or not isinstance(csrf_token, str) or not isinstance(expires_at, int):
        return None
    if expires_at < int(time.time()):
        return None
    return {"sub": username, "csrf": csrf_token, "exp": expires_at}


def validate_csrf(session_payload: dict[str, Any] | None, submitted_token: str | None) -> bool:
    if not session_payload or not submitted_token:
        return False
    expected_token = session_payload.get("csrf")
    return isinstance(expected_token, str) and hmac.compare_digest(expected_token, submitted_token)


def build_login_redirect(path: str, *, next_path: str | None) -> str:
    if next_path:
        return f"{path}?{urlencode({'next': next_path})}"
    return path


def sanitize_next_path(value: str | None, *, default: str = "/") -> str:
    if not value:
        return default
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return default
    if not parsed.path.startswith("/"):
        return default
    return value


def _urlsafe_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _urlsafe_decode(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(value + padding)
