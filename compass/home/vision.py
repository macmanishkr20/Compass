"""Looking at the photographs once, so an editor can work from them later.

A storyboard is a series of decisions about *which* picture goes where: the
idol opens, the procession builds, the family portrait closes. Compass was
making those decisions from a list of filenames — `0012-IMG_4021.JPG` — which
is no basis for deciding anything. On the turn the photos arrive the model can
see them, but a reel is almost always asked for afterwards ("make another
version, 16:9"), and by then the pictures are gone from the conversation and
only their names remain.

So each photograph is described once, on the way in, and the description is
kept beside it. One short line: what is in it, and a word for where it belongs
in a festival film. That line is what the catalogue shows from then on, and it
is the difference between an order chosen by theme and an order chosen by
filename.

Deliberately cheap and deliberately optional. Descriptions are a convenience:
a failed pass leaves the pictures undescribed and everything still works, the
way it did before this module existed.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from pathlib import Path

logger = logging.getLogger("compass.home.vision")

#: How many pictures go into one request. Large enough that twenty photos is a
#: handful of calls, small enough that one failure does not cost the lot.
BATCH = 6
#: Never describe more than this in a single turn. Somebody dropping two
#: hundred photos in at once should not wait for all of them.
MAX_PER_TURN = 30
#: Room for the answer. Cataloguing is not a reasoning task, but the model
#: behind a utility call may be one anyway: at 900 tokens gpt-5 spent 896 of
#: them thinking and had nothing left to answer with, and six descriptions
#: came back as none. Minimal effort and a generous ceiling, because the
#: failure mode is silent and the tokens are cheap.
_MAX_TOKENS = 3_000

#: The longest edge sent to the model. A description needs the gist, not the
#: grain, and a 4000px photo costs many times more to look at than a 512px one.
THUMB_EDGE = 512

_PROMPT = (
    "You are cataloguing photographs so an editor can cut a short film from "
    "them later. For each image, in order, give:\n"
    "  note  — one line, at most 14 words, saying what is actually in it: who "
    "or what, and what is happening. Concrete, not poetic.\n"
    "  beat  — one word for where it belongs in a celebration film, from: "
    "arrival, ritual, idol, procession, dance, music, food, crowd, portrait, "
    "children, decoration, farewell, other.\n"
    "Answer for every image you are given, in the same order."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "images": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "note": {"type": "string"},
                    "beat": {"type": "string"},
                },
                "required": ["note", "beat"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["images"],
    "additionalProperties": False,
}


def _thumbnail(path: Path) -> str | None:
    """A small JPEG of `path` as a data URL, or None if it cannot be read."""
    try:
        from PIL import Image

        with Image.open(path) as image:
            image = image.convert("RGB")
            image.thumbnail((THUMB_EDGE, THUMB_EDGE))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=70, optimize=True)
        return ("data:image/jpeg;base64,"
                + base64.b64encode(buffer.getvalue()).decode())
    except Exception as err:  # noqa: BLE001 — an unreadable photo is skipped
        logger.warning("vision: could not read %s: %s", path.name, err)
        return None


async def _describe_batch(client, files: list) -> dict[str, dict]:
    """One request, several pictures. Returns {id: {note, beat}}."""
    import json

    thumbs, kept = [], []
    for file in files:
        thumb = _thumbnail(file.path)
        if thumb:
            thumbs.append(thumb)
            kept.append(file)
    if not thumbs:
        return {}
    try:
        answer = await client.complete_utility(
            _PROMPT,
            "Describe these " + str(len(thumbs)) + " images, in order.",
            images=thumbs, max_tokens=_MAX_TOKENS, effort="minimal",
            schema=_SCHEMA, schema_name="catalogue")
        rows = (json.loads(answer) or {}).get("images") or []
    except Exception as err:  # noqa: BLE001 — descriptions are a convenience
        logger.warning("vision: could not describe a batch: %s", err)
        return {}

    out: dict[str, dict] = {}
    for file, row in zip(kept, rows):
        note = " ".join(str(row.get("note", "")).split())[:120]
        beat = " ".join(str(row.get("beat", "")).split()).lower()[:20]
        if note:
            out[file.id] = {"note": note, "beat": beat}
    return out


async def describe_new(session_id: str) -> int:
    """Describe this thread's pictures that have no description yet.

    Returns how many were described. Never raises: the catalogue simply keeps
    whatever it already had.
    """
    from compass.common.gateway.azure_client import get_model_client
    from compass.home import media

    pending = [f for f in media.listing(session_id)
               if f.kind == "image" and not f.note][:MAX_PER_TURN]
    if not pending:
        return 0

    client = get_model_client()
    batches = [pending[i:i + BATCH] for i in range(0, len(pending), BATCH)]
    # Concurrently: six photographs at a time is a couple of seconds, and
    # twenty photographs one batch after another is most of a minute.
    results = await asyncio.gather(
        *(_describe_batch(client, batch) for batch in batches),
        return_exceptions=True)

    described: dict[str, dict] = {}
    for result in results:
        if isinstance(result, dict):
            described.update(result)
    if described:
        media.annotate(session_id, described)
    return len(described)
