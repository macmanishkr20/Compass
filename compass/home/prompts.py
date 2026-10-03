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
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from compass.common.config import get_settings
from compass.common.persistence.catalog import Collection

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
    #: Whose library this is in. A saved prompt is a private note in the
    #: person's own words — more so than most records here, since people
    #: write them about their own work and their own day — so unlike the
    #: other stores this one does not fail open on an empty owner. See the
    #: note on `list`.
    owner: str = ""

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


#: The saved prompts, as one collection. The file keeps its name and its
#: shape, so a local install notices nothing; a cosmos install gets the same
#: documents in the shared `catalog` container.
def _prompts() -> Collection:
    global _collection
    if _collection is None:
        _collection = Collection("prompt", "prompts.json", shape="list")
    return _collection


_collection: Collection | None = None


class PromptLibrary:
    """Saved prompts, newest first. Local JSON or Cosmos — see persistence."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    def _folder(self) -> Path:
        settings = get_settings()
        folder = settings.workspace_root / settings.data_dir
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _order_path(self, owner: str = "") -> Path:
        """Where this person's arrangement is kept.

        One file per person. It was one file for the box, so dragging a row
        in one account rearranged the next account's list — the arrangement
        is as personal as the prompts it arranges. The unsuffixed name is
        kept for the empty owner so an install that never had accounts reads
        the file it already wrote.
        """
        if not owner:
            return self._folder() / "prompt_order.json"
        # Hashed rather than spelled out: an owner is an email address, and
        # that is somebody's identity sitting in a filename on disk.
        tag = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:16]
        return self._folder() / f"prompt_order.{tag}.json"

    def _clean(self, rows: list[dict]) -> list[SavedPrompt]:
        """Turn stored rows into prompts, dropping what cannot be one."""
        known = {f.name for f in fields(SavedPrompt)}
        out: list[SavedPrompt] = []
        for row in rows:
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

    async def _read(self) -> list[SavedPrompt]:
        return self._clean(await _prompts().all())

    async def list(self, owner: str = "") -> list[SavedPrompt]:
        """This person's prompts, newest first.

        An exact match, deliberately, where the rest of Compass treats an
        empty owner as legacy and shows it to everybody. A prompt is the one
        thing here somebody writes *about themselves* — "ask how I'm doing
        today", in their own voice — and showing one person's to the next is
        a different kind of wrong from showing them a shared pipeline. The
        cost of being strict is that prompts saved before owners existed stop
        appearing until `scripts/claim_unowned_prompts.py` names them; the
        cost of being lax is that they appear for strangers.
        """
        rows = [r for r in await self._read() if (r.owner or "") == (owner or "")]
        return sorted(rows, key=lambda r: r.created_at, reverse=True)

    async def add(self, *, title: str, text: str, session_id: str = "",
                  owner: str = "") -> SavedPrompt:
        text = _clean(text, MAX_TEXT)
        if not text:
            raise ValueError("a saved prompt needs some text")
        prompt = SavedPrompt(
            id=uuid.uuid4().hex[:12],
            title=_clean(title, MAX_TITLE) or _title_from(text),
            text=text,
            icon=icon_for(f"{title} {text}"),
            session_id=session_id,
            owner=owner,
        )
        await _prompts().put(prompt.to_dict())
        return prompt

    async def update(self, prompt_id: str, *, title: str, text: str,
                     owner: str = "") -> SavedPrompt | None:
        for row in await self._read():
            # Not theirs reads as not there, so editing by id tells a
            # stranger nothing about whether the id exists.
            if row.id != prompt_id or (row.owner or "") != (owner or ""):
                continue
            row.title = _clean(title, MAX_TITLE) or row.title
            row.text = _clean(text, MAX_TEXT) or row.text
            row.icon = icon_for(f"{row.title} {row.text}")
            await _prompts().put(row.to_dict())
            return row
        return None

    async def delete(self, prompt_id: str, owner: str = "") -> bool:
        mine = {r.id for r in await self._read() if (r.owner or "") == (owner or "")}
        if prompt_id not in mine:
            return False
        return await _prompts().remove(prompt_id)

    # ── the order they are shown in ─────────────────────────────────────
    #
    # Kept apart from the prompts themselves, as a list of keys, because the
    # arrangement covers rows this file knows nothing about: the built-in
    # starters live in the client and are draggable alongside saved ones. A
    # key here is a saved prompt's id or a starter's name, and the library
    # does not care which — it stores the sequence and lets the client say
    # what the names mean. Anything it cannot match is skipped when the list
    # is drawn, so a deleted prompt or a retired starter leaves no hole.

    def _read_order(self, owner: str = "") -> list[str]:
        path = self._order_path(owner)
        if not path.exists():
            return []
        try:
            keys = json.loads(path.read_text() or "[]")
        except (OSError, json.JSONDecodeError):
            logger.warning("prompt order unreadable at %s", path)
            return []
        return _clean_keys(keys)

    async def order(self, owner: str = "") -> list[str]:
        async with self._lock:
            return self._read_order(owner)

    async def set_order(self, keys: list[str], owner: str = "") -> list[str]:
        clean = _clean_keys(keys)
        async with self._lock:
            path = self._order_path(owner)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(clean, indent=1))
            tmp.replace(path)
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
            # `low`, not `minimal`, because the two model families accept
            # overlapping but different ladders and this call is fixed at
            # whatever is deployed: gpt-5 takes minimal…high, gpt-6-astra
            # takes low…max and rejects `minimal` with a 400. `low` is the
            # weakest level both agree on, and it still means do not
            # deliberate — which is all a rewrite of one prompt needs.
            effort="low",
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


#: Rewriting one prompt on its own terms. Deliberately not SHARPEN_SYSTEM:
#: that one selects a thread out of a conversation and merges it, which is
#: the right job when a prompt is first saved and the wrong one afterwards.
#: A prompt already in the library has no conversation behind it any more —
#: the only material is the prompt itself.
REFINE_SYSTEM = """You improve a single saved prompt so that it works better
when it is used to start a new conversation.

The prompt is the whole input. There is no conversation to draw on and
nothing to merge. Work only with what is in front of you.

WHAT TO DO:

  * Keep what the person is asking for exactly as they meant it. This is the
    one rule that outranks the others. A clearer prompt that asks for
    something different is a failure, however well written.
  * Make it self-contained, so it reads correctly with no prior context.
  * Say what good output looks like when the prompt implies it and does not
    say it: the form (a list, a table, a draft, a short answer), the
    audience, the tone, the length. Add these only when they are genuinely
    implied — inventing requirements is changing the request.
  * Keep the person's voice. A prompt written in the first person about
    their own day stays in the first person. Do not make it corporate, do
    not pad it, do not add flattery or preamble.
  * Keep it tight. A prompt that has doubled in length is usually worse.
    Most good prompts here are one to four sentences.

WHAT NOT TO DO:

  * Do not answer the prompt. You are rewriting the request, not fulfilling
    it.
  * Do not add placeholders like [insert topic] unless the original already
    had one.
  * Do not add a greeting, a sign-off, or "please".
  * If the prompt is already clear, self-contained and specific, return it
    essentially unchanged. Saying so with a near-identical answer is a
    correct outcome; changing it for the sake of changing it is not.

Be careful not to read that last rule as a reason to do nothing. A vague
prompt is not a clear one, and leaving it vague is the common failure here.
Where the subject is fixed but the shape of the answer is not, settling the
shape is exactly the improvement being asked for — that is not inventing a
requirement, it is spelling out what was already implied.

Two worked examples.

  Vague in, specific out:
    "write something about our quarter"
  becomes
    "Write a short summary of how our last quarter went. Cover what we
    shipped, how we did against the targets we set, and the one or two
    things that most need attention next quarter. Keep it to a few
    paragraphs, plain and factual, for an internal audience."

  Already good, so barely touched:
    "Ask how I'm doing today and offer to either listen while I vent or
    help me tackle one small nagging task."
  becomes essentially itself. It is specific, self-contained, and in the
  person's own voice. Tightening the wording slightly is fine; restructuring
  it, formalising it, or bolting requirements onto it is not.

Also give it a short title — at most a few words, naming what the prompt is
for, in the person's own register.

MARK WHAT YOU CHANGED. In the rewritten prompt, wrap each phrase you added
or materially altered in ‹ and ›. Mark whole phrases, not single words, and
mark only what genuinely changed — a rewrite where everything is marked
tells the reader nothing. If you changed nothing of substance, mark nothing.

Say what you did in `note`: a few words per change, separated by " · ", in
the present tense and about the prompt rather than about yourself. "Split
the two offers into a clear choice · added a wait-for-answer instruction",
not "I have restructured your prompt".

Answer with JSON only, and nothing else:

{"title": "...", "text": "...", "note": "..."}
"""

#: What each variant asks for on top of the rules above. The person picks
#: one from the review bar; `clearer` is what the first run does.
REFINE_VARIANTS: dict[str, str] = {
    "clearer": (
        "This run: make it CLEARER. Resolve what is vague, put the steps in "
        "the order they happen, and make sure each sentence does one job."
    ),
    "shorter": (
        "This run: make it SHORTER. Same request, fewer words — cut "
        "repetition, hedging and anything the model would infer anyway. Do "
        "not drop a requirement to save words; losing part of the ask is a "
        "failure, not a shorter prompt."
    ),
    "specific": (
        "This run: make it MORE SPECIFIC. Name the form of the answer, the "
        "steps to follow, and the failure modes to avoid, as far as the "
        "prompt already implies them. This is the one variant where being "
        "longer is usually right."
    ),
}

#: The span markers the model is asked for, and the pattern that finds them.
#: Chosen because they are vanishingly rare in real prose, so a prompt that
#: happens to contain one is not a case worth designing around.
_MARK_OPEN, _MARK_CLOSE = "‹", "›"
_MARKED = re.compile(f"{_MARK_OPEN}([^{_MARK_CLOSE}]*){_MARK_CLOSE}")


def _unmark(text: str) -> str:
    """The prompt as it will be stored: the markers taken back out."""
    return text.replace(_MARK_OPEN, "").replace(_MARK_CLOSE, "")


def _unchanged(text: str, title: str) -> dict:
    """What refining answers with when it could not do better: what it was
    given, marked as nothing changed."""
    return {"title": _clean(title, MAX_TITLE) or _title_from(text),
            "text": text, "marked": text, "note": "", "changes": 0}


def _note_from(raw: str) -> str:
    """The `note` field, read from the same reply `_parse` read.

    Parsed here rather than inside `_parse`, which is shared with sharpening
    and returns its dict straight to the caller — a key added there would
    change what that endpoint answers with.
    """
    body = (raw or "").strip()
    if "```" in body:
        body = max(body.split("```"), key=len)
        if body.lstrip().startswith("json"):
            body = body.lstrip()[4:]
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return ""
    try:
        return _clean(str(json.loads(body[start:end + 1]).get("note") or ""), 240)
    except json.JSONDecodeError:
        return ""


async def refine(text: str, title: str = "", variant: str = "clearer") -> dict:
    """A better-written version of one saved prompt, and a title for it.

    The counterpart to `sharpen`, for a prompt that is already in the
    library. Sharpening reaches into the conversation a prompt came from;
    once it is saved that conversation is no longer the point, and on a
    prompt opened from the library it is usually not even there. This reads
    the prompt alone.

    `variant` is which way to pull: clearer, shorter, or more specific. The
    editor offers all three after the first run, because "better" is not one
    direction and the person is the one who knows which they wanted.

    Returns `text` ready to store, `marked` with the changed phrases wrapped
    for the editor to highlight, a `note` saying what was done and a count
    of the marked spans.

    Returns the original on any failure, for the same reason `sharpen` does:
    it sits behind an optional button, and somebody who asked for a tidy-up
    should be left with their own words rather than an error.
    """
    from compass.common.gateway.azure_client import get_model_client

    text = _clean(text, MAX_TEXT)
    if not text:
        return {"title": _clean(title, MAX_TITLE), "text": "",
                "marked": "", "note": "", "changes": 0}

    ask = f"The saved prompt to improve:\n{text}"
    if title.strip():
        ask += f"\n\nIts current title: {_clean(title, MAX_TITLE)}"
    ask += "\n\n" + REFINE_VARIANTS.get(variant, REFINE_VARIANTS["clearer"])

    try:
        collected = ""
        async for item in get_model_client().stream_chat(
            [{"role": "system", "content": REFINE_SYSTEM},
             {"role": "user", "content": ask}],
            max_output_tokens=900,
            # The same reasoning as in `sharpen`: the weakest level both
            # deployed model families accept.
            effort="low",
        ):
            collected += getattr(item, "text", "") or ""
        parsed = _parse(collected)
        if parsed:
            parsed.pop("used", None)
            marked = _strip_opener(parsed["text"])
            clean = _unmark(marked)
            if not is_opener(clean):
                spans = [s for s in _MARKED.findall(marked) if s.strip()]
                return {
                    "title": _unmark(parsed["title"]),
                    "text": clean,
                    "marked": marked,
                    "note": _note_from(collected),
                    "changes": len(spans),
                }
            logger.info("refining returned an opener; keeping the original")
        else:
            logger.info("prompt refining returned nothing usable")
    except Exception:  # noqa: BLE001 — an optional nicety must not fail an edit
        logger.exception("prompt refining failed")
    return _unchanged(text, title)


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


# ══════════════════════════════════════════════════════════════════════════
# Composing a saved prompt out of a conversation, with its working shown.
#
# A third agent, beside `sharpen` and `refine`, and it exists because the
# save dialog now shows what was read rather than only what came out.
# Sharpening answers with the merged prompt and the numbers it used; that is
# the right answer for the endpoint it serves and it is left exactly as it
# is. This one is asked a different question — for every message, in or out,
# why, and what it contributed — so the dialog can list them, let somebody
# disagree, and re-join the lines without asking a model again.
# ══════════════════════════════════════════════════════════════════════════

COMPOSE_SYSTEM = """You read one conversation and decide which of the
person's own messages belong in a single saved prompt, then say what each
one contributes.

The person's messages are numbered. One is marked <<SAVED>> — that is the
one they chose to keep, and it is always in.

FOR EVERY NUMBERED MESSAGE, decide `keep`:

  in  — it is part of the same request as the saved one: it refines it, adds
        a requirement, narrows a constraint, adds content or a rule, or is
        the request a bare opener was leading up to.
  out — it is not. The usual reasons, and the words to use for them:
          "a question, not an instruction"  — they asked you something
          "a different task"                — a separate request entirely
          "no instruction in it"            — a greeting, thanks, an ack
          "already covered"                 — it repeats one that is in

A message belongs by subject, not by position. A thread can pause and
resume: if 1 and 2 are about a poster, 3 to 7 about something else, and 8
returns to the poster, then 8 is in and 3 to 7 are out.

FOR EVERY MESSAGE YOU KEEP, write `line`: the one sentence it contributes to
the finished prompt, in the imperative, as an instruction to Compass. Not a
quote of what they typed — the instruction inside it. "Use the darker gold,
not the yellow one." contributes "Use the darker EY gold rather than the
yellow." Read in order, the kept lines must read as one coherent prompt,
with no repetition between them and nothing that contradicts another.

FOR EVERY MESSAGE, write `tag`: a few words saying why, in lower case. For a
kept one say what it does — "format", "adds content", "narrows a
constraint", "adds a rule", "refines the output". For the saved one, "the
one you saved". For a dropped one, use the wording above.

Also give `title`: a few words naming what the whole prompt is for.

Answer with JSON only, and nothing else:

{"title": "...",
 "parts": [{"n": 1, "keep": true, "tag": "...", "line": "..."}, ...]}

Every numbered message gets an entry. A dropped one has no `line`.
"""


async def compose(turns: list[dict], selected: str) -> dict:
    """Build a saved prompt from a conversation and show the working.

    Returns the title, the merged text, and one part per message the person
    wrote: its number, whether it is in, why, and the line it contributes.
    The dialog re-joins the kept lines itself when somebody unticks one, so
    disagreeing with the model costs nothing and asks it nothing.

    Returns the selected message alone on any failure — the same promise the
    other two make, for the same reason.
    """
    from compass.common.gateway.azure_client import get_model_client

    selected = _clean(selected, MAX_TEXT)
    asked = [t for t in (turns or []) if (t.get("role") == "user")
             and str(t.get("text") or "").strip()]
    if not selected or not asked:
        return {"title": _title_from(selected), "text": selected, "parts": []}

    body, anchor, count = _transcript(turns, selected)
    ask = (f"The conversation, oldest first:\n{body}\n\n"
           f"Message {anchor} is the one they saved. Decide every message, "
           f"and write the line each kept one contributes.")

    try:
        collected = ""
        async for item in get_model_client().stream_chat(
            [{"role": "system", "content": COMPOSE_SYSTEM},
             {"role": "user", "content": ask}],
            max_output_tokens=1600,
            effort="low",
        ):
            collected += getattr(item, "text", "") or ""
        parsed = _compose_parse(collected, anchor, count)
        if parsed["parts"]:
            return parsed
        logger.info("composing returned nothing usable")
    except Exception:  # noqa: BLE001 — an optional nicety must not fail a save
        logger.exception("composing a saved prompt failed")
    return {"title": _title_from(selected), "text": selected, "parts": []}


def _compose_parse(raw: str, anchor: int, count: int) -> dict:
    """The model's JSON, validated against the conversation actually sent.

    A number it invented, or one outside the range, is dropped rather than
    shown: the tray is a claim about specific messages, and a row pointing at
    a message that was never there would make the whole list untrustworthy.
    """
    body = (raw or "").strip()
    if "```" in body:
        body = max(body.split("```"), key=len)
        if body.lstrip().startswith("json"):
            body = body.lstrip()[4:]
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return {"title": "", "text": "", "parts": []}
    try:
        data = json.loads(body[start:end + 1])
    except json.JSONDecodeError:
        return {"title": "", "text": "", "parts": []}

    seen: set[int] = set()
    parts: list[dict] = []
    for row in data.get("parts") or []:
        if not isinstance(row, dict):
            continue
        try:
            n = int(row.get("n"))
        except (TypeError, ValueError):
            continue
        if n < 1 or n > count or n in seen:
            continue
        seen.add(n)
        line = _clean(str(row.get("line") or ""), MAX_TEXT)
        # The saved message is always in, whatever the model decided, and it
        # must carry a line or the merged prompt could come out empty.
        keep = bool(row.get("keep")) or n == anchor
        if keep and not line:
            keep = n == anchor and bool(line)
        parts.append({
            "n": n,
            "keep": keep and bool(line),
            "tag": _clean(str(row.get("tag") or ""), 60),
            "line": line,
        })
    parts.sort(key=lambda p: p["n"])
    text = " ".join(p["line"] for p in parts if p["keep"])
    return {
        "title": _clean(str(data.get("title") or ""), MAX_TITLE) or _title_from(text),
        "text": text,
        "parts": parts,
        "anchor": anchor,
    }
