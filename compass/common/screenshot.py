"""Headless-browser screenshots (Playwright) — the capability that lets
Compass see a running frontend/app and post it back for verification."""

from __future__ import annotations

import base64
import logging

from compass.common.urls import refuse_reason

logger = logging.getLogger("compass.screenshot")

_UNAVAILABLE = "Playwright is not installed on the server host (pip install playwright && playwright install chromium)."


async def capture(url: str, *, full_page: bool = False, width: int = 1280, height: int = 800) -> bytes:
    """Return a PNG screenshot of `url`. Raises RuntimeError on failure.

    Only the web can be photographed. The URL reaching here can come from the
    model, and a screenshot of `file:///etc/passwd` is that file's contents
    rendered as an image and handed back as something to read — a file read
    with no permission gate in front of it, dressed as a picture.
    """
    if refusal := refuse_reason(url):
        raise RuntimeError(refusal)
    try:
        from playwright.async_api import async_playwright
    except ImportError as err:  # pragma: no cover
        raise RuntimeError(_UNAVAILABLE) from err

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox"])
        try:
            page = await browser.new_page(viewport={"width": width, "height": height})
            try:
                await page.goto(url, wait_until="networkidle", timeout=20_000)
            except Exception:
                # networkidle can hang on apps with long-poll/websocket; fall
                # back to a plain load so we still get a shot.
                await page.goto(url, wait_until="load", timeout=20_000)
            await page.wait_for_timeout(600)
            return await page.screenshot(full_page=full_page, type="png")
        finally:
            await browser.close()


async def capture_data_uri(url: str, *, full_page: bool = False) -> str:
    png = await capture(url, full_page=full_page)
    return "data:image/png;base64," + base64.b64encode(png).decode()


# A tool result references an image by a short id (screenshot://<id>) rather
# than carrying base64 into the model's context. The id is served back to the
# browser, which is what puts the picture in the transcript.
#
# Held in memory AND written down. In memory alone it was good for forty
# shots and the life of the process: a conversation reopened after a restart
# had holes where its pictures were, and nobody could tell whether the image
# had failed or the server had been bounced. A screenshot is part of what the
# turn said, so it lasts as long as the turn does.
import collections
import uuid as _uuid

_CACHE: "collections.OrderedDict[str, bytes]" = collections.OrderedDict()
_CACHE_MAX = 40

#: Where the durable copy goes, beside the generated pictures.
_PREFIX = "screenshots"

#: The filing tasks still in flight. `create_task` holds only a weak
#: reference, so a task nobody else is holding can be collected part-way
#: through and simply stop — which is what was happening: the bytes reached
#: blob storage sometimes and the index row was written less often than that,
#: with no error either way. Held here until each one finishes.
_IN_FLIGHT: set = set()


def _keep(sid: str, png: bytes, owner: str = "", session_id: str = "") -> None:
    """Hold it in memory and put it somewhere it survives a restart.

    Fire-and-forget, and never fatal: a screenshot that could not be filed is
    still in the cache and still shows in the turn that took it. The write is
    scheduled rather than awaited because every caller of this is sync and on
    the hot path of a tool result.

    `owner` is taken here or not at all. A screenshot is the one artefact
    whose owner cannot be recovered afterwards: the id is served to the
    browser rather than written into the message, so nothing in the stored
    conversation points at it, and an hour later there is no way left to say
    whose it was. The route that serves these reads what is recorded here.
    """
    _CACHE[sid] = png
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    try:
        import asyncio

        task = asyncio.get_running_loop().create_task(
            _persist(sid, png, owner, session_id)
        )
        _IN_FLIGHT.add(task)
        task.add_done_callback(_IN_FLIGHT.discard)
    except RuntimeError:
        # No loop (a sync caller outside the server) — memory only.
        pass


async def _persist(sid: str, png: bytes, owner: str = "", session_id: str = "") -> None:
    from compass.common.gateway import images

    name = f"{_PREFIX}/{sid}.png"
    try:
        await images.store_bytes(name, png)
    except Exception:  # noqa: BLE001 — the cache already has it
        logger.debug("could not file screenshot %s", sid, exc_info=True)
        return
    # Only once the bytes are down: a row pointing at nothing would make the
    # serving route refuse a picture that was never there to refuse.
    from compass.common import media_index

    await media_index.record(
        blob_name=name,
        url=f"/v1/screenshot-cache/{sid}",
        kind="screenshot",
        owner=owner,
        session_id=session_id,
        size_bytes=len(png),
    )


async def load_stored(sid: str) -> bytes | None:
    """The durable copy, for a shot the cache has forgotten."""
    from compass.common.gateway import images

    raw, _ = await images.fetch(f"{_PREFIX}/{sid}.png")
    return raw


async def capture_cached(
    url: str,
    *,
    full_page: bool = False,
    owner: str = "",
    session_id: str = "",
) -> tuple[str, int, int]:
    """Capture and store the PNG; return (id, width, height)."""
    png = await capture(url, full_page=full_page)
    sid = _uuid.uuid4().hex[:12]
    _keep(sid, png, owner, session_id)
    # cheap PNG dimension read (IHDR at bytes 16..24)
    w = int.from_bytes(png[16:20], "big") if len(png) > 24 else 0
    h = int.from_bytes(png[20:24], "big") if len(png) > 24 else 0
    return sid, w, h


def store_png(
    png: bytes, *, owner: str = "", session_id: str = ""
) -> tuple[str, int, int]:
    """Cache raw PNG bytes (e.g. from the agent browser) under a short id and
    return (id, width, height) — the same screenshot://<id> path the UI serves."""
    sid = _uuid.uuid4().hex[:12]
    _keep(sid, png, owner, session_id)
    w = int.from_bytes(png[16:20], "big") if len(png) > 24 else 0
    h = int.from_bytes(png[20:24], "big") if len(png) > 24 else 0
    return sid, w, h


def get_cached(sid: str) -> bytes | None:
    return _CACHE.get(sid)


async def html_to_pdf(html: str, *, width: int = 1000) -> bytes:
    """Print a self-contained HTML document to PDF. Raises on failure.

    Here rather than in a module because it is a shared capability and this is
    already the file that owns the headless browser. Design has its own PDF
    path and keeps it: that one detects slides and fixed-size sheets and prints
    one page per artboard, which is the right answer for a design and the wrong
    one for a document. This is the plain case — a flowing document that the
    stylesheet paginates.

    `prefer_css_page_size` is what makes that work: the caller declares `@page`
    and `break-inside` and gets a paginated PDF, instead of one page as tall as
    the whole document. The HTML must be self-contained — no network fetches
    are made, so styles are inline and there are no images to wait for.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as err:  # pragma: no cover - optional dependency
        raise RuntimeError(_UNAVAILABLE) from err

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox"])
        try:
            page = await browser.new_page(viewport={"width": width, "height": 1200})
            await page.set_content(html, wait_until="load")
            # Print styles only resolve under the print emulation; without this
            # the @page box is ignored and the colours come back washed out.
            await page.emulate_media(media="print")
            await page.wait_for_timeout(120)
            return await page.pdf(
                print_background=True,
                prefer_css_page_size=True,
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            )
        finally:
            await browser.close()
