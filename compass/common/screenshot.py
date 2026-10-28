"""Headless-browser screenshots (Playwright) — the capability that lets
Compass see a running frontend/app and post it back for verification."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import sys
from typing import Any, Awaitable, Callable, TypeVar

from compass.common.urls import refuse_reason

logger = logging.getLogger("compass.screenshot")

_UNAVAILABLE = "Playwright is not installed on the server host (pip install playwright && playwright install chromium)."

#: Browsers already on the host, tried in order when the bundled Chromium is
#: not there.
#:
#: `playwright install chromium` downloads a ~150MB zip over HTTPS, and on a
#: managed laptop behind a TLS-inspecting proxy that download cannot be
#: verified — "unable to get local issuer certificate" — so the browser never
#: arrives and no amount of server-side diagnosis helps. These channels need no
#: download whatsoever: they drive the Edge or Chrome that is already
#: installed. Edge is on every Windows machine, and both are Chromium engines,
#: which is what `page.pdf()` requires.
_CHANNELS: tuple[str, ...] = tuple(
    c.strip()
    for c in os.getenv("COMPASS_BROWSER_CHANNELS", "msedge,chrome").split(",")
    if c.strip()
)

_NO_SUBPROCESS = (
    "This server cannot start a browser: on Windows, Playwright needs "
    "asyncio's Proactor event loop and this process is on the Selector loop. "
    "uvicorn switches loops when it supervises the server, so restart Compass "
    "without --reload and without --workers."
)

T = TypeVar("T")

#: A job that drives a started Playwright and comes back with bytes. Taking
#: the Playwright rather than opening its own is what lets `_with_a_browser`
#: decide *where* the browser runs.
Job = Callable[[Any], Awaitable[T]]


# --------------------------------------------------------- starting a browser
#
# Every browser failure that is ordinary on Windows and impossible on this
# machine is answered here, once, for both the screenshot path and the PDF
# path. Design's exporter already carried these answers; the shared capability
# did not, which is why Estimate's PDF failed on Windows with a driver stack
# trace where Design's said what to run.


def _off_the_proactor_loop() -> bool:
    """True when this loop cannot spawn the driver Playwright needs.

    Windows asyncio starts subprocesses on the Proactor loop and nowhere else,
    and uvicorn puts the server on the Selector loop whenever it supervises
    one — which both `--reload` and `--workers` do. So the ordinary Windows
    development command is the one that cannot print a PDF, and the
    NotImplementedError it raises says nothing about any of this.
    """
    if sys.platform != "win32":
        return False
    proactor = getattr(asyncio, "ProactorEventLoop", None)
    if proactor is None:  # pragma: no cover - win32 always has it
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - always called from a coroutine
        return False
    return not isinstance(loop, proactor)


def _on_a_proactor_loop(job: Job[T]) -> T:
    """Run `job` on a Proactor loop of its own, on this thread.

    The loop belongs to the call: it is created, used and closed here, so
    nothing of it outlives the export and the server's own loop is untouched.
    """
    loop = asyncio.ProactorEventLoop()  # type: ignore[attr-defined]
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_started(job))
    finally:
        asyncio.set_event_loop(None)
        loop.close()


async def _started(job: Job[T]) -> T:
    """Start Playwright, hand it to `job`, stop it however `job` ends."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as err:  # pragma: no cover - optional dependency
        raise RuntimeError(_UNAVAILABLE) from err

    pw = await async_playwright().start()
    try:
        return await job(pw)
    finally:
        await pw.stop()


async def _with_a_browser(job: Job[T]) -> T:
    """Run `job` somewhere a browser can actually be started.

    Here normally. On a Windows server stuck on the Selector loop, on a worker
    thread with a Proactor loop of its own — because a PDF that prints under
    `--reload` is worth more than a sentence explaining why it cannot. If even
    that cannot spawn the driver, the sentence is what comes back.
    """
    if not _off_the_proactor_loop():
        return await _started(job)
    try:
        return await asyncio.to_thread(_on_a_proactor_loop, job)
    except NotImplementedError as err:
        raise RuntimeError(_NO_SUBPROCESS) from err


async def _launch(pw: Any) -> Any:
    """Chromium, else the Edge or Chrome already on the host, else a sentence.

    A present Playwright with no browser is the ordinary state of a machine
    where nobody ran the install step — and the *permanent* state of one where
    the install cannot run, because the proxy in front of it will not let the
    download be verified. So a missing Chromium is not the end of the attempt:
    the browser the person already has is tried next, and it needs no download
    at all.

    Untranslated, any of this reaches the route as a 500 with a driver path in
    it, and the person reads a stack trace to learn what to do.
    """
    tried: list[str] = []
    try:
        return await pw.chromium.launch(args=["--no-sandbox"])
    except Exception as err:  # noqa: BLE001 — one answer for every launch fault
        detail = str(err)
        if "Executable doesn" not in detail and "playwright install" not in detail:
            # Not a missing browser — a real launch fault, and trying three
            # more browsers would only bury it.
            raise RuntimeError(f"Could not start Chromium: {detail[:300]}") from err
        tried.append("the bundled Chromium, which is not installed")

    for channel in _CHANNELS:
        try:
            return await pw.chromium.launch(channel=channel, args=["--no-sandbox"])
        except Exception as err:  # noqa: BLE001 — try the next one, then report
            tried.append(f"{channel} ({_first_line(err)})")

    raise RuntimeError(
        "No browser could be started for the export. Tried " + "; ".join(tried)
        + ". Either run `playwright install chromium` on the server host, or "
        "point COMPASS_BROWSER_CHANNELS at a Chromium-based browser that is "
        "installed. Export Excel and Print need no browser on the server."
    )


def _first_line(err: Exception) -> str:
    """One line of a driver error. They run to twenty, and the rest is the
    install advice we are already giving."""
    for line in str(err).splitlines():
        if line.strip():
            return line.strip()[:90]
    return err.__class__.__name__


async def capture(url: str, *, full_page: bool = False, width: int = 1280, height: int = 800) -> bytes:
    """Return a PNG screenshot of `url`. Raises RuntimeError on failure.

    Only the web can be photographed. The URL reaching here can come from the
    model, and a screenshot of `file:///etc/passwd` is that file's contents
    rendered as an image and handed back as something to read — a file read
    with no permission gate in front of it, dressed as a picture.
    """
    if refusal := refuse_reason(url):
        raise RuntimeError(refusal)

    async def shoot(pw: Any) -> bytes:
        browser = await _launch(pw)
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

    return await _with_a_browser(shoot)


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
    async def print_it(pw: Any) -> bytes:
        browser = await _launch(pw)
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

    return await _with_a_browser(print_it)
