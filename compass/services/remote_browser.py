"""Interactive remote browser — a real server-side Chromium (Playwright) whose
frames stream to the client and whose input (mouse, keyboard, scroll) is driven
by the client.

This is what lets the Compass browser pane behave like claude.ai's: every site
renders live and is fully interactive, including sites that refuse to be framed
(X-Frame-Options / CSP frame-ancestors) — because Chromium navigates to them
directly instead of embedding them in an <iframe>.

Transport is a WebSocket (see server.browser_ws). Rendering uses Chrome
DevTools' `Page.startScreencast`, which pushes a JPEG frame only when the page
changes visually — cheap and smooth. Input is normalised (0..1 of the pane) on
the client and mapped to viewport CSS pixels here, so it stays correct across
pane resizes and expand/collapse.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("compass.remote_browser")

# One warm Chromium process shared across sessions; each WebSocket gets its own
# isolated context+page (its own cookies/history), closed when it disconnects.
_pw = None
_browser = None
_launch_lock = asyncio.Lock()

_BUTTONS = {0: "left", 1: "middle", 2: "right"}
# Browser KeyboardEvent.key values that map 1:1 onto Playwright key names and
# must go through key press (not text insertion) to trigger the right handlers.
_SPECIAL_KEYS = {
    "Enter", "Backspace", "Delete", "Tab", "Escape", "ArrowLeft", "ArrowRight",
    "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown",
}


async def _ensure_browser():
    global _pw, _browser
    if _browser is not None and _browser.is_connected():
        return _browser
    async with _launch_lock:
        if _browser is not None and _browser.is_connected():
            return _browser
        from playwright.async_api import async_playwright

        if _pw is None:
            _pw = await async_playwright().start()
        _browser = await _pw.chromium.launch(args=["--no-sandbox"])
        return _browser


class RemoteBrowserSession:
    """A single interactive page. `on_event` receives dicts to forward to the
    client: {'t':'frame','data':<b64 jpeg>} and {'t':'nav', url, title, ...}."""

    def __init__(self, on_event: Callable[[dict[str, Any]], Awaitable[None]]):
        self._on_event = on_event
        self._context = None
        self._page = None
        self._cdp = None
        self.width = 1280
        self.height = 800
        self._closed = False

    async def start(self, width: int = 1280, height: int = 800) -> None:
        self.width = max(320, min(width, 2000))
        self.height = max(240, min(height, 1400))
        browser = await _ensure_browser()
        self._context = await browser.new_context(
            viewport={"width": self.width, "height": self.height},
            device_scale_factor=1,
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        self._page = await self._context.new_page()
        self._page.on("framenavigated", self._on_frame_navigated)
        self._cdp = await self._context.new_cdp_session(self._page)
        self._cdp.on("Page.screencastFrame", self._on_screencast_frame)
        await self._start_screencast()

    async def _start_screencast(self) -> None:
        await self._cdp.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": 62,
                "maxWidth": self.width,
                "maxHeight": self.height,
                "everyNthFrame": 1,
            },
        )

    async def _on_screencast_frame(self, params: dict[str, Any]) -> None:
        # Ack immediately so Chromium keeps sending frames, then forward.
        sid = params.get("sessionId")
        with contextlib.suppress(Exception):
            await self._cdp.send("Page.screencastFrameAck", {"sessionId": sid})
        if self._closed:
            return
        with contextlib.suppress(Exception):
            await self._on_event({"t": "frame", "data": params["data"]})

    def _on_frame_navigated(self, frame) -> None:
        # Only the top frame's URL matters for the address bar.
        if self._page and frame == self._page.main_frame:
            asyncio.create_task(self._emit_nav())

    async def _emit_nav(self) -> None:
        if self._closed or not self._page:
            return
        with contextlib.suppress(Exception):
            title = await self._page.title()
            await self._on_event(
                {"t": "nav", "url": self._page.url, "title": title}
            )

    # -- commands from the client -------------------------------------------
    async def goto(self, url: str) -> None:
        if not self._page:
            return
        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as err:  # navigation errors surface, don't kill the session
            await self._on_event({"t": "error", "message": str(err)})
        await self._emit_nav()

    async def reload(self) -> None:
        if self._page:
            with contextlib.suppress(Exception):
                await self._page.reload(wait_until="domcontentloaded", timeout=30_000)
            await self._emit_nav()

    async def go_back(self) -> None:
        if self._page:
            with contextlib.suppress(Exception):
                await self._page.go_back(wait_until="domcontentloaded", timeout=30_000)
            await self._emit_nav()

    async def go_forward(self) -> None:
        if self._page:
            with contextlib.suppress(Exception):
                await self._page.go_forward(wait_until="domcontentloaded", timeout=30_000)
            await self._emit_nav()

    async def resize(self, width: int, height: int) -> None:
        if not self._page:
            return
        self.width = max(320, min(int(width), 2000))
        self.height = max(240, min(int(height), 1400))
        with contextlib.suppress(Exception):
            await self._page.set_viewport_size(
                {"width": self.width, "height": self.height}
            )
            # Restart the screencast so its max dimensions track the new size.
            await self._cdp.send("Page.stopScreencast")
            await self._start_screencast()

    def _to_px(self, x: float, y: float) -> tuple[float, float]:
        return (
            max(0.0, min(float(x), 1.0)) * self.width,
            max(0.0, min(float(y), 1.0)) * self.height,
        )

    async def mouse_move(self, x: float, y: float) -> None:
        if self._page:
            px, py = self._to_px(x, y)
            with contextlib.suppress(Exception):
                await self._page.mouse.move(px, py)

    async def mouse_down(self, x: float, y: float, button: int = 0) -> None:
        if self._page:
            px, py = self._to_px(x, y)
            with contextlib.suppress(Exception):
                await self._page.mouse.move(px, py)
                await self._page.mouse.down(button=_BUTTONS.get(button, "left"))

    async def mouse_up(self, x: float, y: float, button: int = 0) -> None:
        if self._page:
            px, py = self._to_px(x, y)
            with contextlib.suppress(Exception):
                await self._page.mouse.move(px, py)
                await self._page.mouse.up(button=_BUTTONS.get(button, "left"))

    async def wheel(self, dx: float, dy: float) -> None:
        if self._page:
            with contextlib.suppress(Exception):
                await self._page.mouse.wheel(float(dx), float(dy))

    async def type_text(self, text: str) -> None:
        if self._page and text:
            with contextlib.suppress(Exception):
                await self._cdp.send("Input.insertText", {"text": text})

    async def press_key(self, key: str) -> None:
        if self._page and key in _SPECIAL_KEYS:
            with contextlib.suppress(Exception):
                await self._page.keyboard.press(key)

    # -- Select / inspect (element picker, like claude.ai's Select tool) -----
    # Headless Chromium won't paint the native DevTools overlay into a
    # screencast, so we read the element's box + identity via page JS and let
    # the client draw the highlight + info card over the frame.
    async def set_select(self, on: bool) -> None:
        # No browser-side state needed — the client only sends `inspect` while
        # the tool is active, and clears its own overlay when it turns off.
        return

    # Shared element probe: role/name/focusable computed the way DevTools shows
    # them, so the hover card matches claude.ai's Select tool.
    _INSPECT_JS = """
    (() => {
      const el = document.elementFromPoint(__X__, __Y__);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      const tag = el.tagName.toLowerCase();
      let cls = '';
      if (typeof el.className === 'string' && el.className.trim())
        cls = '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.');
      const roleMap = {a: el.hasAttribute('href') ? 'link' : 'generic',
        button:'button', h1:'heading', h2:'heading', h3:'heading', h4:'heading',
        h5:'heading', h6:'heading', img:'image', nav:'navigation', main:'main',
        header:'banner', footer:'contentinfo', ul:'list', ol:'list',
        li:'listitem', p:'paragraph', input:'textbox', textarea:'textbox',
        select:'combobox', table:'table', form:'form', section:'region'};
      const role = el.getAttribute('role') || roleMap[tag] || 'generic';
      let name = el.getAttribute('aria-label') || '';
      if (!name && tag === 'img') name = el.getAttribute('alt') || '';
      if (!name) name = (el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
      let focusable = false;
      if (el.tabIndex >= 0) focusable = true;
      else if (tag === 'a' && el.hasAttribute('href')) focusable = true;
      else if (tag === 'input' && el.type !== 'hidden' && !el.disabled) focusable = true;
      else if ((tag === 'button' || tag === 'select' || tag === 'textarea') && !el.disabled) focusable = true;
      return JSON.stringify({
        tag: tag, id: el.id ? '#' + el.id : '', cls: cls,
        x: r.left, y: r.top, w: r.width, h: r.height,
        role: role, name: name, focusable: focusable,
      });
    })()
    """

    # Richer probe used on click: the opening tag, a selector, key computed
    # styles and text — what gets attached to the prompt as element context.
    _PICK_JS = """
    (() => {
      const el = document.elementFromPoint(__X__, __Y__);
      if (!el) return null;
      const tag = el.tagName.toLowerCase();
      const classAttr = (typeof el.className === 'string') ? el.className.trim() : '';
      let opening = '<' + tag;
      if (el.id) opening += ' id="' + el.id + '"';
      if (classAttr) opening += ' class="' + classAttr + '"';
      opening += '>';
      let sel = tag + (el.id ? '#' + el.id : '') +
        (classAttr ? '.' + classAttr.split(/\\s+/).join('.') : '');
      const cs = getComputedStyle(el);
      const props = ['display','color','background-color','font-family','font-size',
        'font-weight','line-height','text-align','padding','margin','border',
        'border-radius','width','height'];
      const styles = {};
      props.forEach(p => { styles[p] = cs.getPropertyValue(p); });
      const r = el.getBoundingClientRect();
      return JSON.stringify({
        tag: tag, opening: opening, selector: sel,
        text: (el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 140),
        styles: styles, w: r.width, h: r.height,
      });
    })()
    """

    @staticmethod
    def _fmt_dim(n: float) -> str:
        return f"{n:.2f}".rstrip("0").rstrip(".")

    async def _eval_at(self, js: str, x: float, y: float) -> dict | None:
        if not self._cdp:
            return None
        px, py = self._to_px(x, y)
        expr = js.replace("__X__", str(int(px))).replace("__Y__", str(int(py)))
        try:
            res = await self._cdp.send(
                "Runtime.evaluate", {"expression": expr, "returnByValue": True}
            )
        except Exception:
            return None
        raw = (res or {}).get("result", {}).get("value")
        if not raw:
            return None
        import json

        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    async def inspect_at(self, x: float, y: float) -> None:
        """Report the element under the cursor for the hover card: box (0..1),
        tag/id/class, size, and the accessibility role/name/focusable."""
        d = await self._eval_at(self._INSPECT_JS, x, y)
        if not d:
            return
        w = max(self.width, 1)
        h = max(self.height, 1)
        await self._on_event(
            {
                "t": "inspect",
                "box": {
                    "x": d["x"] / w, "y": d["y"] / h,
                    "w": d["w"] / w, "h": d["h"] / h,
                },
                "label": f"{d['tag']}{d['id']}{d['cls']}",
                "tag": d["tag"],
                "sub": f"{d['id']}{d['cls']}",
                "dim": f"{self._fmt_dim(d['w'])} × {self._fmt_dim(d['h'])}",
                "role": d.get("role") or d["tag"],
                "name": d.get("name") or "",
                "focusable": bool(d.get("focusable")),
            }
        )

    async def pick_at(self, x: float, y: float) -> None:
        """On a click in Select mode, emit the element's opening tag, selector,
        key CSS and text so the client can attach it to the prompt."""
        d = await self._eval_at(self._PICK_JS, x, y)
        if not d:
            return
        d["t"] = "pick"
        d["dim"] = f"{self._fmt_dim(d['w'])} × {self._fmt_dim(d['h'])}"
        await self._on_event(d)

    async def close(self) -> None:
        self._closed = True
        with contextlib.suppress(Exception):
            if self._cdp:
                await self._cdp.send("Page.stopScreencast")
        with contextlib.suppress(Exception):
            if self._context:
                await self._context.close()
        self._page = None
        self._context = None
        self._cdp = None


async def handle_command(sess: RemoteBrowserSession, msg: dict[str, Any]) -> None:
    """Dispatch one decoded client message to the session."""
    t = msg.get("t")
    if t == "nav":
        await sess.goto(str(msg.get("url", "")))
    elif t == "reload":
        await sess.reload()
    elif t == "back":
        await sess.go_back()
    elif t == "forward":
        await sess.go_forward()
    elif t == "resize":
        await sess.resize(msg.get("w", 1280), msg.get("h", 800))
    elif t == "move":
        await sess.mouse_move(msg.get("x", 0), msg.get("y", 0))
    elif t == "down":
        await sess.mouse_down(msg.get("x", 0), msg.get("y", 0), msg.get("button", 0))
    elif t == "up":
        await sess.mouse_up(msg.get("x", 0), msg.get("y", 0), msg.get("button", 0))
    elif t == "wheel":
        await sess.wheel(msg.get("dx", 0), msg.get("dy", 0))
    elif t == "type":
        await sess.type_text(str(msg.get("text", "")))
    elif t == "key":
        await sess.press_key(str(msg.get("key", "")))
    elif t == "select":
        await sess.set_select(bool(msg.get("on", False)))
    elif t == "inspect":
        await sess.inspect_at(msg.get("x", 0), msg.get("y", 0))
    elif t == "pick":
        await sess.pick_at(msg.get("x", 0), msg.get("y", 0))
