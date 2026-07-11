"""Central configuration.

Mirrors Claude Code's settings cascade in miniature: environment variables
(deployment concern) override the project file `.compass/settings.json`
(checked-in policy), which overrides built-in defaults.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field


class AzureOpenAISettings(BaseModel):
    endpoint: str = ""
    api_key: str = ""
    api_version: str = "2024-10-21"
    deployment: str = "gpt-4o"
    # Selectable deployments shown in the model picker. Defaults to just the
    # primary deployment; set AZURE_OPENAI_DEPLOYMENTS="gpt-5,gpt-4o,o3-mini".
    deployments: list[str] = []
    # Capacity/availability fallback, same role as Claude Code's fallbackModel.
    fallback_deployment: str | None = None
    # Small/cheap deployment used for compaction summaries (queryHaiku analog).
    utility_deployment: str | None = None
    # Text-to-speech deployment (e.g. gpt-4o-mini-tts). Empty = read-aloud
    # falls back to the browser's built-in voice.
    tts_deployment: str = ""
    # Voice for TTS: alloy, ash, ballad, coral, echo, fable, nova, onyx, sage,
    # shimmer. "coral" and "sage" are the warm, expressive ones.
    tts_voice: str = "coral"

    @property
    def model_options(self) -> list[str]:
        opts = list(self.deployments) if self.deployments else []
        if self.deployment and self.deployment not in opts:
            opts.insert(0, self.deployment)
        return opts or [self.deployment]


class GitHubSettings(BaseModel):
    # Personal access token with `repo` scope. Enables repo listing, clone,
    # and push. Empty = GitHub features disabled (local folders still work).
    token: str = ""
    api_url: str = "https://api.github.com"

    @property
    def enabled(self) -> bool:
        return bool(self.token)


class StorageSettings(BaseModel):
    # "local" = JSONL files under data/ (zero-config default)
    # "cosmos" = Azure Cosmos DB (NoSQL API), serverless-friendly
    backend: str = "local"
    cosmos_endpoint: str = ""
    cosmos_key: str = ""
    cosmos_database: str = "compass"
    cosmos_container: str = "transcripts"
    # Blob storage for large artifacts (tool-result spills). Empty = local disk.
    blob_connection_string: str = ""
    blob_container: str = "compass-artifacts"

    @property
    def cosmos_configured(self) -> bool:
        return bool(self.cosmos_endpoint and self.cosmos_key)

    @property
    def blob_configured(self) -> bool:
        return bool(self.blob_connection_string)


class TelemetrySettings(BaseModel):
    # Azure Application Insights connection string. Empty = telemetry off.
    connection_string: str = ""
    role_name: str = "compass"

    @property
    def enabled(self) -> bool:
        return bool(self.connection_string)


class AuthSettings(BaseModel):
    """Session-token auth for the API surface.

    Local mode: users come from COMPASS_AUTH_USERS ("alice:pw1,bob:pw2").
    Tokens are HMAC-signed with COMPASS_AUTH_SECRET; if no secret is set a
    per-process random one is used (sessions end on restart — fine for dev,
    set a stable secret in production). COMPASS_AUTH_ENABLED=0 disables the
    gate entirely (every request runs as "guest")."""

    enabled: bool = True
    users: dict[str, str] = {"admin": "compass"}  # demo default; see .env
    secret: str = ""
    token_ttl_hours: float = 12.0


class ContextSettings(BaseModel):
    context_window_tokens: int = 128_000
    # Headroom for reasoning models (gpt-5, o-series): reasoning tokens count
    # against this budget, so keep it generous or answers can be truncated.
    max_output_tokens: int = 8_192
    # autocompact fires when estimated prompt tokens exceed this ratio.
    autocompact_threshold: float = 0.80
    # microcompact: tool results older than the last N are stubbed out.
    microcompact_keep_recent: int = 5
    microcompact_min_chars: int = 2_000
    # per-result budget: single tool result larger than this is truncated and
    # the full content is spilled to disk (toolResultStorage analog).
    tool_result_max_chars: int = 30_000


class LoopSettings(BaseModel):
    max_turns: int = 50
    max_output_tokens_recovery_limit: int = 3  # same constant as query.ts
    max_subagent_depth: int = 2
    max_tool_concurrency: int = 10  # CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY
    permission_timeout_seconds: float = 300.0
    tool_timeout_seconds: float = 300.0


class PermissionRule(BaseModel):
    """One allow/ask/deny rule, e.g. {"tool": "bash", "pattern": "git *", "action": "allow"}."""

    tool: str
    pattern: str = "*"
    action: str  # "allow" | "ask" | "deny"


class Settings(BaseModel):
    azure: AzureOpenAISettings = Field(default_factory=AzureOpenAISettings)
    github: GitHubSettings = Field(default_factory=GitHubSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    loop: LoopSettings = Field(default_factory=LoopSettings)
    permission_mode: str = "default"  # default | accept_edits | plan | bypass
    permission_rules: list[PermissionRule] = Field(default_factory=list)
    workspace_root: Path = Field(default_factory=Path.cwd)
    data_dir: Path = Path("data")
    mock_model: bool = False  # run without Azure credentials (tests/demos)
    # Mock scenario: "read_only" (echo; auto-allowed) or "permission" (issues
    # a mutating command so the Allow/Deny permission flow is demoable).
    mock_scenario: str = "read_only"
    # MCP servers: path to a config file (relative to workspace) and/or an
    # inline JSON object in COMPASS_MCP_SERVERS. Inline wins on name clashes.
    mcp_config_path: str = ".compass/mcp.json"
    mcp_servers_inline: str = ""  # JSON: {"name": {"type": "stdio", ...}}

    @property
    def sessions_dir(self) -> Path:
        return self.workspace_root / self.data_dir / "sessions"

    @property
    def tool_results_dir(self) -> Path:
        return self.workspace_root / self.data_dir / "tool_results"

    @property
    def workspaces_dir(self) -> Path:
        """Base directory for app-managed workspaces (cloned repos, new
        folders). The Compass repo itself is the built-in 'default' workspace."""
        return self.workspace_root / self.data_dir / "workspaces"


def _load_project_file(root: Path) -> dict:
    path = root / ".compass" / "settings.json"
    if path.is_file():
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def _load_dotenv() -> None:
    """Load a `.env` file into os.environ before settings are read, so
    `AZURE_OPENAI_*` and friends work no matter how the server is launched
    (no need to `source .env`). Real shell variables always win over the file.
    Looks in COMPASS_WORKSPACE then the current directory. A no-op if
    python-dotenv isn't installed or no .env exists."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    candidates = []
    if ws := os.environ.get("COMPASS_WORKSPACE"):
        candidates.append(Path(ws) / ".env")
    candidates.append(Path.cwd() / ".env")
    for path in candidates:
        if path.is_file():
            load_dotenv(path, override=False)
            return


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_dotenv()
    root = Path(os.environ.get("COMPASS_WORKSPACE", os.getcwd())).resolve()
    data = _load_project_file(root)
    settings = Settings.model_validate(data) if data else Settings()
    settings.workspace_root = root

    azure = settings.azure
    azure.endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", azure.endpoint)
    azure.api_key = os.environ.get("AZURE_OPENAI_API_KEY", azure.api_key)
    azure.api_version = os.environ.get("AZURE_OPENAI_API_VERSION", azure.api_version)
    azure.deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", azure.deployment)
    azure.fallback_deployment = os.environ.get(
        "AZURE_OPENAI_FALLBACK_DEPLOYMENT", azure.fallback_deployment
    )
    azure.utility_deployment = os.environ.get(
        "AZURE_OPENAI_UTILITY_DEPLOYMENT", azure.utility_deployment
    )
    if deps := os.environ.get("AZURE_OPENAI_DEPLOYMENTS"):
        azure.deployments = [d.strip() for d in deps.split(",") if d.strip()]
    azure.tts_deployment = os.environ.get(
        "AZURE_OPENAI_TTS_DEPLOYMENT", azure.tts_deployment
    )
    azure.tts_voice = os.environ.get("COMPASS_TTS_VOICE", azure.tts_voice)

    settings.github.token = os.environ.get("GITHUB_TOKEN", settings.github.token)
    settings.github.api_url = os.environ.get(
        "GITHUB_API_URL", settings.github.api_url
    )

    storage = settings.storage
    storage.backend = os.environ.get("COMPASS_STORAGE_BACKEND", storage.backend).lower()
    storage.cosmos_endpoint = os.environ.get("AZURE_COSMOS_ENDPOINT", storage.cosmos_endpoint)
    storage.cosmos_key = os.environ.get("AZURE_COSMOS_KEY", storage.cosmos_key)
    storage.cosmos_database = os.environ.get("AZURE_COSMOS_DATABASE", storage.cosmos_database)
    storage.cosmos_container = os.environ.get("AZURE_COSMOS_CONTAINER", storage.cosmos_container)
    storage.blob_connection_string = os.environ.get(
        "AZURE_STORAGE_CONNECTION_STRING", storage.blob_connection_string
    )
    storage.blob_container = os.environ.get("AZURE_STORAGE_CONTAINER", storage.blob_container)
    # Convenience: setting Cosmos credentials implies the cosmos backend
    # unless the backend was pinned explicitly.
    if storage.cosmos_configured and "COMPASS_STORAGE_BACKEND" not in os.environ:
        storage.backend = "cosmos"

    settings.telemetry.connection_string = os.environ.get(
        "APPLICATIONINSIGHTS_CONNECTION_STRING", settings.telemetry.connection_string
    )
    settings.telemetry.role_name = os.environ.get(
        "COMPASS_TELEMETRY_ROLE", settings.telemetry.role_name
    )

    auth = settings.auth
    if os.environ.get("COMPASS_AUTH_ENABLED", "").lower() in ("0", "false", "no"):
        auth.enabled = False
    if users_raw := os.environ.get("COMPASS_AUTH_USERS"):
        parsed: dict[str, str] = {}
        for pair in users_raw.split(","):
            name, _, password = pair.strip().partition(":")
            if name and password:
                parsed[name] = password
        if parsed:
            auth.users = parsed
    auth.secret = os.environ.get("COMPASS_AUTH_SECRET", auth.secret)
    if ttl := os.environ.get("COMPASS_AUTH_TOKEN_TTL_HOURS"):
        try:
            auth.token_ttl_hours = float(ttl)
        except ValueError:
            pass

    settings.mcp_config_path = os.environ.get(
        "COMPASS_MCP_CONFIG", settings.mcp_config_path
    )
    settings.mcp_servers_inline = os.environ.get(
        "COMPASS_MCP_SERVERS", settings.mcp_servers_inline
    )

    if os.environ.get("COMPASS_MOCK_MODEL", "").lower() in ("1", "true", "yes"):
        settings.mock_model = True
    settings.mock_scenario = os.environ.get(
        "COMPASS_MOCK_SCENARIO", settings.mock_scenario
    ).lower()
    if mode := os.environ.get("COMPASS_PERMISSION_MODE"):
        settings.permission_mode = mode

    settings.sessions_dir.mkdir(parents=True, exist_ok=True)
    settings.tool_results_dir.mkdir(parents=True, exist_ok=True)
    settings.workspaces_dir.mkdir(parents=True, exist_ok=True)
    return settings
