"""Model gateway — the port of services/api/claude.ts, for Azure OpenAI.

One place in the codebase talks to the model. Responsibilities:
  * streaming chat completions with tool (function) calling
  * accumulating streamed tool_call argument fragments by index
  * retry ladder with exponential backoff, then fallback deployment
  * usage capture (including cached prompt tokens — Azure's automatic
    prompt caching plays the role of Anthropic cache breakpoints, which is
    why callers must keep the message prefix byte-stable across iterations)
  * a mock implementation so the engine runs without credentials
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol

from compass.config import get_settings

logger = logging.getLogger("compass.gateway")

MAX_RETRIES = 4
BASE_DELAY_SECONDS = 1.0


class ContextOverflowError(Exception):
    """Prompt too long for the model window. The loop reacts with an
    emergency compact (reactiveCompact analog) rather than failing the turn."""


@dataclass
class StreamDelta:
    text: str = ""


@dataclass
class ToolCallDraft:
    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass
class CompletionResult:
    content: str
    tool_calls: list[ToolCallDraft]
    finish_reason: str | None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_prompt_tokens: int = 0
    model: str = ""


StreamItem = StreamDelta | CompletionResult


class ModelClient(Protocol):
    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_output_tokens: int | None = None,
        deployment: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[StreamItem]: ...

    async def complete_utility(self, prompt: str, text: str) -> str: ...


class AzureModelClient:
    """Real gateway. Lazily constructs the OpenAI SDK client so importing
    compass never requires credentials."""

    def __init__(self) -> None:
        self._client = None
        self._tts_client = None

    def _get_client(self):
        if self._client is None:
            from openai import AsyncAzureOpenAI

            azure = get_settings().azure
            if not azure.endpoint or not azure.api_key:
                raise RuntimeError(
                    "Azure OpenAI is not configured. Set AZURE_OPENAI_ENDPOINT and "
                    "AZURE_OPENAI_API_KEY, or run with COMPASS_MOCK_MODEL=1."
                )
            self._client = AsyncAzureOpenAI(
                azure_endpoint=azure.endpoint,
                api_key=azure.api_key,
                api_version=azure.api_version,
            )
        return self._client

    def _get_tts_client(self):
        """Separate client for the TTS resource — it may live in a different
        region with its own endpoint/key/version. Falls back to the main
        client's credentials when the TTS-specific ones aren't set."""
        if self._tts_client is None:
            from openai import AsyncAzureOpenAI

            azure = get_settings().azure
            # Same credentials as the main client -> reuse it, no second connection.
            if (
                azure.tts_endpoint_effective == azure.endpoint
                and azure.tts_api_key_effective == azure.api_key
                and azure.tts_api_version_effective == azure.api_version
            ):
                self._tts_client = self._get_client()
            else:
                self._tts_client = AsyncAzureOpenAI(
                    azure_endpoint=azure.tts_endpoint_effective,
                    api_key=azure.tts_api_key_effective,
                    api_version=azure.tts_api_version_effective,
                )
        return self._tts_client

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_output_tokens: int | None = None,
        deployment: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[StreamItem]:
        settings = get_settings()
        primary = deployment or settings.azure.deployment
        ladder = [primary]
        if settings.azure.fallback_deployment and not deployment:
            ladder.append(settings.azure.fallback_deployment)

        last_error: Exception | None = None
        for target in ladder:
            for attempt in range(MAX_RETRIES):
                try:
                    async for item in self._stream_once(
                        target, messages, tools, max_output_tokens, effort
                    ):
                        yield item
                    return
                except ContextOverflowError:
                    raise  # handled by the loop, never retried here
                except Exception as err:  # noqa: BLE001 — categorized below
                    last_error = err
                    if _is_auth_error(err):
                        raise RuntimeError(
                            "Azure OpenAI rejected the credentials (401). Check that "
                            "AZURE_OPENAI_API_KEY is one of the keys for the SAME "
                            f"resource as AZURE_OPENAI_ENDPOINT ({settings.azure.endpoint}), "
                            f"and that the deployment '{target}' exists there. For an "
                            "Azure AI Foundry resource, copy the key from that project's "
                            "Keys & Endpoint page."
                        ) from err
                    if not _is_retryable(err):
                        raise
                    delay = BASE_DELAY_SECONDS * (2**attempt) + random.random()
                    logger.warning(
                        "retryable API error on %s (attempt %d): %s — sleeping %.1fs",
                        target, attempt + 1, err, delay,
                    )
                    await asyncio.sleep(delay)
            logger.warning("deployment %s exhausted retries, trying fallback", target)
        raise last_error or RuntimeError("model request failed")

    async def _create_stream_adapting(self, kwargs: dict[str, Any], effort: str | None):
        """Create the streaming request, adapting to per-model parameter quirks.

        Different Azure deployments reject different params: reasoning models
        want max_completion_tokens and no temperature; older ones want
        max_tokens; some don't accept reasoning_effort. Rather than hard-code a
        model matrix, we react to the 400 message and retry with the offending
        param removed/renamed — a few bounded retries at most."""
        from openai import BadRequestError

        client = self._get_client()
        for _ in range(4):
            try:
                return await client.chat.completions.create(**kwargs)
            except BadRequestError as err:
                low = str(err).lower()
                if "context" in low and "length" in low:
                    raise ContextOverflowError(str(err)) from err
                if "max_completion_tokens" in low and "max_tokens" in low:
                    # old deployment wants the legacy name
                    if "max_completion_tokens" in kwargs:
                        kwargs["max_tokens"] = kwargs.pop("max_completion_tokens")
                        continue
                if "reasoning_effort" in low and "reasoning_effort" in kwargs:
                    kwargs.pop("reasoning_effort", None)
                    continue
                if "temperature" in low and "temperature" in kwargs:
                    kwargs.pop("temperature", None)
                    continue
                if "stream_options" in low and "stream_options" in kwargs:
                    kwargs.pop("stream_options", None)
                    continue
                raise
        # Last attempt, unguarded — surface the real error if it still fails.
        return await client.chat.completions.create(**kwargs)

    async def _stream_once(
        self,
        deployment: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_output_tokens: int | None,
        effort: str | None = None,
    ) -> AsyncIterator[StreamItem]:

        settings = get_settings()
        kwargs: dict[str, Any] = {
            "model": deployment,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            # Reasoning models (gpt-5, o-series) require max_completion_tokens;
            # older models accept it too on current API versions. If a very old
            # deployment rejects it, we fall back to max_tokens below.
            "max_completion_tokens": max_output_tokens or settings.context.max_output_tokens,
        }
        if tools:
            kwargs["tools"] = tools
        # reasoning_effort applies only to reasoning-capable deployments (o-series,
        # gpt-5 family). Passed when set; a deployment that rejects it drops it
        # and retries rather than failing the turn.
        if effort:
            kwargs["reasoning_effort"] = effort

        stream = await self._create_stream_adapting(kwargs, effort)

        content_parts: list[str] = []
        drafts: dict[int, ToolCallDraft] = {}
        finish_reason: str | None = None
        usage: Any = None

        async for chunk in stream:
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta and delta.content:
                content_parts.append(delta.content)
                yield StreamDelta(text=delta.content)
            if delta and delta.tool_calls:
                for tc in delta.tool_calls:
                    draft = drafts.setdefault(tc.index, ToolCallDraft())
                    if tc.id:
                        draft.id = tc.id
                    if tc.function and tc.function.name:
                        draft.name = tc.function.name
                    if tc.function and tc.function.arguments:
                        draft.arguments += tc.function.arguments
            if choice.finish_reason:
                finish_reason = choice.finish_reason

        cached = 0
        if usage and getattr(usage, "prompt_tokens_details", None):
            cached = getattr(usage.prompt_tokens_details, "cached_tokens", 0) or 0
        yield CompletionResult(
            content="".join(content_parts),
            tool_calls=[drafts[i] for i in sorted(drafts)],
            finish_reason=finish_reason,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            cached_prompt_tokens=cached,
            model=deployment,
        )

    async def complete_utility(self, prompt: str, text: str) -> str:
        """Small non-streaming call for compaction summaries (queryHaiku analog)."""
        settings = get_settings()
        deployment = settings.azure.utility_deployment or settings.azure.deployment
        from openai import BadRequestError

        base = {
            "model": deployment,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": text},
            ],
        }
        try:
            response = await self._get_client().chat.completions.create(
                **base, max_completion_tokens=2_000
            )
        except BadRequestError:
            response = await self._get_client().chat.completions.create(
                **base, max_tokens=2_000
            )
        return response.choices[0].message.content or ""

    async def synthesize_speech(
        self, text: str, voice: str, instructions: str | None
    ) -> bytes:
        """Expressive text-to-speech via the audio/speech endpoint. The
        `instructions` field (gpt-4o-mini-tts only) steers tone/emotion/accent;
        a deployment that rejects it retries without it so plain tts-1 still
        works, just without the affect."""
        from openai import BadRequestError

        deployment = get_settings().azure.tts_deployment
        kwargs: dict[str, Any] = {
            "model": deployment,
            "voice": voice,
            "input": text,
            "response_format": "mp3",
        }
        if instructions:
            kwargs["instructions"] = instructions
        tts = self._get_tts_client()
        try:
            resp = await tts.audio.speech.create(**kwargs)
        except BadRequestError as err:
            if instructions and "instructions" in str(err).lower():
                kwargs.pop("instructions", None)
                resp = await tts.audio.speech.create(**kwargs)
            else:
                raise
        # Binary response: prefer the async reader, fall back to .content.
        if hasattr(resp, "aread"):
            return await resp.aread()
        return resp.content


def _is_retryable(err: Exception) -> bool:
    try:
        from openai import (
            APIConnectionError,
            APITimeoutError,
            InternalServerError,
            RateLimitError,
        )

        return isinstance(
            err, (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError)
        )
    except ImportError:
        return False


def _is_auth_error(err: Exception) -> bool:
    try:
        from openai import AuthenticationError, PermissionDeniedError

        if isinstance(err, (AuthenticationError, PermissionDeniedError)):
            return True
    except ImportError:
        pass
    return getattr(err, "status_code", None) in (401, 403)


class MockModelClient:
    """Deterministic scripted model for tests and credential-free demos.

    Two scenarios, selected by COMPASS_MOCK_SCENARIO:
      * read_only  — first call runs `echo` (auto-allowed read-only path)
      * permission — first call runs a mutating `touch`, which the rule
        engine classifies as state-changing -> the surface gets a live
        permission_request and the Allow/Deny flow is fully demoable
        without credentials. The closing reply acknowledges the verdict.
    """

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_output_tokens: int | None = None,
        deployment: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[StreamItem]:
        scenario = get_settings().mock_scenario
        tool_results = [m for m in messages if m.get("role") == "tool"]

        # Knowledge-style questions (e.g. "…SQL…") don't need tools — answer
        # directly in Markdown so the UI's code-block rendering is exercised
        # without live credentials.
        last_user = next(
            (m for m in reversed(messages) if m.get("role") == "user"), None
        )
        prompt_text = str(last_user.get("content", "")).lower() if last_user else ""
        if not tool_results and any(
            kw in prompt_text
            for kw in ("artifact", "webpage", "web page", "html", "landing", "widget", "build a page")
        ):
            async for item in self._stream_markdown(_HTML_ARTIFACT):
                yield item
            return
        if not tool_results and any(
            kw in prompt_text for kw in ("sql", "salary", "select ", "query")
        ):
            async for item in self._stream_markdown(_SQL_ANSWER):
                yield item
            return

        if tools and not tool_results:
            if scenario == "permission":
                text = "I'll create a marker file — this needs your approval."
                command = "touch data/permission-demo.txt"
            else:
                text = "I'll check the workspace first."
                command = "echo compass-smoke-ok"
            for word in text.split(" "):
                yield StreamDelta(text=word + " ")
                await asyncio.sleep(0)
            yield CompletionResult(
                content=text,
                tool_calls=[
                    ToolCallDraft(
                        id="call_mock_1",
                        name="bash",
                        arguments=json.dumps({"command": command}),
                    )
                ],
                finish_reason="tool_calls",
                prompt_tokens=420,
                completion_tokens=24,
                model="mock",
            )
            return

        # Closing turn: acknowledge what actually happened to the tool call —
        # in the permission scenario the result differs by verdict, and the
        # mock behaves like a real model would: it adapts to the tool_result.
        last_result = str(tool_results[-1].get("content", "")) if tool_results else ""
        if "denied" in last_result or "timed out" in last_result:
            text = (
                "Understood — you denied the change, so I left the workspace "
                "untouched. Nothing was modified."
            )
        elif scenario == "permission":
            text = "Done — you approved it, and data/permission-demo.txt was created."
        else:
            text = "Done — the command ran successfully and the workspace is reachable."
        for word in text.split(" "):
            yield StreamDelta(text=word + " ")
            await asyncio.sleep(0)
        yield CompletionResult(
            content=text,
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=560,
            completion_tokens=18,
            model="mock",
        )

    async def _stream_markdown(self, markdown: str) -> AsyncIterator[StreamItem]:
        """Stream a fixed Markdown answer token-ish by token, preserving
        newlines and code fences so the client renders it live."""
        import re

        # Simulate time-to-first-token so the client's "thinking" loader is
        # exercised the same way a real model's round-trip would show it.
        await asyncio.sleep(0.7)
        for piece in re.split(r"(\s+)", markdown):
            if piece:
                yield StreamDelta(text=piece)
                await asyncio.sleep(0.004)
        yield CompletionResult(
            content=markdown,
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=180,
            completion_tokens=len(markdown) // 4,
            model="mock",
        )

    async def complete_utility(self, prompt: str, text: str) -> str:
        return "Mock summary of the session so far."


_HTML_ARTIFACT = """Here's a small interactive counter page — it renders live in \
the panel on the right.

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Click Counter</title>
<style>
  body { margin:0; height:100vh; display:grid; place-items:center;
    font-family: system-ui, sans-serif;
    background: linear-gradient(135deg,#1f2430,#2b3a2f); color:#fff; }
  .card { text-align:center; padding:40px 56px; border-radius:20px;
    background: rgba(255,255,255,0.06); border:1px solid rgba(255,255,255,0.12);
    backdrop-filter: blur(8px); }
  h1 { margin:0 0 6px; font-size:1.1rem; font-weight:600; opacity:.8; }
  .n { font-size:4rem; font-weight:800; margin:10px 0 20px; font-variant-numeric:tabular-nums; }
  button { font:inherit; font-weight:600; padding:12px 22px; border-radius:12px;
    border:none; cursor:pointer; background:#e0a65a; color:#1a1205; }
  button:active { transform: translateY(1px); }
</style>
</head>
<body>
  <div class="card">
    <h1>You have clicked</h1>
    <div class="n" id="n">0</div>
    <button onclick="c++;document.getElementById('n').textContent=c">Click me</button>
  </div>
  <script>let c = 0;</script>
</body>
</html>
```

Click **Open** on the card to see it, then hit *Click me* — the count is live."""


_SQL_ANSWER = """Here are the common ways to fetch the **maximum salary** in SQL.

## 1. Just the value — `MAX()`
```sql
SELECT MAX(salary) AS max_salary
FROM employees;
```

## 2. The employee(s) who earn it
```sql
SELECT *
FROM employees
WHERE salary = (SELECT MAX(salary) FROM employees);
```

## 3. Top row with `ORDER BY` + `LIMIT`
```sql
SELECT name, salary
FROM employees
ORDER BY salary DESC
LIMIT 1;
```

**Which to use?** Reach for approach **2** when ties matter — `LIMIT 1`
returns only a single row even if several employees share the top salary,
whereas the subquery returns every employee at the maximum."""


_client: ModelClient | None = None


def get_model_client() -> ModelClient:
    global _client
    if _client is None:
        _client = MockModelClient() if get_settings().mock_model else AzureModelClient()
    return _client
