"""The prompt library: things worth asking again.

A chat is disposable and a good prompt is not. The ones worth keeping are
usually not the ones typed first — they are what a question turned into after
a couple of rounds of getting it wrong — and until now the only way to keep
one was to scroll back and copy it out of a transcript that a new
conversation would leave behind.

So a saved prompt is taken from a message, given a title, and shown on Home
where a new chat starts. The starters that shipped with the product stay
underneath: an empty library should still suggest something, and a library
with four entries should not be padded with suggestions nobody asked for.

Sharpening is offered rather than applied. A prompt written mid-conversation
leans on what was already on screen — "do that for the other one too" — and
is useless as a starting point, so the model can rewrite it to stand alone.
It is a suggestion in a text box the person can edit or ignore, because a
rewrite that quietly changes what somebody meant is worse than the original.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from compass.common.config import get_settings

logger = logging.getLogger("compass.home")

#: What the Home screen shows without opening anything. More than this and the
#: starters stop being a glance and start being a list to read.
ON_SCREEN = 4

#: A title is a label, not a sentence.
MAX_TITLE = 80
MAX_TEXT = 4000


@dataclass
class SavedPrompt:
    #: `id` and `title` carry defaults so that a row missing either is a row
    #: with a gap to fill rather than an exception that takes the whole
    #: library with it. Only `text` is genuinely required — a saved prompt
    #: without one is not a saved prompt, and `_read` drops those.
    text: str
    id: str = ""
    title: str = ""
    #: Which glyph the row carries. The same four the built-in starters use,
    #: chosen from the prompt's own words rather than asked for — one more
    #: required field in a save dialog is one more reason not to save.
    icon: str = "bulb"
    created_at: float = field(default_factory=time.time)
    #: Where it came from, when it came from a conversation. Kept so a saved
    #: prompt can be traced back to the chat that produced it.
    session_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


#: Words that suggest what kind of thing a prompt is. Read in order, first
#: match wins, and "bulb" is the honest default for anything else.
_ICONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("film", ("video", "teaser", "clip", "footage", "photo", "image", "render")),
    ("pen", ("write", "draft", "email", "message", "reply", "summar", "rewrite",
             "note", "post")),
    ("branch", ("brainstorm", "ideas", "names", "options", "alternatives",
                "compare", "plan")),
)


def icon_for(text: str) -> str:
    low = (text or "").lower()
    for icon, words in _ICONS:
        if any(w in low for w in words):
            return icon
    return "bulb"


def _clean(value: str, cap: int) -> str:
    return " ".join((value or "").split())[:cap]


class PromptLibrary:
    """Saved prompts on disk, newest first."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    def _path(self) -> Path:
        settings = get_settings()
        folder = settings.workspace_root / settings.data_dir
        folder.mkdir(parents=True, exist_ok=True)
        return folder / "prompts.json"

    def _read(self) -> list[SavedPrompt]:
        path = self._path()
        if not path.exists():
            return []
        try:
            rows = json.loads(path.read_text() or "[]")
        except (OSError, json.JSONDecodeError):
            logger.warning("prompt library unreadable at %s", path)
            return []
        known = {f.name for f in fields(SavedPrompt)}
        out: list[SavedPrompt] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or not row.get("text"):
                continue
            # Unknown keys dropped rather than fatal: one bad row must not
            # take the whole library with it.
            try:
                prompt = SavedPrompt(**{k: v for k, v in row.items() if k in known})
            except TypeError:
                logger.warning("skipping an unreadable prompt row")
                continue
            # Fill what a hand-edited or older row may be missing, so a gap
            # shows up as a sensible default rather than a blank line.
            if not prompt.id:
                prompt.id = uuid.uuid4().hex[:12]
            if not prompt.title:
                prompt.title = _title_from(prompt.text)
            if not prompt.icon:
                prompt.icon = icon_for(prompt.text)
            out.append(prompt)
        return out

    def _write(self, rows: list[SavedPrompt]) -> None:
        tmp = self._path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps([r.to_dict() for r in rows], indent=1))
        tmp.replace(self._path())

    async def list(self) -> list[SavedPrompt]:
        async with self._lock:
            rows = self._read()
        return sorted(rows, key=lambda r: r.created_at, reverse=True)

    async def add(self, *, title: str, text: str, session_id: str = "") -> SavedPrompt:
        text = _clean(text, MAX_TEXT)
        if not text:
            raise ValueError("a saved prompt needs some text")
        prompt = SavedPrompt(
            id=uuid.uuid4().hex[:12],
            title=_clean(title, MAX_TITLE) or _title_from(text),
            text=text,
            icon=icon_for(f"{title} {text}"),
            session_id=session_id,
        )
        async with self._lock:
            rows = self._read()
            rows.append(prompt)
            self._write(rows)
        return prompt

    async def update(self, prompt_id: str, *, title: str, text: str) -> SavedPrompt | None:
        async with self._lock:
            rows = self._read()
            for row in rows:
                if row.id == prompt_id:
                    row.title = _clean(title, MAX_TITLE) or row.title
                    row.text = _clean(text, MAX_TEXT) or row.text
                    row.icon = icon_for(f"{row.title} {row.text}")
                    self._write(rows)
                    return row
        return None

    async def delete(self, prompt_id: str) -> bool:
        async with self._lock:
            rows = self._read()
            kept = [r for r in rows if r.id != prompt_id]
            if len(kept) == len(rows):
                return False
            self._write(kept)
        return True


def _title_from(text: str) -> str:
    """A title when nobody typed one: the opening words, which is what a
    person would have written anyway."""
    words = _clean(text, MAX_TEXT).split()
    title = " ".join(words[:7])
    return (title + "…") if len(words) > 7 else title


_library: PromptLibrary | None = None


def get_prompt_library() -> PromptLibrary:
    global _library
    if _library is None:
        _library = PromptLibrary()
    return _library


# ── sharpening ──────────────────────────────────────────────────────────
SHARPEN_SYSTEM = """You rewrite one prompt so it can be reused on its own.

You are given a prompt somebody typed during a conversation, and sometimes a
little of what was on screen around it. Mid-conversation prompts lean on that
context — "do the same for the other one", "now make it shorter" — and are
useless as a starting point for a new chat.

Rewrite it so it stands alone and says plainly what is wanted. Keep the
person's intent exactly: do not add requirements they did not ask for, do not
broaden the subject, and do not turn a specific request into a general one.
If the prompt already stands alone, return it close to unchanged.

Use the surrounding context only to resolve what the prompt refers to. Never
fold in other topics from the conversation.

Reply with JSON and nothing else:
{"title": "<= 6 words, plain, no quotes", "text": "the rewritten prompt"}"""


async def sharpen(text: str, context: str = "") -> dict:
    """A clearer version of one prompt, plus a title for it.

    Returns the original on any failure rather than raising: this sits behind
    an optional button in a dialog, and a model that is rate limited or
    talkative should leave the person with the prompt they already had.
    """
    from compass.common.gateway.azure_client import get_model_client

    text = _clean(text, MAX_TEXT)
    if not text:
        return {"title": "", "text": ""}

    ask = f"The prompt to rewrite:\n{text}"
    if context.strip():
        ask += (f"\n\nWhat was on screen around it, for reference only:\n"
                f"{_clean(context, 2000)}")

    try:
        collected = ""
        async for item in get_model_client().stream_chat(
            [{"role": "system", "content": SHARPEN_SYSTEM},
             {"role": "user", "content": ask}],
            max_output_tokens=800,
            effort="minimal",
        ):
            collected += getattr(item, "text", "") or ""
        parsed = _parse(collected)
        if parsed:
            return parsed
        logger.info("prompt sharpening returned nothing usable")
    except Exception:  # noqa: BLE001 — an optional nicety must not fail a save
        logger.exception("prompt sharpening failed")
    return {"title": _title_from(text), "text": text}


def _parse(raw: str) -> dict | None:
    """The model's JSON, from a reply that may be wrapped in a fence."""
    body = (raw or "").strip()
    if "```" in body:
        parts = body.split("```")
        body = max(parts, key=len)
        if body.lstrip().startswith("json"):
            body = body.lstrip()[4:]
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(body[start:end + 1])
    except json.JSONDecodeError:
        return None
    text = _clean(str(data.get("text") or ""), MAX_TEXT)
    if not text:
        return None
    return {"title": _clean(str(data.get("title") or ""), MAX_TITLE) or _title_from(text),
            "text": text}
