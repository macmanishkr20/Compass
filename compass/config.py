"""Central configuration.

Mirrors Claude Code's settings cascade in miniature: environment variables
(deployment concern) override the project file `.compass/settings.json`
(checked-in policy), which overrides built-in defaults.
"""

from __future__ import annotations

import json
import logging
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
    # TTS may live in a different Azure resource/region with its own endpoint
    # and key. When these are blank, TTS reuses the main endpoint/key/version.
    tts_endpoint: str = ""
    tts_api_key: str = ""
    tts_api_version: str = ""
    # Voice for TTS: alloy, ash, ballad, coral, echo, fable, nova, onyx, sage,
    # shimmer. "coral" and "sage" are the warm, expressive ones.
    tts_voice: str = "coral"
    # Realtime (speech-to-speech) — powers the Home "voice mode" via the Azure
    # OpenAI Realtime API/WebRTC. A SEPARATE deployment from the chat model
    # (e.g. gpt-4o-realtime-preview). Empty = voice mode unavailable.
    realtime_deployment: str = ""
    realtime_voice: str = "alloy"  # alloy|ash|ballad|coral|echo|sage|shimmer|verse|marin

    @property
    def realtime_configured(self) -> bool:
        return bool(self.endpoint and self.api_key and self.realtime_deployment)

    @property
    def tts_endpoint_effective(self) -> str:
        return self.tts_endpoint or self.endpoint

    @property
    def tts_api_key_effective(self) -> str:
        return self.tts_api_key or self.api_key

    @property
    def tts_api_version_effective(self) -> str:
        return self.tts_api_version or self.api_version

    @property
    def model_options(self) -> list[str]:
        opts = list(self.deployments) if self.deployments else []
        if self.deployment and self.deployment not in opts:
            opts.insert(0, self.deployment)
        return opts or [self.deployment]


class AiSearchSettings(BaseModel):
    """Azure AI Search — powers the Home-only "Work IQ" hybrid retrieval.
    Entirely optional; when unset, Work IQ reports itself as not configured and
    the Home chat behaves exactly as it does without it."""

    endpoint: str = ""  # https://<name>.search.windows.net
    api_key: str = ""
    index: str = ""
    api_version: str = "2024-07-01"
    vector_field: str = "contentVector"  # embedding field to search
    content_fields: str = "content"  # comma-sep fields returned as context
    title_field: str = "title"  # optional field used to label sources
    url_field: str = ""  # optional field with a source URL
    top_k: int = 5
    semantic_config: str = ""  # optional — enables semantic reranking when set
    embed_deployment: str = ""  # Azure OpenAI embeddings deployment for vectors

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.api_key and self.index)


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
    # Home/Chat threads live in their own container so they stay isolated from
    # the agent transcripts (same isolation the local JSONL layout gives).
    cosmos_chat_container: str = "chat"
    # Blob storage for large artifacts (tool-result spills). Empty = local disk.
    blob_connection_string: str = ""
    blob_container: str = "compass-artifacts"

    @property
    def cosmos_configured(self) -> bool:
        return bool(self.cosmos_endpoint and self.cosmos_key)

    @property
    def blob_configured(self) -> bool:
        return bool(self.blob_connection_string)


class RedisSettings(BaseModel):
    """Azure Cache for Redis — cross-instance state/cache (session cache,
    distributed locks, ephemeral caches). Empty URL = in-process only."""

    url: str = ""  # rediss://:<key>@<name>.redis.cache.windows.net:6380/0

    @property
    def configured(self) -> bool:
        return bool(self.url)


class ServiceBusSettings(BaseModel):
    """Azure Service Bus — async/queued jobs (Routines, long agent runs).
    Empty = run inline (current behaviour)."""

    connection_string: str = ""
    queue_name: str = "compass-jobs"

    @property
    def configured(self) -> bool:
        return bool(self.connection_string)


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
    # The token is also set as an httpOnly cookie (no browser localStorage).
    # Behind TLS (Azure Front Door / Container Apps) set COMPASS_AUTH_COOKIE_SECURE=1.
    cookie_name: str = "compass_token"
    cookie_secure: bool = False
    cookie_samesite: str = "lax"  # lax | strict | none


class ContextSettings(BaseModel):
    context_window_tokens: int = 128_000
    # Headroom for reasoning models (gpt-5, o-series): reasoning tokens count
    # against this budget, so keep it generous or answers can be truncated.
    # Large single-shot artifacts (a full interactive HTML login page is ~400+
    # lines) plus reasoning easily blow past 8k, which truncated the artifact
    # and split it across two turns — so give a generous ceiling. Overridable
    # via COMPASS_MAX_OUTPUT_TOKENS.
    max_output_tokens: int = 32_768
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
    ai_search: AiSearchSettings = Field(default_factory=AiSearchSettings)
    github: GitHubSettings = Field(default_factory=GitHubSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    service_bus: ServiceBusSettings = Field(default_factory=ServiceBusSettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    # Azure Key Vault URL; when set, secrets are pulled into the environment at
    # startup (Container Apps can also map them to env vars natively).
    key_vault_url: str = ""
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


def _load_key_vault() -> None:
    """When AZURE_KEY_VAULT_URL is set, pull every secret into os.environ before
    settings are read — so the same code runs locally from .env and in Azure
    from Key Vault. Key Vault names use '-'; they map to '_' env vars (e.g.
    'AZURE-OPENAI-API-KEY' -> 'AZURE_OPENAI_API_KEY'). Real env vars still win.
    Best-effort: a missing SDK, bad URL, or denied access never blocks startup.
    """
    url = os.environ.get("AZURE_KEY_VAULT_URL", "").strip()
    if not url:
        return
    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient

        client = SecretClient(vault_url=url, credential=DefaultAzureCredential())
        for prop in client.list_properties_of_secrets():
            name = (prop.name or "").replace("-", "_")
            if name and name not in os.environ:
                os.environ[name] = client.get_secret(prop.name).value or ""
    except Exception as err:  # noqa: BLE001 — never let secret loading crash boot
        logging.getLogger("compass.config").warning(
            "Key Vault load skipped (%s): %s", url, err
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_dotenv()
    _load_key_vault()
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
    azure.tts_endpoint = os.environ.get("AZURE_OPENAI_TTS_ENDPOINT", azure.tts_endpoint)
    azure.tts_api_key = os.environ.get("AZURE_OPENAI_TTS_API_KEY", azure.tts_api_key)
    azure.tts_api_version = os.environ.get(
        "AZURE_OPENAI_TTS_API_VERSION", azure.tts_api_version
    )
    azure.tts_voice = os.environ.get("COMPASS_TTS_VOICE", azure.tts_voice)
    azure.realtime_deployment = os.environ.get(
        "AZURE_OPENAI_REALTIME_DEPLOYMENT", azure.realtime_deployment
    )
    azure.realtime_voice = os.environ.get(
        "AZURE_OPENAI_REALTIME_VOICE", azure.realtime_voice
    )

    if mot := os.environ.get("COMPASS_MAX_OUTPUT_TOKENS"):
        try:
            settings.context.max_output_tokens = int(mot)
        except ValueError:
            pass

    ais = settings.ai_search
    ais.endpoint = os.environ.get("AZURE_AISEARCH_ENDPOINT", ais.endpoint)
    ais.api_key = os.environ.get("AZURE_AISEARCH_API_KEY", ais.api_key)
    ais.index = os.environ.get("AZURE_AISEARCH_INDEX", ais.index)
    ais.api_version = os.environ.get("AZURE_AISEARCH_API_VERSION", ais.api_version)
    ais.vector_field = os.environ.get("AZURE_AISEARCH_VECTOR_FIELD", ais.vector_field)
    ais.content_fields = os.environ.get("AZURE_AISEARCH_CONTENT_FIELDS", ais.content_fields)
    ais.title_field = os.environ.get("AZURE_AISEARCH_TITLE_FIELD", ais.title_field)
    ais.url_field = os.environ.get("AZURE_AISEARCH_URL_FIELD", ais.url_field)
    ais.semantic_config = os.environ.get("AZURE_AISEARCH_SEMANTIC_CONFIG", ais.semantic_config)
    ais.embed_deployment = os.environ.get("AZURE_AISEARCH_EMBED_DEPLOYMENT", ais.embed_deployment)
    if tk := os.environ.get("AZURE_AISEARCH_TOP_K"):
        try:
            ais.top_k = int(tk)
        except ValueError:
            pass

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
    storage.cosmos_chat_container = os.environ.get(
        "AZURE_COSMOS_CHAT_CONTAINER", storage.cosmos_chat_container
    )
    # Convenience: setting Cosmos credentials implies the cosmos backend
    # unless the backend was pinned explicitly.
    if storage.cosmos_configured and "COMPASS_STORAGE_BACKEND" not in os.environ:
        storage.backend = "cosmos"

    settings.redis.url = os.environ.get("AZURE_REDIS_URL", settings.redis.url)
    settings.service_bus.connection_string = os.environ.get(
        "AZURE_SERVICE_BUS_CONNECTION_STRING", settings.service_bus.connection_string
    )
    settings.service_bus.queue_name = os.environ.get(
        "AZURE_SERVICE_BUS_QUEUE", settings.service_bus.queue_name
    )
    settings.key_vault_url = os.environ.get("AZURE_KEY_VAULT_URL", settings.key_vault_url)

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
    auth.cookie_name = os.environ.get("COMPASS_AUTH_COOKIE_NAME", auth.cookie_name)
    if os.environ.get("COMPASS_AUTH_COOKIE_SECURE", "").lower() in ("1", "true", "yes"):
        auth.cookie_secure = True
    auth.cookie_samesite = os.environ.get(
        "COMPASS_AUTH_COOKIE_SAMESITE", auth.cookie_samesite
    ).lower()

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
