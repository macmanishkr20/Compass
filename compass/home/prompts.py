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

#: An arrangement is a list of keys: a saved prompt's id, or the name of one
#: of the client's built-in starters. Both are short, and neither the count
#: nor the length is anything the browser needs to argue about.
MAX_KEY = 64
MAX_ORDER = 500


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


def _clean_keys(keys: object) -> list[str]:
    """The stored arrangement, with anything that is not a key dropped.

    Deduplicated because a key appearing twice would put one row in two
    places, and capped because this is written from the browser and a list
    of keys has no business being longer than a library.
    """
    if not isinstance(keys, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for key in keys:
        if not isinstance(key, str):
            continue
        key = _clean(key, MAX_KEY)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= MAX_ORDER:
            break
    return out


class PromptLibrary:
    """Saved prompts on disk, newest first."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    def _folder(self) -> Path:
        settings = get_settings()
        folder = settings.workspace_root / settings.data_dir
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _path(self) -> Path:
        return self._folder() / "prompts.json"

    def _order_path(self) -> Path:
        return self._folder() / "prompt_order.json"

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

    # ── the order they are shown in ─────────────────────────────────────
    #
    # Kept apart from the prompts themselves, as a list of keys, because the
    # arrangement covers rows this file knows nothing about: the built-in
    # starters live in the client and are draggable alongside saved ones. A
    # key here is a saved prompt's id or a starter's name, and the library
    # does not care which — it stores the sequence and lets the client say
    # what the names mean. Anything it cannot match is skipped when the list
    # is drawn, so a deleted prompt or a retired starter leaves no hole.

    def _read_order(self) -> list[str]:
        path = self._order_path()
        if not path.exists():
            return []
        try:
            keys = json.loads(path.read_text() or "[]")
        except (OSError, json.JSONDecodeError):
            logger.warning("prompt order unreadable at %s", path)
            return []
        return _clean_keys(keys)

    async def order(self) -> list[str]:
        async with self._lock:
            return self._read_order()

    async def set_order(self, keys: list[str]) -> list[str]:
        clean = _clean_keys(keys)
        async with self._lock:
            tmp = self._order_path().with_suffix(".json.tmp")
            tmp.write_text(json.dumps(clean, indent=1))
            tmp.replace(self._order_path())
        return clean


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
#: How much of a conversation to send. A long session is mostly the model
#: talking; what matters here is the person's own turns, so those are kept in
#: full and the replies are summarised down to a line apiece.
MAX_TURNS = 40
MAX_TURN_CHARS = 700

SHARPEN_SYSTEM = """You select the prompts that belong to one thread of a
conversation and merge them into a single prompt that can start a new chat.

The person's prompts are numbered. One is marked <<SELECTED>>. That one is
the anchor, and your job is in two parts.

FIRST, decide which numbered prompts belong to the same thread as the anchor.

A thread is one line of enquiry: what the person was trying to find out or
get done around the anchor. It is not only a request and its refinements.
Ask yourself what a reader would say the person was doing across these
turns, and include every prompt that is part of it. A prompt belongs when:

  * it refines the anchor — adds a requirement, narrows it, corrects it;
  * it continues the same subject — a further question about the topic the
    anchor opened, or about something that topic leads straight into. "What
    is GenAI?" followed by "are agents good for complex tasks?" is one
    person working through one subject, not two subjects. Neither one
    develops the other as a request, and they still belong together;
  * it supplies something the anchor needed — a place, a name, a file, a
    choice you asked them for. "What is today's weather?" followed by
    "Bangalore 562125" is one thread: the second is the answer that
    completes the first;
  * it is the request a bare opener was leading up to.

A prompt does not belong merely because it sits next to the anchor.
Conversations change subject constantly, and position proves nothing. Judge
by subject matter.

  * A thread can pause and resume. If prompts 1 and 2 are about a parser,
    3 to 7 are about something else, and 8 returns to the parser, then 8
    belongs and 3 to 7 do not.
  * A thread can start before the anchor as well as after it.
  * If nothing belongs, the anchor alone is the thread. This is a normal and
    common answer. Do not reach for a different subject to pad it out.

One exception to that last rule, and it is not optional. Some prompts carry
no subject of their own and cannot stand alone, because there is nothing in
them to save:

  * greetings and acknowledgements — "hi", "hello", "ok", "thanks", "are you
    there", "can you help";
  * bare answers — a place, a number, a name, a yes or no, given because you
    asked for it: "Bangalore 562125", "the second one", "yes, do that".

When the anchor is one of these, its meaning lives in the turns around it.
Its thread is the question or request it belongs to, and you must include
that. Returning a bare greeting or a lone postcode as the saved prompt is
always wrong.

SECOND, write one prompt covering everything the selected thread asked for,
as if the person had known at the start what they wanted.

You are merging, not elaborating. Every requirement must be one they stated
in a prompt you selected.

  * Do not turn a question into a specification. If the thread is one short
    prompt, your prompt is that prompt, give or take a word. Never add
    sub-topics, examples, deliverables or "include X and Y" lists.
  * A thread of questions becomes one prompt that asks all of them, in their
    own words. "What is GenAI?" with "are agents good for complex tasks?"
    becomes a prompt asking both — not an essay brief about either.
  * Your prompt should be about as long as the selected prompts put
    together, and no longer. If it is longer, you have invented something.
  * Do not carry over the assistant's suggestions — a library it proposed,
    an approach it offered — unless the person explicitly took them up.
  * Write it as an instruction in their voice, never referring to the
    conversation ("as discussed", "the one above").

Reply with JSON and nothing else:
{"used": [numbers of the prompts you merged, including the anchor],
 "title": "<= 6 words, plain, no quotes",
 "text": "the consolidated prompt"}"""


def _transcript(turns: list[dict], selected: str) -> tuple[str, int, int]:
    """The conversation as the model sees it.

    The person's prompts are numbered so the model can say which ones it
    merged — a claim that can be checked, rather than a rewrite that has to
    be taken on trust. Assistant turns are unnumbered and cut short: they are
    here so a follow-up like "make it shorter" has something to point at, not
    so their content can be folded into somebody's request.

    Returns the transcript, the anchor's number, and how many prompts there
    are, because the caller has to validate what comes back against both.
    """
    lines: list[str] = []
    number = 0
    anchor = 0
    want = _clean(selected, MAX_TURN_CHARS)[:120]
    for turn in turns[-MAX_TURNS:]:
        is_person = (turn.get("role") or "") == "user"
        body = _clean(str(turn.get("text") or ""), MAX_TURN_CHARS)
        if not body:
            continue
        if not is_person:
            lines.append(f"    Assistant: {body[:200]}")
            continue
        number += 1
        # The first exact match anchors it: a prompt repeated verbatim should
        # attach to the one they clicked, which is the earliest.
        if not anchor and body[:120] == want:
            anchor = number
            lines.append(f"[{number}] <<SELECTED>> Person: {body}")
        else:
            lines.append(f"[{number}] Person: {body}")
    if not anchor:
        # The selection is not in the turns we were given — an edited message,
        # or a client sending a trimmed history. It still gets a number so the
        # model has something to anchor on.
        number += 1
        anchor = number
        lines.append(f"[{number}] <<SELECTED>> Person: {_clean(selected, MAX_TURN_CHARS)}")
    return "\n".join(lines), anchor, number


def _used_from(raw: object, anchor: int, count: int) -> list[int]:
    """The prompt numbers the model says it merged, made safe.

    Anything outside the conversation is dropped, and the anchor is put back
    if it was left out: a thread that does not contain the prompt somebody
    picked is not that prompt's thread, whatever the model replied.
    """
    out: list[int] = []
    if isinstance(raw, list):
        for item in raw:
            try:
                n = int(item)
            except (TypeError, ValueError):
                continue
            if 1 <= n <= count and n not in out:
                out.append(n)
    if anchor not in out:
        out.append(anchor)
    return sorted(out)


#: Openers and acknowledgements. A closed set, so this is decided in code
#: rather than left to the model to notice: the instruction not to return a
#: greeting is one rule among many in a long prompt, and measured over five
#: runs it was obeyed unreliably — four replies carried "Hi." into the saved
#: prompt and one returned the greeting alone. What can be detected should
#: not be hoped for.
_GREETINGS = frozenset({
    "hi", "hii", "hiii", "hello", "helo", "hey", "heya", "yo", "hiya",
    "good morning", "good afternoon", "good evening", "morning", "evening",
    "ok", "okay", "k", "kk", "sure", "right", "cool", "nice", "great",
    "thanks", "thank you", "thankyou", "ty", "cheers", "ta",
    "please", "pls", "yes", "yeah", "yep", "no", "nope",
    "are you there", "you there", "can you help", "help", "hi there",
    "hello there", "test", "testing",
})


def is_opener(text: str) -> bool:
    """Whether a prompt is a greeting or acknowledgement and nothing else.

    Deliberately narrow: it only fires on a short phrase that is entirely one
    of the known openers once punctuation is stripped. "hi, can you read this
    file" is a real request that happens to start with a greeting, and must
    not be caught.
    """
    stripped = " ".join((text or "").lower().replace("!", " ").replace(".", " ")
                        .replace(",", " ").replace("?", " ").split())
    return bool(stripped) and stripped in _GREETINGS


async def sharpen(text: str, context: str = "",
                  turns: list[dict] | None = None) -> dict:
    """One prompt merged from the selected prompt's thread.

    Returns the original on any failure rather than raising: this sits behind
    an optional button in a dialog, and a model that is rate limited or
    talkative should leave the person with the prompt they already had.
    """
    from compass.common.gateway.azure_client import get_model_client

    text = _clean(text, MAX_TEXT)
    if not text:
        return {"title": "", "text": "", "used": []}

    anchor, count = 0, 0
    if turns:
        body, anchor, count = _transcript(turns, text)
        ask = (f"The conversation, oldest first:\n{body}\n\n"
               f"The anchor is prompt {anchor}. Select its thread and merge it.")
        if is_opener(text):
            # Said here, about this conversation, rather than relied upon from
            # the system prompt — a specific instruction about the case in
            # hand is obeyed where a general one is not.
            ask += (f"\n\nNote: prompt {anchor} is a greeting. It asks for "
                    f"nothing, so it cannot be the saved prompt and none of "
                    f"its words belong in your answer — do not open with "
                    f"\"Hi\" or \"Hello\". Its thread is the first real "
                    f"question or request the person made after it, together "
                    f"with anything continuing that. Merge those.")
    elif context.strip():
        # The older shape, kept so an out-of-date client still gets a rewrite.
        ask = (f"The prompt to rewrite:\n{text}\n\n"
               f"Nearby conversation, for reference only:\n{_clean(context, 2000)}")
    else:
        ask = f"The prompt to rewrite:\n{text}"

    try:
        collected = ""
        async for item in get_model_client().stream_chat(
            [{"role": "system", "content": SHARPEN_SYSTEM},
             {"role": "user", "content": ask}],
            max_output_tokens=900,
            effort="minimal",
        ):
            collected += getattr(item, "text", "") or ""
        parsed = _parse(collected)
        if parsed:
            parsed["used"] = _used_from(parsed.get("used"), anchor, count) if count else []
            parsed["text"] = _strip_opener(parsed["text"])
            if is_opener(parsed["text"]):
                # A greeting in, a greeting out: the one answer that is always
                # wrong. Better to hand back the original and let the person
                # write it than to save a prompt that asks for nothing.
                logger.info("sharpening returned an opener; keeping the original")
            else:
                return parsed
        else:
            logger.info("prompt sharpening returned nothing usable")
    except Exception:  # noqa: BLE001 — an optional nicety must not fail a save
        logger.exception("prompt sharpening failed")
    return {"title": _title_from(text), "text": text,
            "used": [anchor] if anchor else []}


def _strip_opener(text: str) -> str:
    """Drop a greeting the model put at the front of an otherwise good prompt.

    "Hi. What is meant by GenAI?" is the right thread with a word in it that
    asks for nothing. Only the first clause is considered, and only when it
    is entirely an opener, so a prompt that genuinely begins "Hello world" is
    left alone.
    """
    for sep in (". ", "! ", ", ", " - ", " — "):
        head, found, tail = text.partition(sep)
        if found and tail.strip() and is_opener(head):
            return tail.strip()
    return text


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
            "text": text,
            # Raw here; `sharpen` validates it against the conversation it
            # actually sent, which is the only place that knows the range.
            "used": data.get("used")}
