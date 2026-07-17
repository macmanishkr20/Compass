"""screenshot — capture a running app/page and post it into the chat.

Lets the agent see a frontend/preview and share it for verification, the way
Claude Code screenshots its browser preview. The image is cached server-side
and referenced by a short `screenshot://<id>` token so the model's context
stays small; the surface renders it inline."""

from __future__ import annotations

from typing import AsyncIterator

from pydantic import BaseModel, Field

from compass.tools.base import Tool, ToolOutput, ToolUseContext, ToolYield


class ScreenshotInput(BaseModel):
    url: str = Field(description="URL to open and screenshot (e.g. a local dev server).")
    full_page: bool = Field(default=False, description="Capture the full scrollable page.")


class ScreenshotTool(Tool):
    name = "screenshot"
    description = (
        "Open a URL in a headless browser and capture a screenshot, then post "
        "it into the chat for visual verification of a frontend, preview, or "
        "artifact. Use after starting a dev server or making UI changes. The "
        "image is shown to the user automatically — in your reply just confirm "
        "briefly; do NOT repeat the screenshot:// token or describe the pixels."
    )
    input_model = ScreenshotInput

    def is_read_only(self, inp: BaseModel) -> bool:
        return True

    async def call(
        self, inp: ScreenshotInput, ctx: ToolUseContext
    ) -> AsyncIterator[ToolYield]:
        from compass.services.screenshot import capture_cached

        try:
            sid, w, h = await capture_cached(inp.url, full_page=inp.full_page)
        except RuntimeError as err:
            yield ToolOutput(str(err), is_error=True)
            return
        except Exception as err:  # noqa: BLE001
            yield ToolOutput(f"Could not screenshot {inp.url}: {err}", is_error=True)
            return
        yield ToolOutput(
            f"Screenshot of {inp.url} ({w}x{h}). screenshot://{sid}"
        )
