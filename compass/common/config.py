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


def _resource_root(url: str) -> str:
    """An Azure resource base URL, from a base URL or a connection string."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if raw.startswith("wss://"):
        raw = "https://" + raw[len("wss://"):]
    elif raw.startswith("ws://"):
        raw = "http://" + raw[len("ws://"):]
    raw = raw.split("?", 1)[0].split("#", 1)[0]
    marker = raw.find("/openai")
    if marker > 0:
        raw = raw[:marker]
    return raw.rstrip("/")


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
    # Image generation (e.g. gpt-image-2). Empty = Compass cannot draw, and
    # every path that would have asked for a picture says so instead of
    # failing: the `generate_image` tool is not offered to a model that has
    # no way to run it, and a design that would have carried generated
    # artwork is written without it.
    #
    # Its own endpoint and key for the same reason TTS has them: an image
    # model is deployed where the region offers it, which is often not where
    # the chat deployment lives. Blank falls back to the main resource, which
    # is what an install with everything in one place wants.
    image_deployment: str = ""
    image_endpoint: str = ""
    image_api_key: str = ""
    # The images API is preview-versioned separately from chat; the chat
    # version here (2024-10-21) does not serve it.
    image_api_version: str = "2025-04-01-preview"
    # What a generated picture costs in time and bytes. "low" is the draft
    # quality and is what a placeholder inside a mockup wants; a flier's
    # single hero image is the one worth paying for.
    image_quality: str = "high"
    image_size: str = "1024x1024"
    # Speech-to-text deployment (e.g. gpt-4o-transcribe or whisper). Empty =
    # an attached audio file is named in the turn rather than transcribed,
    # because a model cannot listen to an upload. Shares the TTS resource's
    # endpoint and key: both are audio models and usually live together.
    transcribe_deployment: str = ""
    # Realtime (speech-to-speech) — powers the Home "voice mode" via the Azure
    # OpenAI Realtime API/WebRTC. A SEPARATE deployment from the chat model
    # (e.g. gpt-realtime). Empty = voice mode unavailable.
    #
    # Lives on the audio resource, like TTS and transcription, and for the
    # same reason: realtime is an audio model, and audio models are deployed
    # together in a region that offers them. This was pointed at the main
    # endpoint, which is where a chat deployment lives — so a perfectly good
    # `gpt-realtime` sitting on the audio resource was asked for at the
    # address of the resource that does not have it, and Azure answered
    # "the realtime operation does not work with the specified model", which
    # reads as the model being wrong rather than the endpoint.
    realtime_deployment: str = ""
    # Its own endpoint and key, when realtime lives somewhere of its own.
    # Blank falls back to the TTS resource, then to the main one — which is
    # what an install with everything in one place wants, and what an install
    # with one audio resource wants too. Read explicitly because a setting
    # that appears in .env and is quietly ignored is worse than one that is
    # missing: the file says the endpoint is configured, and nothing disagrees
    # until the requests go somewhere else.
    realtime_endpoint: str = ""
    realtime_api_key: str = ""
    realtime_voice: str = "alloy"  # alloy|ash|ballad|coral|echo|sage|shimmer|verse|marin
    # The language spoken in voice mode, as an ISO-639-1 code.
    #
    # Pinned rather than detected, because detection is what goes wrong.
    # Whisper guesses the language from the audio, and on the short, quiet
    # fragments a conversation opens with it guesses badly — an English
    # sentence comes back as German, the model answers the German it was
    # handed, and the session stays in a language nobody chose. Telling it
    # what it is about to hear costs nothing and removes the whole failure.
    #
    # Blank restores auto-detection for anyone who genuinely switches
    # languages mid-conversation and would rather have the guess.
    realtime_language: str = "en"

    @property
    def realtime_configured(self) -> bool:
        return bool(
            self.realtime_deployment
            and self.realtime_endpoint_effective
            and self.realtime_api_key_effective
        )

    @property
    def realtime_endpoint_effective(self) -> str:
        """The resource base URL to reach realtime at.

        Normalised, because the obvious thing to paste here is the wrong
        shape. Azure's portal shows realtime as a ready-made connection
        string — `wss://<res>.services.ai.azure.com/openai/v1/realtime?model=
        gpt-realtime` — and that is a socket address, complete with scheme,
        path and query, while everything else in this file is a resource
        root. Joined naively it produces `wss://…/openai/v1/realtime?model=
        gpt-realtime/openai/v1/realtime/client_secrets`, which is not a URL
        anybody meant, and Azure answers it with a 404 or an Unauthorized
        that says nothing about the address being wrong.

        So either form is accepted and reduced to the root: the socket scheme
        becomes https, and anything from `/openai` onwards is dropped.
        """
        return _resource_root(
            self.realtime_endpoint or self.tts_endpoint or self.endpoint)

    @property
    def realtime_api_key_effective(self) -> str:
        return self.realtime_api_key or self.tts_api_key or self.api_key

    @property
    def transcribe_configured(self) -> bool:
        return bool(
            self.transcribe_deployment
            and self.tts_endpoint_effective
            and self.tts_api_key_effective
        )

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
    def image_configured(self) -> bool:
        """Whether Compass can draw at all.

        Asked before the tool is offered and before a design is told it may
        ask for artwork, so that an install without an image deployment never
        promises a picture it cannot produce.
        """
        return bool(
            self.image_deployment
            and self.image_endpoint_effective
            and self.image_api_key_effective
        )

    @property
    def image_endpoint_effective(self) -> str:
        """Where the image model lives.

        Its own setting first, then the audio resource, then the main one —
        the same chain realtime walks, and for the same reason. Image models
        are regional and get deployed next to the other models the region
        offers: on this install `gpt-image-2` sits beside `gpt-realtime` and
        `gpt-4o-mini-tts`, and nothing is deployed for it on the resource the
        chat model uses.

        Falling back to the main endpoint instead — which is what this did
        first — asks a resource that has no image deployment, and Azure
        answers DeploymentNotFound, which reads as the model being wrong
        rather than the address.

        Normalised, because the realtime setting is a socket connection
        string — `wss://<res>.../openai/v1/realtime?model=gpt-realtime` —
        and what is wanted here is the resource root it points at.
        """
        return _resource_root(
            self.image_endpoint
            or self.realtime_endpoint
            or self.tts_endpoint
            or self.endpoint
        )

    @property
    def image_api_key_effective(self) -> str:
        return (
            self.image_api_key
            or self.realtime_api_key
            or self.tts_api_key
            or self.api_key
        )

    @property
    def model_options(self) -> list[str]:
        opts = list(self.deployments) if self.deployments else []
        if self.deployment and self.deployment not in opts:
            opts.insert(0, self.deployment)
        return opts or [self.deployment]


#: Every level any deployment is known to take, weakest first. This is the
#: union and not an offer: it is what a *stored* setting may legally say,
#: because one setting outlives the deployment it was chosen against and may
#: be used against several at once. What a given call may actually send is
#: narrower, and `normalize_effort` is the one place that decides it.
EFFORT_LEVELS: tuple[str, ...] = (
    "none", "minimal", "low", "medium", "high", "xhigh", "max",
)

#: What each family accepts, weakest first, keyed by the prefix its
#: deployments are named with.
#:
#: The ladder was a single constant until a second family was deployed, on the
#: reasoning that the accepted set is a property of the deployed snapshot. It
#: is — but it turned out not to be a *growing* property, and the constant had
#: nowhere to put that. gpt-5-2025-08-07 refuses `xhigh`, `max` and `none`
#: with "Supported values are: 'minimal', 'low', 'medium', and 'high'";
#: gpt-6-sol-2026-09-22 and gpt-6-astra-2026-09-03 refuse `minimal` with
#: "Supported values are: 'none', 'low', 'medium', 'high', 'xhigh', and
#: 'max'". The ladders overlap and neither contains the other, so no single
#: list can be right for both, and the one that was there advertised
#: `minimal` to a deployment that rejects it — a 400 the user chose from a
#: menu, which is the thing this is all meant to avoid.
#:
#: The two floors are named differently and are not the same rung: gpt-5's
#: `minimal` barely thinks, gpt-6's `none` does not think at all. Each family
#: was asked directly and answers the exact reverse of the other — gpt-5
#: takes `minimal` and refuses `none`, gpt-6 takes `none` and refuses
#: `minimal`.
#:
#: All of this was measured from what the API says when it refuses, including
#: `gpt-6-sol`, which shares this prefix but was asked rather than assumed to
#: match. Do the same before adding a family: `scripts/` has a probe for it.
EFFORT_LADDERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("gpt-6", ("none", "low", "medium", "high", "xhigh", "max")),
    ("gpt-5", ("minimal", "low", "medium", "high")),
)

#: For a deployment belonging to no family measured above. The three levels
#: every known ladder contains: an unmeasured deployment is the one case where
#: offering a level nobody has confirmed is exactly how the 400 gets back in.
DEFAULT_EFFORT_LADDER: tuple[str, ...] = ("low", "medium", "high")


def effort_levels_for(deployment: str) -> tuple[str, ...]:
    """The levels this deployment accepts — what a picker may offer."""
    name = (deployment or "").lower()
    for prefix, ladder in EFFORT_LADDERS:
        if name.startswith(prefix):
            return ladder
    return DEFAULT_EFFORT_LADDER

#: What each level does, in the same terms the model-facing docs use. Shown in
#: the UI so the choice means something to whoever is making it.
EFFORT_BEHAVIOUR: dict[str, str] = {
    "none": "Does not think at all — the gpt-6 floor, one rung below minimal.",
    "minimal": "Barely thinks. Fastest and cheapest, for work that needs none.",
    "low": "Thinks as little as possible. Skips thinking on simple work, where speed matters most.",
    "medium": "Moderate thinking. May skip thinking for simple queries.",
    "high": "Almost always thinks. Deep reasoning on complex tasks.",
    "xhigh": "More than high, where the deployment offers it.",
    "max": "The most thinking the deployment will do.",
}


class ToolSettings(BaseModel):
    """How tool definitions are put to the model."""

    #: Constrain the model's sampling so a tool call cannot arrive with a
    #: missing field or the wrong type. Off by default, and deliberately: it
    #: reshapes every tool's schema into a stricter subset, which changes what
    #: the model sends, and Compass already answers a malformed call with an
    #: instructive error the model recovers from. Turn it on with
    #: COMPASS_STRICT_TOOLS=1 once you have measured it on your own work.
    strict_schemas: bool = False

    #: Search the web. Azure runs this one itself: Compass does not execute
    #: it, and the model gets cited results back inside the same turn. On by
    #: default because Compass otherwise has no way to look anything up at
    #: all, which is the difference between an answer and a guess on anything
    #: newer than the training data. It is billed per search and the query
    #: leaves the tenancy, so COMPASS_WEB_SEARCH=0 turns it off.
    web_search: bool = True

    #: Run Python in a sandbox Azure hosts. Off by default: it overlaps with
    #: bash, which Compass already has in the workspace and which costs
    #: nothing extra. Worth turning on for computation or data work that
    #: should not touch the workspace at all. COMPASS_CODE_INTERPRETER=1.
    code_interpreter: bool = False

    #: Above this many tools, the ones that arrive from MCP servers stop being
    #: described on every request and are found by searching instead. Compass's
    #: own twelve are always described. Chosen below the 30–50 range where
    #: choosing between descriptions starts to get worse, and above the point
    #: where a search round trip costs more than the descriptions would.
    #: 0 turns it off and sends everything, whatever the count.
    search_above: int = 24


class ThinkingSettings(BaseModel):
    """How much the model reasons before answering, and how much of that you see.

    Reasoning models decide per request whether deeper thinking will help, so
    effort is a posture rather than a token budget: it shifts how willing the
    model is to think and how far it goes, and a simple question can still come
    back with no thinking at all. That is expected, not a fault.

    Reasoning is billed as output and counts against the same output cap as the
    answer, so a cap sized for a reply with no thinking is too small once the
    model starts thinking.
    """

    #: Default posture for a new conversation. Held stable for the life of one,
    #: because the resolved effort is part of the cached prompt prefix and
    #: changing it starts the cache over.
    default_effort: str = "medium"

    #: Writing a whole prototype is the most demanding thing Compass asks of a
    #: model, and it already reserves 64k output tokens for it. It ran at the
    #: server's own default only because Compass sent no effort at all, which
    #: was never a decision. This makes it one.
    design_effort: str = "high"

    #: What the advisor tool thinks at. The top of the ladder on purpose: the
    #: whole point of a separate, explicitly-called tool is that it buys
    #: something the default turn does not have. Dropping this to the default
    #: effort would leave a tool that costs a round trip and returns what the
    #: caller could already do itself.
    advisor_effort: str = "high"

    #: "summarized" streams a readable summary of the reasoning as it is
    #: produced; "omitted" asks for none, which reaches the first word of the
    #: answer sooner. Billing is identical either way — only what you see
    #: changes.
    display: str = "summarized"

    #: The Responses API is what carries reasoning summaries and the encrypted
    #: reasoning that survives a tool call. Azure serves it only on
    #: 2025-03-01-preview and later, so it has its own version rather than
    #: forcing the whole deployment onto a newer one.
    responses_api_version: str = "2025-04-01-preview"

    #: Deployment name prefixes that reason. Anything else keeps the plain
    #: chat-completions path, which has no reasoning to ask for.
    reasoning_models: list[str] = ["gpt-5", "o1", "o3", "o4", "gpt-6"]

    #: A standing instruction about when thinking is worth it, added to every
    #: system prompt: "more" lowers the threshold, "less" raises it, "" leaves
    #: the model's own judgement alone. Reach for effort first — it is
    #: calibrated, where this is wording — and use this only when the effort
    #: levels do not land where a particular workload needs them.
    posture: str = ""

    #: The language every reply is written in, whatever language the question
    #: was asked in. Empty means the model decides from the conversation,
    #: which is the right default — naming a language nobody chose is worse
    #: than inferring one well. Set it when replies must be in one language
    #: regardless: COMPASS_RESPONSE_LANGUAGE="Hindi".
    response_language: str = ""

    #: Set false to keep every request on chat completions, giving up thinking
    #: text and encrypted reasoning. The escape hatch if a resource misbehaves.
    enabled: bool = True

    def reasons(self, deployment: str) -> bool:
        """Whether this deployment is one that thinks."""
        name = (deployment or "").lower()
        return self.enabled and any(name.startswith(p) for p in self.reasoning_models)

    def normalize_effort(self, effort: str | None, deployment: str = "") -> str | None:
        """Coerce a requested effort onto the ladder *this* deployment accepts.

        The single place a level is chosen, so it is the single place a level
        the API would refuse can be stopped. A stored setting outlives the
        deployment it was picked against — a session set to `minimal` on gpt-5
        and resumed against gpt-6-astra, or one set to `xhigh` before that was
        known to be refused — and every one of those is a 400 on a turn the
        person did nothing wrong to start.

        Off the ladder becomes the nearest rung on it rather than None, which
        would drop the field and hand the turn the deployment's own default:
        `xhigh` and `max` are Claude's names for the top and land on `high`
        where there is nothing above it, and `minimal` lands on `low` where
        there is nothing below. Both keep the intent — the most thinking
        available, or the least — which is what the level was chosen for.

        A level no deployment takes is still dropped: it means nothing, and
        guessing a rung for it would be inventing an intent nobody expressed.
        """
        if not effort:
            return None
        value = effort.strip().lower()
        if value not in EFFORT_LEVELS:
            return None
        ladder = effort_levels_for(deployment)
        if value in ladder:
            return value
        wanted = EFFORT_LEVELS.index(value)
        # Nearest rung, and on a tie the stronger one. The tie is real:
        # `minimal` sits exactly between gpt-6's `none` and `low`, so a
        # session set to minimal on gpt-5 and resumed on gpt-6 lands on one of
        # them by arithmetic alone. Rounding up is the deliberate half — the
        # two are not equivalent, `none` does not think at all, and quietly
        # taking a model's reasoning away is a worse surprise than quietly
        # giving it a little.
        return min(
            ladder,
            key=lambda level: (abs(EFFORT_LEVELS.index(level) - wanted),
                               -EFFORT_LEVELS.index(level)),
        )


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
    # Session metadata (ownership, titles). Named in full rather than derived
    # from the transcripts container: it used to be built as
    # f"{cosmos_container}_meta", which quietly assumed a naming convention
    # the account did not have to share. Pointed at a `transcripts-meta`
    # container, that built `transcripts_meta`, and
    # `create_container_if_not_exists` would have made a second, underscore
    # named one beside it and written metadata there — no error, just two
    # containers where the reader looks in the wrong one.
    cosmos_meta_container: str = "transcripts-meta"
    # Blob storage for large artifacts (tool-result spills). Empty = local disk.
    blob_connection_string: str = ""
    blob_container: str = "compass-artifacts"
    # Where a record of what was deleted goes, and for how long it is kept.
    # Deleting means deleting — the rows and the bytes go — so what is left
    # behind is a note that it happened: who, what, which conversation, when
    # and what went with it. Never the content itself.
    #
    # Cosmos expires these rows itself, from the per-item `ttl` this number
    # becomes, so nothing has to run a sweep. Months rather than days because
    # that is how retention is written down in the policies this answers to;
    # `COMPASS_AUDIT_RETENTION_MONTHS=0` keeps them forever.
    cosmos_audit_container: str = "audit"
    audit_retention_months: float = 3.0

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
    #: Login name -> the identity that owns records. A username is not a
    #: person: `admin` is the shipped demo credential and the person using it
    #: has a real address, and if those stay two identities then records owned
    #: by one are invisible to the other. Override with COMPASS_AUTH_ALIASES
    #: ("admin:someone@example.com,root:someone@example.com"). A name that is
    #: not listed is its own identity and owns nothing to begin with.
    identity_aliases: dict[str, str] = {"admin": "macmanishkr20@gmail.com"}
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
    #
    # Was 30_000 — about 7,500 tokens for one tool result, so four file reads
    # reached the compaction ceiling on their own. That ceiling is not the
    # model's window but the deployment's minute: on a 50,000 token/minute
    # quota a 28,000-token call is 1.8 calls per minute, and a mission of 132
    # turns is then an hour of waiting regardless of how fast the model is.
    # Nothing is lost by trimming — the full output is written to disk and the
    # stub names the file, so a model that needs the rest can go and read it.
    tool_result_max_chars: int = 8_000


class MissionSettings(BaseModel):
    """Long-running builds: plan, build one feature at a time, review, repeat.

    Off by default and opted into, like Estimate. A mission runs unattended
    for hours and spends money doing it — that is a decision a deployment
    makes deliberately, not one it discovers. `COMPASS_MISSIONS=1` turns it
    on; with it off no routes are mounted and nothing is imported.
    """

    enabled: bool = False

    #: How many missions may run at once.
    #:
    #: There was no limit: the scheduler started every mission whose slot had
    #: arrived, on the same tick. That reads like throughput and is not.
    #: Missions are token-hungry and the deployment has a tokens-per-minute
    #: ceiling, so three at once do not finish in a third of the time — they
    #: take turns being rate-limited, and all three take longer than running
    #: them one after another would have. Concurrency without a budget is
    #: contention.
    #:
    #: Two is a deliberate default rather than one: a mission spends a good
    #: part of its life waiting on a command or a file, and a second one uses
    #: that room without doubling the token rate. Raise it if the quota is
    #: generous; set it to 1 to go strictly one at a time.
    max_parallel: int = 2


class SandboxSettings(BaseModel):
    """OS-enforced limits on what a command can touch.

    On by default. That was a decision to test rather than assume, so the
    everyday toolchain was measured under it: git init and commit, a venv,
    `pip install`, `npm install` — all unaffected. The single casualty was
    `git config --global`, which is a write to the home directory and exactly
    the kind of thing an agent should not be doing behind your back.

    `COMPASS_SANDBOX=0` turns it off. Where no boundary exists — Windows —
    Compass warns once and runs without one, unless `COMPASS_SANDBOX_STRICT`
    says that is unacceptable.
    """

    enabled: bool = True
    #: Extra directories commands may write to, beyond the workspace and temp.
    allow_write: list[str] = []
    #: Extra paths whose contents may not be read, beyond the built-in list of
    #: key and credential locations (see sandbox.policy.DEFAULT_SECRET_PATHS).
    deny_read: list[str] = []
    #: Whether sandboxed commands may reach the network at all. Phase 0 is all
    #: or nothing — a per-domain allowlist needs a proxy in front of it.
    network: bool = True
    #: Refuse to run rather than run unsandboxed where no boundary exists
    #: (Windows, or Linux without bubblewrap). For deployments where the
    #: sandbox is a security gate rather than a convenience.
    fail_if_unavailable: bool = False


class LoopSettings(BaseModel):
    max_turns: int = 50
    max_output_tokens_recovery_limit: int = 3  # same constant as query.ts
    max_subagent_depth: int = 2
    max_tool_concurrency: int = 10  # CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY
    permission_timeout_seconds: float = 300.0
    tool_timeout_seconds: float = 300.0


class EstimateSettings(BaseModel):
    """The Estimate module — feasibility and cost for a proposed build.

    Off by default, which is the opposite of Pipelines and deliberate. The
    numbers this module produces are opinionated: rate cards, loaded hourly
    rates, model prices and scale tiers that were set for one organisation.
    They are defensible once someone has read them and agreed they describe
    *their* costs, and misleading before that — so a deployment opts in, rather
    than finding a costing section it never asked for quoting figures it has
    never seen. `COMPASS_ESTIMATE=1` turns it on; off, nothing is imported, no
    routes are mounted and no section appears.

    The two model-touching stages have their own switches because they fail in
    different directions. With `classifier` off, a use case left unlabelled
    falls to keyword rules — cheaper, no request, and occasionally wrong in a
    way a person can see and fix. With `architect` off, the delivery platform
    comes from the same keyword matching. Neither changes how anything is
    priced; both change what gets priced, which is exactly the influence a
    model is allowed to have here. An estimate is reproducible with them on or
    off, and stating that as two flags rather than one keeps it testable.

    `live_pricing` queries the public Azure Retail Prices API to refine unit
    prices, falling back to the catalog baseline on any failure. Off makes an
    estimate fully offline and pins it to the catalog — which is the right
    setting when you need two runs a month apart to be comparable.
    """

    enabled: bool = False
    classifier: bool = True
    architect: bool = True
    #: Drafting a brief from a paragraph. Its own switch because it is the one
    #: place a model writes *inputs* rather than picking a label, and a
    #: deployment may reasonably want the form filled by hand even where it is
    #: happy for a use case to be classified. Nothing is costed from a draft
    #: until a person has reviewed it, but "reviewed" is a habit, and a habit
    #: is a weaker guarantee than a switch.
    draft: bool = True
    live_pricing: bool = True


class PipelineSettings(BaseModel):
    """The Pipelines module, on by default and switchable off.

    The switch still matters even now that it defaults on. `COMPASS_PIPELINES=0`
    removes the module completely rather than hiding it: nothing is imported,
    no routes are mounted, and the section does not appear in the UI, leaving
    Compass byte-for-byte what it was before the package existed. That is a
    real escape hatch for a deployment that does not want it, and it is
    testable rather than decorative because the route table is compared
    against a recorded snapshot in both states.

    `secrets_backend` is separate from the module's own switch because the
    choice it makes is not reversible for free: a connection's credential is
    stored behind a reference, and moving the store later is a migration.
    """

    enabled: bool = True
    #: "local" keeps secrets in an encrypted file under the data directory;
    #: "keyvault" uses the vault named by `key_vault_url`. The indirection
    #: matters more than the choice — see compass/pipelines/secrets.py.
    secrets_backend: str = "local"
    #: A ceiling on one run's nodes, so a cycle or a runaway fan-out cannot
    #: spin forever. Fabric caps a pipeline at 120 activities; this counts
    #: executions instead, because fan-out multiplies them.
    max_node_runs: int = 500
    #: A pipeline may not be scheduled until it has succeeded once by hand.
    #: Cheap, and it catches the class of mistake that only shows up when
    #: nobody is watching.
    require_manual_first_run: bool = True


class BusinessFunctionSettings(BaseModel):
    """Business functions — Finance, Talent, and the features inside them.

    Off by default, for the reason Estimate is: what this mounts is not a
    general capability but a set of claims about how one firm runs itself —
    which features Finance offers, who may approve leave, when a period is
    closed. A deployment opts in once somebody has read the catalog and agrees
    it describes them. `COMPASS_BUSINESS_FUNCTIONS=1` turns it on; off,
    nothing is imported, no routes are mounted and no section appears.

    `strict_catalog` decides what a broken manifest means. Off, which is the
    default, a manifest that fails validation is logged and skipped and the
    rest still loads — a typo in Talent must not take Finance down with it.
    On, the server refuses to start, which is what a deployment pipeline
    wants: a silently-missing function becomes a failed deploy rather than a
    support ticket three weeks later.
    """

    enabled: bool = False
    strict_catalog: bool = False


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
    thinking: ThinkingSettings = Field(default_factory=ThinkingSettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)
    pipelines: PipelineSettings = Field(default_factory=PipelineSettings)
    estimate: EstimateSettings = Field(default_factory=EstimateSettings)
    business_functions: BusinessFunctionSettings = Field(
        default_factory=BusinessFunctionSettings
    )
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    missions: MissionSettings = Field(default_factory=MissionSettings)
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
        logging.getLogger("compass.common.config").warning(
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
    azure.image_deployment = os.environ.get(
        "AZURE_OPENAI_IMAGE_DEPLOYMENT", azure.image_deployment
    )
    azure.image_endpoint = os.environ.get(
        "AZURE_OPENAI_IMAGE_ENDPOINT", azure.image_endpoint
    )
    azure.image_api_key = os.environ.get(
        "AZURE_OPENAI_IMAGE_API_KEY", azure.image_api_key
    )
    azure.image_api_version = os.environ.get(
        "AZURE_OPENAI_IMAGE_API_VERSION", azure.image_api_version
    )
    azure.image_quality = os.environ.get("COMPASS_IMAGE_QUALITY", azure.image_quality)
    azure.image_size = os.environ.get("COMPASS_IMAGE_SIZE", azure.image_size)
    azure.realtime_deployment = os.environ.get(
        "AZURE_OPENAI_REALTIME_DEPLOYMENT", azure.realtime_deployment
    )
    azure.realtime_endpoint = os.environ.get(
        "AZURE_OPENAI_REALTIME_ENDPOINT", azure.realtime_endpoint
    )
    azure.realtime_api_key = os.environ.get(
        "AZURE_OPENAI_REALTIME_API_KEY", azure.realtime_api_key
    )
    azure.realtime_language = os.environ.get(
        "COMPASS_VOICE_LANGUAGE", azure.realtime_language
    ).strip().lower()
    azure.realtime_voice = os.environ.get(
        "AZURE_OPENAI_REALTIME_VOICE", azure.realtime_voice
    )

    think = settings.thinking
    think.default_effort = os.environ.get(
        "COMPASS_THINKING_EFFORT", think.default_effort
    ).lower()
    think.display = os.environ.get("COMPASS_THINKING_DISPLAY", think.display).lower()
    think.design_effort = os.environ.get(
        "COMPASS_DESIGN_EFFORT", think.design_effort
    )
    think.advisor_effort = os.environ.get(
        "COMPASS_ADVISOR_EFFORT", think.advisor_effort
    ).lower()
    think.posture = os.environ.get("COMPASS_THINKING_POSTURE", think.posture).lower()
    think.response_language = os.environ.get(
        "COMPASS_RESPONSE_LANGUAGE", think.response_language
    ).strip()
    for flag, field_name in (("COMPASS_WEB_SEARCH", "web_search"),
                             ("COMPASS_CODE_INTERPRETER", "code_interpreter")):
        if (raw := os.environ.get(flag)) is not None:
            setattr(settings.tools, field_name,
                    raw.strip().lower() not in ("0", "false", "no", "off"))
    if raw := os.environ.get("COMPASS_TOOL_SEARCH_ABOVE"):
        try:
            settings.tools.search_above = max(0, int(raw))
        except ValueError:
            pass
    if (flag := os.environ.get("COMPASS_STRICT_TOOLS")) is not None:
        settings.tools.strict_schemas = flag.strip().lower() not in (
            "0", "false", "no", "off",
        )
    think.responses_api_version = os.environ.get(
        "AZURE_OPENAI_RESPONSES_API_VERSION", think.responses_api_version
    )
    if (flag := os.environ.get("COMPASS_THINKING_ENABLED")) is not None:
        think.enabled = flag.strip().lower() not in ("0", "false", "no", "off")
    if models := os.environ.get("COMPASS_REASONING_MODELS"):
        think.reasoning_models = [m.strip().lower() for m in models.split(",") if m.strip()]
    if think.default_effort not in EFFORT_LEVELS:
        think.default_effort = ThinkingSettings().default_effort
    if think.design_effort not in EFFORT_LEVELS:
        think.design_effort = ThinkingSettings().design_effort

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
    storage.cosmos_audit_container = os.environ.get(
        "COMPASS_AUDIT_CONTAINER", storage.cosmos_audit_container
    )
    if months := os.environ.get("COMPASS_AUDIT_RETENTION_MONTHS"):
        try:
            storage.audit_retention_months = max(0.0, float(months))
        except ValueError:
            logger.warning(
                "COMPASS_AUDIT_RETENTION_MONTHS is not a number; keeping %s",
                storage.audit_retention_months,
            )
    storage.cosmos_meta_container = os.environ.get(
        "AZURE_COSMOS_META_CONTAINER", storage.cosmos_meta_container
    )
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
    if aliases_raw := os.environ.get("COMPASS_AUTH_ALIASES"):
        aliases: dict[str, str] = {}
        for pair in aliases_raw.split(","):
            name, _, identity = pair.strip().partition(":")
            if name and identity:
                aliases[name] = identity
        if aliases:
            auth.identity_aliases = aliases
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

    pipelines = settings.pipelines
    if (flag := os.environ.get("COMPASS_PIPELINES", "").strip().lower()):
        pipelines.enabled = flag in ("1", "true", "yes", "on")
    pipelines.secrets_backend = os.environ.get(
        "COMPASS_PIPELINES_SECRETS", pipelines.secrets_backend
    ).lower()

    if (cap := os.environ.get("COMPASS_MISSIONS_MAX_PARALLEL", "").strip()):
        try:
            settings.missions.max_parallel = max(1, int(cap))
        except ValueError:
            logging.getLogger("compass.common.config").warning(
                "COMPASS_MISSIONS_MAX_PARALLEL=%r is not a number; keeping %d",
                cap, settings.missions.max_parallel,
            )
    if (flag := os.environ.get("COMPASS_MISSIONS", "").strip().lower()):
        settings.missions.enabled = flag in ("1", "true", "yes", "on")

    sandbox = settings.sandbox
    if (flag := os.environ.get("COMPASS_SANDBOX", "").strip().lower()):
        sandbox.enabled = flag in ("1", "true", "yes", "on")
    if (flag := os.environ.get("COMPASS_SANDBOX_NETWORK", "").strip().lower()):
        sandbox.network = flag in ("1", "true", "yes", "on")
    if (flag := os.environ.get("COMPASS_SANDBOX_STRICT", "").strip().lower()):
        sandbox.fail_if_unavailable = flag in ("1", "true", "yes", "on")
    for name, target in (("ALLOW_WRITE", "allow_write"), ("DENY_READ", "deny_read")):
        raw = os.environ.get(f"COMPASS_SANDBOX_{name}", "").strip()
        if raw:
            setattr(sandbox, target,
                    [part.strip() for part in raw.split(",") if part.strip()])

    estimate = settings.estimate
    if (flag := os.environ.get("COMPASS_ESTIMATE", "").strip().lower()):
        estimate.enabled = flag in ("1", "true", "yes", "on")
    for name in ("classifier", "architect", "draft", "live_pricing"):
        env = os.environ.get(f"COMPASS_ESTIMATE_{name.upper()}", "").strip().lower()
        if env:
            setattr(estimate, name, env in ("1", "true", "yes", "on"))

    bf = settings.business_functions
    if (flag := os.environ.get("COMPASS_BUSINESS_FUNCTIONS", "").strip().lower()):
        bf.enabled = flag in ("1", "true", "yes", "on")
    if (flag := os.environ.get("COMPASS_BUSINESS_FUNCTIONS_STRICT", "").strip().lower()):
        bf.strict_catalog = flag in ("1", "true", "yes", "on")

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
