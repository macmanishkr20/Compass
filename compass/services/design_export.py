"""Exporting a design.

A design is one standalone HTML document, so every format here is produced by
rendering that document in headless Chromium — the same engine the preview
iframe uses, which is what makes the export match what the user sees.

    html   the document itself (served straight from the store)
    pdf    Chromium's print output
    png    a full-page screenshot
    zip    the document plus its design-system notes and a README
    pptx   one PowerPoint slide per marked slide, each a full-bleed render

PPTX needs python-pptx; PDF and PNG need Playwright. Both are optional — a
missing one raises RuntimeError, which the API turns into a 501 rather than
pretending the format exists.
"""

from __future__ import annotations

import io
import zipfile

_NO_PLAYWRIGHT = (
    "Playwright is not installed on the server host "
    "(pip install playwright && playwright install chromium)."
)
_NO_PPTX = "python-pptx is not installed on the server host (pip install python-pptx)."

# What marks one slide. Deliberately explicit — a bare `section` would turn an
# ordinary long page into a stack of half-height pages, so the slides template
# tells the model to mark each slide with class="slide" instead of guessing.
_SLIDE_SELECTORS = ("[data-slide]", ".slide")


async def _render(html: str, width: int, height: int):
    """Open the document in Chromium. Caller drives the page, then closes it."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as err:  # pragma: no cover - host without Playwright
        raise RuntimeError(_NO_PLAYWRIGHT) from err

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(args=["--no-sandbox"])
    page = await browser.new_page(viewport={"width": width, "height": height})
    # set_content rather than a data: URL — @import'd fonts need a real origin
    # to resolve against, and about:blank gives them one.
    await page.set_content(html, wait_until="load")
    await page.wait_for_timeout(700)  # let webfonts and entry animations settle

    async def close() -> None:
        await browser.close()
        await pw.stop()

    return page, close


async def _slide_box(page) -> dict | None:
    """The bounding box of one slide, if this document is a deck."""
    for selector in _SLIDE_SELECTORS:
        slides = await page.query_selector_all(selector)
        if len(slides) > 1:
            return await slides[0].bounding_box()
    return None


async def to_pdf(html: str, *, width: int = 1280) -> bytes:
    page, close = await _render(html, width, 900)
    try:
        # print_background keeps the palette; the design decides its own size,
        # so the page box follows the rendered width rather than a paper size.
        box = await _slide_box(page)
        if box:
            # A deck prints one slide per page. Without the break rule Chromium
            # would flow the slides together and cut them mid-height.
            await page.add_style_tag(
                content=(
                    "@media print{"
                    + ",".join(_SLIDE_SELECTORS)
                    + "{break-after:page;break-inside:avoid}}"
                )
            )
            page_width, page_height = box["width"], box["height"]
        else:
            page_width = width
            # One tall page, so nothing is cut mid-section. scrollHeight alone
            # under-measures when the body's own box is the taller one, which
            # spills the footer onto a second page.
            page_height = await page.evaluate(
                "Math.ceil(Math.max("
                "document.documentElement.scrollHeight,"
                "document.body.scrollHeight,"
                "document.body.getBoundingClientRect().bottom)) + 2"
            )
        return await page.pdf(
            print_background=True,
            width=f"{page_width}px",
            height=f"{page_height}px",
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
        )
    finally:
        await close()


async def to_png(html: str, *, width: int = 1280) -> bytes:
    page, close = await _render(html, width, 900)
    try:
        return await page.screenshot(full_page=True, type="png")
    finally:
        await close()


async def to_thumbnail(html: str) -> bytes:
    """A small PNG of the top of the design, for the projects table.

    Rendered at full width and scaled down by the device pixel ratio rather
    than resized afterwards — that keeps it sharp without needing an imaging
    library on the host.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as err:  # pragma: no cover - host without Playwright
        raise RuntimeError(_NO_PLAYWRIGHT) from err

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(args=["--no-sandbox"])
    try:
        page = await browser.new_page(
            viewport={"width": 1280, "height": 1600}, device_scale_factor=0.25
        )
        await page.set_content(html, wait_until="load")
        await page.wait_for_timeout(500)
        return await page.screenshot(type="png")
    finally:
        await browser.close()
        await pw.stop()


def to_zip(*, name: str, html: str, prompt: str, system_notes: str = "") -> bytes:
    readme = [
        f"# {name}",
        "",
        "Designed with Compass Design.",
        "",
        "## Brief",
        "",
        prompt or "(no brief recorded)",
        "",
        "## Files",
        "",
        "- `index.html` — the design. Open it in any browser; it is self-contained.",
    ]
    if system_notes:
        readme.append("- `design-system.md` — the system this design follows.")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("index.html", html)
        z.writestr("README.md", "\n".join(readme) + "\n")
        if system_notes:
            z.writestr("design-system.md", system_notes)
    return buf.getvalue()


async def to_pptx(html: str, *, width: int = 1600, height: int = 900) -> bytes:
    """Render each slide to a full-bleed image on a 16:9 deck.

    HTML and PowerPoint have no shared layout model, so a faithful export means
    rendering, not translating: the deck is what the browser drew.
    """
    try:
        from pptx import Presentation
        from pptx.util import Emu
    except ImportError as err:  # pragma: no cover - host without python-pptx
        raise RuntimeError(_NO_PPTX) from err

    page, close = await _render(html, width, height)
    try:
        shots: list[bytes] = []
        for selector in _SLIDE_SELECTORS:
            slides = await page.query_selector_all(selector)
            if len(slides) > 1:
                for slide in slides:
                    shots.append(await slide.screenshot(type="png"))
                break
        if not shots:  # not a deck — one slide holding the whole design
            shots = [await page.screenshot(full_page=True, type="png")]
    finally:
        await close()

    deck = Presentation()
    deck.slide_width = Emu(12192000)  # 13.333in — 16:9
    deck.slide_height = Emu(6858000)  # 7.5in
    blank = deck.slide_layouts[6]
    for png in shots:
        slide = deck.slides.add_slide(blank)
        slide.shapes.add_picture(
            io.BytesIO(png), 0, 0, width=deck.slide_width, height=deck.slide_height
        )

    out = io.BytesIO()
    deck.save(out)
    return out.getvalue()
