"""Interactive remote browser — a real server-side Chromium (Playwright) whose
frames stream to the client and whose input (mouse, keyboard, scroll) is driven
by the client.

This is what lets the Compass browser pane behave like claude.ai's: every site
renders live and is fully interactive, including sites that refuse to be framed
(X-Frame-Options / CSP frame-ancestors) — because Chromium navigates to them
directly instead of embedding them in an <iframe>.

Transport is a WebSocket (see server.browser_ws). Rendering is a loop of
JPEG screenshots taken at the viewer's device pixel ratio, sent only when the
picture actually changes and slowed right down when it does not.

It used DevTools' `Page.startScreencast`, which is faster and cannot do this
job: screencast captures the compositor surface at CSS resolution and ignores
the device scale factor, so on a Retina display every frame was stretched over
four times the pixels it contained and the pane looked soft beside a real
browser. Screenshots honour the scale. Measured on a real page at a 700x850
viewport: 16ms a frame at 1x, 50ms at 2x.

Input is normalised (0..1 of the pane) on the client and mapped to viewport
CSS pixels here, so it stays correct across pane resizes and expand/collapse.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import time
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


#: Enough that text keeps its edges. The old screencast ran at 62, which
#: spends its bits exactly where letters need them.
JPEG_QUALITY = 80

#: The largest viewport a client may ask for.
#:
#: Not sized to the viewport presets, which top out at 1920x1080: responsive
#: mode asks for the pane's own size, and a pane on a large or ultrawide
#: display is easily wider than any preset. The old 2000x1400 cap silently
#: truncated those — rendering a narrower layout than the pane actually has,
#: which is the wrong answer given confidently.
MAX_VIEWPORT_W = 2560
MAX_VIEWPORT_H = 1600


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
        #: Device pixels per CSS pixel, as the viewer's display has them.
        #:
        #: Was fixed at 1, which is why the pane looked soft next to a real
        #: browser: a Retina display packs two device pixels into every CSS
        #: pixel, so a frame rendered at one was being stretched over four and
        #: the text lost its edges. Rendering at the viewer's ratio is what
        #: makes the stream match the browser beside it.
        #:
        #: Fixed for the life of the session, because Playwright takes it as a
        #: context option and changing it means throwing the page away. A
        #: window dragged to a different display therefore keeps the ratio it
        #: started with — a 2x frame on a 1x screen simply downscales, which
        #: costs bandwidth and nothing else.
        self.scale = 1.0
        self._capture: asyncio.Task | None = None
        #: When the page was last given a reason to move. Motion is read
        #: from this rather than inferred from frames being different: a
        #: scroll arrives as a burst of wheel events with still moments
        #: between them, and comparing pictures reads each of those gaps
        #: as the page having settled.
        self._last_input = 0.0
        #: Whether the settled, full-resolution frame has been sent for
        #: what is currently on screen. Cleared by any motion frame.
        self._sharpened = False
        #: The last motion frame, to tell a real change from an echo.
        self._last_motion: str | None = None
        #: Whether a full-resolution frame is worth making at all. False when
        #: the client is already scaling the frame down to fit.
        self._wants_sharp = True
        self._closed = False

    async def start(
        self, width: int = 1280, height: int = 800, scale: float = 1.0,
    ) -> None:
        self.width = max(320, min(width, MAX_VIEWPORT_W))
        self.height = max(240, min(height, MAX_VIEWPORT_H))
        # Capped at 2. Beyond that the frame costs four times the bytes for a
        # sharpness nobody can see, and a client is free to claim anything.
        self.scale = max(1.0, min(float(scale or 1.0), 2.0))
        browser = await _ensure_browser()
        self._context = await browser.new_context(
            viewport={"width": self.width, "height": self.height},
            device_scale_factor=self.scale,
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
        self._capture = asyncio.create_task(self._sharpen_loop())

    #: How long after the last input the page counts as settled. A scroll is
    #: a burst of wheel events with still moments between them, so this is
    #: what stops those gaps being mistaken for having stopped.
    SETTLE_AFTER = 0.5
    #: How often to check whether it has. Cheap: it is a clock, not a frame.
    IDLE_POLL = 0.12

    async def _start_screencast(self) -> None:
        """The motion stream: Chromium pushes a frame whenever the page
        changes, and only then. Cheap, event-driven, and smooth."""
        await self._cdp.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                # 62 was visible on text: thin strokes and letter edges are
                # the first thing a low-quality JPEG spends.
                "quality": JPEG_QUALITY,
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
        data = params["data"]
        # Only a frame that actually looks different means the page moved.
        #
        # Taking the sharp screenshot makes Chromium produce a frame of its
        # own, and treating that as motion cleared the flag that had just
        # been set — so the loop sharpened again, which produced another
        # frame, and so on. Measured before this check: 23 full-resolution
        # frames in 2.5 seconds on a page nobody was touching, about 1.4MB a
        # second to say nothing had happened.
        if data != self._last_motion:
            self._last_motion = data
            self._sharpened = False
        with contextlib.suppress(Exception):
            await self._on_event({"t": "frame", "data": data})

    async def _sharpen_loop(self) -> None:
        """Replace the last motion frame with a detailed one, once it stops.

        The screencast above is what makes scrolling smooth, and it cannot
        make the page sharp: it captures the compositor surface at CSS
        resolution and ignores the device scale factor entirely — measured, a
        600x800 viewport at scale 2 still arrives as 600x800 — so on a Retina
        display every frame is stretched over four times the pixels it has.
        That is the softness, and no screencast setting fixes it.

        A screenshot does honour the scale, and costs about 50ms at 2x
        against 16ms at 1x. Too slow to stream, which is the whole reason the
        screencast stays: this sends exactly one, when the page has stopped
        moving and a person is about to read it. Motion is smooth because it
        is cheap; the still picture is sharp because there is time to make it.
        """
        while not self._closed:
            await asyncio.sleep(self.IDLE_POLL)
            # On a 1x display the two scales are the same picture, so the
            # second pass is pure waste — skipped rather than sent twice.
            if self._closed or not self._page or self._sharpened:
                continue
            if self.scale <= 1.0 or not self._wants_sharp:
                continue
            if time.monotonic() - self._last_input < self.SETTLE_AFTER:
                continue
            # Claimed before the await: a screencast frame arriving mid-shot
            # clears it again, and the next pass will redo the work.
            self._sharpened = True
            shot = await self._shoot("device")
            if shot and not self._closed and self._sharpened:
                await self._emit_frame(shot)

    async def _shoot(self, scale: str) -> bytes | None:
        """One frame, or None if the page was busy navigating or closing."""
        try:
            return await self._page.screenshot(
                type="jpeg", quality=JPEG_QUALITY, scale=scale, timeout=5000,
            )
        except Exception:  # noqa: BLE001 — a missed frame is not an error
            return None

    async def _emit_frame(self, jpeg: bytes) -> None:
        with contextlib.suppress(Exception):
            await self._on_event(
                {"t": "frame", "data": base64.b64encode(jpeg).decode()}
            )

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
        self._last_input = time.monotonic()
        if not self._page:
            return
        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as err:  # navigation errors surface, don't kill the session
            await self._on_event({"t": "error", "message": str(err)})
        await self._emit_nav()

    async def reload(self) -> None:
        self._last_input = time.monotonic()
        if self._page:
            with contextlib.suppress(Exception):
                await self._page.reload(wait_until="domcontentloaded", timeout=30_000)
            await self._emit_nav()

    async def go_back(self) -> None:
        self._last_input = time.monotonic()
        if self._page:
            with contextlib.suppress(Exception):
                await self._page.go_back(wait_until="domcontentloaded", timeout=30_000)
            await self._emit_nav()

    async def go_forward(self) -> None:
        self._last_input = time.monotonic()
        if self._page:
            with contextlib.suppress(Exception):
                await self._page.go_forward(wait_until="domcontentloaded", timeout=30_000)
            await self._emit_nav()

    async def resize(
        self, width: int, height: int, sharp: bool = True,
    ) -> None:
        self._last_input = time.monotonic()
        # The client knows how big the frame will be drawn and the server does
        # not: in a device preset a 1920-wide page may be shown in a 700-wide
        # pane, where a full-resolution frame is several megabytes of detail
        # thrown away on arrival.
        self._wants_sharp = bool(sharp)
        if not self._page:
            return
        self.width = max(320, min(int(width), MAX_VIEWPORT_W))
        self.height = max(240, min(int(height), MAX_VIEWPORT_H))
        with contextlib.suppress(Exception):
            await self._page.set_viewport_size(
                {"width": self.width, "height": self.height}
            )
            # The screencast caps frames at the size it was started with, so
            # it has to be restarted or the frame stays the old shape.
            await self._cdp.send("Page.stopScreencast")
            await self._start_screencast()
            self._sharpened = False

    def _to_px(self, x: float, y: float) -> tuple[float, float]:
        return (
            max(0.0, min(float(x), 1.0)) * self.width,
            max(0.0, min(float(y), 1.0)) * self.height,
        )

    async def mouse_move(self, x: float, y: float) -> None:
        self._last_input = time.monotonic()
        if self._page:
            px, py = self._to_px(x, y)
            with contextlib.suppress(Exception):
                await self._page.mouse.move(px, py)

    async def mouse_down(self, x: float, y: float, button: int = 0) -> None:
        self._last_input = time.monotonic()
        if self._page:
            px, py = self._to_px(x, y)
            with contextlib.suppress(Exception):
                await self._page.mouse.move(px, py)
                await self._page.mouse.down(button=_BUTTONS.get(button, "left"))

    async def mouse_up(self, x: float, y: float, button: int = 0) -> None:
        self._last_input = time.monotonic()
        if self._page:
            px, py = self._to_px(x, y)
            with contextlib.suppress(Exception):
                await self._page.mouse.move(px, py)
                await self._page.mouse.up(button=_BUTTONS.get(button, "left"))

    async def wheel(self, dx: float, dy: float) -> None:
        self._last_input = time.monotonic()
        if self._page:
            with contextlib.suppress(Exception):
                await self._page.mouse.wheel(float(dx), float(dy))

    async def type_text(self, text: str) -> None:
        self._last_input = time.monotonic()
        if self._page and text:
            with contextlib.suppress(Exception):
                await self._cdp.send("Input.insertText", {"text": text})

    async def press_key(self, key: str) -> None:
        self._last_input = time.monotonic()
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
        if self._capture:
            self._capture.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._capture
            self._capture = None
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
        await sess.resize(
            msg.get("w", 1280), msg.get("h", 800), bool(msg.get("sharp", True))
        )
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
