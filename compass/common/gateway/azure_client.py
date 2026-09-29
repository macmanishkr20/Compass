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
import os
import random
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping, Protocol

from compass.common.config import get_settings
from compass.common.gateway.refusals import (
    REFUSED,
    Refusal,
    from_choice,
    from_error,
)
from compass.common.gateway import limits
from compass.common.gateway.responses import (
    REASONING_META_KEY,
    ReasoningTrace,
    ResponsesOutcome,
    ThinkingDelta,
    build_request,
    consume,
    parse_response,
    sse_events,
    ToolArgsDelta,
)

logger = logging.getLogger("compass.gateway")

# Optional per-token pacing for the mock, so streaming/loading UI can be observed
# (e.g. in a preview). 0 = as fast as possible (default).
_MOCK_SLOW = float(os.getenv("COMPASS_MOCK_SLOW", "0") or 0)

MAX_RETRIES = 4
BASE_DELAY_SECONDS = 1.0

#: Longest a single retry will wait. A token-quota window renews once a
#: minute, so anything shorter cannot outlast one; much longer means a turn
#: that has silently stopped being worth waiting for.
MAX_RETRY_SLEEP_SECONDS = 75.0

#: What a quota window costs when the headers do not say. Azure reports
#: `x-ratelimit-renewalperiod-tokens: 60` on this resource, but a deployment
#: that omits it should still wait a sensible minute rather than a second.
DEFAULT_RENEWAL_SECONDS = 60.0


def _retry_after_seconds(headers: Mapping[str, Any] | None) -> float | None:
    """How long to wait before retrying, read from the response itself.

    Azure's `Retry-After` is not usable on its own here, and this is measured
    rather than assumed: a 429 raised by exhausting the *token* quota comes
    back with `retry-after: 1`, `retry-after-ms: 0` and `reset-tokens: 0`,
    while the thing that actually has to happen is a 60-second window
    renewal. Obeying `Retry-After` retries immediately into the same wall —
    which is precisely what Compass was doing: four attempts, 1+2+4+8 seconds,
    all inside one exhausted minute, and the session died about 45 seconds
    before the quota would have freed itself.

    So the token quota is read first and believed over `Retry-After`. The
    header ladder below is in order of how much it knows.
    """
    if not headers:
        return None

    def number(name: str) -> float | None:
        raw = headers.get(name) or headers.get(name.title())
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError, AttributeError):
            return None

    # Out of tokens for this window: nothing is retryable until it renews.
    remaining = number("x-ratelimit-remaining-tokens")
    if remaining is not None and remaining <= 0:
        renewal = number("x-ratelimit-renewalperiod-tokens")
        reset = number("x-ratelimit-reset-tokens")
        # `reset` is the honest one when it is non-zero; this resource reports
        # 0, which cannot be right for an exhausted window, so it is ignored.
        wait = reset if (reset and reset > 0) else (renewal or DEFAULT_RENEWAL_SECONDS)
        return min(wait, MAX_RETRY_SLEEP_SECONDS)

    # Request-per-minute exhaustion, same reasoning. Compared against None
    # rather than truth-tested: zero remaining is exactly the case this is
    # looking for, and `0 or 1` quietly reads it as "no header".
    requests_left = number("x-ratelimit-remaining-requests")
    if requests_left is not None and requests_left <= 0:
        renewal = number("x-ratelimit-renewalperiod-requests") or DEFAULT_RENEWAL_SECONDS
        return min(renewal, MAX_RETRY_SLEEP_SECONDS)

    ms = number("retry-after-ms")
    if ms and ms > 0:
        return min(ms / 1000.0, MAX_RETRY_SLEEP_SECONDS)
    secs = number("retry-after")
    if secs and secs > 0:
        return min(secs, MAX_RETRY_SLEEP_SECONDS)
    return None


def _retry_after_of(err: Exception) -> float | None:
    """The wait an error is carrying, whichever path raised it.

    Two paths reach here: the reasoning path raises `_RetryableHTTPError`
    with the wait already read, and the chat-completions path raises the
    SDK's own errors, which keep the response on them. Both know the same
    headers; only the packaging differs.
    """
    carried = getattr(err, "retry_after", None)
    if isinstance(carried, (int, float)) and carried > 0:
        return min(float(carried), MAX_RETRY_SLEEP_SECONDS)
    headers = getattr(getattr(err, "response", None), "headers", None)
    return _retry_after_seconds(headers)


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
    #: What the model thought before answering, when it reasoned at all. The
    #: items in it are handed back verbatim on the next request so the model
    #: resumes its reasoning instead of re-deriving it after every tool call.
    #: Empty on a turn the model answered directly, which is normal and not a
    #: fault: a reasoning model decides per request whether thinking helps.
    reasoning: ReasoningTrace = field(default_factory=ReasoningTrace)
    #: Set when the turn was declined rather than finished. A refusal arrives
    #: as a perfectly successful response, so nothing that watches only for
    #: exceptions will see it — this is how the loop is told.
    refusal: Refusal | None = None
    #: Searches made and code run by Azure inside this turn. Reported rather
    #: than requested: it has already happened by the time we see it, and the
    #: only thing left to do with it is show it. Empty on every turn that used
    #: no server tool, which is most of them.
    hosted: list[dict[str, Any]] = field(default_factory=list)


#: Reasoning arrives on its own channel, ahead of and separate from the answer.
StreamItem = StreamDelta | ThinkingDelta | ToolArgsDelta | CompletionResult


class ModelClient(Protocol):
    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_output_tokens: int | None = None,
        deployment: str | None = None,
        effort: str | None = None,
        reasoning_by_index: dict[int, list[dict[str, Any]]] | None = None,
    ) -> AsyncIterator[StreamItem]: ...

    async def complete_utility(
        self,
        prompt: str,
        text: str,
        *,
        max_tokens: int = 2_000,
        prefer_main: bool = False,
        model: str = "",
        images: list[str] | None = None,
        effort: str | None = None,
        schema: dict[str, Any] | None = None,
        schema_name: str = "result",
    ) -> str: ...


def _credentials_error(deployment: str, azure) -> RuntimeError:
    """The 401 worth reading: which key, which resource, which deployment."""
    return RuntimeError(
        "Azure OpenAI rejected the credentials (401). Check that "
        "AZURE_OPENAI_API_KEY is one of the keys for the SAME resource as "
        f"AZURE_OPENAI_ENDPOINT ({azure.endpoint}), and that the deployment "
        f"'{deployment}' exists there. For an Azure AI Foundry resource, copy "
        "the key from that project's Keys & Endpoint page."
    )


def _http_error(status: int, message: str, deployment: str, azure,
                headers: Mapping[str, Any] | None = None) -> Exception:
    """Turn a failed reasoning request into the error the loop expects.

    The reasoning path is raw HTTP, so it has to categorize failures itself
    where the SDK would otherwise do it: the retry ladder above only retries
    what `_is_retryable` recognizes.
    """
    if status in (401, 403):
        return _credentials_error(deployment, azure)
    declined = from_error(message) if status == 400 else None
    if declined:
        return RefusedError(declined)
    if status == 404:
        return RuntimeError(
            f"Azure has no Responses API at api-version "
            f"{get_settings().thinking.responses_api_version} for deployment "
            f"'{deployment}' ({message}). Set AZURE_OPENAI_RESPONSES_API_VERSION "
            "to one the resource serves, or COMPASS_THINKING_ENABLED=0 to stay "
            "on chat completions without thinking."
        )
    if status in (408, 409, 429) or status >= 500:
        return _RetryableHTTPError(f"Azure returned {status}: {message}",
                                   retry_after=_retry_after_seconds(headers))
    return RuntimeError(f"Azure returned {status}: {message}")


class _RetryableHTTPError(Exception):
    """A reasoning-path failure worth another attempt.

    Carries how long to wait when the response said so, because only the
    response knows: a 429 for an exhausted token window needs a minute, and
    a 500 needs a moment.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class RefusedError(Exception):
    """The request was declined before the model answered.

    Raised rather than returned because there is no turn to return: nothing
    was generated. It is deliberately not retryable — sending the same prompt
    again gets the same answer — and deliberately not a generic error, so the
    loop can say what happened instead of showing a stack trace's worth of
    Azure JSON.
    """

    def __init__(self, refusal: Refusal) -> None:
        super().__init__(refusal.message())
        self.refusal = refusal


def _warn_if_reasoning_ate_the_budget(
    response: Any, content: str, deployment: str, cap: int
) -> None:
    """Say when an answer was crowded out by the thinking that preceded it.

    Reasoning is billed as output and shares the output cap with the answer,
    so a cap sized for a reply without thinking produces a short answer or no
    answer at all — and the call returns an empty string, which reads like the
    model having nothing to say. The two ways out are a bigger cap or a lower
    effort, so both are named.
    """
    usage = getattr(response, "usage", None)
    details = getattr(usage, "completion_tokens_details", None)
    spent = getattr(details, "reasoning_tokens", 0) or 0
    if not spent:
        return
    finish = getattr(response.choices[0], "finish_reason", None)
    if content and finish != "length":
        logger.debug("%s spent %d tokens thinking of %d", deployment, spent, cap)
        return
    logger.warning(
        "%s spent %d of its %d output tokens thinking, leaving %s. Raise the "
        "cap for this call or lower the effort.",
        deployment, spent, cap,
        "a truncated answer" if content else "nothing for the answer",
    )


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
                # A whole design — eight working screens of markup — takes
                # minutes to write. The SDK's default gives up long before.
                timeout=900.0,
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
        reasoning_by_index: dict[int, list[dict[str, Any]]] | None = None,
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
                    # A deployment that reasons goes to the Responses API,
                    # which is the only one that will show its thinking or
                    # hand back reasoning that survives a tool call. Anything
                    # else has no thinking to ask for.
                    if settings.thinking.reasons(target):
                        stream = self._stream_responses(
                            target, messages, tools, max_output_tokens, effort,
                            reasoning_by_index,
                        )
                    else:
                        stream = self._stream_once(
                            target, messages, tools, max_output_tokens, effort
                        )
                    async for item in stream:
                        yield item
                    return
                except ContextOverflowError:
                    raise  # handled by the loop, never retried here
                except Exception as err:  # noqa: BLE001 — categorized below
                    last_error = err
                    if _is_auth_error(err):
                        raise _credentials_error(target, settings.azure) from err
                    if not _is_retryable(err):
                        raise
                    # What the response asked for beats the ladder. An
                    # exhausted token window is not a transient blip that
                    # doubling will outrun — it is a minute that has to pass,
                    # and backing off 1, 2, 4, 8 seconds inside it just spends
                    # the retry budget without ever reaching the other side.
                    asked = _retry_after_of(err)
                    delay = (asked if asked is not None
                             else BASE_DELAY_SECONDS * (2**attempt) + random.random())
                    logger.warning(
                        "retryable API error on %s (attempt %d/%d): %s — "
                        "sleeping %.1fs%s",
                        target, attempt + 1, MAX_RETRIES, err, delay,
                        " (quota window)" if asked is not None else "",
                    )
                    await asyncio.sleep(delay)
            logger.warning("deployment %s exhausted retries, trying fallback", target)
        raise last_error or RuntimeError("model request failed")

    async def _stream_responses(
        self,
        deployment: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_output_tokens: int | None,
        effort: str | None,
        reasoning_by_index: dict[int, list[dict[str, Any]]] | None,
    ) -> AsyncIterator[StreamItem]:
        """One turn on the Responses API, where the thinking is visible.

        Kept on raw HTTP rather than the SDK: this needs `store: false` with
        `include: ["reasoning.encrypted_content"]` and the reasoning-summary
        event stream, and the SDK version a deployment happens to have is not
        something to depend on for that.
        """
        import httpx

        settings = get_settings()
        azure, thinking = settings.azure, settings.thinking
        url = (
            f"{azure.endpoint.rstrip('/')}/openai/responses"
            f"?api-version={thinking.responses_api_version}"
        )
        body = build_request(
            deployment=deployment,
            messages=messages,
            tools=tools,
            max_output_tokens=max_output_tokens or settings.context.max_output_tokens,
            effort=thinking.normalize_effort(effort),
            display=thinking.display,
            reasoning_by_index=reasoning_by_index,
            server_tools=True,
        )

        outcome = ResponsesOutcome()
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=30.0)) as http:
            async with http.stream(
                "POST", url, json=body,
                headers={"api-key": azure.api_key, "content-type": "application/json"},
            ) as response:
                # Every response names the deployment's quota, including the
                # 429s. Recording it is what lets the context budget be a
                # measured fact rather than a guess written in a config file.
                limits.remember(response.headers)
                if response.status_code != 200:
                    raw = (await response.aread()).decode("utf-8", "replace")
                    message = raw
                    try:
                        message = json.loads(raw)["error"]["message"]
                    except (ValueError, KeyError, TypeError):
                        pass
                    low = message.lower()
                    if "context" in low and "length" in low:
                        raise ContextOverflowError(message)
                    raise _http_error(response.status_code, message, deployment,
                                      azure, response.headers)

                async for item in consume(sse_events(response.aiter_lines()), outcome):
                    # Each kind is named. The bare `else` this replaces would
                    # have wrapped an argument fragment as answer text and put
                    # raw tool JSON in front of the reader.
                    if isinstance(item, (ThinkingDelta, ToolArgsDelta)):
                        yield item
                    elif isinstance(item, str):
                        yield StreamDelta(text=item)

        yield CompletionResult(
            content=outcome.text,
            tool_calls=[
                ToolCallDraft(id=c["id"], name=c["name"], arguments=c["arguments"])
                for c in outcome.tool_calls
            ],
            finish_reason=outcome.finish_reason,
            prompt_tokens=outcome.prompt_tokens,
            completion_tokens=outcome.completion_tokens,
            cached_prompt_tokens=outcome.cached_prompt_tokens,
            model=deployment,
            reasoning=outcome.reasoning,
            refusal=outcome.refusal,
            hosted=outcome.hosted,
        )

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
                # A declined prompt is an answer, not a parameter to strip and
                # retry: the loop below would otherwise drop fields one by one
                # and send the same refused request four more times.
                if (declined := from_error(str(err))) is not None:
                    raise RefusedError(declined) from err
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
        refusal: Refusal | None = None
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
                declined = from_choice(choice, partial=bool(content_parts))
                if declined:
                    refusal = declined
                    finish_reason = REFUSED

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
            refusal=refusal,
        )

    async def complete_reasoning(
        self,
        prompt: str,
        text: str,
        *,
        max_tokens: int,
        deployment: str,
        images: list[str] | None = None,
        effort: str | None = None,
        prior: ReasoningTrace | None = None,
        schema: dict[str, Any] | None = None,
        schema_name: str = "result",
        server_tools: bool = False,
    ) -> tuple[str, ReasoningTrace]:
        """One non-streaming call that actually reasons, and says what it cost.

        This is what a long single-shot generation needs that chat completions
        cannot give it: reasoning asked for properly, the reasoning itself
        handed back so a follow-up step resumes it instead of starting over,
        an honest reason when the answer was cut short, and the token count
        that explains it.

        `prior` is the reasoning from the previous step of the same piece of
        work. Passing it is what makes a repair round a continuation rather
        than a stranger looking at the document for the first time.
        """
        import httpx

        settings = get_settings()
        azure, thinking = settings.azure, settings.thinking
        url = (
            f"{azure.endpoint.rstrip('/')}/openai/responses"
            f"?api-version={thinking.responses_api_version}"
        )

        user: Any = text
        if images:
            user = [{"type": "text", "text": text}] + [
                {"type": "image_url", "image_url": {"url": src}} for src in images
            ]
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user},
        ]
        # The prior reasoning belongs to the assistant turn before this one, so
        # it is attached to a placeholder for that turn.
        by_index: dict[int, list[dict[str, Any]]] = {}
        if prior and prior.items:
            messages.insert(1, {"role": "assistant", "content": ""})
            by_index[1] = prior.items

        body = build_request(
            deployment=deployment,
            messages=messages,
            tools=None,
            max_output_tokens=max_tokens,
            effort=thinking.normalize_effort(effort or thinking.default_effort),
            display=thinking.display,
            reasoning_by_index=by_index,
            stream=False,
            schema=schema,
            schema_name=schema_name,
            server_tools=server_tools,
        )

        async with httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=30.0)) as http:
            response = await http.post(
                url, json=body,
                headers={"api-key": azure.api_key, "content-type": "application/json"},
            )
        limits.remember(response.headers)
        if response.status_code != 200:
            message = response.text
            try:
                message = response.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                pass
            low = message.lower()
            if "context" in low and "length" in low:
                raise ContextOverflowError(message)
            raise _http_error(response.status_code, message, deployment, azure,
                              response.headers)

        outcome = parse_response(response.json())
        if outcome.finish_reason == "length":
            logger.warning(
                "%s ran out of room: %d of %d output tokens went to thinking, "
                "leaving %s. Raise the cap for this call or lower the effort.",
                deployment, outcome.reasoning.tokens, max_tokens,
                "a truncated answer" if outcome.text else "nothing for the answer",
            )
        elif outcome.reasoning.tokens:
            logger.info(
                "%s thought for %d tokens before answering (%d of %d used)",
                deployment, outcome.reasoning.tokens,
                outcome.completion_tokens, max_tokens,
            )
        return outcome.text, outcome.reasoning

    async def complete_utility(
        self,
        prompt: str,
        text: str,
        *,
        max_tokens: int = 2_000,
        prefer_main: bool = False,
        model: str = "",
        images: list[str] | None = None,
        effort: str | None = None,
        schema: dict[str, Any] | None = None,
        schema_name: str = "result",
    ) -> str:
        """Non-streaming call for side tasks (compaction summaries, suggestions,
        design generation). `max_tokens` must be generous for reasoning models:
        thinking is billed against the same budget, so a small cap can consume
        it entirely and return an empty string — which is why a call that comes
        back empty for that reason now says so in the log instead of looking
        like the model had nothing to say. `prefer_main` puts the primary
        deployment first when output quality matters more than cost. `effort`
        steers how much of the budget goes to reasoning; lowering it is the
        other way out of a truncated answer.

        Falls back to the main deployment when the configured utility deployment
        does not exist on the resource — otherwise a stale
        AZURE_OPENAI_UTILITY_DEPLOYMENT silently breaks every caller (compaction
        summaries included) with a 404."""
        settings = get_settings()
        from openai import BadRequestError, NotFoundError

        # With images the user turn becomes multimodal — the same call, with the
        # pictures alongside the words, which is what "design from this
        # screenshot" needs.
        user_content: object = text
        if images:
            user_content = [{"type": "text", "text": text}] + [
                {"type": "image_url", "image_url": {"url": src}} for src in images
            ]
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_content},
        ]
        order = (
            (settings.azure.deployment, settings.azure.utility_deployment)
            if prefer_main
            else (settings.azure.utility_deployment, settings.azure.deployment)
        )
        # A chosen deployment leads; the usual order still follows it, so an
        # unavailable choice degrades instead of failing the request.
        candidates = [d for d in ((model,) + order) if d]
        wanted = settings.thinking.normalize_effort(
            effort or settings.thinking.default_effort
        )
        last: Exception | None = None
        for deployment in dict.fromkeys(candidates):  # de-duped, order kept
            # A deployment that reasons goes to the API that will actually
            # reason. Every caller here — a design, a repair, a summary —
            # gets that without having to ask for it.
            if settings.thinking.reasons(deployment):
                try:
                    answer, _ = await self.complete_reasoning(
                        prompt, text, max_tokens=max_tokens, deployment=deployment,
                        images=images, effort=effort,
                        schema=schema, schema_name=schema_name,
                    )
                    return answer
                except ContextOverflowError:
                    raise
                except NotFoundError as err:
                    logger.warning("deployment %r not found; falling back", deployment)
                    last = err
                    continue
                except Exception as err:  # noqa: BLE001 — fall back to chat completions
                    logger.warning(
                        "reasoning call to %s failed (%s); retrying on chat "
                        "completions without thinking", deployment, err,
                    )
            base: dict[str, Any] = {"model": deployment, "messages": messages}
            if schema:
                # Same guarantee on the older API, under its own name.
                base["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name, "strict": True, "schema": schema,
                    },
                }
            # Effort applies only where there is reasoning to steer; a
            # deployment that rejects it drops it and retries below.
            if wanted and settings.thinking.reasons(deployment):
                base["reasoning_effort"] = wanted
            try:
                try:
                    response = await self._get_client().chat.completions.create(
                        **base, max_completion_tokens=max_tokens
                    )
                except BadRequestError as err:
                    low = str(err).lower()
                    if "reasoning_effort" in low:
                        base.pop("reasoning_effort", None)
                    if "response_format" in low or "json_schema" in low:
                        # An older deployment cannot constrain the answer.
                        # Better a reply that has to be parsed than no reply.
                        base.pop("response_format", None)
                        logger.warning(
                            "%s will not enforce a JSON schema; falling back to "
                            "parsing the reply", deployment,
                        )
                    response = await self._get_client().chat.completions.create(
                        **base, max_tokens=max_tokens
                    )
                content = response.choices[0].message.content or ""
                _warn_if_reasoning_ate_the_budget(
                    response, content, deployment, max_tokens
                )
                return content
            except NotFoundError as err:  # deployment missing — try the next one
                logger.warning("utility deployment %r not found; falling back", deployment)
                last = err
        if last:
            raise last
        return ""

    async def transcribe_audio(self, data: bytes, filename: str, *,
                               deployment: str) -> str:
        """The words in one audio file, through the audio resource's client.

        Azure takes the file as multipart, so the bytes go as a (name, bytes)
        tuple — nothing is written to disk on the way past.
        """
        client = self._get_tts_client()
        result = await client.audio.transcriptions.create(
            model=deployment,
            file=(filename or "audio", data),
        )
        return (getattr(result, "text", "") or "").strip()

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
    # The reasoning path raises this itself; it is not an SDK type, so it is
    # checked before the SDK import that the rest of this depends on.
    if isinstance(err, _RetryableHTTPError):
        return True
    try:
        from openai import (
            APIConnectionError,
            APITimeoutError,
            InternalServerError,
            RateLimitError,
        )

        return isinstance(
            err,
            (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError),
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
        reasoning_by_index: dict[int, list[dict[str, Any]]] | None = None,
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
        if (
            not tool_results
            and "azure" in prompt_text
            and any(
                kw in prompt_text
                for kw in ("diagram", "architecture", "infrastructure", "iad", "topology")
            )
        ):
            async for item in self._stream_markdown(_AZURE_ARTIFACT):
                yield item
            return
        if not tool_results and any(
            kw in prompt_text
            for kw in ("diagram", "flowchart", "architecture", "sequence diagram", "mermaid")
        ):
            async for item in self._stream_markdown(_MERMAID_ARTIFACT):
                yield item
            return
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
                # In slow mode the tool genuinely takes a few seconds, so the
                # "running" activity bar (and its shimmer) is observable.
                command = (
                    "sleep 3 && echo compass-smoke-ok"
                    if _MOCK_SLOW
                    else "echo compass-smoke-ok"
                )
            for word in text.split(" "):
                yield StreamDelta(text=word + " ")
                await asyncio.sleep(_MOCK_SLOW)
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
            await asyncio.sleep(_MOCK_SLOW)
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
                await asyncio.sleep(max(0.004, _MOCK_SLOW))
        yield CompletionResult(
            content=markdown,
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=180,
            completion_tokens=len(markdown) // 4,
            model="mock",
        )

    async def complete_utility(
        self,
        prompt: str,
        text: str,
        *,
        max_tokens: int = 2_000,
        prefer_main: bool = False,
        model: str = "",
        images: list[str] | None = None,
        effort: str | None = None,
        schema: dict[str, Any] | None = None,
        schema_name: str = "result",
    ) -> str:
        return "Mock summary of the session so far."


_AZURE_ARTIFACT = """Here's an Azure infrastructure architecture for the login \
module. Compass compiles it with the real Azure icons; click to view it inline, \
then open in diagrams.net or download the editable .drawio.

```azure
{
  "title": "Login module — Azure IAD",
  "groups": [
    {"id": "prod", "label": "Production Subscription"},
    {"id": "vnet", "label": "App Virtual Network (VNet)", "parent": "prod"}
  ],
  "nodes": [
    {"id": "users", "service": "users", "label": "Users (Browsers / Mobile Apps)"},
    {"id": "afd", "service": "front_door", "label": "Azure Front Door (WAF)", "group": "prod"},
    {"id": "apim", "service": "apim", "label": "API Management (APIM)", "group": "vnet"},
    {"id": "web", "service": "app_service", "label": "Web App (App Service)", "group": "vnet"},
    {"id": "fn", "service": "function_app", "label": "Function Apps (custom flows)", "group": "vnet"},
    {"id": "entra", "service": "entra_id", "label": "Microsoft Entra ID (B2C)", "group": "vnet"},
    {"id": "redis", "service": "redis", "label": "Cache for Redis (Sessions)", "group": "vnet"},
    {"id": "sql", "service": "sql_database", "label": "SQL Database (User/App Data)", "group": "vnet"},
    {"id": "kv", "service": "key_vault", "label": "Key Vault (Secrets/Certs)", "group": "vnet"},
    {"id": "ai", "service": "app_insights", "label": "Application Insights", "group": "prod"},
    {"id": "logs", "service": "log_analytics", "label": "Log Analytics Workspace", "group": "prod"}
  ],
  "edges": [
    {"from": "users", "to": "afd", "label": "HTTPS"},
    {"from": "afd", "to": "apim", "label": "HTTPS + WAF"},
    {"from": "apim", "to": "web", "label": "JWT"},
    {"from": "web", "to": "entra", "label": "OIDC / OAuth2"},
    {"from": "web", "to": "redis", "label": "Sessions"},
    {"from": "web", "to": "sql", "label": "Read/Write"},
    {"from": "web", "to": "kv", "label": "Secrets", "dashed": true},
    {"from": "web", "to": "ai", "label": "Telemetry", "dashed": true},
    {"from": "ai", "to": "logs", "label": "KQL"}
  ]
}
```

Open it in diagrams.net to edit, or download the .drawio to export to Visio/PNG."""


_MERMAID_ARTIFACT = """Here's the Compass architecture as a Mermaid diagram — \
it auto-lays-out, so nothing overlaps.

```mermaid
flowchart LR
  subgraph Client
    UI[Browser Web UI]
  end
  subgraph Server[Compass FastAPI Server]
    API[API Layer (REST + SSE)]
    LOOP[Agent Loop (query_loop)]
    GATE{Permission gate}
    TOOLS[Tool Orchestration + Execution]
    API --> LOOP
    LOOP --> GATE
    GATE -->|allow| TOOLS
  end
  subgraph Azure[Azure OpenAI]
    CHAT[(Azure OpenAI Chat + tools)]
    TTS[(Speech synthesis - TTS)]
  end
  subgraph Data[Persistence]
    STORE[(Session store - transcripts + metadata)]
    COSMOS[(Azure Cosmos DB)]
  end
  UI -->|SSE + REST| API
  LOOP -->|streams tokens + events| CHAT
  LOOP -->|append| STORE
  STORE -.->|if configured| COSMOS
  TOOLS -->|discover + call tools| MCP[MCP Servers]
  API -.->|read-aloud request| TTS
```

Open it to see the rendered flowchart; the layout is computed, not hand-placed."""


_HTML_ARTIFACT = """Here's a small product page — a calm, editorial layout that
renders live in the panel.

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Compass — Feature Overview</title>
<style>
  :root {
    --ground:#F7F5F0; --panel:#FFFFFF; --ink:#211E1A; --muted:#6E665C;
    --line:#E7E1D6; --accent:#B0762A; --accent-soft:#F3E7D3;
    --mono:ui-monospace,"SF Mono",Menlo,monospace;
    --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--ground); color:var(--ink);
    font-family:var(--sans); line-height:1.6; -webkit-font-smoothing:antialiased; }
  .wrap { max-width:820px; margin:0 auto; padding:56px 28px 72px; }
  .eyebrow { font-family:var(--mono); font-size:.72rem; letter-spacing:.16em;
    text-transform:uppercase; color:var(--accent); font-weight:600; }
  h1 { font-size:clamp(1.9rem,4vw,2.6rem); letter-spacing:-.02em; margin:.35em 0 .3em;
    text-wrap:balance; }
  .lede { font-size:1.1rem; color:var(--muted); max-width:60ch; margin:0 0 40px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:16px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:14px;
    padding:22px; transition:transform .16s, box-shadow .16s; }
  .card:hover { transform:translateY(-2px); box-shadow:0 12px 30px rgba(40,34,22,.10); }
  .ico { width:38px; height:38px; display:grid; place-items:center; border-radius:10px;
    background:var(--accent-soft); color:var(--accent); margin-bottom:14px; }
  .card h3 { margin:0 0 6px; font-size:1.02rem; }
  .card p { margin:0; color:var(--muted); font-size:.92rem; }
  @media (prefers-reduced-motion:reduce){ .card{transition:none;} }
</style>
</head>
<body>
  <div class="wrap">
    <span class="eyebrow">Agent Console</span>
    <h1>Everything the agent needs, in one calm surface.</h1>
    <p class="lede">A streaming loop, a permission gate you control, workspaces,
      and live artifacts — designed to feel considered, not busy.</p>
    <div class="grid">
      <div class="card"><div class="ico">◆</div><h3>Streaming loop</h3>
        <p>Tokens and tool calls flow end-to-end with no buffering boundary.</p></div>
      <div class="card"><div class="ico">▣</div><h3>Permission gate</h3>
        <p>Allow, ask, deny — four modes, decided before any tool runs.</p></div>
      <div class="card"><div class="ico">❖</div><h3>Workspaces</h3>
        <p>Point at a folder or clone a repo; commit straight from chat.</p></div>
      <div class="card"><div class="ico">▤</div><h3>Live artifacts</h3>
        <p>HTML renders beside the chat with a code and preview toggle.</p></div>
    </div>
  </div>
</body>
</html>
```

Open the card to see it render — a light, editorial layout with a proper
palette and type scale."""


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
