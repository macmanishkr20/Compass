"""Headless-browser screenshots (Playwright) — the capability that lets
Compass see a running frontend/app and post it back for verification."""

from __future__ import annotations

import base64

_UNAVAILABLE = "Playwright is not installed on the server host (pip install playwright && playwright install chromium)."


async def capture(url: str, *, full_page: bool = False, width: int = 1280, height: int = 800) -> bytes:
    """Return a PNG screenshot of `url`. Raises RuntimeError on failure."""
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


# Small in-memory cache so a tool result can reference an image by a short id
# (screenshot://<id>) instead of carrying base64 into the model's context.
import collections
import uuid as _uuid

_CACHE: "collections.OrderedDict[str, bytes]" = collections.OrderedDict()
_CACHE_MAX = 40


async def capture_cached(url: str, *, full_page: bool = False) -> tuple[str, int, int]:
    """Capture and store the PNG; return (id, width, height)."""
    png = await capture(url, full_page=full_page)
    sid = _uuid.uuid4().hex[:12]
    _CACHE[sid] = png
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    # cheap PNG dimension read (IHDR at bytes 16..24)
    w = int.from_bytes(png[16:20], "big") if len(png) > 24 else 0
    h = int.from_bytes(png[20:24], "big") if len(png) > 24 else 0
    return sid, w, h


def store_png(png: bytes) -> tuple[str, int, int]:
    """Cache raw PNG bytes (e.g. from the agent browser) under a short id and
    return (id, width, height) — the same screenshot://<id> path the UI serves."""
    sid = _uuid.uuid4().hex[:12]
    _CACHE[sid] = png
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    w = int.from_bytes(png[16:20], "big") if len(png) > 24 else 0
    h = int.from_bytes(png[20:24], "big") if len(png) > 24 else 0
    return sid, w, h


def get_cached(sid: str) -> bytes | None:
    return _CACHE.get(sid)
