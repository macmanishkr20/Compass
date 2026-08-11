"""browser — computer-use for the agent.

The agent drives a real headless Chromium: navigate, click a button/link by its
text, fill a form field by its label, type, press keys, scroll, or read the
page. After every action it captures a screenshot, which is (a) posted into the
chat for the user (screenshot://id) and (b) fed back to the model as a vision
image (ctx.pending_vision), so the agent SEES the page and decides the next
step — the see → act → see loop that lets it log in and navigate on its own.
"""

from __future__ import annotations

import base64
from typing import AsyncIterator, Literal

from pydantic import BaseModel, Field

from compass.tools.base import Tool, ToolOutput, ToolUseContext, ToolYield


class BrowserInput(BaseModel):
    action: Literal["navigate", "click", "fill", "type", "key", "scroll", "read"] = Field(
        description=(
            "navigate: open a URL. click: click a button/link/tab by its visible "
            "text (put the text in `target`), or by pixel with `x`/`y`. fill: type "
            "`value` into the field named/labelled/placeholdered `target`. type: "
            "type `value` into the focused field. key: press `key` (e.g. Enter). "
            "scroll: scroll by `dy` px. read: return the page's visible text."
        )
    )
    url: str = Field("", description="navigate: the URL to open.")
    target: str = Field(
        "", description="click: the button/link text. fill: the field's label/placeholder/name."
    )
    value: str = Field("", description="fill/type: the text to enter.")
    key: str = Field("", description="key: a key to press, e.g. 'Enter', 'Tab', 'Escape'.")
    x: float = Field(0, description="click by coordinate (only if no `target`).")
    y: float = Field(0, description="click by coordinate (only if no `target`).")
    dy: float = Field(600, description="scroll: pixels to scroll (positive = down).")


class BrowserTool(Tool):
    name = "browser"
    description = (
        "Drive a real web browser to inspect or operate a running app — navigate, "
        "click, fill forms, type, scroll, read. Use this to open a local dev "
        "server, LOG IN (fill the fields and click Sign in), move between pages, "
        "and SHOW the result. Every action returns a screenshot you can see, so "
        "act one step at a time and look at each screenshot before the next step. "
        "Prefer clicking/filling by visible text over pixel coordinates."
    )
    input_model = BrowserInput

    def is_read_only(self, inp: BrowserInput) -> bool:
        # Auto-allowed so the see→act→see loop flows without a prompt per click.
        return True

    async def call(
        self, inp: BrowserInput, ctx: ToolUseContext
    ) -> AsyncIterator[ToolYield]:
        from compass.services.agent_browser import get_agent_browser
        from compass.services.screenshot import store_png

        b = get_agent_browser(ctx.session_id)
        try:
            if inp.action == "navigate":
                if not inp.url:
                    yield ToolOutput("browser navigate needs a `url`.", is_error=True)
                    return
                await b.navigate(inp.url)
                self._register_session(ctx, b.current_url() or inp.url)
            elif inp.action == "click":
                if inp.target:
                    await b.click(inp.target)
                else:
                    await b.click_xy(inp.x, inp.y)
            elif inp.action == "fill":
                await b.fill(inp.target, inp.value)
            elif inp.action == "type":
                await b.type_text(inp.value)
            elif inp.action == "key":
                await b.press(inp.key)
            elif inp.action == "scroll":
                await b.scroll(inp.dy)
            elif inp.action == "read":
                text = await b.read_text()
                yield ToolOutput(f"Page text at {b.current_url()}:\n\n{text}")
                return
        except Exception as err:  # noqa: BLE001 — surface + still show the page state
            sid = await self._capture(b, ctx)
            where = f" screenshot://{sid}" if sid else ""
            yield ToolOutput(
                f"browser {inp.action} failed: {err}.{where}", is_error=True
            )
            return

        sid = await self._capture(b, ctx)
        url = b.current_url()
        self._register_session(ctx, url)  # a click may have navigated
        title = await b.title()
        shot = f" screenshot://{sid}" if sid else ""
        # `Title:` and `Now at` are parsed by the UI to render a browser-preview
        # card (app screenshot + Open button), like claude.ai.
        yield ToolOutput(
            f"Done: {inp.action}. Title: {title}. Now at {url}.{shot}\n"
            "Look at the screenshot before deciding the next action."
        )

    def _register_session(self, ctx: ToolUseContext, url: str) -> None:
        """Surface the live browser session in the Background tasks panel with an
        Open/Preview URL (claude.ai's browser-session card)."""
        if not url:
            return
        try:
            from compass.services.background_tasks import registry

            registry.register_browser(
                ctx.session_id, url, workspace_id=getattr(ctx, "workspace_id", None)
            )
        except Exception:  # noqa: BLE001 — panel visibility must never break a turn
            pass

    async def _capture(self, browser, ctx: ToolUseContext) -> str:
        """Screenshot the page: cache it for the UI (screenshot://id) and queue
        it as a vision image for the model. Returns the cache id."""
        from compass.services.screenshot import store_png

        try:
            png = await browser.screenshot()
        except Exception:  # noqa: BLE001
            return ""
        sid, _w, _h = store_png(png)
        ctx.pending_vision.append(
            "data:image/png;base64," + base64.b64encode(png).decode()
        )
        return sid
