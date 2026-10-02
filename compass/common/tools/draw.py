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
            "A brief for an illustrator, not the request you were given. "
            "Four or five sentences: the subject and what it is doing, the "
            "setting and time of day, the medium and finish (a photograph "
            "and its lens, or an illustration and its technique), the light, "
            "the palette, the composition and where the eye goes, the mood. "
            "Decide what was left unspecified rather than leaving it vague — "
            "vague briefs are what produce flat, generic pictures.\n"
            "Text in the image: if this picture IS the deliverable, put the "
            "words that must appear in quotes, keep them short, and say "
            "where they sit. If it is going INSIDE a layout you are also "
            "writing — a web page, a design — ask for no lettering and let "
            "your own markup carry the words, which keeps them crisp and "
            "selectable. Generated lettering is unreliable past a few words "
            "either way, so never ask for a paragraph."
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
        "Draw a real picture from a description and get back a URL. Use it "
        "whenever something visual is asked for and a picture is the thing "
        "wanted: a poster, a hero image, an illustration, a texture, a "
        "photograph that does not exist. The image is stored and the URL is "
        "permanent, so it can go straight into an <img src> or be handed to "
        "the person as the answer.\n"
        "Where you are writing a layout yourself, this is for the "
        "photographic and illustrative parts only: charts, diagrams, icons, "
        "wireframes and UI chrome you draw in SVG or CSS, where the text "
        "stays crisp and the colours still answer to the palette. Where the "
        "picture IS the deliverable, draw the whole thing — including its "
        "own short lettering."
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
        await _file_it(picture, "image", ctx, inp.prompt)
        yield ToolOutput(
            f"Drew it. Use this exact URL as the image source:\n{picture.url}\n"
            f"({picture.width}x{picture.height}, {picture.bytes / 1024:.0f}KB)"
        )


class EditInput(BaseModel):
    image: str = Field(
        description=(
            "The URL of the picture to change, exactly as it was given to "
            "you — /v1/media/generated/<id>.png. Only pictures Compass drew "
            "can be edited."
        )
    )
    prompt: str = Field(
        description=(
            "What to change, said as a change rather than as a new brief. "
            "Name the one thing that moves and say the rest stays: 'Change "
            "the headline to read MYSURU, keep everything else identical' "
            "beats 'a poster for Mysuru'. A description that restates the "
            "whole picture gets you a different picture, not an edit."
        )
    )
    shape: Literal["square", "landscape", "portrait"] = Field(
        default="portrait",
        description="The aspect to return. Match the original unless the "
                    "person asked for a different one.",
    )


class EditImageTool(Tool):
    name = "edit_image"
    description = (
        "Change a picture Compass already drew — fix the wording, swap a "
        "colour, move something, take something out — and get back a URL for "
        "the new version. The original is left alone: an edit is a new "
        "picture, because the old one is in the conversation above and "
        "rewriting it would change what was already said."
    )
    input_model = EditInput

    def is_read_only(self, inp: EditInput) -> bool:
        return True

    async def call(self, inp: EditInput, ctx: ToolUseContext) -> AsyncIterator[ToolYield]:
        picture = await images.edit(
            inp.image, inp.prompt, size=_SHAPES.get(inp.shape, _SHAPES["portrait"])
        )
        if not picture.ok:
            yield ToolOutput(f"Could not edit that: {picture.error}", is_error=True)
            return
        await _file_it(picture, "edit", ctx, inp.prompt, source=inp.image)
        yield ToolOutput(
            f"Edited. Use this exact URL as the image source:\n{picture.url}\n"
            f"({picture.width}x{picture.height}, {picture.bytes / 1024:.0f}KB)"
        )


async def _file_it(
    picture, kind: str, ctx: ToolUseContext, prompt: str, *, source: str = "",
) -> None:
    """Write the row that says what this picture is and whose it is.

    Separate from storing the bytes because they answer different questions:
    the blob is how a picture is shown again, and this is how it is *found* —
    everything this person has made, everything this conversation made — from
    a browser that has never seen the machine that drew it.

    Never raises. A picture whose row failed to write is still drawn, still
    stored and still in the reply.
    """
    from compass.common import media_index

    await media_index.record(
        blob_name=picture.url.split("/v1/media/", 1)[-1],
        url=picture.url,
        kind=kind,
        session_id=getattr(ctx, "session_id", "") or "",
        owner=getattr(ctx, "owner", "") or "",
        prompt=prompt,
        width=picture.width,
        height=picture.height,
        size_bytes=picture.bytes,
        source=source,
    )
