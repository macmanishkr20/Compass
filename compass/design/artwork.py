"""Artwork in a design — which templates get it, and filling it in.

The design module writes HTML in one completion; it has no tool loop to call
`generate_image` from. So the picture is asked for in the markup and drawn
afterwards: the model writes

    <img data-draw="a marigold Ganpati motif, flat vector, gold linework"
         data-shape="portrait" alt="...">

and this replaces the `data-draw` with a real `src` once the document is
written. One pass, every picture at once, after the markup exists — which
also means a design whose artwork fails is still a design.

WHICH TEMPLATES
---------------
Not all of them, and the briefs are why. They are specific about how things
are meant to be drawn, and a raster would be worse at most of it:

  mockups.md  "Charts are drawn ... Inline SVG or CSS, never an image"
  flier.md    "Any ornament is drawn as inline SVG"
  diagram.md  "a technical drawing, not an illustration"
  object3d.md "using CSS 3D transforms"
  animation.md"using CSS keyframes"
  wireframe.md"low-fidelity greyscale ... no colour"
  resume.md   "A photo only if one is supplied"

Those rules are right and this does not override them. Generated imagery is
for *photographic or illustrative content* — a festival poster's motif, a
slide's backdrop, the dish photographs inside a food app's mockup — and the
charts, diagrams, icons and ornament stay as SVG, where the text is crisp and
the colours still answer to the palette.

So the split below is by what the brief asks the output to BE, not by whether
the word "image" appears in it.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import logging
import re

from compass.common.gateway import images

logger = logging.getLogger("compass.design")

#: Templates whose output genuinely wants photographic or illustrative
#: content. Each with the reason, because the next person to add a template
#: has to make this judgement too.
DRAWS = {
    # A poster's whole job is to be looked at from across a room; the
    # occasion's motif is the loudest thing on it after the name.
    "flier",
    # A deck carries backdrops and section imagery between the text slides.
    "slides",
    # A high-fidelity screen is not high-fidelity with grey boxes where the
    # product's photographs go — a food app shows dishes, a listing shows
    # rooms. The charts in it stay SVG; the brief is explicit and right.
    "mockups",
    # The same, at phone size.
    "mobile",
    # A marketing email is a hero banner and some text under it.
    "email",
    # "Animate X" often means animating a thing that has to exist first.
    "animation",
    # The template with no brief at all: whatever was asked for, so allow it
    # and let the request decide.
    "blank",
}

#: Templates that deliberately do not draw, with the reason kept beside them
#: so this reads as a decision rather than an omission.
DOES_NOT_DRAW = {
    "wireframe": "low-fidelity greyscale by definition; artwork would defeat it",
    "diagram": "a technical drawing — SVG keeps the text crisp and the arrows true",
    "object3d": "built from CSS 3D transforms, not rendered to a flat image",
    "document": "typeset paper; the content is the writing",
    "resume": "a photo only if one is supplied, never invented",
    "research": "findings and evidence, written up",
    "colortype": "swatches and specimens are CSS, and must be exact",
}

#: How many pictures one design may ask for. A deck could ask for thirty and
#: each is a slow, paid request; past a handful the wait stops being worth
#: the result, and a model that wants more usually wants decoration.
MAX_PER_DESIGN = 6

#: What the model is told, appended to the prompt for a drawing template.
PROMPT_BLOCK = (
    "ARTWORK. You can commission photographic or illustrative images for "
    "this design. Where one belongs, write an img tag with a `data-draw` "
    "attribute describing the picture and no `src`:\n"
    '  <img data-draw="a marigold and vermilion Ganpati motif, flat vector, '
    'gold linework, on cream, no lettering" data-shape="portrait" '
    'alt="Ganpati motif" class="...">\n'
    "`data-shape` is square, landscape or portrait. Style the element with "
    "your own CSS as usual — it becomes a real image before the design is "
    "shown. Describe it the way you would to an illustrator: subject, "
    "composition, medium, palette.\n"
    "Ask for artwork only where a photograph or an illustration is what "
    "belongs — a hero image, a backdrop, a texture, the product photography "
    "inside a screen. Do NOT ask for charts, diagrams, icons, logos, "
    "wireframe boxes, UI chrome, or anything whose text must be legible: "
    "those you draw yourself in SVG or CSS, where the text is crisp and the "
    "colours follow the palette. Generated lettering is unreliable, so say "
    "'no text' in the description and let your own markup carry the words.\n"
    f"At most {MAX_PER_DESIGN} of them, and fewer is usually better."
)

#: `<img ... data-draw="..." ...>`, however the attributes are ordered and
#: whether or not the tag is self-closed.
_IMG = re.compile(r"<img\b[^>]*\bdata-draw\s*=\s*([\"'])(.*?)\1[^>]*>", re.I | re.S)
_SHAPE = re.compile(r"\bdata-shape\s*=\s*([\"'])(.*?)\1", re.I)


def wanted_by(template: str) -> bool:
    """Whether this template's output should be offered artwork at all."""
    return template in DRAWS


def prompt_block(template: str) -> str:
    """The instruction to append, or "" when this template does not draw or
    this install cannot."""
    if not wanted_by(template) or not images.available():
        return ""
    return PROMPT_BLOCK


async def fill(html: str) -> tuple[str, int, int]:
    """Replace every `data-draw` placeholder with a drawn picture.

    Returns the markup, how many were drawn and how many were asked for. A
    picture that could not be drawn has its element removed rather than left
    pointing at nothing: a broken image icon in the middle of a poster is
    worse than the poster without it, and the layout was written to tolerate
    the slot being empty far better than it tolerates a 404.
    """
    if not html:
        return html, 0, 0
    found = list(_IMG.finditer(html))
    if not found:
        return html, 0, 0
    if not images.available():
        # Asked for but impossible: take the placeholders out so the design
        # renders, rather than leaving `data-draw` tags the browser shows as
        # broken images.
        return _IMG.sub("", html), 0, len(found)

    wanted = found[:MAX_PER_DESIGN]
    shapes = {"square", "landscape", "portrait"}

    async def one(match: re.Match) -> str:
        prompt = html_mod.unescape(match.group(2)).strip()
        shape_m = _SHAPE.search(match.group(0))
        shape = (shape_m.group(2).lower() if shape_m else "square")
        size = {
            "landscape": "1536x1024",
            "portrait": "1024x1536",
        }.get(shape if shape in shapes else "square", "1024x1024")
        # Medium, not the configured default. Measured against this
        # deployment: low 24s, medium 37s, high 112s for one picture. A
        # design's artwork is an element on a page — a backdrop, a photo
        # inside a screen — usually shown at a fraction of its size, and six
        # of them at high quality would add two minutes to a document that
        # already takes minutes. The one picture a person is going to look
        # at closely is the one Home draws, and that one stays high.
        picture = await images.draw(prompt, size=size, quality="medium")
        if not picture.ok:
            logger.warning("design artwork failed (%s): %s", prompt[:60], picture.error)
            return ""
        # The placeholder's own attributes are kept — the model styled it —
        # and only `data-draw` is traded for a `src`.
        tag = match.group(0)
        tag = _IMG_DRAW_ATTR.sub("", tag, count=1)
        return tag[:4] + f' src="{picture.url}"' + tag[4:]

    results = await asyncio.gather(*(one(m) for m in wanted))

    out: list[str] = []
    cursor = 0
    drawn = 0
    for match, replacement in zip(wanted, results):
        out.append(html[cursor:match.start()])
        out.append(replacement)
        cursor = match.end()
        if replacement:
            drawn += 1
    out.append(html[cursor:])
    filled = "".join(out)
    # Anything past the cap is removed rather than left as a placeholder.
    if len(found) > MAX_PER_DESIGN:
        filled = _IMG.sub("", filled)
    return filled, drawn, len(found)


#: Just the attribute, for trading it out of a tag whose other attributes the
#: model wrote and we keep.
_IMG_DRAW_ATTR = re.compile(r"\s*\bdata-draw\s*=\s*([\"']).*?\1", re.I | re.S)
