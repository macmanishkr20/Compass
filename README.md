# Compass

A Python / FastAPI port of the **Claude Code agent architecture**, backed by
**Azure OpenAI**. Same skeleton, same invariants: an async-generator streaming
chain end-to-end, a typed agent-loop state machine, one permission choke point
for every tool call, a graduated context-compaction pipeline, and subagents as
plain recursion.

```
surfaces        web UI at / + SSE + REST permission bridge      api/server.py, api/static/index.html
orchestration   ask() session turn -> query() agent loop        core/query_engine.py, core/query_loop.py
tool runtime    partition -> parallel reads / serial writes     core/tool_orchestration.py
trust gate      validate -> hooks -> rules -> ask -> execute    core/tool_execution.py
policy          allow/ask/deny rules, 4 modes, lifecycle hooks  policy/
bash security   segments, injection + destructive detection    tools/bash_security.py
context engine  budget -> microcompact -> autocompact -> reactive   context/compaction.py
model gateway   Azure OpenAI streaming, retry ladder, fallback  gateway/azure_client.py
MCP client      stdio / http / sse servers as native tools      services/mcp/
persistence     local JSONL or Azure Cosmos DB + Blob spills    persistence/
telemetry       Application Insights (optional, env-gated)      services/telemetry.py
tools (8+MCP)   file_read/write/edit, glob, grep, bash, todo_write, agent, mcp__*
```

Everything is configured through `.env` (see `.env.example`): each Azure
service — OpenAI, Cosmos DB, Blob Storage, Application Insights — is
optional; leave its keys blank and Compass uses a local fallback, fill them
in and the capability switches on with no code changes. Persistence is
**Cosmos DB** (document-per-message, partitioned by session — the natural
backend for an append-only JSON transcript) with local JSONL as the
zero-config default.

## Architecture mapping

| Claude Code | Compass | Notes |
|---|---|---|
| `QueryEngine.ts ask()` | `core/query_engine.py` | session turn: hooks, system prompt, transcript |
| `query.ts queryLoop()` | `core/query_loop.py` | `while True` with typed `Continue`/`Terminal` transitions |
| `toolOrchestration.ts runTools()` | `core/tool_orchestration.py` | identical partition algorithm; ≤10 parallel reads |
| `toolExecution.ts runToolUse()` | `core/tool_execution.py` | 9-gate pipeline; denials are `tool_result` errors, never exceptions |
| `Tool.ts` interface | `tools/base.py` | Pydantic schema, per-input `is_concurrency_safe`, streaming `call()` |
| `useCanUseTool` / permission bridge | `PermissionBroker` | ask verdicts stream as SSE events, resolved via REST |
| `utils/hooks.ts` | `policy/hooks.py` | matchers (exact/pipe/regex) + per-hook timeouts; exit code 2 blocks |
| `services/compact/*` | `context/compaction.py` | four stages; threshold uses usage-anchored token count |
| `utils/tokens.ts tokenCountWithEstimation` | `context/compaction.py count_context_tokens` | real API usage of last response + estimate of the tail |
| `utils/Shell.ts` cwd tracking | `tools/shell_session.py` | persistent working directory; `cd` carries between bash calls |
| `services/api/claude.ts` | `gateway/azure_client.py` | Anthropic `tool_use` ⇢ OpenAI function calling |
| Anthropic cache breakpoints | Azure automatic prompt caching | keep the message prefix byte-stable; cached tokens tracked |
| `AgentTool` + fork subagents | `tools/agent.py` | recursive `query()`; inherited broker/abort/cost tracker |
| `sessionStorage.ts` JSONL | `persistence/session_store.py` | the transcript is the database; sidechains tagged by `agent_id` |
| `CLAUDE.md` memory | `COMPASS.md` | loaded into the system prompt when present |

### Fidelity notes

Three mechanisms were brought to parity with the original after the first pass:

- **Token counting** — the autocompact threshold reads the *real* prompt+output
  tokens of the last API response (stamped on the assistant message) and only
  char-estimates the tool-results appended since, matching
  `tokenCountWithEstimation`. The earlier char-only estimate under-counted a
  40k-token context as ~330 tokens and would never have fired.
- **Hooks** — each hook carries a matcher (tested against the tool name:
  `bash`, `file_write|file_edit`, or a regex) and its own timeout; both the
  flat and Claude-Code-nested config shapes and CamelCase event keys are
  accepted.
- **Persistent shell** — bash tracks a per-session working directory the same
  way `Shell.ts` does (fresh process per command, `pwd -P` captured and fed
  forward), so `cd` persists across calls while each command stays isolated.
  Like the original, `export` does not persist; only the cwd does.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# no credentials needed — scripted mock model
.venv/bin/python scripts/smoke.py

# real Azure OpenAI
cp .env.example .env   # fill in endpoint, key, deployment (auto-loaded on start)
.venv/bin/uvicorn compass.api.server:app --port 8000
# then open http://localhost:8000 — chat UI with live tool cards,
# permission Allow/Deny buttons, session resume, and cost readout
```

`.env` is loaded automatically (via python-dotenv) — no need to `source` it.
Real shell variables still override the file. **The endpoint and key must come
from the same Azure resource**: on the resource's *Keys & Endpoint* page (or
the Azure AI Foundry project), copy `AZURE_OPENAI_ENDPOINT` and one of its keys
together — a key from a different resource returns `401 invalid subscription
key`. Confirm `AZURE_OPENAI_DEPLOYMENT` matches a deployment name in that
resource.

### MCP servers

Declare servers in `.compass/mcp.json` (or inline via `COMPASS_MCP_SERVERS`);
discovered tools appear as `mcp__{server}__{tool}` and pass through the same
permission gate as built-ins. `${VAR}` in any value expands from `.env`:

```json
{
  "sample": {"type": "stdio", "command": "python",
             "args": ["scripts/sample_mcp_server.py"]},
  "internal": {"type": "http", "url": "https://mcp.example.com/mcp",
               "headers": {"Authorization": "Bearer ${INTERNAL_MCP_TOKEN}"}}
}
```

### Talk to it

```bash
SID=$(curl -s -X POST localhost:8000/v1/sessions -d '{}' \
      -H 'content-type: application/json' | jq -r .session_id)

# streams SSE: text_delta, tool_call_started, tool_result, permission_request, ...
curl -N -X POST localhost:8000/v1/sessions/$SID/messages \
  -H 'content-type: application/json' \
  -d '{"content": "List the python files here and summarize the project"}'
```

When the agent wants to do something state-changing, the stream emits
`permission_request` with a `request_id` and blocks. Approve from anywhere:

```bash
curl -X POST localhost:8000/v1/sessions/$SID/permissions/$REQUEST_ID \
  -H 'content-type: application/json' -d '{"behavior": "allow"}'
```

Other endpoints: `POST /v1/sessions/{sid}/abort`,
`GET /v1/sessions/{sid}/transcript`, `GET /v1/sessions`, `GET /healthz`.

### Conversation management &amp; checkpoints

Conversations carry server-side metadata (persisted next to the transcript in
the local JSON file or a Cosmos `*_meta` container) and support the full
lifecycle plus transcript checkpoints:

| Operation | Endpoint | Notes |
|---|---|---|
| Rename / pin / archive / move to folder | `PATCH /v1/sessions/{id}` | also sets per-conversation `mode` and `effort` |
| Delete | `DELETE /v1/sessions/{id}` | removes transcript + metadata |
| Fork | `POST /v1/sessions/{id}/fork` | copies history (optionally up to a `up_to_uuid` checkpoint) into a new conversation |
| Edit a past prompt | `POST /v1/sessions/{id}/messages/{uuid}/edit` | truncates the transcript at that message and re-runs — a checkpoint |
| Regenerate | `POST /v1/sessions/{id}/regenerate` | drops the last answer and re-runs the last user turn |
| Rich list | `GET /v1/sessions` | returns title, pin, archive, group, mode, effort, timestamps, message count — the UI computes **Group by** (none / folder / date) and **Sort by** (recent / created / title) from these |

The web UI exposes all of this: a per-conversation ⋯ menu (rename, pin, fork,
move to folder, archive, delete), inline prompt **edit &amp; resend** and
**regenerate** actions on messages, group-by / sort-by / show-archived
controls in the sidebar, and topbar **permission mode** (default / accept
edits / plan / bypass) and **model effort** (minimal / low / medium / high)
selectors. Mode drives the permission gate; effort is passed to Azure as
`reasoning_effort` on reasoning-capable deployments (silently ignored
otherwise).

### Authentication

Every stateful endpoint requires a bearer token. Sign in via
`POST /v1/auth/login {username, password}`; users are configured in `.env`
(`COMPASS_AUTH_USERS=alice:pw1,bob:pw2` — the default demo account is
`admin` / `compass`, change it before exposing the server). Tokens are
HMAC-signed (`COMPASS_AUTH_SECRET`; blank = per-process random) and expire
after `COMPASS_AUTH_TOKEN_TTL_HOURS` (12h default). Set
`COMPASS_AUTH_ENABLED=0` to disable the gate entirely (all requests run as
`guest` and the web UI skips the login screen). The `require_user` dependency
in `compass/api/auth.py` is the single seam to swap in Entra ID/OIDC later.

### Read-aloud (text-to-speech)

The message "read aloud" button uses Azure OpenAI TTS for an expressive,
emotive voice (`compass/services/speech.py`) — the `instructions` prompt
steers tone, warmth, pacing, and accent, which a plain `tts-1` model can't do.
Configure it in `.env`:

```
AZURE_OPENAI_TTS_DEPLOYMENT=gpt-4o-mini-tts   # deploy this model in Azure first
COMPASS_TTS_VOICE=coral                       # coral/sage are the warm ones
```

Deploy `gpt-4o-mini-tts` in your Azure AI Foundry project and use an API
version of `2025-03-01-preview` or later (`AZURE_OPENAI_API_VERSION`) so the
expressive `instructions` are honored. Until the model is deployed the button
falls back to the browser's built-in voice automatically — no error, just a
flatter read.

## Policy configuration

`.compass/settings.json` (workspace root):

```json
{
  "permission_mode": "default",
  "permission_rules": [
    {"tool": "bash", "pattern": "git *", "action": "allow"},
    {"tool": "bash", "pattern": "rm *", "action": "deny"},
    {"tool": "file_write", "pattern": "*", "action": "ask"}
  ]
}
```

`.compass/hooks.json` — shell commands receiving the event payload as JSON on
stdin; exit code 2 blocks, stdout JSON can rewrite input or force continuation:

```json
{
  "pre_tool_use": ["python scripts/audit_tool_call.py"],
  "stop": ["python scripts/check_tests_pass.py"]
}
```

Events: `session_start`, `user_prompt_submit`, `pre_tool_use`,
`post_tool_use`, `post_tool_use_failure`, `stop`, `subagent_stop`.

## Invariants this port defends (same as the original)

1. **Every `tool_call` gets exactly one `tool_result`** — aborts, denials,
   crashes, and unknown tools all synthesize error results.
2. **Nothing buffers** — every layer is an async generator; a token is on the
   wire the moment the model emits it.
3. **One trust choke point** — built-in, subagent, and future MCP tools all
   pass through `run_tool_use`'s gates in the same order.
4. **The model's view shrinks; the record never does** — compaction appends
   boundary messages, the JSONL transcript keeps everything for resume.
5. **Read-only parallelism is judged per input** — `bash("git status")`
   parallelizes; `bash("rm …")` serializes and asks.
6. **Cache-prefix stability is an architectural constraint** — stable system
   prompt, stable message prefix, cached-token accounting in the cost tracker.
