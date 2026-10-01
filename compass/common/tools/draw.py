"""generate_image — draw a picture, for any agent that needs one.

Shared rather than per-module because the need is: Home cuts a teaser and has
no footage to cut, Code writes a page and has no hero for it, a mission
builds a product that needs a placeholder, a design wants a photograph it
cannot draw in CSS. One tool, offered everywhere a loop runs.

Offered only when there is a deployment to run it on. A model told it can
draw, that then cannot, spends a turn discovering that — so the tool is left
out of the list entirely on an install without an image model, which is the
difference between a capability that is missing and one that is broken.

What it returns is a URL, not bytes. A picture is a megabyte; a turn that
answers with one has spent its context on something the person cannot read,
and the model only ever needs the address to put in an `<img>`.
"""

from __future__ import annotations

from typing import AsyncIterator, Literal

from pydantic import BaseModel, Field

from compass.common.gateway import images
from compass.common.tools.base import Tool, ToolOutput, ToolUseContext, ToolYield


class DrawInput(BaseModel):
    prompt: str = Field(
        description=(
            "What to draw, described the way you would describe it to an "
            "illustrator: the subject, the composition, the medium and the "
            "palette. 'A marigold and vermilion Ganpati motif, flat vector, "
            "gold linework, on cream' beats 'a festival image'. Say what must "
            "NOT be in it too — generated lettering is unreliable, so ask for "
            "no text when the layout supplies its own."
        )
    )
    shape: Literal["square", "landscape", "portrait"] = Field(
        default="square",
        description=(
            "The aspect to draw at. Pick the one the slot wants: a hero "
            "banner is landscape, a phone screen is portrait."
        ),
    )
    quality: Literal["low", "medium", "high"] = Field(
        default="high",
        description=(
            "'low' is a draft and is much faster — right for a placeholder "
            "inside a mockup. Use 'high' for the one picture a person will "
            "actually look at."
        ),
    )
    transparent: bool = Field(
        default=False,
        description=(
            "Draw on transparency rather than a background, for a logo or a "
            "cut-out that sits over something else."
        ),
    )


_SHAPES = {"square": "1024x1024", "landscape": "1536x1024", "portrait": "1024x1536"}


class DrawTool(Tool):
    name = "generate_image"
    description = (
        "Draw an image from a description and get back a URL you can put in "
        "an <img src>. Use it for photographic or illustrative content — a "
        "hero image, a texture, an illustration, a placeholder photograph "
        "inside a mockup. Do NOT use it for charts, diagrams, icons, logos "
        "you were given, wireframes or anything with text in it: those are "
        "drawn in SVG or CSS, where the text is crisp and the colours can be "
        "changed afterwards. The image is stored and the URL is permanent."
    )
    input_model = DrawInput

    def is_read_only(self, inp: DrawInput) -> bool:
        # It writes a file nobody asked about and touches no workspace. It
        # costs money, which is what the permission gate is for elsewhere —
        # but so does every turn, and a picture is cheaper than the turn that
        # asked for it.
        return True

    async def call(self, inp: DrawInput, ctx: ToolUseContext) -> AsyncIterator[ToolYield]:
        picture = await images.draw(
            inp.prompt,
            size=_SHAPES.get(inp.shape, _SHAPES["square"]),
            quality=inp.quality,
            transparent=inp.transparent,
        )
        if not picture.ok:
            # Handed back as a tool error so the model can decide: write the
            # page without the picture, or try a prompt the filter accepts.
            yield ToolOutput(f"Could not draw that: {picture.error}", is_error=True)
            return
        yield ToolOutput(
            f"Drew it. Use this exact URL as the image source:\n{picture.url}\n"
            f"({picture.width}x{picture.height}, {picture.bytes / 1024:.0f}KB)"
        )
