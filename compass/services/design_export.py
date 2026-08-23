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
import re
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


# A deck that presents itself shows one slide at a time — the others are
# hidden, stacked or translated off-screen by its own script. Exporting has to
# see all of them, so the whole deck is unfolded first and any chrome the
# author marked as presentation-only is dropped.
_UNFOLD_SLIDES = """
  .slide, [data-slide] {
    display: block !important;
    visibility: visible !important;
    opacity: 1 !important;
    position: static !important;
    transform: none !important;
    inset: auto !important;
  }
  [class*="deck"], [class*="slides"], [id*="deck"], [id*="slides"] {
    height: auto !important;
    max-height: none !important;
    overflow: visible !important;
  }
  [data-export-hide], .deck-nav, .slide-nav, .deck-controls { display: none !important; }
"""


async def _render(html: str, width: int, height: int, *, scale: float = 1):
    """Open the document in Chromium. Caller drives the page, then closes it."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as err:  # pragma: no cover - host without Playwright
        raise RuntimeError(_NO_PLAYWRIGHT) from err

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(args=["--no-sandbox"])
    page = await browser.new_page(
        viewport={"width": width, "height": height}, device_scale_factor=scale
    )
    # set_content rather than a data: URL — @import'd fonts need a real origin
    # to resolve against, and about:blank gives them one.
    await page.set_content(html, wait_until="load")
    await page.wait_for_timeout(700)  # let webfonts and entry animations settle

    async def close() -> None:
        await browser.close()
        await pw.stop()

    return page, close


async def _unfold(page) -> int:
    """Show every slide at once. Returns how many the document has."""
    count = 0
    for selector in _SLIDE_SELECTORS:
        found = await page.query_selector_all(selector)
        if len(found) > 1:
            count = len(found)
            break
    if count:
        await page.add_style_tag(content=_UNFOLD_SLIDES)
        await page.wait_for_timeout(250)  # let the relayout settle
    return count


async def _slide_box(page) -> dict | None:
    """The page box a deck should print at, or None if this isn't a deck.

    A deck built from fixed 16:9 sections prints one slide per page at that
    size. A deck whose slides are whatever height their content needs — which
    is most of them once unfolded — has to print at the tallest, or the long
    ones split across pages and the short ones leave a gap.
    """
    for selector in _SLIDE_SELECTORS:
        slides = await page.query_selector_all(selector)
        if len(slides) <= 1:
            continue
        boxes = [b for b in [await s.bounding_box() for s in slides] if b]
        if not boxes:
            continue
        widest = max(b["width"] for b in boxes)
        tallest = max(b["height"] for b in boxes)
        return {"width": widest, "height": tallest + 2}
    return None


async def to_pdf(html: str, *, width: int = 1280) -> bytes:
    page, close = await _render(html, width, 900)
    try:
        # print_background keeps the palette; the design decides its own size,
        # so the page box follows the rendered width rather than a paper size.
        await _unfold(page)
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
    # Twice the device scale: the export is a picture of the design, and a
    # 1x screenshot of a page is soft the moment anyone zooms it.
    page, close = await _render(html, width, 900, scale=2)
    try:
        await _unfold(page)
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


# What a finished design is checked against before it is handed over. The
# thresholds are the ones everybody agrees on: WCAG AA for contrast, the usual
# 44px for something you have to hit with a finger.
_AUDIT_JS = r"""() => {
  const lum = (c) => {
    const m = c.match(/\d+(\.\d+)?/g); if (!m) return null;
    const [r,g,b] = m.slice(0,3).map(Number).map(v => { v/=255;
      return v<=0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); });
    return 0.2126*r + 0.7152*g + 0.0722*b;
  };
  const bgOf = (el) => { let n=el;
    while (n) { const b=getComputedStyle(n).backgroundColor;
      if (b && !/rgba\(0, 0, 0, 0\)|transparent/.test(b)) return b; n=n.parentElement; }
    return 'rgb(255,255,255)'; };
  const ratio = (a,b) => { const L1=lum(a), L2=lum(b); if(L1==null||L2==null) return null;
    const [hi,lo]=L1>L2?[L1,L2]:[L2,L1]; return (hi+0.05)/(lo+0.05); };

  // the whole page: the shell around the screens carries controls too, and a
  // stretched icon or a thin-grey label in the header counts just as much
  const root = document.body;

  let worst = 99, worstText = '';
  for (const el of root.querySelectorAll('p,td,li,span,h3,h4,label,button,a,div')) {
    const t=(el.textContent||'').trim();
    if (!t || el.children.length || el.offsetParent === null) continue;
    const cs=getComputedStyle(el);
    const big = parseFloat(cs.fontSize) >= 24
             || (parseFloat(cs.fontSize) >= 18.66 && parseInt(cs.fontWeight,10) >= 700);
    if (big) continue;
    const r=ratio(cs.color, bgOf(el));
    if (r && r < worst) { worst=r; worstText=t.slice(0,32); }
  }

  const tiny = [...root.querySelectorAll('button, a, input[type=checkbox], [role=button]')]
      .filter(b => b.offsetParent !== null)
      .map(b => ({r: b.getBoundingClientRect(), t: (b.textContent||'').trim().slice(0,20)}))
      .filter(x => x.r.width && (x.r.height < 24 || x.r.width < 24));

  // how loud is the colour? count strongly-saturated pixels' worth of area
  let painted = 0, total = 0;
  const hueOf = (c) => { const m=c.match(/\d+(\.\d+)?/g); if(!m) return null;
    let [r,g,b]=m.slice(0,3).map(Number).map(v=>v/255);
    const mx=Math.max(r,g,b), mn=Math.min(r,g,b), l=(mx+mn)/2;
    const sat = mx===mn ? 0 : (l>0.5 ? (mx-mn)/(2-mx-mn) : (mx-mn)/(mx+mn));
    return {sat, l}; };
  for (const el of root.querySelectorAll('*')) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height || el.offsetParent === null) continue;
    const area = Math.min(r.width, 1600) * Math.min(r.height, 1200);
    if (el.children.length === 0 || getComputedStyle(el).backgroundColor !== 'rgba(0, 0, 0, 0)') {
      const h = hueOf(getComputedStyle(el).backgroundColor);
      total += area;
      if (h && h.sat > 0.30 && h.l > 0.15 && h.l < 0.75) painted += area;
    }
  }
  const accentShare = total ? Math.round(painted / total * 100) : 0;

  const bigIcons = [...root.querySelectorAll('svg')]
      .filter(v => v.offsetParent !== null)
      .map(v => v.getBoundingClientRect())
      .filter(r => r.width > 64 && r.height > 64 && r.width < 400).length;

  const emptyTables = [...root.querySelectorAll('table')]
      .filter(t => t.querySelectorAll('tbody tr').length === 0).length;

  // A title that is a span is a title only to the eye.
  const headings = root.querySelectorAll('h1,h2,h3').length;

  // Numbers belong in the mono. A column of them in the sans will not line up.
  const isMono = (f) => /mono|menlo|consolas|courier/i.test(f);
  const sansNumbers = [...root.querySelectorAll('td,th,span,div,p,strong,b,dd')]
      .filter(e => e.children.length === 0 && e.offsetParent !== null)
      .filter(e => {
        const t = (e.textContent || '').trim();
        return t.length > 1 && /^[$€£¥+\-–\s]*\d[\d.,\s]*[%kKMmh€$]*$/.test(t);
      })
      .filter(e => !isMono(getComputedStyle(e).fontFamily));

  // How many measures were given a colour of their own? One hue across a page
  // of charts is the dull failure; it is the one to name out loud.
  const fams = new Set();
  for (const el of root.querySelectorAll('*')) {
    const r = el.getBoundingClientRect();
    if (r.width * r.height < 120 || el.offsetParent === null) continue;
    const cs = getComputedStyle(el);
    for (const v of [cs.backgroundColor, cs.fill]) {
      const m = v && v.match(/\d+(\.\d+)?/g);
      if (!m || m.length < 3) continue;
      if (m.length > 3 && Number(m[3]) < 0.4) continue;
      const [cr, cg, cb] = m.slice(0, 3).map(Number);
      const mx = Math.max(cr, cg, cb), mn = Math.min(cr, cg, cb), d = mx - mn;
      if (d < 45) continue;                       // neutral, not a hue
      let h = mx === cr ? 60 * (((cg - cb) / d) % 6)
            : mx === cg ? 60 * (((cb - cr) / d) + 2)
                        : 60 * (((cr - cg) / d) + 4);
      fams.add(Math.round(((h + 360) % 360) / 40));
    }
  }

  // Anything whose job is to carry a value. Counted on the screen in front of
  // us only: the colours above were measured there, and a prototype's other
  // screens are display:none, so counting theirs would compare two pages.
  const dataBits = [...root.querySelectorAll(
    'svg rect, [class*=bar], [class*=meter], [class*=chart], [class*=heat], progress'
  )].filter(e => e.getBoundingClientRect().width > 0).length;

  // The signed-in person belongs at the foot of the sidebar, inside it.
  let sidebarFootGap = null;
  const cols = [...root.querySelectorAll('aside, nav, [class*=sidebar], [class*=sidenav]')]
      .filter(e => { const r = e.getBoundingClientRect();
                     return r.height > innerHeight * 0.7 && r.width > 120 && r.width < 340; });
  if (cols.length) {
    const sr = cols[0].getBoundingClientRect();
    const kids = [...cols[0].children].filter(k => k.getBoundingClientRect().height > 8);
    if (kids.length) {
      sidebarFootGap = Math.round(sr.bottom - kids[kids.length - 1].getBoundingClientRect().bottom);
    }
  }

  return {
    headings,
    sansNumbers: sansNumbers.length,
    sansNumberFirst: sansNumbers.length ? sansNumbers[0].textContent.trim().slice(0, 18) : '',
    hues: fams.size,
    dataBits,
    sidebarFootGap,
    contrast: Math.round(worst*100)/100, contrastOn: worstText,
    tiny: tiny.length, tinyFirst: tiny.length ? tiny[0].t : '',
    bigIcons,
    accentShare,
    emptyTables,
    overflowPx: Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth)
  };
}"""


async def element_shot(html: str, path: str, *, pad: int = 24) -> str:
    """A picture of one element, in place, as a data: URL.

    Rendered from the document it lives in and cropped to the element with a
    little of its surroundings, so a question like "this is cramped" has
    something to be cramped against. Returns "" if it cannot be taken —
    a missing photograph is not worth failing an edit over.
    """
    if not html or not path:
        return ""
    try:
        page, close = await _render(html, 1280, 900, scale=2)
    except Exception:  # noqa: BLE001 - no browser on this host
        return ""
    try:
        box = await page.evaluate(
            """(sel) => {
              const el = document.querySelector(sel);
              if (!el) return null;
              const r = el.getBoundingClientRect();
              if (!r.width || !r.height) return null;
              return {x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height};
            }""",
            path,
        )
        if not box:
            return ""
        full_w = await page.evaluate("() => document.documentElement.scrollWidth")
        full_h = await page.evaluate(
            "() => Math.max(document.documentElement.scrollHeight, document.body.scrollHeight)"
        )
        clip = {
            "x": max(0, box["x"] - pad),
            "y": max(0, box["y"] - pad),
            "width": min(box["w"] + pad * 2, full_w),
            "height": min(box["h"] + pad * 2, full_h),
        }
        shot = await page.screenshot(type="png", clip=clip)
        import base64

        return "data:image/png;base64," + base64.b64encode(shot).decode()
    except Exception:  # noqa: BLE001
        return ""
    finally:
        try:
            await close()
        except Exception:  # noqa: BLE001
            pass


async def audit(html: str) -> list[str]:
    """Look at the finished design the way a reviewer would, and say what is
    wrong with it. Never raises: a check that fails is not a design that fails."""
    findings: list[str] = []
    try:
        page, close = await _render(html, 1280, 900)
    except Exception:  # noqa: BLE001 - no browser on this host
        return []
    try:
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)[:120]))
        await page.wait_for_timeout(500)
        found = await page.evaluate(_AUDIT_JS)

        if errors:
            findings.append(f"its own script threw ({errors[0]})")
        if found.get("contrast", 99) < 4.5:
            findings.append(
                f"contrast {found['contrast']}:1 on \u201c{found['contrastOn']}\u201d"
                " \u2014 below the 4.5:1 floor"
            )
        if found.get("tiny"):
            findings.append(
                f"{found['tiny']} hit area(s) under 24px, starting with "
                f"\u201c{found['tinyFirst']}\u201d"
            )
        if found.get("bigIcons"):
            findings.append(
                f"{found['bigIcons']} icon(s) blown up past 64px — an svg with "
                "no width in a flex row"
            )
        if found.get("accentShare", 0) > 32:
            findings.append(
                f"strong colour covers about {found['accentShare']}% of the page "
                "— colour belongs on the data and the status, not on the chrome"
            )
        if not found.get("headings"):
            findings.append(
                "no heading element anywhere — the titles are spans and divs, so "
                "only their type size says they are titles"
            )
        if found.get("sansNumbers", 0) > 6:
            findings.append(
                f"{found['sansNumbers']} numbers set in the sans rather than the "
                f"mono, starting with \u201c{found['sansNumberFirst']}\u201d — a "
                "column of them will not line up"
            )
        if found.get("dataBits", 0) >= 6 and found.get("hues", 0) <= 1:
            findings.append(
                "every measure on the page is drawn in the same colour — a second "
                "measure earns a second hue"
            )
        gap = found.get("sidebarFootGap")
        if gap is not None and gap > 24:
            findings.append(
                f"the sidebar's last block stops {gap}px short of its foot — the "
                "signed-in person belongs at the bottom of it"
            )
        if found.get("emptyTables"):
            findings.append(f"{found['emptyTables']} table(s) with no rows")

        for width, label in ((1024, "1024"), (640, "640")):
            await page.set_viewport_size({"width": width, "height": 900})
            await page.wait_for_timeout(250)
            over = await page.evaluate(
                "() => Math.max(0, document.documentElement.scrollWidth"
                " - document.documentElement.clientWidth)"
            )
            if over > 2:
                findings.append(f"{over}px of sideways scroll at {label}px")
    except Exception:  # noqa: BLE001 - the check is a courtesy, not a gate
        return []
    finally:
        try:
            await close()
        except Exception:  # noqa: BLE001
            pass
    return findings


def split_page(html: str) -> tuple[str, str, str]:
    """Pull a self-contained page apart into markup, stylesheet and script.

    A design is written as one file so it can be dropped anywhere. An archive
    is the other thing a person wants — the same prototype as a small project
    they can open, read and edit — so the styles become styles.css, the
    behaviour becomes app.js, and the markup links to both. Declared JSON (the
    tweak sheet) stays where it is: that is data, not behaviour.
    """
    css_parts: list[str] = []
    js_parts: list[str] = []

    def take_style(m):
        css_parts.append(m.group(1).strip())
        return ""

    def take_script(m):
        attrs = m.group(1) or ""
        if "src=" in attrs or "application/json" in attrs:
            return m.group(0)          # a link out, or declared data: leave it
        js_parts.append(m.group(2).strip())
        return ""

    markup = re.sub(r"<style[^>]*>(.*?)</style>", take_style, html, flags=re.S | re.I)
    markup = re.sub(r"<script([^>]*)>(.*?)</script>", take_script, markup, flags=re.S | re.I)

    css = "\n\n".join(x for x in css_parts if x)
    js = "\n\n".join(x for x in js_parts if x)

    if css:
        link = '<link rel="stylesheet" href="styles.css">'
        markup = (
            re.sub(r"</head>", "  " + link + "\n</head>", markup, count=1, flags=re.I)
            if re.search(r"</head>", markup, re.I)
            else link + markup
        )
    if js:
        tag = '<script src="app.js" defer></script>'
        markup = (
            re.sub(r"</body>", "  " + tag + "\n</body>", markup, count=1, flags=re.I)
            if re.search(r"</body>", markup, re.I)
            else markup + tag
        )

    markup = re.sub(r"\n{3,}", "\n\n", markup)
    return markup, css, js


SERVE_PY = (
    '#!/usr/bin/env python3\n'
    '"""Run this prototype:  python3 serve.py\n\n'
    'Serves this folder and opens it in your browser. Nothing to install —\n'
    'it uses only what comes with Python.\n'
    '"""\n\n'
    'import http.server\n'
    'import socketserver\n'
    'import webbrowser\n'
    'from pathlib import Path\n\n'
    'PORT = 8420\n'
    'HERE = Path(__file__).resolve().parent\n\n\n'
    'class Handler(http.server.SimpleHTTPRequestHandler):\n'
    '    def __init__(self, *args, **kwargs):\n'
    '        super().__init__(*args, directory=str(HERE), **kwargs)\n\n'
    '    def log_message(self, fmt, *args):\n'
    '        pass\n\n\n'
    'if __name__ == "__main__":\n'
    '    with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as httpd:\n'
    '        url = f"http://127.0.0.1:{PORT}/"\n'
    '        print(f"Prototype running at {url}  (ctrl-c to stop)")\n'
    '        webbrowser.open(url)\n'
    '        try:\n'
    '            httpd.serve_forever()\n'
    '        except KeyboardInterrupt:\n'
    '            print("Stopped.")\n'
)


def project_archive(
    *,
    name: str,
    prompt: str,
    pages: list[dict],
    files: list[tuple[str, bytes]] | None = None,
    system_notes: str = "",
) -> bytes:
    """The project as something you can run: each page as markup, its styles
    and its behaviour in their own files, whatever the project was given, and
    a serve.py that opens the lot in a browser. Instant — nothing is rendered
    and no model is asked."""

    def safe(text: str) -> str:
        keep = "".join(c for c in text if c.isalnum() or c in " ._-").strip()
        return keep or "page"

    readme = [
        "# " + name,
        "",
        "A prototype from Compass Design.",
        "",
        "## Run it",
        "",
        "```",
        "python3 serve.py",
        "```",
        "",
        "That serves this folder and opens it. Clicking works, so do the tabs,",
        "the selections and the notices — it is the prototype, not a picture of",
        "it. Opening index.html directly works too.",
        "",
        "## Brief",
        "",
        prompt or "(no brief recorded)",
        "",
        "## What is in here",
        "",
    ]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, page in enumerate(pages):
            stem = "index" if i == 0 else safe(
                (page.get("name") or f"page-{i + 1}").rsplit(".", 1)[0]
            ).replace(" ", "-").lower()
            markup, css, js = split_page(page.get("html") or "")

            # each page links to its own pair, so pages stay independent
            if stem != "index":
                markup = markup.replace('href="styles.css"', f'href="{stem}.css"')
                markup = markup.replace('src="app.js"', f'src="{stem}.js"')
            z.writestr(f"{stem}.html", markup)
            readme.append(f"- `{stem}.html` — the markup for {page.get('name') or stem}.")
            if css:
                z.writestr(f"{stem if stem != 'index' else 'styles'}.css", css)
                readme.append(
                    f"- `{stem if stem != 'index' else 'styles'}.css` — its styles."
                )
            if js:
                z.writestr(f"{stem if stem != 'index' else 'app'}.js", js)
                readme.append(
                    f"- `{stem if stem != 'index' else 'app'}.js` — what it does when "
                    "you click: navigation, tabs, selection, notices."
                )

        z.writestr("serve.py", SERVE_PY)
        readme.append("- `serve.py` — runs it locally; needs nothing installed.")

        for rel, blob in files or []:
            z.writestr(f"files/{rel}", blob)
        if files:
            readme.append(
                f"- `files/` — the {len(files)} file(s) attached to this project."
            )
        if system_notes:
            z.writestr("design-system.md", system_notes)
            readme.append("- `design-system.md` — the system this design follows.")

        z.writestr("README.md", "\n".join(readme) + "\n")
    return buf.getvalue()


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


async def to_pptx(html: str, *, width: int = 1600) -> bytes:
    """Render each slide to an image and lay them out as a deck.

    HTML and PowerPoint share no layout model, so a faithful export means
    rendering rather than translating. Two things decide whether the result
    looks like the design or like a photocopy of it:

    * the deck's page is sized to the slides' own aspect, so nothing is
      stretched to fit a 16:9 box it was never drawn in; and
    * the render happens at twice the device scale, because a slide laid out
      at 700-odd CSS pixels stretched across thirteen inches is about 50 DPI
      — which is exactly what "pixelated" looks like.

    A slide that doesn't share the deck's aspect is fitted inside it and
    centred on the design's own background rather than distorted.
    """
    try:
        from pptx import Presentation
        from pptx.dml.color import RGBColor
        from pptx.util import Emu
    except ImportError as err:  # pragma: no cover - host without python-pptx
        raise RuntimeError(_NO_PPTX) from err

    try:
        from playwright.async_api import async_playwright
    except ImportError as err:  # pragma: no cover - host without Playwright
        raise RuntimeError(_NO_PLAYWRIGHT) from err

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(args=["--no-sandbox"])
    try:
        page = await browser.new_page(
            viewport={"width": width, "height": 900}, device_scale_factor=2
        )
        await page.set_content(html, wait_until="load")
        await page.wait_for_timeout(700)
        await _unfold(page)

        background = await page.evaluate(
            "getComputedStyle(document.body).backgroundColor || 'rgb(255,255,255)'"
        )

        shots: list[tuple[bytes, float]] = []   # (png, aspect)
        for selector in _SLIDE_SELECTORS:
            slides = await page.query_selector_all(selector)
            if len(slides) > 1:
                for slide in slides:
                    box = await slide.bounding_box()
                    if not box or box["width"] < 2 or box["height"] < 2:
                        continue
                    try:
                        shots.append(
                            (await slide.screenshot(type="png"), box["width"] / box["height"])
                        )
                    except Exception:  # noqa: BLE001 - a slide with no box
                        continue
                break
        if not shots:  # not a deck — one slide holding the whole design
            box = await page.evaluate(
                "[document.documentElement.scrollWidth, document.documentElement.scrollHeight]"
            )
            shots = [
                (
                    await page.screenshot(full_page=True, type="png"),
                    (box[0] / box[1]) if box[1] else 16 / 9,
                )
            ]
    finally:
        await browser.close()
        await pw.stop()

    # A deck drawn to one shape keeps that shape. A deck whose slides are
    # whatever height their content needs — which is most of them once
    # unfolded — gets the standard 16:9 canvas, and each slide is fitted
    # inside it rather than forced into it.
    aspects = [a for _, a in shots]
    uniform = max(aspects) - min(aspects) <= min(aspects) * 0.05
    aspect = aspects[0] if uniform else 16 / 9

    deck = Presentation()
    deck.slide_width = Emu(12192000)                    # 13.333in, the usual canvas
    deck.slide_height = Emu(int(12192000 / aspect))
    blank = deck.slide_layouts[6]
    fill = _rgb(background)

    for png, aspect in shots:
        slide = deck.slides.add_slide(blank)
        if fill:
            slide.background.fill.solid()
            slide.background.fill.fore_color.rgb = RGBColor(*fill)
        # Contain, never stretch: a slide of a different shape is centred.
        page_w, page_h = deck.slide_width, deck.slide_height
        w = page_w
        h = int(page_w / aspect)
        if h > page_h:
            h = page_h
            w = int(page_h * aspect)
        slide.shapes.add_picture(
            io.BytesIO(png), int((page_w - w) / 2), int((page_h - h) / 2), width=w, height=h
        )

    out = io.BytesIO()
    deck.save(out)
    return out.getvalue()


def _rgb(css: str) -> tuple[int, int, int] | None:
    """The r,g,b of a computed CSS colour, if it is opaque enough to matter."""
    import re

    m = re.match(r"rgba?\(([^)]+)\)", css or "")
    if not m:
        return None
    parts = [p.strip() for p in m.group(1).replace("/", ",").split(",")]
    try:
        r, g, b = (int(float(parts[i])) for i in range(3))
    except (ValueError, IndexError):
        return None
    if len(parts) > 3:
        try:
            if float(parts[3]) < 0.5:
                return None
        except ValueError:
            pass
    return r, g, b
