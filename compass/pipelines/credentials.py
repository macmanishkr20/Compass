"""Credential types: what a connection needs, how it is applied, how it is proved.

Modelled directly on n8n's credential classes, because the shape is right and
the reasons are worth restating. An n8n credential declares three things and
nothing else:

    properties    the fields a person fills in, with types and help
    authenticate  how those fields become an authorized request
    test          a request that proves the credential actually works

Compass had none of the three. Auth was a hardcoded `Bearer {secret}` line in
the connector handler, the create-connection form was a name and one opaque
"secret" box whatever the service, and whether a credential worked was
discovered when a pipeline ran — often at the far end of a schedule, which is
the worst possible moment and the furthest from the person who could fix it.

The `test` is the part worth insisting on. A credential that cannot be proved
at the moment it is entered is a credential that gets proved in production. It
turns "the pipeline failed at 3am" into "that token is expired, here is the
message the provider gave us", while the person is still looking at the form.

On OAuth, the honest version. n8n Cloud offers one-click Google sign-in
because n8n operates a registered, Google-verified OAuth application; their
own documentation says managed OAuth is not available to self-hosted users,
who must register an app and supply a client id and secret. Compass is
self-hosted, so it follows the self-hosted path: the client id and secret are
fields on the credential like any other, and `oauth.py` runs the standard
authorization-code flow against them. If a Compass OAuth application is ever
registered and configured, `CLIENT_DEFAULTS` fills those fields in and the
person sees the one-click flow instead — the flow itself does not change.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Any

#: Placeholder substituted at request time. Mirrors n8n's `{{$credentials.x}}`
#: without adopting a template language for four cases.
CRED = "{{cred:%s}}"


@dataclass(frozen=True)
class CredField:
    """One field on a credential form."""

    name: str
    label: str
    #: "string" | "password" | "url". A password is masked in the browser and
    #: never returned by the API once stored.
    kind: str = "string"
    required: bool = True
    default: str = ""
    help: str = ""
    #: Not asked for; produced by the OAuth flow and stored beside the rest.
    from_oauth: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CredentialType:
    """A kind of connection, in the three parts n8n uses."""

    kind: str
    label: str
    #: "token" — a pasted value that does not expire on a timer.
    #: "oauth2" — an authorization-code flow with refresh.
    #: "none"  — the endpoint needs nothing.
    auth: str
    fields: tuple[CredField, ...] = ()
    #: How the credential becomes an authorized request. Header name to a
    #: value template, resolved against the stored fields.
    headers: dict[str, str] = field(default_factory=dict)
    #: A cheap, read-only request that proves the credential. `url` is
    #: absolute; a 2xx is a pass and the body is not used, so the endpoint
    #: should be the smallest one the provider offers.
    test_url: str = ""
    test_method: str = "GET"
    #: OAuth endpoints, for auth == "oauth2".
    authorize_url: str = ""
    token_url: str = ""
    scopes: tuple[str, ...] = ()
    #: What to tell someone before they start, and where the provider
    #: documents its own setup. Shown on the form, not buried in a wiki.
    setup_note: str = ""
    docs_url: str = ""

    def public(self) -> dict[str, Any]:
        """The API view. Never includes stored values — only the shape."""
        return {
            "kind": self.kind,
            "label": self.label,
            "auth": self.auth,
            "fields": [f.to_dict() for f in self.fields if not f.from_oauth],
            "test_url": self.test_url,
            "scopes": list(self.scopes),
            "setup_note": self.setup_note,
            "docs_url": self.docs_url,
            "redirect_uri": redirect_uri(),
            "has_client_default": self.kind in CLIENT_DEFAULTS,
        }


def redirect_uri() -> str:
    """Where the provider sends the person back.

    Registered with the provider ahead of time and therefore fixed: it is
    shown on the form so it can be copied into the provider's console, which
    is the single most common thing to get wrong when setting OAuth up.
    """
    base = os.environ.get("COMPASS_PUBLIC_URL", "http://localhost:8000").rstrip("/")
    return f"{base}/v1/pipeline-connections/oauth/callback"


#: A registered Compass OAuth application, when one exists. Empty by default:
#: shipping a client id would be pretending to an approval Google has not
#: given. Set COMPASS_OAUTH_GOOGLE_CLIENT_ID / _SECRET (and the Microsoft
#: pair) and the form stops asking for them.
def _client_defaults() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for kind, prefix in (("gmail", "GOOGLE"), ("outlook", "MICROSOFT")):
        cid = os.environ.get(f"COMPASS_OAUTH_{prefix}_CLIENT_ID", "")
        secret = os.environ.get(f"COMPASS_OAUTH_{prefix}_CLIENT_SECRET", "")
        if cid and secret:
            out[kind] = {"client_id": cid, "client_secret": secret}
    return out


CLIENT_DEFAULTS = _client_defaults()


_OAUTH_FIELDS = (
    CredField("client_id", "Client ID",
              help="From the app you registered with the provider."),
    CredField("client_secret", "Client secret", kind="password"),
    CredField("access_token", "Access token", required=False, from_oauth=True),
    CredField("refresh_token", "Refresh token", required=False, from_oauth=True),
    CredField("expires_at", "Expires at", required=False, from_oauth=True),
)


CREDENTIAL_TYPES: tuple[CredentialType, ...] = (
    CredentialType(
        kind="github",
        label="GitHub",
        auth="token",
        fields=(
            CredField("access_token", "Personal access token", kind="password",
                      help="Settings → Developer settings → Personal access "
                           "tokens. Needs `repo` for private repositories."),
            CredField("server", "GitHub server", required=False,
                      default="https://api.github.com",
                      help="Change only for GitHub Enterprise."),
        ),
        headers={"Authorization": "Bearer " + CRED % "access_token"},
        # The same endpoint n8n tests GitHub against: the smallest call that
        # cannot succeed without a valid token.
        test_url="https://api.github.com/user",
        setup_note="A personal access token does not expire on a timer, so a "
                   "GitHub connection keeps working once it is set up.",
        docs_url="https://docs.github.com/en/authentication/"
                 "keeping-your-account-and-data-secure/managing-your-personal-access-tokens",
    ),
    CredentialType(
        kind="gmail",
        label="Gmail",
        auth="oauth2",
        fields=_OAUTH_FIELDS,
        headers={"Authorization": "Bearer " + CRED % "access_token"},
        test_url="https://gmail.googleapis.com/gmail/v1/users/me/profile",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=("https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.send"),
        setup_note="Google does not let a self-hosted app sign people in "
                   "without its own registration. Create a project in the "
                   "Google Cloud console, enable the Gmail API, add the "
                   "redirect URI below, and paste the client id and secret. "
                   "This is the same setup self-hosted n8n requires.",
        docs_url="https://developers.google.com/identity/protocols/oauth2/web-server",
    ),
    CredentialType(
        kind="outlook",
        label="Outlook",
        auth="oauth2",
        fields=_OAUTH_FIELDS,
        headers={"Authorization": "Bearer " + CRED % "access_token"},
        test_url="https://graph.microsoft.com/v1.0/me",
        authorize_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        scopes=("offline_access", "Mail.Read", "Mail.Send", "User.Read"),
        setup_note="Register an application in the Microsoft Entra admin "
                   "centre, add the redirect URI below as a Web platform, and "
                   "paste the application (client) id and a client secret.",
        docs_url="https://learn.microsoft.com/en-us/entra/identity-platform/"
                 "quickstart-register-app",
    ),
    CredentialType(
        kind="mssql",
        label="SQL Server",
        auth="token",
        fields=(
            CredField("host", "Server",
                      help="Host name, e.g. sql-prod.database.windows.net."),
            CredField("port", "Port", required=False, default="1433"),
            CredField("database", "Database"),
            CredField("user", "User"),
            CredField("password", "Password", kind="password"),
            CredField("encrypt", "Encrypt", required=False, default="yes",
                      help="'yes' for Azure SQL and anything over a network. "
                           "'no' only for a local server you trust."),
        ),
        # Nothing to put in a header: this connection is a driver handshake,
        # not an HTTP request. `apply_auth` returns nothing for it and the
        # handler reads the fields directly, which is why the test below is a
        # driver connection rather than a URL.
        setup_note="A database login, used by the SQL Server steps. Testing "
                   "it opens a real connection and runs SELECT 1.",
        docs_url="https://learn.microsoft.com/en-us/sql/connect/odbc/"
                 "download-odbc-driver-for-sql-server",
    ),
    CredentialType(
        kind="rest",
        label="HTTP endpoint",
        auth="token",
        fields=(
            CredField("access_token", "Token or API key", kind="password",
                      required=False,
                      help="Sent as a bearer token. Leave empty for an open "
                           "endpoint."),
            CredField("test_url", "Test URL", kind="url", required=False,
                      help="A GET that should return 2xx with this "
                           "credential, so the connection can be proved."),
        ),
        headers={"Authorization": "Bearer " + CRED % "access_token"},
        setup_note="For any API without a built-in connector.",
    ),
)

_BY_KIND = {c.kind: c for c in CREDENTIAL_TYPES}


def get_type(kind: str) -> CredentialType | None:
    return _BY_KIND.get(kind)


def all_types() -> list[CredentialType]:
    return list(CREDENTIAL_TYPES)


def apply_auth(kind: str, values: dict[str, Any]) -> dict[str, str]:
    """The headers this credential contributes to a request.

    The declarative half of n8n's `authenticate`. A header whose template
    resolves to nothing is dropped rather than sent empty — an
    `Authorization: Bearer ` header is worse than no header, because the
    provider's error then describes a malformed token rather than a missing
    one.
    """
    spec = get_type(kind)
    if spec is None:
        return {}
    out: dict[str, str] = {}
    for name, template in spec.headers.items():
        resolved = template
        for f in spec.fields:
            token = CRED % f.name
            if token in resolved:
                resolved = resolved.replace(token, str(values.get(f.name, "") or ""))
        if resolved.strip() and not resolved.rstrip().endswith("Bearer"):
            out[name] = resolved
    return out
