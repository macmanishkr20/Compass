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

import contextlib
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
    # Listen before the document runs. A syntax error in the design's own
    # script throws while the page is parsing, so a listener attached after
    # set_content never hears the one failure that matters most: it takes the
    # whole prototype's behaviour down with it.
    thrown: list[str] = []
    page.on("pageerror", lambda e: thrown.append(str(e)[:120]))
    page.thrown = thrown  # the caller reads this rather than re-attaching
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



# A design built of page sheets prints one page per sheet. The sizes are the
# ones the templates are told to use, in CSS pixels at 96dpi — 794x1123 is A4,
# which Chromium turns into 595x842pt.
_PAGE_SIZES = ((794, 1123), (816, 1056), (1123, 794), (1056, 816))
# ...and what that paper is actually called, so the PDF says A4 rather than
# 595.5pt rounded up to 596.
_PAPER = {
    (794, 1123): ("210mm", "297mm"),
    (816, 1056): ("8.5in", "11in"),
    (1123, 794): ("297mm", "210mm"),
    (1056, 816): ("11in", "8.5in"),
}

_PAGES_JS = """(sizes) => {
  for (const [w, h] of sizes) {
    let found = [...document.querySelectorAll('body *')].filter((e) => {
      const r = e.getBoundingClientRect();
      // Between two thirds and one and a half pages tall: a sheet, possibly
      // overflowing. Anything taller is the container holding the sheets.
      return Math.abs(r.width - w) < 14 && r.height > h * 0.6 && r.height < h * 1.6;
    });
    // A sheet often holds a content frame of the same width; keep the sheet.
    found = found.filter((e) => !found.some((o) => o !== e && o.contains(e)));
    if (!found.length) continue;
    found.forEach((e) => e.setAttribute('data-dz-page', '1'));
    // :last-of-type only works if the sheets are the last of their tag among
    // their siblings, which is not something a design promises. Say which one
    // is last, or a break after it prints one blank page too many.
    found[found.length - 1].setAttribute('data-dz-page-last', '1');
    // The space between sheets usually belongs to their parent — a column gap
    // or its padding — and zeroing the sheets' own margins leaves it behind.
    if (found[0].parentElement) {
      found[0].parentElement.setAttribute('data-dz-pagewrap', '1');
    }
    return { width: w, height: h, count: found.length };
  }
  return null;
}"""

# What a sheet has to lose to be a page: the ground behind it, the gap between
# it and the next one, and the shadow that suggested it was floating.
_PRINT_PAGES = """
  html, body {
    background: #fff !important;
    margin: 0 !important;
    padding: 0 !important;
  }
  [data-dz-page] {
    margin: 0 !important;
    box-shadow: none !important;
    border-radius: 0 !important;
    break-after: page;
    break-inside: avoid;
    page-break-after: always;
  }
  [data-dz-pagewrap] {
    gap: 0 !important;
    row-gap: 0 !important;
    padding: 0 !important;
    margin: 0 !important;
    background: #fff !important;
    min-height: 0 !important;
    justify-content: flex-start !important;
    align-content: flex-start !important;
  }
  [data-dz-page-last] {
    break-after: auto !important;
    page-break-after: auto !important;
  }
"""


async def _page_box(page) -> dict | None:
    """Tag the sheets this design is built of, and say how big a page is."""
    try:
        found = await page.evaluate(_PAGES_JS, [list(x) for x in _PAGE_SIZES])
    except Exception:  # noqa: BLE001
        return None
    return found or None


async def to_pdf(html: str, *, width: int = 1280) -> bytes:
    page, close = await _render(html, width, 900)
    try:
        # print_background keeps the palette; the design decides its own size,
        # so the page box follows the rendered width rather than a paper size.
        await _unfold(page)
        css_paged = False
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
        elif sheets := await _page_box(page):
            # Built of sheets — a document, a flier, a resume. One PDF page per
            # sheet, at the sheet's size, so A4 comes out as A4.
            await page.add_style_tag(content=_PRINT_PAGES)
            await page.wait_for_timeout(150)
            # Let the CSS name the page. A4 is 841.89pt and a 1123px sheet is
            # 842.25 — a third of a point taller, which is enough to push every
            # sheet onto a second page. Declaring @page at the sheet's own size
            # makes them the same box by construction.
            await page.add_style_tag(content=(
                f"@page {{ size: {sheets['width']}px {sheets['height']}px;"
                " margin: 0; }"
            ))
            css_paged = True
            page_width = page_height = 0
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
        if css_paged:
            return await page.pdf(
                print_background=True,
                prefer_css_page_size=True,
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
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

  // Name the offenders as selectors. An icon button has no text, so reporting
  // the text reported nothing at all — and nothing is not something a
  // stylesheet can target.
  const sel = (e) => {
    const cls = (e.getAttribute('class') || '').trim().split(/\s+/)[0];
    return e.tagName.toLowerCase() + (cls ? '.' + cls : '');
  };
  const tinyEls = [...root.querySelectorAll('button, a, input[type=checkbox], [role=button]')]
      .filter(b => b.offsetParent !== null)
      .filter(b => { const r = b.getBoundingClientRect();
                     return r.width && (r.height < 24 || r.width < 24); });
  const tiny = tinyEls;
  const tinyWhich = [...new Set(tinyEls.map(sel))].slice(0, 5);

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
    tiny: tiny.length, tinyWhich,
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


_NAV_JS = r"""() => {
  // What is on screen right now, as a fingerprint we can compare against.
  const showing = () => {
    const s = [...document.querySelectorAll('section[id], [data-screen], main > section')]
      .find(e => e.getBoundingClientRect().width > 0);
    return s ? (s.id || s.getAttribute('data-screen') || '') + ':' +
               Math.round(s.getBoundingClientRect().height) : '';
  };
  const nav = [...document.querySelectorAll(
    'nav a, nav button, aside a, aside button, [data-screen]'
  )].filter(e => e.getBoundingClientRect().width > 0);
  if (nav.length < 3) return null;            // not a shell with a nav

  const first = showing();
  if (!first) return null;
  for (const item of nav) {
    const was = showing();
    item.click();
    if (showing() !== was) return null;       // something moved: the nav works
  }
  // Nothing moved for any item. Name the second one — the first is usually
  // the screen already showing, so clicking it legitimately changes nothing.
  const t = (nav[1] || nav[0]).textContent.trim().replace(/\s+/g, ' ');
  return t.slice(0, 24);
}"""


_SCREENS_JS = r"""() => {
  // A prototype is many screens in one document, and all but one are hidden.
  // Everything measured above was measured on that one. Show each of the
  // others in turn and look at it properly — a table with no rows or text
  // below the contrast floor on screen four is just as wrong as on screen one.
  const screens = [...document.querySelectorAll('section[id], [data-screen], main > section')]
    .filter(s => s.parentElement && s.parentElement.querySelectorAll(
      'section[id], [data-screen], main > section').length > 1);
  if (screens.length < 2) return [];

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

  const name = (s) => {
    const h = s.querySelector('h1, h2');
    return (h ? h.textContent : (s.id || s.getAttribute('data-screen') || ''))
      .trim().replace(/\s+/g, ' ').slice(0, 24) || 'a screen';
  };

  const was = screens.map(s => s.style.display);
  const out = [];
  for (const target of screens) {
    screens.forEach(s => { s.style.display = s === target ? 'block' : 'none'; });
    void target.offsetHeight;                       // force the relayout

    let worst = 99, worstText = '';
    for (const el of target.querySelectorAll('p,td,li,span,h3,h4,label,button,a,div')) {
      const t = (el.textContent || '').trim();
      if (!t || el.children.length) continue;
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      const cs = getComputedStyle(el);
      const big = parseFloat(cs.fontSize) >= 24
               || (parseFloat(cs.fontSize) >= 18.66 && parseInt(cs.fontWeight,10) >= 700);
      if (big) continue;
      const v = ratio(cs.color, bgOf(el));
      if (v && v < worst) { worst = v; worstText = t.slice(0, 28); }
    }

    const empty = [...target.querySelectorAll('table')]
        .filter(t => t.querySelectorAll('tbody tr').length === 0).length;
    const blown = [...target.querySelectorAll('svg')]
        .map(v => v.getBoundingClientRect())
        .filter(r => r.width > 64 && r.height > 64 && r.width < 400).length;
    // A screen that is a heading and a gap has not been thought about.
    const thin = target.textContent.trim().length < 400;

    out.push({
      name: name(target),
      contrast: Math.round(worst * 100) / 100, contrastOn: worstText,
      empty, blown, thin,
    });
  }
  screens.forEach((s, i) => { s.style.display = was[i]; });
  return out;
}"""


# Templates that produce a printed piece rather than a screen, and the one
# that produces a poster — where the headline has to carry the page.
_SHEET_KINDS = {"flier", "resume", "document", "slides", "email"}
_APP_KINDS = {"mockups", "mobile", "wireframe"}
_POSTER_KINDS = {"flier"}

_SHEET_JS = r"""() => {
  const vis = (e) => e.getBoundingClientRect().width > 0;
  const texts = [...document.querySelectorAll('*')].filter(
    (e) => e.children.length === 0 && (e.textContent || '').trim().length > 1 && vis(e));
  if (!texts.length) return null;
  const size = (e) => parseFloat(getComputedStyle(e).fontSize);
  const mono = (e) => /mono|menlo|courier|consolas/i.test(getComputedStyle(e).fontFamily);

  // The sheet is the block the design prints on, which is the one with a
  // fixed width — not the full-bleed wrapper around it. Taking the largest
  // area finds the wrapper every time, and then nothing matches a page size.
  const PAGES = [[794, 1123], [816, 1056], [1280, 720]];
  const boxes = [...document.querySelectorAll('body, body *')]
    .map((e) => ({ e, r: e.getBoundingClientRect() }))
    .filter((x) => x.r.width >= 300 && x.r.width <= 1400 && x.r.height > 200);
  let sheet = null;
  for (const [w] of PAGES) {                    // a declared page wins outright
    const hit = boxes.find((x) => Math.abs(x.r.width - w) < 12);
    if (hit) { sheet = hit.e; break; }
  }
  if (!sheet) {                                 // else the tallest thing that
    let tall = 0;                               // is not the full window width
    for (const x of boxes) {
      if (x.r.width > innerWidth - 24) continue;
      if (x.r.height > tall) { tall = x.r.height; sheet = x.e; }
    }
  }
  sheet = sheet || document.body;
  const sr = sheet.getBoundingClientRect();

  const sizes = texts.map(size).sort((a, b) => b - a);
  const body = sizes[Math.floor(sizes.length / 2)];
  const head = texts.reduce((a, e) => (size(e) > size(a) ? e : a), texts[0]);
  const hr = head.getBoundingClientRect();

  // A sentence, as opposed to a label or a figure.
  const prose = texts.filter((e) => (e.textContent || '').trim().split(/\s+/).length >= 5);
  const monoProse = prose.filter(mono);

  // The dashboard's vocabulary: a grid of bordered, rounded cards.
  const panels = [...document.querySelectorAll('div,section,article,aside')].filter((e) => {
    const cs = getComputedStyle(e), r = e.getBoundingClientRect();
    if (r.width < 140 || r.height < 70) return false;
    const edged = parseFloat(cs.borderTopWidth) > 0 || cs.boxShadow !== 'none'
               || cs.backgroundColor !== 'rgba(0, 0, 0, 0)';
    return edged && parseFloat(cs.borderTopLeftRadius) > 2;
  });

  // The page it has declared itself to be: A4, Letter, or a 16:9 slide. If it
  // named none of them, A4's proportions stand in — a sheet three times taller
  // than it is wide is a scroll however it was sized.
  let page = null;
  for (const [w, h] of PAGES) if (Math.abs(sr.width - w) < 12) page = h;
  const declared = page !== null;
  if (!declared) page = Math.round(sr.width * 1.414);

  return {
    width: Math.round(sr.width), height: Math.round(sr.height), page, declared,
    headSize: Math.round(sizes[0]), bodySize: Math.round(body),
    headText: head.textContent.trim().slice(0, 34),
    headTop: +((hr.top - sr.top) / Math.max(1, sr.height)).toFixed(2),
    panels: panels.length,
    monoProse: monoProse.length,
    monoProseFirst: monoProse.length ? monoProse[0].textContent.trim().slice(0, 40) : '',
    tiny: texts.filter((e) => size(e) < 11).length,
    words: (document.body.innerText || '').trim().split(/\s+/).length,
  };
}"""


_TWEAKS_JS = r"""() => {
  // A tweak sheet promises knobs that work. Turn each one and see whether the
  // page moves: a control that changes nothing is worse than a missing one,
  // because it makes the panel look broken.
  var el = document.getElementById('tweaks');
  if (!el) return [];
  var sheet;
  try { sheet = JSON.parse(el.textContent || '[]'); } catch (e) { return ['unreadable']; }
  if (!Array.isArray(sheet) || !sheet.length) return [];

  const fingerprint = () => {
    let out = '';
    // Every element, not a sample of them: the one an unsampled knob moves is
    // exactly the one a stride skips, and skipping it calls the knob dead.
    const all = [...document.querySelectorAll('*')].slice(0, 1500);
    for (let i = 0; i < all.length; i++) {
      const e = all[i], r = e.getBoundingClientRect();
      if (!r.width && !r.height) continue;
      const cs = getComputedStyle(e);
      // Where it sits as well as how big it is: a gap or a margin moves things
      // without resizing them, and size alone would call that knob dead. The
      // transform matters for the same reason — a scale leaves the measured
      // box where it was — and so do opacity and visibility.
      out += cs.color + cs.backgroundColor + cs.fontSize + cs.borderTopWidth
           + cs.transform + cs.opacity + cs.visibility + cs.display
           + Math.round(r.width) + 'x' + Math.round(r.height)
           + '@' + Math.round(r.left) + ',' + Math.round(r.top) + ';';
    }
    return out + '#' + document.documentElement.scrollHeight;
  };

  const root = document.documentElement;
  const dead = [];
  for (const knob of sheet) {
    if (!knob || !knob.var) continue;
    const was = root.style.getPropertyValue(knob.var);
    let values = (knob.options || []).slice(0, 4);
    if (knob.type === 'range') {
      // Only a real CSS unit may be appended. A sheet that says "unit":"x"
      // means a multiplier, and --fit: 0.85x is not a value at all — which
      // would make a working knob look dead.
      const u = /^(px|%|em|rem|ch|vw|vh|vmin|vmax|deg|rad|turn|s|ms|fr|pt|cm|mm|in)$/
        .test(knob.unit || '') ? knob.unit : '';
      values = [String(knob.min) + u, String(knob.max) + u];
    }
    if (values.length < 2) continue;           // nothing to compare it against
    const seen = new Set();
    for (const v of values) {
      root.style.setProperty(knob.var, v);
      seen.add(fingerprint());
    }
    if (was) root.style.setProperty(knob.var, was);
    else root.style.removeProperty(knob.var);
    if (seen.size < 2) dead.push(knob.name || knob.var);
  }
  return dead;
}"""


_DOC_KINDS = {"document", "research", "email"}

_DOC_JS = r"""() => {
  // A document is judged as typography: the measure, the reading size, the
  // hierarchy, and whether its tables say what they are.
  const vis = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const paras = [...document.querySelectorAll('p, li')]
    .filter((e) => vis(e) && (e.textContent || '').trim().split(/\s+/).length >= 12);
  if (!paras.length) return null;

  // The body is whatever most of the prose is set in.
  const tally = {};
  for (const e of paras) {
    const cs = getComputedStyle(e);
    const key = Math.round(parseFloat(cs.fontSize)) + '|' + cs.fontFamily;
    (tally[key] = tally[key] || { n: 0, e }).n++;
  }
  const body = Object.values(tally).sort((a, b) => b.n - a.n)[0].e;
  const bs = getComputedStyle(body);
  const size = parseFloat(bs.fontSize);
  const lh = parseFloat(bs.lineHeight) / size;
  // Characters per line: the glyph width of this face, not a guess of 0.5em.
  const probe = document.createElement('span');
  probe.style.cssText = 'position:absolute;visibility:hidden;white-space:pre;font:' + bs.font;
  probe.textContent = 'abcdefghijklmnopqrstuvwxyz ';
  document.body.appendChild(probe);
  const glyph = probe.getBoundingClientRect().width / 27;
  probe.remove();
  const measure = Math.round(body.getBoundingClientRect().width / Math.max(glyph, 1));

  // Headings, largest first, and whether the scale actually steps down.
  const heads = ['h1', 'h2', 'h3'].map((t) => {
    const one = [...document.querySelectorAll(t)].find(vis);
    return one ? Math.round(parseFloat(getComputedStyle(one).fontSize)) : 0;
  });

  const tables = [...document.querySelectorAll('table')].filter(vis);
  // A table with more columns than the measure can carry: every cell becomes a
  // narrow paragraph and the table grows until it leaves the page.
  let cramped = 0, crampedCols = 0, crampedLines = 0;
  for (const t of tables) {
    const row = t.querySelector('tbody tr') || t.querySelector('tr');
    if (!row) continue;
    const cells = [...row.children].filter(vis);
    let worstLines = 0, narrowest = 1e6;
    for (const c of t.querySelectorAll('td')) {
      const cs = getComputedStyle(c);
      const lh = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.4;
      const inner = c.getBoundingClientRect().height
                  - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom);
      const lines = Math.round(inner / Math.max(lh, 1));
      if ((c.textContent || '').trim().split(/\s+/).length > 3) {
        worstLines = Math.max(worstLines, lines);
        narrowest = Math.min(narrowest, c.getBoundingClientRect().width);
      }
    }
    if (worstLines > 3 || (narrowest < 150 && narrowest < 1e6)) {
      cramped++;
      crampedCols = Math.max(crampedCols, cells.length);
      crampedLines = Math.max(crampedLines, worstLines);
    }
  }
  // A caption is a <caption>, or the small line of text just after the table.
  const uncaptioned = tables.filter((t) => {
    if (t.querySelector('caption')) return false;
    const after = t.nextElementSibling;
    if (!after) return true;
    const txt = (after.textContent || '').trim();
    const small = parseFloat(getComputedStyle(after).fontSize) < size;
    return !(small && txt.length > 12 && txt.length < 400);
  }).length;

  // Sheets: a document is pages, and nothing may cross a page's edge.
  const pageish = [...document.querySelectorAll('*')].filter((e) => {
    const r = e.getBoundingClientRect();
    // A page is about as tall as a page. A 794-wide column five thousand
    // pixels tall is the stack the sheets sit in, not one of the sheets.
    return Math.abs(r.width - 794) < 14 && r.height > 640 && r.height < 1800;
  });
  // A page usually holds a content frame of the same width; count the page,
  // not both of them.
  const sheets = pageish.filter((e) => !pageish.some((o) => o !== e && o.contains(e)));
  // A page that has outgrown its paper hides its own overflow: nothing hangs
  // out of a sheet that grew to fit. Measure against the page's height.
  const PAGE_H = 1123;
  let spill = 0;
  for (const sheet of sheets) {
    const sr = sheet.getBoundingClientRect();
    if (sr.height > PAGE_H + 2) { spill++; continue; }
    for (const kid of sheet.querySelectorAll('*')) {
      const r = kid.getBoundingClientRect();
      if (!r.height) continue;
      if (r.bottom > sr.top + PAGE_H + 2 || r.top < sr.top - 2) { spill++; break; }
    }
  }

  // Two cells with no space between them read as one word — "5Settled",
  // "MATURITY (1-5)JUDGEMENT". The eye catches it instantly; nothing else did.
  // Cells always touch — the padding is inside them — so measure where the
  // WORDS end and begin, not the boxes.
  const inkBox = (el) => {
    const range = document.createRange();
    range.selectNodeContents(el);
    const b = range.getBoundingClientRect();
    return b && b.width ? b : null;
  };
  let touching = 0, touchingFirst = '';
  for (const row of document.querySelectorAll('tr')) {
    const cells = [...row.children].filter(vis);
    for (let i = 1; i < cells.length; i++) {
      const ta = (cells[i - 1].textContent || '').trim();
      const tb = (cells[i].textContent || '').trim();
      if (!ta || !tb) continue;
      const a = inkBox(cells[i - 1]), b = inkBox(cells[i]);
      if (!a || !b || Math.abs(a.top - b.top) > 6) continue;
      if (b.left - a.right < 6) {
        touching++;
        if (!touchingFirst) touchingFirst = ta.slice(-16) + tb.slice(0, 16);
      }
    }
  }

  return {
    cramped, crampedCols, crampedLines,
    touching, touchingFirst,
    size: Math.round(size), leading: Math.round(lh * 100) / 100, measure,
    heads, sections: document.querySelectorAll('h2').length,
    tables: tables.length, uncaptioned,
    sheets: sheets.length, spill,
    words: (document.body.innerText || '').trim().split(/\s+/).length,
  };
}"""


_CHECKLIST_JS = r"""() => {
  const vis = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };

  // Focus states. Take a handful of the things a keyboard can reach and see
  // whether anything about them changes when it arrives.
  const reachable = [...document.querySelectorAll(
    'a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])'
  )].filter(vis).slice(0, 12);
  let noFocusRing = 0;
  const look = (e) => {
    const cs = getComputedStyle(e);
    return cs.outlineStyle + cs.outlineWidth + cs.outlineColor
         + cs.boxShadow + cs.borderColor + cs.backgroundColor;
  };
  const had = document.activeElement;
  for (const e of reachable) {
    const before = look(e);
    try { e.focus({ preventScroll: true }); } catch (err) { continue; }
    if (document.activeElement !== e) continue;
    if (look(e) === before) noFocusRing++;
  }
  try { (had && had.focus) ? had.focus({ preventScroll: true }) : document.activeElement.blur(); }
  catch (err) { /* nothing to restore to */ }

  // Spacing from the scale. Every padding, margin and gap the design sets,
  // measured against multiples of four.
  const scale = new Set([0, 4, 8, 12, 16, 20, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128]);
  let spaced = 0, offScale = 0, offFirst = '';
  const props = ['paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
                 'marginTop', 'marginRight', 'marginBottom', 'marginLeft', 'gap'];
  for (const e of [...document.querySelectorAll('body *')].slice(0, 900)) {
    if (!vis(e)) continue;
    const cs = getComputedStyle(e);
    for (const prop of props) {
      const raw = cs[prop];
      if (!raw || raw === 'normal' || raw.indexOf(' ') > -1) continue;
      const v = parseFloat(raw);
      if (!v || v < 0 || v > 160) continue;
      spaced++;
      // Round first: a browser resolves em and rem to fractions of a pixel.
      if (!scale.has(Math.round(v))) {
        offScale++;
        if (!offFirst) offFirst = prop.replace(/([A-Z])/g, ' $1').toLowerCase()
                                    + ' ' + Math.round(v * 10) / 10 + 'px';
      }
    }
  }

  return {
    focusChecked: reachable.length,
    noFocusRing,
    spaced,
    offScale,
    offFirst,
  };
}"""

_TARGETS_JS = """() => {
  // Factory's floor for something a finger has to hit, reported as selectors
  // so a rule can be written against them.
  const small = [...document.querySelectorAll(
    'a[href], button, input:not([type=hidden]), select, [role=button]'
  )].filter((e) => {
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && (r.width < 44 || r.height < 44);
  });
  const sel = (e) => {
    const cls = (e.getAttribute('class') || '').trim().split(/\\s+/)[0];
    return e.tagName.toLowerCase() + (cls ? '.' + cls : '');
  };
  return { n: small.length, which: [...new Set(small.map(sel))].slice(0, 5) };
}"""


def system_expectations(*systems: dict | None) -> dict | None:
    """What an attached design system demands, as things that can be measured.

    Its custom properties are the contract: the colours it names, the faces it
    names, and the property names themselves. Prose in the notes is left for
    the model to read — this is only the part a browser can be asked about.
    """
    import re as _re

    colours: list[str] = []
    faces: list[str] = []
    names: list[str] = []
    label = ""
    for system in systems:
        if not system:
            continue
        label = label or (system.get("name") or "")
        css = (system.get("css") or "")[:20_000]
        for name, value in _re.findall(r"(--[\w-]+)\s*:\s*([^;{}]+)", css):
            value = value.strip()
            names.append(name)
            if _re.fullmatch(r"#[0-9a-fA-F]{3,8}", value):
                colours.append(_hex_to_rgb(value))
            elif value.lower().startswith(("rgb", "hsl")):
                colours.append(value)
            elif any(k in name for k in ("font", "face", "family")) or "," in value:
                first = value.split(",")[0].strip().strip("\"'")
                if first and not first.endswith(("px", "rem", "em", "%")):
                    faces.append(first)
    if not (colours or faces or names):
        return None
    return {
        "name": label,
        "colours": sorted(set(colours)),
        "faces": sorted(set(faces)),
        "names": sorted(set(names)),
    }


def _hex_to_rgb(value: str) -> str:
    """#RGB or #RRGGBB as an rgb() string, so one comparison covers both."""
    v = value.lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    if len(v) < 6:
        return value
    return f"rgb({int(v[0:2], 16)}, {int(v[2:4], 16)}, {int(v[4:6], 16)})"


_SYSTEM_JS = r"""(want) => {
  const parse = (c) => {
    const m = (c || '').match(/[\d.]+/g);
    if (!m || m.length < 3) return null;
    if (m.length > 3 && Number(m[3]) < 0.35) return null;   // barely there
    return m.slice(0, 3).map(Number);
  };
  const near = (a, b) => Math.abs(a[0]-b[0]) + Math.abs(a[1]-b[1]) + Math.abs(a[2]-b[2]) < 24;
  const neutral = (c) => Math.max(...c) - Math.min(...c) < 26;   // grey, paper, ink
  const palette = want.colours.map(parse).filter(Boolean);

  // Which of the system's own tokens the document bothered to declare.
  const declared = new Set();
  for (const sheet of document.styleSheets) {
    let rules; try { rules = sheet.cssRules; } catch (e) { continue; }
    for (const rule of rules || []) {
      if (!rule.style) continue;
      for (let i = 0; i < rule.style.length; i++) {
        const n = rule.style[i];
        if (n.startsWith('--')) declared.add(n);
      }
    }
  }
  const missing = want.names.filter((n) => !declared.has(n));

  // Colour actually painted, by area, ignoring neutrals — a brand is not
  // broken by grey.
  const strays = {};
  let onPalette = 0, offPalette = 0;
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const area = Math.min(r.width, 1600) * Math.min(r.height, 1600);
    const cs = getComputedStyle(el);
    for (const raw of [cs.backgroundColor, cs.borderTopColor, cs.fill]) {
      const c = parse(raw);
      if (!c || neutral(c)) continue;
      if (palette.length && palette.some((p) => near(p, c))) onPalette += area;
      else {
        offPalette += area;
        const key = 'rgb(' + c.join(', ') + ')';
        strays[key] = (strays[key] || 0) + area;
      }
    }
  }
  const worst = Object.entries(strays).sort((a, b) => b[1] - a[1])[0];

  // The faces the words are actually set in.
  const used = {};
  for (const el of document.querySelectorAll('p,li,h1,h2,h3,h4,td,th,span,div,button,a')) {
    const t = (el.textContent || '').trim();
    if (!t || el.children.length) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const face = getComputedStyle(el).fontFamily.split(',')[0].replace(/["']/g, '').trim();
    used[face] = (used[face] || 0) + r.width * r.height;
  }
  const facesUsed = Object.entries(used).sort((a, b) => b[1] - a[1]).map((x) => x[0]);
  const offFace = want.faces.length
    ? facesUsed.filter((f) => !want.faces.some(
        (w) => f.toLowerCase() === w.toLowerCase())).slice(0, 3)
    : [];

  return {
    missing: missing.slice(0, 6),
    missingCount: missing.length,
    tokenCount: want.names.length,
    onPalette: Math.round(onPalette),
    offPalette: Math.round(offPalette),
    stray: worst ? worst[0] : '',
    facesUsed: facesUsed.slice(0, 3),
    offFace,
  };
}"""


# Templates where running to several pages is the point, so filling them by
# measurement is right. A flier is one sheet by definition and a deck's slides
# are authored, not flowed.
_PAGED_KINDS = {"document", "research"}

_REFLOW_JS = r"""async () => {
  // Every decision below is a measurement, so nothing may be measured while
  // the type is still arriving: a page laid out in the fallback face and
  // re-laid in the real one is a page whose heights were all wrong. The
  // render already pauses for this, but a pause is a guess and this is the
  // answer.
  try { await document.fonts.ready; } catch (e) {}

  const W = 794;
  // The running header and footer belong to the page, not to the text: they
  // are never lifted off it and never removed when a sheet is cloned.
  const PAGE_FURNITURE = new Set(['HEADER', 'FOOTER']);
  let sheets = [...document.querySelectorAll('body *')].filter((e) => {
    const r = e.getBoundingClientRect();
    return Math.abs(r.width - W) < 14 && r.height > 640 && r.height < 2400;
  });
  sheets = sheets.filter((e) => !sheets.some((o) => o !== e && o.contains(e)));
  if (sheets.length < 1) return { moved: 0, pages: 0, why: 'no sheets' };

  // The flow inside a sheet is whichever descendant holds the most blocks.
  // The footer is not part of it: it belongs to the page, not to the text.
  // Walk down the chain of wrappers — sheet, page, content, measure — and
  // stop at the first element holding more than one block. Picking whichever
  // element has the most children instead dives straight past the page's own
  // column into a section inside it, and then only that section's paragraphs
  // get moved while the page stays exactly as overfull as it was.
  // Only these can hold a paragraph. Descending into anything else — a table
  // whose sheet happens to carry nothing but that table — makes the page's
  // column a <tbody>, and every block added to it afterwards is hoisted back
  // out by the parser: headings end up between rows, cells end up outside the
  // table, and the document comes apart while every page still measures full.
  const HOLDS_FLOW = /^(DIV|SECTION|MAIN|ARTICLE|BODY|ASIDE)$/;

  const flowOf = (sheet) => {
    let node = sheet;
    for (let depth = 0; depth < 8; depth++) {
      const kids = [...node.children].filter((k) => !PAGE_FURNITURE.has(k.tagName));
      if (kids.length !== 1) break;
      if (!HOLDS_FLOW.test(kids[0].tagName)) break;
      node = kids[0];
    }
    return node;
  };

  const flows = sheets.map(flowOf);
  if (flows.some((f) => !f)) return { moved: 0, pages: sheets.length, why: 'no flow' };

  // Everything the document says, in order, lifted off the paper.
  const blocks = [];
  for (const f of flows) {
    for (const k of [...f.children]) {
      if (PAGE_FURNITURE.has(k.tagName)) continue;
      blocks.push(k);
      k.remove();
    }
  }
  if (!blocks.length) return { moved: 0, pages: sheets.length, why: 'nothing to place' };

  // A page is full when it passes the page's height — measured from the top of
  // the sheet, not against the sheet's own box. A sheet given min-height grows
  // with whatever is put on it, so nothing ever hangs out of it and a test that
  // watches for overhang says every page fits while one page takes the lot.
  const PAGE_H = 1123;
  const spills = (sheet) => {
    const sr = sheet.getBoundingClientRect();
    if (sr.height > PAGE_H + 1) return true;
    for (const k of sheet.querySelectorAll('*')) {
      const r = k.getBoundingClientRect();
      if (r.height && r.bottom > sr.top + PAGE_H + 1) return true;
    }
    return false;
  };

  const addSheet = () => {
    const last = sheets[sheets.length - 1];
    const copy = last.cloneNode(true);
    const flow = flowOf(copy);
    for (const k of [...flow.children]) if (!PAGE_FURNITURE.has(k.tagName)) k.remove();
    last.parentNode.insertBefore(copy, last.nextSibling);
    sheets.push(copy);
    flows.push(flow);
  };

  // Place each block, and when one cannot fit a page even on its own, open it
  // and place its children instead — a section longer than a page is carried
  // over, with its wrapper re-opened on the next sheet so the styling holds.
  let at = 0, moved = 0;
  let stack = [];                       // wrappers currently open, innermost last
  const tip = () => (stack.length ? stack[stack.length - 1] : flows[at]);
  // The running footer is usually a child of the very element being filled, so
  // appending puts the text after it — the footer ends up mid-page and the last
  // block hangs off the bottom. Everything goes in above it.
  const footIn = (el) => [...el.children].find((k) => k.tagName === 'FOOTER') || null;
  const add = (parent, node) => parent.insertBefore(node, footIn(parent));

  const turnPage = () => {
    at += 1;
    moved += 1;
    if (at >= sheets.length) addSheet();
    const rebuilt = [];
    let parent = flows[at];
    for (const open of stack) {
      const copy = open.cloneNode(false);
      add(parent, copy);
      rebuilt.push(copy);
      parent = copy;
    }
    stack = rebuilt;
  };

  // Something that only introduces what comes after it: a heading, a table's
  // header row, or a small wrapper holding just those. Left at the foot of a
  // page when the block it introduces moves on, it prints as a page carrying
  // a title and nothing else — or, for a table, a row of column names with no
  // rows under them.
  const introduces = (el) => {
    if (!el) return false;
    if (el.tagName === 'THEAD') return true;
    // A table carrying only its header row is not a table yet — it is the
    // heading of the one that follows. Documents come back with a long table
    // written as several: the column names in one, the rows in the next. Left
    // at the foot of a page, that first one prints as a page of column names
    // with nothing under them.
    if (el.tagName === 'TABLE') return !el.querySelector('tbody tr, tr td');
    if (/^H[1-6]$/.test(el.tagName)) return true;
    if (el.querySelector('p, table, ul, ol, img, figure, blockquote')) return false;
    return el.querySelector('h1, h2, h3, h4, h5, h6') !== null
      && el.textContent.trim().length <= 160;
  };

  // Take that introduction off the page, so it can travel with what it
  // introduces. Returns null when the page does not end with one.
  // Where a block may legally sit. Moving a heading into a table wrapper is
  // not a layout mistake the reader forgives later: the parser rewrites the
  // markup around it, and the page comes apart.
  const INSIDE_TABLE = /^(TABLE|THEAD|TBODY|TFOOT|TR)$/;

  // Introductions come in runs — a section heading, then the column names of
  // the table under it — and taking only the innermost leaves the rest
  // stranded one page back. Capped, so a page can never be emptied entirely
  // by carrying its own contents forward.
  const liftIntroduction = () => {
    const parent = tip();
    const taken = [];
    while (taken.length < 3) {
      const kids = [...parent.children].filter((k) => !PAGE_FURNITURE.has(k.tagName));
      const last = kids[kids.length - 1];
      if (!introduces(last)) break;
      // A header row travels only within a table, and everything else only
      // outside one. The page it lands on rebuilds the same wrappers, so
      // matching the parent here is enough to know it will fit there.
      const inTable = INSIDE_TABLE.test(parent.tagName);
      if (inTable !== (last.tagName === 'THEAD')) break;
      last.remove();
      taken.unshift(last);
    }
    return taken;
  };

  const put = (node, depth) => {
    add(tip(), node);
    if (!spills(sheets[at])) return;
    node.remove();
    // Not the first thing here, so try a fresh page with the same wrappers.
    if (flows[at].children.length || stack.length) {
      // Whatever introduces this block comes with it. Without this, the
      // heading of a section stays on the page the section left, and a
      // table's header row keeps a page to itself while its body starts the
      // next one.
      const intro = liftIntroduction();
      turnPage();
      for (const el of intro) add(tip(), el);
      add(tip(), node);
      if (!spills(sheets[at])) return;
      node.remove();
    }
    // Alone on a page and still too tall: break it open, or accept it.
    if (node.children.length > 1 && depth < 4) {
      const shell = node.cloneNode(false);
      add(tip(), shell);
      stack.push(shell);
      for (const kid of [...node.children]) put(kid, depth + 1);
      stack.pop();
    } else {
      add(tip(), node);
    }
  };

  for (const block of blocks) put(block, 0);

  // A page can still finish over the paper: what is measured here and what
  // Chromium measures when it prints are not identical, and a heading carried
  // forward to stay with its section can tip the page it lands on. Such a page
  // prints a blank one after it, so its last block is moved to a sheet of its
  // own inserted directly after — a short page reads as a page, a blank one
  // does not. One sheet per offender, so this cannot cascade.
  for (let i = 0; i < sheets.length; i++) {
    if (!spills(sheets[i])) continue;
    const kids = [...flows[i].children].filter((k) => !PAGE_FURNITURE.has(k.tagName));
    if (kids.length < 2) continue;
    const blank = sheets[i].cloneNode(true);
    const blankFlow = flowOf(blank);
    for (const k of [...blankFlow.children])
      if (!PAGE_FURNITURE.has(k.tagName)) k.remove();
    sheets[i].parentNode.insertBefore(blank, sheets[i].nextSibling);
    sheets.splice(i + 1, 0, blank);
    flows.splice(i + 1, 0, blankFlow);
    blankFlow.insertBefore(kids[kids.length - 1], footIn(blankFlow));
    moved += 1;
  }

  // Any sheet left with nothing on it is not a page.
  for (let i = sheets.length - 1; i >= 0; i--) {
    const kids = [...flows[i].children].filter((k) => !PAGE_FURNITURE.has(k.tagName));
    if (!kids.length) { sheets[i].remove(); sheets.splice(i, 1); flows.splice(i, 1); }
  }

  return { moved, pages: sheets.length,
           spilling: sheets.filter(spills).length };
}"""


async def reflow_pages(html: str) -> tuple[str, dict]:
    """Fill the sheets by measuring, and hand back the document that results.

    Returns the html unchanged, and a reason, when there is nothing to do or
    anything goes wrong — a document that cannot be re-flowed is still a
    document.
    """
    try:
        page, close = await _render(html, 1280, 900)
    except Exception:  # noqa: BLE001 - no browser on this host
        return html, {"moved": 0, "why": "no browser"}
    try:
        # Measure the way the printer will. The export applies a print
        # stylesheet and prints with print media, and a page laid out under
        # screen rules can be a different height under those — which is how a
        # document whose sheets all fit on screen still printed blank pages
        # between the full ones.
        # Measure exactly what the printer will see. The export tags the
        # sheets, applies a print stylesheet keyed to that tag, and prints
        # with print media; a document laid out under screen rules has
        # different heights, which is how sheets that all fit on screen still
        # printed a blank page between the full ones. Tagging has to come
        # first — the stylesheet selects on the tag, so without it the rules
        # match nothing and the measurement is the screen's again.
        with contextlib.suppress(Exception):
            await _page_box(page)
            await page.add_style_tag(content=_PRINT_PAGES)
            await page.emulate_media(media="print")
            await page.wait_for_timeout(150)
        report = await page.evaluate(_REFLOW_JS)
        if not report or not report.get("moved"):
            return html, report or {"moved": 0}
        out = await page.evaluate(
            "() => '<!DOCTYPE html>\\n' + document.documentElement.outerHTML"
        )
        return (out or html), report
    except Exception:  # noqa: BLE001
        return html, {"moved": 0, "why": "reflow failed"}
    finally:
        try:
            await close()
        except Exception:  # noqa: BLE001
            pass



async def audit(
    html: str, kind: str = "", system: dict | None = None
) -> list[str]:
    """Look at the finished design the way a reviewer would, and say what is
    wrong with it. Never raises: a check that fails is not a design that fails.

    `system` is what `system_expectations` made of the design systems the
    project attached, if any — the colours, faces and tokens the design is
    supposed to be built from. Without one, only the house rules apply.

    `kind` is the template, because the criteria are not the same. A flier is
    not a worse dashboard: its dates belong in prose rather than in a
    monospace column, its palette may be mostly one warm colour, and a sheet
    that is 794px wide is meant not to reflow at 640.
    """
    findings: list[str] = []
    sheet = kind in _SHEET_KINDS
    poster = kind in _POSTER_KINDS
    prose = kind in _DOC_KINDS
    app = not sheet                       # an unknown template is treated as a screen
    try:
        page, close = await _render(html, 1280, 900)
    except Exception:  # noqa: BLE001 - no browser on this host
        return []
    try:
        errors: list[str] = getattr(page, "thrown", [])
        await page.wait_for_timeout(500)
        found = await page.evaluate(_AUDIT_JS)
        # Everything _AUDIT_JS measured, it measured on whichever screen was
        # showing. A prototype is six or eight of them and the rest are
        # display:none, so the checks that can be run per screen are — and
        # where they are, they replace the single-screen answer rather than
        # sitting beside it.
        try:
            screens = await page.evaluate(_SCREENS_JS) if app else []
        except Exception:  # noqa: BLE001
            screens = []

        # The design system the project attached, if it attached one. This
        # runs for every template: a brand does not stop applying because the
        # thing being built is a flier rather than a dashboard.
        if system:
            try:
                held = await page.evaluate(_SYSTEM_JS, system)
            except Exception:  # noqa: BLE001
                held = None
            if held:
                named = f"\u201c{system['name']}\u201d" if system.get("name") else "the design system"
                if held["tokenCount"] and held["missingCount"] >= held["tokenCount"] * 0.5:
                    some = ", ".join(held["missing"][:4])
                    findings.append(
                        f"{held['missingCount']} of {held['tokenCount']} tokens from "
                        f"{named} are never declared ({some}) — the design was built "
                        "beside the system rather than from it"
                    )
                total = held["onPalette"] + held["offPalette"]
                if total > 1000 and held["offPalette"] > total * 0.35:
                    share = round(held["offPalette"] / total * 100)
                    findings.append(
                        f"{share}% of the coloured area is not in {named}'s palette, "
                        f"mostly {held['stray']} — pick from the system's colours"
                    )
                if held["offFace"]:
                    findings.append(
                        f"set in {', '.join(held['offFace'])}, which "
                        f"{'is' if len(held['offFace']) == 1 else 'are'} not "
                        f"{named}'s face — it names "
                        f"{', '.join(system['faces'][:3])}"
                    )

        if errors:
            findings.append(f"its own script threw ({errors[0]})")
        if not screens and found.get("contrast", 99) < 4.5:
            findings.append(
                f"contrast {found['contrast']}:1 on \u201c{found['contrastOn']}\u201d"
                " \u2014 below the 4.5:1 floor"
            )
        if found.get("tiny"):
            findings.append(
                f"{found['tiny']} hit area(s) under 24px: "
                + ", ".join(found.get("tinyWhich") or ["unnamed elements"])
            )
        if app and not screens and found.get("bigIcons"):
            findings.append(
                f"{found['bigIcons']} icon(s) blown up past 64px — an svg with "
                "no width in a flex row"
            )
        if app and found.get("accentShare", 0) > 32:
            findings.append(
                f"strong colour covers about {found['accentShare']}% of the page "
                "— colour belongs on the data and the status, not on the chrome"
            )
        if not found.get("headings"):
            findings.append(
                "no heading element anywhere — the titles are spans and divs, so "
                "only their type size says they are titles"
            )
        if app and found.get("sansNumbers", 0) > 6:
            findings.append(
                f"{found['sansNumbers']} numbers set in the sans rather than the "
                f"mono, starting with \u201c{found['sansNumberFirst']}\u201d — a "
                "column of them will not line up"
            )
        if app and found.get("dataBits", 0) >= 6 and found.get("hues", 0) <= 1:
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
        if not screens and found.get("emptyTables"):
            findings.append(f"{found['emptyTables']} table(s) with no rows")

        # A prototype's first claim is that you can click through it. Try the
        # navigation and see whether the screen underneath actually changes —
        # a dead nav looks identical to a working one in a screenshot.
        try:
            dead = await page.evaluate(_NAV_JS) if app else None
        except Exception:  # noqa: BLE001
            dead = None
        if dead:
            findings.append(
                f"the sidebar navigation does not switch screens — "
                f"“{dead}” leaves the same one showing"
            )

        def _where(hits: list[dict]) -> str:
            """Name the screens a fault landed on, without listing eight."""
            names = [h.get("name") or "a screen" for h in hits]
            if len(names) <= 3:
                return " and ".join([", ".join(names[:-1]), names[-1]] if len(names) > 1
                                    else names)
            return f"{len(names)} screens ({', '.join(names[:3])} and more)"

        # One colour used on every screen is one fault, not eight. Say it once,
        # so the repair reads it as the systemic thing it is.
        low = [x for x in screens or [] if x.get("contrast", 99) < 4.5]
        if low:
            worst = min(low, key=lambda x: x["contrast"])
            findings.append(
                f"contrast {worst['contrast']}:1 on \u201c{worst['contrastOn']}\u201d"
                f" — below the 4.5:1 floor, on {_where(low)}"
                + (" — the same colour throughout" if len(low) > 2 else "")
            )
        for key, say in (
            ("empty", "a table with no rows"),
            ("blown", "an icon blown up past 64px"),
            ("thin", "barely anything on it — a screen is a working page, not a "
                     "header and a gap"),
        ):
            hits = [x for x in screens or [] if x.get(key)]
            if hits:
                findings.append(f"{say}, on {_where(hits)}")

        if sheet:
            try:
                paper = await page.evaluate(_SHEET_JS)
            except Exception:  # noqa: BLE001
                paper = None
            if paper:
                page_h = paper.get("page")
                # A document is many sheets on purpose; its pages are checked
                # one by one below instead.
                if page_h and not prose and paper["height"] > page_h * 1.08:
                    over = paper["height"] - page_h
                    findings.append(
                        f"the sheet runs {paper['height']}px on a page of {page_h}"
                        f" — {over}px past the bottom. Take a block out or tighten"
                        " the spacing; it has to be one sheet"
                    )
                if paper["monoProse"]:
                    findings.append(
                        f"{paper['monoProse']} sentence(s) set in the monospace, "
                        f"starting with \u201c{paper['monoProseFirst']}\u201d — the "
                        "mono is for a code or an id, never for prose, a date or a "
                        "time written out"
                    )
                if paper["tiny"]:
                    findings.append(
                        f"{paper['tiny']} run(s) of text under 11px — too small to "
                        "read on paper"
                    )
                if poster:
                    ratio = paper["headSize"] / max(1, paper["bodySize"])
                    if ratio < 3:
                        findings.append(
                            f"the headline \u201c{paper['headText']}\u201d is only "
                            f"{ratio:.1f}\u00d7 the body text — a poster is read from "
                            "across a room, so it wants four times or more"
                        )
                    if paper["headTop"] > 0.34:
                        findings.append(
                            "the largest thing on the sheet starts below the top "
                            "third — a poster leads with what it is"
                        )
                    if paper["panels"] > 3:
                        findings.append(
                            f"{paper['panels']} bordered panels — a printed piece "
                            "groups with space and a rule, not with a grid of cards"
                        )
                    if paper["words"] > 220:
                        findings.append(
                            f"{paper['words']} words on a poster — it has to be "
                            "read standing up, so cut it to the ones that matter"
                        )

        # A document is judged as typography rather than as layout.
        if prose:
            try:
                doc = await page.evaluate(_DOC_JS)
            except Exception:  # noqa: BLE001
                doc = None
            if doc:
                if doc["size"] < 15:
                    findings.append(
                        f"the body is set at {doc['size']}px — under 15 is a screen "
                        "size, not a reading size"
                    )
                if not 1.35 <= doc["leading"] <= 1.85:
                    findings.append(
                        f"a line height of {doc['leading']} on the body — prose wants "
                        "between 1.4 and 1.75, or the lines crowd or drift apart"
                    )
                if not 45 <= doc["measure"] <= 95:
                    findings.append(
                        f"the measure is about {doc['measure']} characters a line — "
                        "a column of prose reads between 45 and 95, ideally near 70"
                    )
                h1, h2, h3 = doc["heads"]
                if h2 and h1 and h2 >= h1:
                    findings.append(
                        f"the section headings ({h2}px) are not smaller than the "
                        f"title ({h1}px) — the hierarchy is flat"
                    )
                if h3 and h2 and h3 >= h2:
                    findings.append(
                        f"sub-headings ({h3}px) are not smaller than the sections "
                        f"they sit under ({h2}px)"
                    )
                if h2 and h2 < doc["size"] * 1.15:
                    findings.append(
                        f"section headings are {h2}px against {doc['size']}px of body "
                        "— too close to read as headings"
                    )
                if not doc["sections"]:
                    findings.append(
                        "no section headings at all — a document of any length is "
                        "navigated by its sections"
                    )
                if doc["uncaptioned"]:
                    findings.append(
                        f"{doc['uncaptioned']} table(s) with no caption — a table in "
                        "a document is numbered and says what it shows"
                    )
                if doc["cramped"]:
                    findings.append(
                        f"a {doc['crampedCols']}-column table in a column of "
                        f"prose — cells wrap to {doc['crampedLines']} lines, so "
                        "the table grows until it leaves the page. Carry fewer "
                        "columns, or move one into the text"
                    )
                if doc["touching"]:
                    findings.append(
                        f"{doc['touching']} pair(s) of table cells with no space "
                        f"between them, reading as one word — "
                        f"\u201c{doc['touchingFirst']}\u201d"
                    )
                if doc["spill"]:
                    findings.append(
                        f"content crosses the edge of {doc['spill']} page(s) — a "
                        "document breaks between sheets, it does not overflow them"
                    )

        # The tweak sheet promises knobs that work.
        try:
            dead_knobs = await page.evaluate(_TWEAKS_JS)
        except Exception:  # noqa: BLE001
            dead_knobs = []
        if dead_knobs:
            names = ", ".join(f"\u201c{k}\u201d" for k in dead_knobs[:4])
            findings.append(
                f"{len(dead_knobs)} tweak control(s) change nothing — {names}. "
                "The custom property is declared but the design never reads it"
            )

        # The published checklist, measured rather than asserted.
        try:
            list_check = await page.evaluate(_CHECKLIST_JS)
        except Exception:  # noqa: BLE001
            list_check = None
        if list_check:
            if list_check["focusChecked"] >= 3 and list_check["noFocusRing"]:
                findings.append(
                    f"{list_check['noFocusRing']} of {list_check['focusChecked']} "
                    "controls show nothing when the keyboard reaches them — a focus "
                    "state has to be visible"
                )
            off, total = list_check["offScale"], list_check["spaced"]
            if total >= 40 and off > total * 0.25:
                findings.append(
                    f"{round(off / total * 100)}% of the spacing is off the 4px "
                    f"scale, starting with {list_check['offFirst']} — pick from 4, "
                    "8, 12, 16, 24, 32, 48, 64, 96, 128 and stay there"
                )

        # A sheet is a fixed width on purpose; it is not meant to reflow.
        for width, label in (() if sheet else ((1024, "1024"), (640, "640"))):
            await page.set_viewport_size({"width": width, "height": 900})
            await page.wait_for_timeout(250)
            over = await page.evaluate(
                "() => Math.max(0, document.documentElement.scrollWidth"
                " - document.documentElement.clientWidth)"
            )
            if over > 2:
                findings.append(f"{over}px of sideways scroll at {label}px")
            if width == 640 and app:
                try:
                    tiny_hits = await page.evaluate(_TARGETS_JS)
                except Exception:  # noqa: BLE001
                    tiny_hits = None
                if tiny_hits and tiny_hits["n"] > 2:
                    findings.append(
                        f"{tiny_hits['n']} controls under 44x44 on a phone — "
                        + ", ".join(tiny_hits.get("which") or ["unnamed elements"])
                        + " — that is the floor for something a finger has to hit"
                    )
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
