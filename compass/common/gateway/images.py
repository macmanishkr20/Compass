"""Drawing — one place that asks the image model for a picture.

Compass could not draw at all before this. Every surface that wanted a
picture either did without one or asked the text model to approximate it in
CSS, which is right for a chart and wrong for a photograph.

One client, one function, used by everything: the `generate_image` tool the
agents get, and the pass that fills a design's artwork in. The alternative —
each module calling the API itself — is how four slightly different retry
policies and four different ideas of where the bytes go come about.

Where the bytes go is deliberate: blob storage, under `generated/`, beside
the uploads in `compass-media`. A generated picture is media like any other,
it is too big for a document, and a data URI of a megabyte pasted into a
design is the thing `large_content` exists to stop.

A missing deployment is a first-class answer, not a crash. The image model
is a separate deployment from the chat model and will often not exist: every
caller asks `image_configured` before offering a picture, and this returns a
reason rather than raising when it is asked anyway.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from dataclasses import dataclass

from compass.common.config import get_settings

logger = logging.getLogger("compass.images")

#: Where generated pictures live inside `compass-media`, beside `chat/` for
#: uploads and `messages/` for oversized message content. Its own prefix so
#: clearing one thread's uploads can never reach another thread's artwork.
PREFIX = "generated"

#: What the API accepts. Anything else is corrected to the nearest, because a
#: model asked for "a wide banner" writes `1792x1024` as readily as the size
#: this deployment actually takes, and failing the whole picture over it
#: helps nobody.
SIZES = ("1024x1024", "1024x1536", "1536x1024")

#: Long enough for a high-quality render, which is tens of seconds, and short
#: enough that a turn does not sit on a dead socket.
TIMEOUT_SECONDS = 300.0


@dataclass
class Picture:
    """A generated image, or the reason there isn't one."""

    url: str = ""
    prompt: str = ""
    width: int = 0
    height: int = 0
    bytes: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.url)


def available() -> bool:
    """Whether anything should offer to draw. See `image_configured`."""
    return get_settings().azure.image_configured


def _normalised_size(size: str) -> str:
    if size in SIZES:
        return size
    try:
        w, h = (int(n) for n in size.lower().split("x", 1))
    except (ValueError, AttributeError):
        return SIZES[0]
    # Nearest by shape rather than by area: a request for a wide image should
    # come back wide, even when the numbers are not ones the API takes.
    if w > h * 1.2:
        return "1536x1024"
    if h > w * 1.2:
        return "1024x1536"
    return "1024x1024"


_client = None


def _get_client():
    """The image client. Reuses the main one when the credentials match, so
    an install with everything on one resource opens one connection."""
    global _client
    if _client is not None:
        return _client
    from openai import AsyncAzureOpenAI

    azure = get_settings().azure
    _client = AsyncAzureOpenAI(
        azure_endpoint=azure.image_endpoint_effective,
        api_key=azure.image_api_key_effective,
        api_version=azure.image_api_version,
        timeout=TIMEOUT_SECONDS,
    )
    return _client


def _reset_client() -> None:
    """Forget the client, for a process that changes the configuration under
    itself — the tests, and anything that re-reads settings."""
    global _client
    _client = None


async def draw(
    prompt: str,
    *,
    size: str = "",
    quality: str = "",
    transparent: bool = False,
) -> Picture:
    """Draw `prompt`, store it, and return where it went.

    Never raises. A picture is an enrichment everywhere it is used — a turn
    that cannot draw should say so and carry on, not fail — so the failure is
    returned as a sentence the caller can show or hand back to the model.
    """
    text = (prompt or "").strip()
    if not text:
        return Picture(error="no prompt was given")
    azure = get_settings().azure
    if not azure.image_configured:
        return Picture(
            error="image generation is not configured on this install "
            "(set AZURE_OPENAI_IMAGE_DEPLOYMENT to a deployed image model)"
        )

    want = _normalised_size(size or azure.image_size)
    kwargs: dict = {
        "model": azure.image_deployment,
        "prompt": text,
        "n": 1,
        "size": want,
        "quality": quality or azure.image_quality,
    }
    if transparent:
        # Only the image models that composite support this; asking for it
        # from one that does not is a 400, so it is opt-in.
        kwargs["background"] = "transparent"
        kwargs["output_format"] = "png"

    try:
        result = await _get_client().images.generate(**kwargs)
    except Exception as err:  # noqa: BLE001 — reported, never fatal
        logger.warning("image generation failed: %s", err)
        return Picture(error=_explain(err))

    data = getattr(result, "data", None) or []
    if not data:
        return Picture(error="the image model returned nothing")
    b64 = getattr(data[0], "b64_json", None)
    if not b64:
        # Some deployments answer with a URL instead of bytes.
        remote = getattr(data[0], "url", "") or ""
        if remote:
            return Picture(url=remote, prompt=text, error="")
        return Picture(error="the image model returned neither bytes nor a URL")

    try:
        raw = base64.b64decode(b64)
    except Exception:  # noqa: BLE001
        return Picture(error="the image model returned something unreadable")

    w, h = (int(n) for n in want.split("x"))
    url = await _store(raw, "png" if transparent else "png")
    if not url:
        return Picture(error="the picture was drawn but could not be stored")
    return Picture(url=url, prompt=text, width=w, height=h, bytes=len(raw))


def _explain(err: Exception) -> str:
    """The one line worth showing a person, out of an SDK exception."""
    status = getattr(err, "status_code", None)
    if status == 404:
        return (
            "the image deployment was not found on this resource — check "
            "AZURE_OPENAI_IMAGE_DEPLOYMENT names a deployment that exists"
        )
    if status == 400:
        # Usually the content filter, and saying so saves a retry loop.
        return f"the image model refused that prompt ({err})"[:200]
    if status == 429:
        return "the image model is rate limited; try again shortly"
    return str(err)[:200] or "the image model could not be reached"


# --------------------------------------------------------------------------- #
# Where the bytes go.
# --------------------------------------------------------------------------- #
def _put_sync(name: str, raw: bytes, content_type: str) -> None:
    from azure.storage.blob import ContentSettings

    from compass.common.persistence import blob

    blob.container("compass-media").upload_blob(
        name,
        raw,
        overwrite=True,
        max_concurrency=blob.CONCURRENCY,
        content_settings=ContentSettings(content_type=content_type),
    )


async def store_bytes(name: str, raw: bytes) -> bool:
    """Put arbitrary image bytes under `name`, for anything that makes a
    picture some other way — the screenshot tool, which has one already and
    only needs it to outlive the process. Same two places, same order."""
    ok = False
    try:
        await asyncio.to_thread(
            (_local_dir() / name.split("/", 1)[-1]).write_bytes, raw
        )
        ok = True
    except OSError as err:
        logger.warning("could not keep %s locally: %s", name, err)
    from compass.common.persistence import blob

    if blob.enabled():
        try:
            await asyncio.to_thread(_put_sync, name, raw, "image/png")
            ok = True
        except Exception as err:  # noqa: BLE001
            logger.warning("could not store %s: %s", name, err)
    return ok


async def _store(raw: bytes, ext: str) -> str:
    """Put the picture where a browser can fetch it. Returns a URL, or "".

    Blob when it is configured, and the local media directory otherwise, on
    the same terms as everything else here: a zero-config install still works,
    it just keeps its pictures on the disk it is running on.
    """
    name = f"{PREFIX}/{uuid.uuid4().hex}.{ext}"
    content_type = "image/png" if ext == "png" else f"image/{ext}"

    from compass.common.persistence import blob

    # Kept on disk as well as in blob, not instead of it. Blob is where it
    # lives — another machine, or this one after the disk is cleared, reads
    # it from there — but the bytes are already in hand here, and serving a
    # 2MB picture measured at 10.9s when it had to come back over the wire.
    # Writing it twice costs nothing and makes every later read local.
    local_ok = False
    try:
        await asyncio.to_thread((_local_dir() / name.split("/", 1)[-1]).write_bytes, raw)
        local_ok = True
    except OSError as err:
        logger.warning("could not keep a local copy of the generated image: %s", err)

    if blob.enabled():
        try:
            await asyncio.to_thread(_put_sync, name, raw, content_type)
            return f"/v1/media/{name}"
        except Exception as err:  # noqa: BLE001 — the local copy may still save it
            logger.error("could not store the generated image (%s); keeping it local", err)
    if local_ok:
        return f"/v1/media/{name}"

    try:
        target = _local_dir() / name.split("/", 1)[-1]
        await asyncio.to_thread(target.write_bytes, raw)
        return f"/v1/media/{name}"
    except OSError as err:
        logger.error("could not write the generated image: %s", err)
        return ""


def _local_dir():
    settings = get_settings()
    path = settings.workspace_root / settings.data_dir / "generated"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _get_sync(name: str) -> tuple[bytes, str]:
    from compass.common.persistence import blob

    stream = blob.container("compass-media").download_blob(
        name, max_concurrency=blob.CONCURRENCY
    )
    kind = getattr(getattr(stream, "properties", None), "content_settings", None)
    return stream.readall(), getattr(kind, "content_type", "") or "image/png"


async def fetch(name: str) -> tuple[bytes | None, str]:
    """One stored picture, for the route that serves it.

    Blob first and local disk second, regardless of which is configured now:
    an install that was local yesterday still has yesterday's pictures on the
    disk, and a broken image in an old design helps nobody.
    """
    from compass.common.persistence import blob

    # Local first, which inverts the order everything else here uses. For a
    # design's history or a message's content the blob is the only copy worth
    # trusting; for a picture, the local file and the blob are the same
    # immutable bytes under an id nothing reuses, and the local one is there
    # in a millisecond rather than eleven seconds.
    try:
        path = _local_dir() / name.split("/", 1)[-1]
        if path.is_file():
            return await asyncio.to_thread(path.read_bytes), "image/png"
    except OSError as err:
        logger.warning("could not read the local generated image %s: %s", name, err)
    if blob.enabled():
        try:
            return await asyncio.to_thread(_get_sync, name)
        except Exception:  # noqa: BLE001 — genuinely gone, or never stored
            pass
    return None, ""
