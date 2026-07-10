"""Session-token authentication for the API surface.

Design goals, in keeping with the rest of Compass:
  * zero-config demo (default admin/compass credentials, documented in .env)
  * no new dependencies — tokens are HMAC-SHA256-signed JSON, stdlib only
  * one seam — `require_user` is the only thing routes depend on, so swapping
    in Entra ID/OIDC later means reimplementing this module, not the routes

Token format: base64url(payload).base64url(signature)
  payload   = {"u": username, "exp": unix_seconds}
  signature = HMAC-SHA256(secret, payload_bytes)

Logout is client-side token discard; tokens are short-lived (12h default).
Passwords compare via hmac.compare_digest (constant-time).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets as _secrets
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from compass.config import get_settings
from compass.services.telemetry import log_event

router = APIRouter(prefix="/v1/auth", tags=["auth"])
_bearer = HTTPBearer(auto_error=False)

# Stable for the process lifetime when no secret is configured.
_process_secret = _secrets.token_bytes(32)


def _secret() -> bytes:
    configured = get_settings().auth.secret
    return configured.encode() if configured else _process_secret


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def mint_token(username: str) -> str:
    ttl = get_settings().auth.token_ttl_hours * 3600
    payload = json.dumps(
        {"u": username, "exp": int(time.time() + ttl)}, separators=(",", ":")
    ).encode()
    signature = hmac.new(_secret(), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(signature)}"


def verify_token(token: str) -> str | None:
    """Returns the username, or None for anything invalid or expired."""
    try:
        payload_b64, signature_b64 = token.split(".", 1)
        payload = _unb64(payload_b64)
        expected = hmac.new(_secret(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(signature_b64)):
            return None
        data = json.loads(payload)
        if data.get("exp", 0) < time.time():
            return None
        return str(data["u"])
    except Exception:  # noqa: BLE001 — any malformed token is just invalid
        return None


async def require_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """FastAPI dependency guarding every stateful route. When auth is
    disabled, everything runs as 'guest' and no header is needed."""
    if not get_settings().auth.enabled:
        return "guest"
    if credentials is None:
        raise HTTPException(status_code=401, detail="authentication required")
    username = verify_token(credentials.credentials)
    if username is None:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    return username


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(body: LoginRequest) -> dict:
    auth = get_settings().auth
    if not auth.enabled:
        return {"token": "", "user": {"username": "guest"}}
    expected = auth.users.get(body.username)
    ok = expected is not None and hmac.compare_digest(
        expected.encode(), body.password.encode()
    )
    log_event("auth_login", ok=ok)
    if not ok:
        raise HTTPException(status_code=401, detail="invalid username or password")
    return {
        "token": mint_token(body.username),
        "user": {"username": body.username},
    }


@router.get("/me")
async def me(username: str = Depends(require_user)) -> dict:
    return {"username": username, "auth_enabled": get_settings().auth.enabled}
