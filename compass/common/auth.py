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

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from compass.common.config import get_settings
from compass.common.telemetry import log_event

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


def _set_auth_cookie(response: Response, token: str) -> None:
    auth = get_settings().auth
    response.set_cookie(
        key=auth.cookie_name,
        value=token,
        httponly=True,  # never readable by browser JS
        secure=auth.cookie_secure,
        samesite=auth.cookie_samesite,
        max_age=int(auth.token_ttl_hours * 3600),
        path="/",
    )


async def require_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """FastAPI dependency guarding every stateful route. The token is read from
    the secure httpOnly cookie (no browser storage), falling back to a Bearer
    header for API clients. When auth is disabled, everything runs as 'guest'."""
    if not get_settings().auth.enabled:
        return "guest"
    token = credentials.credentials if credentials else None
    if not token:
        token = request.cookies.get(get_settings().auth.cookie_name)
    if not token:
        raise HTTPException(status_code=401, detail="authentication required")
    username = verify_token(token)
    if username is None:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    # Canonicalised here as well as at login, so a token minted before an
    # alias was configured still resolves to the right identity.
    from compass.common.users import canonical

    return canonical(username)


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(body: LoginRequest, response: Response) -> dict:
    auth = get_settings().auth
    if not auth.enabled:
        return {"user": {"username": "guest"}}
    expected = auth.users.get(body.username)
    ok = expected is not None and hmac.compare_digest(
        expected.encode(), body.password.encode()
    )
    log_event("auth_login", ok=ok)
    if not ok:
        raise HTTPException(status_code=401, detail="invalid username or password")
    from compass.common.users import canonical, get_user_store

    identity = canonical(body.username)
    await get_user_store().record_login(body.username)
    token = mint_token(body.username)
    _set_auth_cookie(response, token)
    # token still returned for non-browser API clients; browsers use the cookie.
    # `username` is the identity that owns records, which is what the client
    # should show and what every store stamps — not necessarily what was typed.
    return {"token": token, "user": {"username": identity}}


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(get_settings().auth.cookie_name, path="/")
    return {"ok": True}


@router.get("/me")
async def me(username: str = Depends(require_user)) -> dict:
    from compass.common.users import get_user_store

    row = await get_user_store().get(username)
    return {"username": username,
            "display_name": (row.display_name if row else ""),
            "auth_enabled": get_settings().auth.enabled}


class DisplayNameRequest(BaseModel):
    name: str = Field(default="", description="What Compass should call you. "
                                              "Empty clears it.")


@router.post("/me/name")
async def set_display_name(
    body: DisplayNameRequest, username: str = Depends(require_user)
) -> dict:
    """Set what this person is called.

    Kept apart from the credential map on purpose: that map says who may sign
    in, and this says how to address them. Changing one should never be a way
    to change the other.
    """
    from compass.common.users import get_user_store

    row = await get_user_store().set_display_name(username, body.name)
    return {"display_name": row.display_name}


@router.get("/users")
async def users(username: str = Depends(require_user)) -> dict:
    """Who has logged in. A record of use, not a grant of access — the
    credential map is still the only thing that decides who may sign in."""
    from compass.common.users import get_user_store

    return {"users": [u.to_dict() for u in await get_user_store().list()],
            "you": username}
