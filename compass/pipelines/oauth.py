"""The authorization-code flow, and keeping the token it produces alive.

This is the piece `nodes/connectors.py` said was missing: "Compass has no
OAuth flow yet, so those connectors work with a token you paste and stop
working when it expires." A pasted Google access token lasts about an hour,
which makes a scheduled pipeline something that works when you build it and
fails overnight — the specific failure that is hardest to diagnose, because
nothing about the graph changed.

Three parts, matching what n8n does:

  start     build the provider's consent URL and remember why we sent them
  callback  swap the code for an access token and a refresh token
  refresh   swap the refresh token for a new access token, before a call

The refresh is the half that makes it worth having. An access token is
short-lived by design; a refresh token is not, so the pipeline keeps working
without anybody signing in again. `fresh_values` is called on the path to
every request, refreshing only when the token is actually near expiry.

Credential values are stored as one JSON document behind the connection's
existing `secret_ref`, so client id, secret, access token, refresh token and
expiry travel together and none of them touch the pipeline graph. A secret
written before this file existed is a bare string; `load_values` reads it as
an access token so existing connections keep working.
"""

from __future__ import annotations

import json
import logging
import secrets as _secrets
import time
from typing import Any
from urllib.parse import urlencode

from compass.pipelines.credentials import CLIENT_DEFAULTS, get_type, redirect_uri
from compass.pipelines.secrets import get_secret_store

logger = logging.getLogger("compass.pipelines.oauth")

#: Refresh this far ahead of expiry. A token that expires mid-request is a
#: failed run; a minute of slack costs nothing.
SKEW_S = 120.0

#: Pending authorizations, keyed by the opaque state we sent the provider.
#: In memory on purpose — a state that does not survive a restart is a state
#: that cannot be replayed, and the flow is seconds long.
_pending: dict[str, dict[str, Any]] = {}

#: How long a started sign-in may sit unfinished.
_STATE_TTL_S = 600.0


def load_values(raw: str) -> dict[str, Any]:
    """The stored credential, as fields.

    A bare string is a secret written before credentials had a shape; it is
    read as an access token so connections made under the old model keep
    working rather than being quietly broken by this file existing.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"access_token": raw}
    return parsed if isinstance(parsed, dict) else {"access_token": raw}


def dump_values(values: dict[str, Any]) -> str:
    return json.dumps(values)


def with_client_defaults(kind: str, values: dict[str, Any]) -> dict[str, Any]:
    """Fill in a registered Compass application's client id and secret, when
    one is configured. Absent that, the person's own are used."""
    default = CLIENT_DEFAULTS.get(kind)
    if not default:
        return values
    out = dict(values)
    out.setdefault("client_id", default["client_id"])
    out.setdefault("client_secret", default["client_secret"])
    return out


def _prune() -> None:
    now = time.time()
    for state in [s for s, p in _pending.items()
                  if now - p["started_at"] > _STATE_TTL_S]:
        _pending.pop(state, None)


def start(connection_id: str, kind: str, values: dict[str, Any]) -> str:
    """The URL to send the person to, or raise saying what is missing."""
    spec = get_type(kind)
    if spec is None or spec.auth != "oauth2":
        raise ValueError(f"{kind} does not sign in — it takes a pasted token.")
    values = with_client_defaults(kind, values)
    if not values.get("client_id") or not values.get("client_secret"):
        raise ValueError(
            "This connection has no client id and secret yet. Register an "
            "application with the provider, add the redirect URI shown on the "
            "form, and save them here first."
        )

    _prune()
    state = _secrets.token_urlsafe(24)
    _pending[state] = {
        "connection_id": connection_id,
        "kind": kind,
        "started_at": time.time(),
    }
    query = {
        "client_id": values["client_id"],
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(spec.scopes),
        "state": state,
        # Google returns a refresh token only on the first consent unless it
        # is asked to prompt again; without this a re-authorisation yields an
        # access token that expires and nothing to renew it with.
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{spec.authorize_url}?{urlencode(query)}"


def pending_for(state: str) -> dict[str, Any] | None:
    _prune()
    return _pending.get(state)


async def exchange(state: str, code: str) -> dict[str, Any]:
    """Swap the authorization code for tokens and store them."""
    import httpx

    pending = _pending.pop(state, None)
    if pending is None:
        raise ValueError("That sign-in has expired. Start it again.")

    from compass.pipelines import store as pstore

    conn = await pstore.connections.get(pending["connection_id"])
    if conn is None:
        raise ValueError("The connection was deleted while signing in.")

    spec = get_type(conn.kind)
    if spec is None:
        raise ValueError(f"no credential type for {conn.kind}")

    raw = await get_secret_store().get(conn.secret_ref) if conn.secret_ref else ""
    values = with_client_defaults(conn.kind, load_values(raw))

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(spec.token_url, data={
            "code": code,
            "client_id": values.get("client_id", ""),
            "client_secret": values.get("client_secret", ""),
            "redirect_uri": redirect_uri(),
            "grant_type": "authorization_code",
        }, headers={"Accept": "application/json"})

    if response.status_code >= 300:
        raise ValueError(_provider_error(response.text, response.status_code))

    payload = response.json()
    values.update(_token_fields(payload))
    await _save(conn, values)
    return values


async def refresh(conn, values: dict[str, Any]) -> dict[str, Any]:
    """Renew an access token from its refresh token."""
    import httpx

    spec = get_type(conn.kind)
    if spec is None or not values.get("refresh_token"):
        return values

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(spec.token_url, data={
            "refresh_token": values["refresh_token"],
            "client_id": values.get("client_id", ""),
            "client_secret": values.get("client_secret", ""),
            "grant_type": "refresh_token",
        }, headers={"Accept": "application/json"})

    if response.status_code >= 300:
        # Reported rather than swallowed: a refresh token that has been
        # revoked needs a person, and a run that fails saying so is more
        # useful than one that fails on a 401 three nodes later.
        raise ValueError(
            "Could not renew this connection's access. Sign in again. "
            f"({_provider_error(response.text, response.status_code)})"
        )

    payload = response.json()
    values.update(_token_fields(payload))
    await _save(conn, values)
    logger.info("refreshed the access token for connection %s", conn.id)
    return values


async def fresh_values(conn, values: dict[str, Any]) -> dict[str, Any]:
    """The credential, refreshed first if its token is about to expire."""
    spec = get_type(conn.kind)
    if spec is None or spec.auth != "oauth2":
        return values
    expires_at = float(values.get("expires_at") or 0)
    if not expires_at or expires_at - SKEW_S > time.time():
        return values
    if not values.get("refresh_token"):
        raise ValueError(
            "This connection's access has expired and it has no refresh "
            "token. Sign in again on the Connections panel."
        )
    return await refresh(conn, values)


def _token_fields(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if token := payload.get("access_token"):
        out["access_token"] = token
    # Only overwrite the refresh token when the provider actually sent one:
    # Google omits it on a refresh, and blindly copying the absent value
    # would throw away the only thing that can renew the next token.
    if refresh_token := payload.get("refresh_token"):
        out["refresh_token"] = refresh_token
    if expires_in := payload.get("expires_in"):
        try:
            out["expires_at"] = time.time() + float(expires_in)
        except (TypeError, ValueError):
            pass
    return out


def _provider_error(body: str, status: int) -> str:
    """The provider's own words, which are usually the actionable ones."""
    try:
        data = json.loads(body)
        detail = data.get("error_description") or data.get("error") or ""
        if isinstance(detail, dict):
            detail = detail.get("message", "")
        if detail:
            return str(detail)[:300]
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    return f"the provider returned {status}"


async def _save(conn, values: dict[str, Any]) -> None:
    from compass.pipelines import store as pstore

    ref = conn.secret_ref or f"{conn.id}/secret"
    await get_secret_store().set(ref, dump_values(values))
    if not conn.secret_ref:
        conn.secret_ref = ref
        pstore.connections._write(conn.id, conn.__dict__)
