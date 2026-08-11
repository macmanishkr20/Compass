"""Agent-driven headless browser — a per-session Playwright page the AGENT
controls via the `browser` tool (this is computer-use for Compass's agent).

Separate from the user-facing streamed remote browser (remote_browser.py),
though both share the same warm Chromium process. Actions favour **semantic**
targets (click a button by its text, fill a field by its label) over raw pixel
coordinates, because that is far more reliable for the model than guessing x/y —
while still supporting coordinate clicks when the model prefers them.

Every action returns a fresh screenshot (PNG) so the caller can feed the current
page back to the model as vision, closing the see → act → see loop.
"""

from __future__ import annotations

import asyncio
import contextlib

_VIEW_W, _VIEW_H = 1280, 900


class AgentBrowser:
    def __init__(self) -> None:
        self._context = None
        self._page = None
        self._lock = asyncio.Lock()

    async def _page_obj(self):
        if self._page is not None:
            return self._page
        async with self._lock:
            if self._page is not None:
                return self._page
            from compass.services.remote_browser import _ensure_browser

            browser = await _ensure_browser()
            self._context = await browser.new_context(
                viewport={"width": _VIEW_W, "height": _VIEW_H},
                device_scale_factor=1,
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
            )
            self._page = await self._context.new_page()
            return self._page

    # -- actions -------------------------------------------------------------
    async def navigate(self, url: str) -> None:
        page = await self._page_obj()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except Exception:
            await page.goto(url, wait_until="load", timeout=30_000)
        await page.wait_for_timeout(500)

    async def click(self, target: str) -> None:
        """Click a button/link/element identified by its visible text."""
        page = await self._page_obj()
        candidates = [
            page.get_by_role("button", name=target, exact=False),
            page.get_by_role("link", name=target, exact=False),
            page.get_by_role("tab", name=target, exact=False),
            page.get_by_text(target, exact=False),
        ]
        for loc in candidates:
            try:
                await loc.first.click(timeout=3_000)
                await page.wait_for_timeout(400)
                return
            except Exception:
                continue
        raise RuntimeError(f"no clickable element matching text '{target}'")

    async def click_xy(self, x: float, y: float) -> None:
        page = await self._page_obj()
        await page.mouse.click(float(x), float(y))
        await page.wait_for_timeout(300)

    async def fill(self, field: str, value: str) -> None:
        """Fill an input identified by its label, placeholder, or name."""
        page = await self._page_obj()
        candidates = [
            page.get_by_label(field, exact=False),
            page.get_by_placeholder(field, exact=False),
            page.locator(
                f"input[name='{field}'], textarea[name='{field}'], "
                f"input[id='{field}'], textarea[id='{field}']"
            ),
        ]
        for loc in candidates:
            try:
                await loc.first.fill(value, timeout=3_000)
                return
            except Exception:
                continue
        # Last resort: type into whatever is focused.
        await page.keyboard.type(value)

    async def type_text(self, text: str) -> None:
        page = await self._page_obj()
        await page.keyboard.type(text)

    async def press(self, key: str) -> None:
        page = await self._page_obj()
        await page.keyboard.press(key)
        await page.wait_for_timeout(300)

    async def scroll(self, dy: float) -> None:
        page = await self._page_obj()
        await page.mouse.wheel(0, float(dy))
        await page.wait_for_timeout(200)

    async def read_text(self, max_chars: int = 5_000) -> str:
        page = await self._page_obj()
        try:
            text = await page.inner_text("body")
        except Exception:
            text = ""
        return text[:max_chars]

    async def screenshot(self) -> bytes:
        page = await self._page_obj()
        return await page.screenshot(type="png")

    def current_url(self) -> str:
        return self._page.url if self._page else ""

    async def title(self) -> str:
        if not self._page:
            return ""
        try:
            return await self._page.title()
        except Exception:  # noqa: BLE001
            return ""

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            if self._context:
                await self._context.close()
        self._page = None
        self._context = None


# One browser per agent session; created on first use, closed with the session.
_sessions: dict[str, AgentBrowser] = {}


def get_agent_browser(session_id: str) -> AgentBrowser:
    b = _sessions.get(session_id)
    if b is None:
        b = AgentBrowser()
        _sessions[session_id] = b
    return b


async def close_agent_browser(session_id: str) -> None:
    b = _sessions.pop(session_id, None)
    if b is not None:
        await b.close()
