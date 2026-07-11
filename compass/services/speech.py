"""Expressive text-to-speech — the "read aloud" backend.

Uses Azure OpenAI's audio/speech model (gpt-4o-mini-tts) with a natural-
language `instructions` prompt that gives the voice warmth, expression, and
pacing — the difference between the flat browser speechSynthesis and the way
Claude reads a response. Markdown is stripped and code blocks are summarized
so the narration stays clean, and over-long text is trimmed to the model's
input limit at a sentence boundary.
"""

from __future__ import annotations

import re

from compass.config import get_settings
from compass.gateway.azure_client import AzureModelClient, get_model_client
from compass.services.telemetry import log_event

# TTS models cap input around 4096 chars; leave headroom.
MAX_TTS_CHARS = 3800

DEFAULT_INSTRUCTIONS = (
    "Affect: a warm, friendly, and knowledgeable colleague explaining "
    "something out loud.\n"
    "Tone: natural and genuinely expressive — never flat or robotic. Let "
    "curiosity and helpfulness come through.\n"
    "Pacing: measured and clear, with natural pauses at commas and periods; "
    "slow down slightly for important points.\n"
    "Delivery: conversational, as if talking to one person. Read technical "
    "terms and identifiers clearly; when you reach a code block, say 'here's "
    "the code' briefly rather than reading symbols."
)


class SpeechDisabledError(RuntimeError):
    pass


async def synthesize(text: str, *, voice: str | None = None) -> bytes:
    settings = get_settings()
    if not settings.azure.tts_deployment:
        raise SpeechDisabledError(
            "Text-to-speech is not configured. Set AZURE_OPENAI_TTS_DEPLOYMENT "
            "to a deployed TTS model (e.g. gpt-4o-mini-tts)."
        )
    client = get_model_client()
    if not isinstance(client, AzureModelClient):
        raise SpeechDisabledError("Text-to-speech is unavailable in mock model mode.")

    spoken = _prepare(text)
    if not spoken:
        raise SpeechDisabledError("Nothing to read.")
    audio = await client.synthesize_speech(
        spoken, voice or settings.azure.tts_voice, DEFAULT_INSTRUCTIONS
    )
    log_event("tts_synthesized", chars=len(spoken), bytes=len(audio))
    return audio


def _prepare(md: str) -> str:
    """Strip Markdown to clean prose and trim to the input limit."""
    text = re.sub(r"```[\s\S]*?```", " (code block) ", md)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_#>]", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", ". ", text).strip()
    if len(text) <= MAX_TTS_CHARS:
        return text
    cut = text[:MAX_TTS_CHARS]
    dot = cut.rfind(". ")
    return (cut[: dot + 1] if dot > MAX_TTS_CHARS * 0.6 else cut).strip()
