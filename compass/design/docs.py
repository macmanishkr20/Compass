"""A design system, rendered as a project you can browse.

Claude Design turns a system into files: one token sheet, a written guide, a
machine-readable record of the parameters, and a page per foundation,
component and template. This module builds that set.

The pages are deliberately *shared* markup. A design system is the claim that
the same structure, re-tokenised, is still the same system — so one set of page
builders reads the tokens and every system gets a faithful specimen without
anyone hand-writing four copies. That also means a system a user imported gets
the same treatment as an included one.

    styles.css          the token sheet plus the component layer
    readme.md           the guide: how to use it, what not to do, the files
    theme.json          the parameters the rest was derived from
    foundations/*.html  colour, type, layout, icons, imagery
    components/*.html   buttons, cards, dialog, forms, navigation, table
    templates/*.html    a deck and a landing page in the system's own voice
    theme.html          the parameters as a reference sheet
"""

from __future__ import annotations

import json
import zipfile
from io import BytesIO

# The document's shape: id, the group it sits under, its file, and the blurb
# shown above its preview.
SECTIONS: list[dict] = [
    {
        "id": "readme",
        "group": "",
        "name": "Readme",
        "file": "readme.md",
        "blurb": "How the system is meant to be used",
    },
    {
        "id": "deck",
        "group": "Templates",
        "name": "Deck",
        "file": "templates/deck.html",
        "blurb": "A presentation starter: cover, contents, dividers, columns, "
        "a data table, a chart, a timeline, a quote and a close, on the "
        "theme's tokens",
    },
    {
        "id": "landing",
        "group": "Templates",
        "name": "Landing",
        "file": "templates/landing/index.html",
        "blurb": "A one-page product landing in the system's own voice — a hero, "
        "a stat row, feature rows, a figure and a closing panel",
    },
    {
        "id": "buttons",
        "group": "Components",
        "name": "Buttons & tags",
        "file": "components/buttons.html",
        "blurb": "Primary actions, icon buttons, a full-width action, and tags",
    },
    {
        "id": "cards",
        "group": "Components",
        "name": "Cards",
        "file": "components/cards.html",
        "blurb": "Content cards with a kicker, title, body and meta row, and the "
        "elevation steps",
    },
    {
        "id": "dialog",
        "group": "Components",
        "name": "Dialog",
        "file": "components/dialog.html",
        "blurb": "A modal surface at the top elevation, shown here inside a "
        "static frame",
    },
    {
        "id": "forms",
        "group": "Components",
        "name": "Forms",
        "file": "components/forms.html",
        "blurb": "Text fields, a segmented control and radios — native elements, "
        "themed states",
    },
    {
        "id": "navigation",
        "group": "Components",
        "name": "Navigation",
        "file": "components/navigation.html",
        "blurb": "The header bar pattern, with the active state and an action",
    },
    {
        "id": "table",
        "group": "Components",
        "name": "Table",
        "file": "components/table.html",
        "blurb": "A data table with the themed header and row rules",
    },
    {
        "id": "color",
        "group": "Foundations",
        "name": "Color",
        "file": "foundations/color.html",
        "blurb": "Colour roles and the 100–900 tonal ramp, with usage notes",
    },
    {
        "id": "icons",
        "group": "Foundations",
        "name": "Icons",
        "file": "foundations/icons.html",
        "blurb": "The icon set at interface sizes, inline and in buttons",
    },
    {
        "id": "imagery",
        "group": "Foundations",
        "name": "Imagery",
        "file": "foundations/image.html",
        "blurb": "How figures are treated, and the caption pattern",
    },
    {
        "id": "layout",
        "group": "Foundations",
        "name": "Spacing & elevation",
        "file": "foundations/layout.html",
        "blurb": "The spacing scale, the grid, and how edges are drawn",
    },
    {
        "id": "typography",
        "group": "Foundations",
        "name": "Typography",
        "file": "foundations/type.html",
        "blurb": "The type scale and the heading/body pairing at real sizes",
    },
    {
        "id": "parameters",
        "group": "Theme",
        "name": "Parameters",
        "file": "theme.html",
        "blurb": "The parameters this system was derived from",
    },
]

# A neutral ramp for systems that arrived without one.
_FALLBACK_RAMP = [
    "#F7F7F8", "#E9E9EC", "#D5D6DA", "#B4B6BD", "#8B8E97",
    "#666A73", "#484C54", "#2C2F35", "#15171B",
]

_GOOGLE_FONTS = (
    "https://fonts.googleapis.com/css2?family=Archivo:wght@400;700"
    "&family=Caprasimo&family=Cormorant+Garamond:wght@400;600&family=Figtree:wght@400;600"
    "&family=Barlow:wght@400;600&family=Barlow+Condensed:wght@500;700&family=Inter:wght@400;600&family=Source+Serif+4:wght@400;600&family=Lora:wght@400&display=swap"
)


def _ramp(system: dict) -> list[str]:
    swatches = [c for c in (system.get("swatches") or []) if isinstance(c, str)]
    if len(swatches) >= 9:
        return swatches[:9]
    if len(swatches) >= 3:  # stretch a short ramp across the nine steps
        out = []
        for i in range(9):
            out.append(swatches[min(len(swatches) - 1, round(i * (len(swatches) - 1) / 8))])
        return out
    return list(_FALLBACK_RAMP)


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    try:
        return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
    except ValueError:
        return 128, 128, 128


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#%02X%02X%02X" % tuple(max(0, min(255, round(c))) for c in rgb)


def _neutrals(ramp: list[str], keep: float = 0.14) -> list[str]:
    """A neutral scale that carries the ramp's temperature but not its colour.

    The ramp a system arrives with is an *accent* ramp — nine steps of one hue.
    Using it for the page ground and body text would tint the whole design.
    Mixing each step most of the way toward its own luminance gives greys that
    still feel warm or cool the way the accent does, which is what a system
    with "one accent" actually looks like.
    """
    out = []
    for value in ramp:
        r, g, b = _hex_to_rgb(value)
        grey = 0.2126 * r + 0.7152 * g + 0.0722 * b
        out.append(
            _rgb_to_hex(
                (
                    grey + (r - grey) * keep,
                    grey + (g - grey) * keep,
                    grey + (b - grey) * keep,
                )
            )
        )
    return out


def theme(system: dict) -> dict:
    """The parameters a system was derived from, with defaults filled in."""
    p = dict(system.get("params") or {})
    # The parameters are the record, so they lead; the card's shorter label
    # ("Archivo") stands in only when a system arrived without parameters.
    fonts = p.get("fonts") or system.get("fonts") or "system-ui / system-ui"
    display = system.get("font_display") or fonts.split("/")[0].strip()
    body = system.get("font_body") or fonts.split("/")[-1].strip()
    return {
        "ground": p.get("ground", "dark" if system.get("dark") else "light"),
        "fonts": fonts,
        "font_display": display,
        "font_body": body,
        "density": p.get("density", "1.00×"),
        "radius": p.get("radius", "8px"),
        "layout": p.get("layout", "grid"),
        "dividers": p.get("dividers", "subtle"),
        "buttons": p.get("buttons", "solid"),
        "color_use": p.get("color_use", "fill"),
        "frame": p.get("frame", "card"),
        "image_treatment": p.get("image_treatment", "none"),
        "icons": p.get("icons", "geometric"),
    }


def styles_css(system: dict) -> str:
    """The one stylesheet: the token sheet, then the component layer."""
    ramp = _ramp(system)
    grey = _neutrals(ramp)
    t = theme(system)
    dark = bool(system.get("dark")) or t["ground"].startswith("dark")
    radius = t["radius"]
    # The accent stays saturated; everything structural comes from the neutrals,
    # so a system with one accent reads as one accent.
    accent = ramp[4]
    ink = grey[0] if dark else grey[8]
    ground = grey[8] if dark else grey[0]
    surface = grey[7] if dark else "#FFFFFF"
    rule = grey[6] if dark else grey[2]
    muted = grey[3] if dark else grey[5]
    steps = "\n".join(f"  --n-{(i + 1) * 100}: {c};" for i, c in enumerate(grey))
    steps += "\n" + "\n".join(f"  --a-{(i + 1) * 100}: {c};" for i, c in enumerate(ramp))
    pill = "999px" if "pill" in t["buttons"] else radius
    shadow = (
        "none" if t["dividers"] == "strong" or t["frame"] == "none"
        else f"0 1px 2px rgba(0,0,0,{0.35 if dark else 0.06})"
    )

    return f"""/* {system.get('name', 'Design system')} — token sheet + component layer.
   Every page in this project is built from these variables; retune here and
   the whole system moves with you. */
@import url('{_GOOGLE_FONTS}');

:root {{
{steps}

  --accent: {accent};
  --ink: {ink};
  --ground: {ground};
  --surface: {surface};
  --muted: {muted};
  --rule-color: {rule};

  --font-heading: '{t["font_display"]}', Georgia, serif;
  --font-body: '{t["font_body"]}', system-ui, sans-serif;
  --font-heading-weight: 700;

  --radius-sm: {radius};
  --radius-md: {radius};
  --radius-lg: {radius};
  --radius-pill: {pill};

  --space-1: 4px;  --space-2: 8px;  --space-3: 12px; --space-4: 16px;
  --space-5: 24px; --space-6: 32px; --space-7: 48px; --space-8: 64px;

  --size-xs: 13px; --size-sm: 15px; --size-md: 18px;
  --size-lg: 24px; --size-xl: 34px; --size-2xl: 52px;

  --rule: 1px solid var(--rule-color);
  --shadow-sm: {shadow};
  --shadow-lg: 0 10px 30px rgba(0,0,0,{0.5 if dark else 0.12});
}}

* {{ box-sizing: border-box; }}

body {{
  margin: 0;
  background: var(--ground);
  color: var(--ink);
  font-family: var(--font-body);
  font-size: var(--size-sm);
  line-height: 1.6;
}}

h1, h2, h3, h4 {{
  font-family: var(--font-heading);
  font-weight: var(--font-heading-weight);
  letter-spacing: -0.015em;
  margin: 0 0 var(--space-3);
  line-height: 1.12;
}}

h1 {{ font-size: var(--size-2xl); }}
h2 {{ font-size: var(--size-xl); }}
h3 {{ font-size: var(--size-lg); }}
p {{ margin: 0 0 var(--space-4); }}
a {{ color: var(--accent); }}

.page {{ padding: var(--space-7); max-width: 1180px; margin: 0 auto; }}
.kicker {{
  font-size: var(--size-xs);
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--accent);
  margin-bottom: var(--space-2);
}}
.muted {{ color: var(--muted); }}
.rule {{ border: 0; border-top: var(--rule); margin: var(--space-5) 0; }}

/* -- buttons */
.btn {{
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  font-family: var(--font-body);
  font-size: var(--size-sm);
  font-weight: 600;
  padding: 10px 18px;
  border-radius: var(--radius-pill);
  border: 1px solid transparent;
  background: var(--accent);
  color: {"#0B0B10" if dark else "#FFFFFF"};
  cursor: pointer;
  text-decoration: none;
}}
.btn.secondary {{ background: var(--surface); color: var(--ink); border-color: var(--rule-color); }}
.btn.quiet {{ background: transparent; color: var(--accent); padding-left: 0; padding-right: 0; }}
.btn.disabled {{ background: var(--n-200); color: var(--n-500); cursor: default; }}
.btn.block {{ width: 100%; justify-content: {"center" if "flush" not in t["buttons"] else "flex-start"}; }}
.icon-btn {{
  width: 40px; height: 40px; padding: 0;
  display: inline-flex; align-items: center; justify-content: center;
  border-radius: var(--radius-sm);
  border: 1px solid var(--rule-color);
  background: var(--surface);
  color: var(--ink);
}}
.icon-btn.solid {{ background: var(--accent); border-color: var(--accent); color: {"#0B0B10" if dark else "#FFFFFF"}; }}

/* -- tags */
.tag {{
  display: inline-block;
  font-size: var(--size-xs);
  padding: 3px 10px;
  border-radius: var(--radius-pill);
  border: 1px solid transparent;
}}
.tag.tint {{ background: color-mix(in srgb, var(--accent) 16%, transparent); color: var(--accent); }}
.tag.outline {{ border-color: var(--accent); color: var(--accent); }}
.tag.neutral {{ background: var(--n-200); color: var(--n-700); }}

/* -- cards */
.card {{
  background: var(--surface);
  border: {"var(--rule)" if t["frame"] != "none" else "0"};
  border-radius: var(--radius-md);
  padding: var(--space-5);
  box-shadow: var(--shadow-sm);
}}
.grid {{ display: grid; gap: var(--space-4); }}
.g3 {{ grid-template-columns: repeat(3, 1fr); }}
.g2 {{ grid-template-columns: repeat(2, 1fr); }}

/* -- forms */
label {{ display: block; font-size: var(--size-xs); color: var(--muted); margin-bottom: 6px; }}
input[type='text'], textarea, select {{
  width: 100%;
  font-family: var(--font-body);
  font-size: var(--size-sm);
  color: var(--ink);
  background: var(--surface);
  border: var(--rule);
  border-radius: var(--radius-sm);
  padding: 10px 12px;
}}
input:focus-visible, textarea:focus-visible, select:focus-visible {{
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}}
.segmented {{ display: inline-flex; border: var(--rule); border-radius: var(--radius-sm); overflow: hidden; }}
.segmented button {{
  border: 0; padding: 9px 16px; background: var(--surface); color: var(--ink);
  font-family: var(--font-body); font-size: var(--size-sm); cursor: pointer;
}}
.segmented button[aria-pressed='true'] {{ background: var(--accent); color: {"#0B0B10" if dark else "#FFFFFF"}; }}

/* -- table */
table {{ width: 100%; border-collapse: collapse; font-size: var(--size-sm); }}
th {{
  text-align: left; font-family: var(--font-body); font-size: var(--size-xs);
  letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted);
  padding: 10px 12px; border-bottom: 2px solid var(--rule-color);
}}
td {{ padding: 12px; border-bottom: var(--rule); }}

/* -- navigation */
.nav {{
  display: flex; align-items: center; gap: var(--space-5);
  padding: var(--space-3) var(--space-5);
  background: var(--surface);
  border-bottom: {"2px solid var(--rule-color)" if t["dividers"] == "strong" else "var(--rule)"};
}}
.nav .brand {{ font-family: var(--font-heading); font-weight: 700; font-size: var(--size-md); }}
.nav a {{ color: var(--ink); text-decoration: none; font-size: var(--size-sm); }}
.nav a.active {{ color: var(--accent); font-weight: 600; }}
.nav .spacer {{ margin-left: auto; }}

/* -- imagery */
figure {{ margin: 0; }}
.figure img, .figure .plate {{
  width: 100%; display: block; border-radius: var(--radius-md);
  filter: {
      'grayscale(1)' if t['image_treatment'] == 'grayscale'
      else 'sepia(0.5) saturate(0.8)' if t['image_treatment'] == 'sepia'
      else 'saturate(1.15) hue-rotate(-6deg)' if t['image_treatment'] == 'warm'
      else 'none'
  };
}}
figcaption {{ font-size: var(--size-xs); color: var(--muted); margin-top: var(--space-2); }}

/* -- slides */
.slide {{
  aspect-ratio: 16 / 9;
  background: var(--surface);
  border: {"var(--rule)" if t["frame"] != "none" else "0"};
  border-radius: var(--radius-md);
  padding: var(--space-7);
  display: flex; flex-direction: column; gap: var(--space-4);
  margin-bottom: var(--space-5);
  overflow: hidden;
}}
.slide.accent {{ background: var(--accent); color: {"#0B0B10" if dark else "#FFFFFF"}; }}
.slide.accent .kicker {{ color: inherit; opacity: 0.75; }}
"""


def _doc(system: dict, title: str, body: str) -> str:
    """Wrap a page body into a standalone document carrying the token sheet."""
    return (
        "<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{title} — {system.get('name', 'Design system')}</title>"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<style>{styles_css(system)}</style></head>"
        f"<body><div class=\"page\">{body}</div></body></html>"
    )


# ---------------------------------------------------------------- foundations

def _p_typography(system: dict) -> str:
    t = theme(system)
    rows = [
        ("2xl", "var(--size-2xl)", "Display"),
        ("xl", "var(--size-xl)", "Section heading"),
        ("lg", "var(--size-lg)", "Subheading"),
        ("md", "var(--size-md)", "Lead paragraph"),
        ("sm", "var(--size-sm)", "Body"),
        ("xs", "var(--size-xs)", "Caption and labels"),
    ]
    scale = "".join(
        f"<tr><td style='width:70px'><code>{name}</code></td>"
        f"<td style='font-size:{size};font-family:var(--font-heading);line-height:1.1'>Ag</td>"
        f"<td class='muted'>{use}</td></tr>"
        for name, size, use in rows
    )
    return _doc(
        system,
        "Typography",
        f"""
        <p class="kicker">Foundations</p>
        <h2>Typography</h2>
        <p class="muted">{t["fonts"]} — headings take <code>var(--font-heading)</code>,
        body copy inherits <code>var(--font-body)</code>. Never name a family directly.</p>
        <hr class="rule">
        <table>{scale}</table>
        <hr class="rule">
        <h3>Pairing at real sizes</h3>
        <h2>The quick brown fox jumps over the lazy dog</h2>
        <p style="max-width:62ch">Body copy sits at {t["density"]} density on a 1.6 line height.
        A measure of 62 characters keeps the eye from losing the line, and the
        heading face is only used above <code>--size-md</code>.</p>
        """,
    )


def _p_color(system: dict) -> str:
    ramp = _ramp(system)
    swatches = "".join(
        f"<div style='flex:1'><div style='height:64px;background:{c};"
        f"border-radius:var(--radius-sm);border:var(--rule)'></div>"
        f"<div class='muted' style='font-size:var(--size-xs);margin-top:6px'>{(i + 1) * 100}<br>{c}</div></div>"
        for i, c in enumerate(ramp)
    )
    roles = [
        ("--accent", ramp[4], "The one colour that means action"),
        ("--ink", ramp[8], "Text and rules at full strength"),
        ("--muted", ramp[5], "Secondary text, labels, captions"),
        ("--surface", "#FFFFFF", "Raised surfaces — cards, dialogs, the nav"),
        ("--ground", ramp[0], "The page behind everything"),
    ]
    role_rows = "".join(
        f"<tr><td><code>{name}</code></td>"
        f"<td><span style='display:inline-block;width:22px;height:22px;border-radius:4px;"
        f"background:{value};border:var(--rule);vertical-align:middle'></span> {value}</td>"
        f"<td class='muted'>{use}</td></tr>"
        for name, value, use in roles
    )
    return _doc(
        system,
        "Color",
        f"""
        <p class="kicker">Foundations</p>
        <h2>Color</h2>
        <p class="muted">One ramp, nine steps. Every neutral in the system is a step of
        it, so the greys carry the same temperature as the accent.</p>
        <div style="display:flex;gap:6px;margin:var(--space-5) 0">{swatches}</div>
        <hr class="rule">
        <h3>Roles</h3>
        <table><tr><th>Token</th><th>Value</th><th>Used for</th></tr>{role_rows}</table>
        """,
    )


def _p_layout(system: dict) -> str:
    t = theme(system)
    steps = "".join(
        f"<tr><td><code>--space-{i}</code></td><td>{v}px</td>"
        f"<td><div style='height:12px;width:{v}px;background:var(--accent);"
        f"border-radius:2px'></div></td></tr>"
        for i, v in enumerate([4, 8, 12, 16, 24, 32, 48, 64], start=1)
    )
    return _doc(
        system,
        "Spacing & elevation",
        f"""
        <p class="kicker">Foundations</p>
        <h2>Spacing &amp; elevation</h2>
        <p class="muted">A {t["layout"]} layout on a four-pixel rhythm. Edges are drawn
        with {t["dividers"]} dividers; radii are {t["radius"]}.</p>
        <table><tr><th>Token</th><th>Value</th><th></th></tr>{steps}</table>
        <hr class="rule">
        <h3>Elevation</h3>
        <div class="grid g3">
          <div class="card"><strong>Flat</strong><p class="muted">The page itself.</p></div>
          <div class="card" style="box-shadow:var(--shadow-sm)"><strong>Raised</strong>
            <p class="muted">Cards and the nav bar.</p></div>
          <div class="card" style="box-shadow:var(--shadow-lg)"><strong>Top</strong>
            <p class="muted">Dialogs, and only dialogs.</p></div>
        </div>
        """,
    )


_ICONS = {
    "arrow": "M5 12h14M13 6l6 6-6 6",
    "check": "M5 13l4 4L19 7",
    "plus": "M12 5v14M5 12h14",
    "search": "M21 21l-4.3-4.3M11 18a7 7 0 100-14 7 7 0 000 14z",
    "layers": "M12 3l9 5-9 5-9-5zM3 13l9 5 9-5",
    "spark": "M12 3l2 6 6 2-6 2-2 6-2-6-6-2 6-2z",
    "doc": "M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8zM14 3v5h5",
    "clock": "M12 21a9 9 0 100-18 9 9 0 000 18zM12 7v5l3 2",
}


def _icon(name: str, size: int = 22) -> str:
    return (
        f"<svg width='{size}' height='{size}' viewBox='0 0 24 24' fill='none' "
        f"stroke='currentColor' stroke-width='1.8' stroke-linecap='round' "
        f"stroke-linejoin='round'><path d='{_ICONS[name]}'/></svg>"
    )


def _p_icons(system: dict) -> str:
    grid = "".join(
        f"<div class='card' style='display:flex;flex-direction:column;align-items:center;gap:8px'>"
        f"{_icon(n, 26)}<span class='muted' style='font-size:var(--size-xs)'>{n}</span></div>"
        for n in _ICONS
    )
    return _doc(
        system,
        "Icons",
        f"""
        <p class="kicker">Foundations</p>
        <h2>Icons</h2>
        <p class="muted">Drawn on a 24px grid at 1.8 stroke, so they sit level with
        the body face. Icons inherit <code>currentColor</code> — never fill them.</p>
        <div class="grid" style="grid-template-columns:repeat(4,1fr);margin:var(--space-5) 0">{grid}</div>
        <hr class="rule">
        <h3>In use</h3>
        <p>Inline with text {_icon("spark", 18)}, in a button, and on their own:</p>
        <div style="display:flex;gap:12px;align-items:center">
          <a class="btn">Continue {_icon("arrow", 18)}</a>
          <button class="icon-btn solid">{_icon("spark", 18)}</button>
          <button class="icon-btn">{_icon("layers", 18)}</button>
        </div>
        """,
    )


def _plate(system: dict, height: int = 260) -> str:
    """A figure. Drawn rather than photographed — nothing here reaches the
    network, so the system shows its treatment on a plate it owns."""
    ramp = _ramp(system)
    return (
        f"<div class='plate' style='height:{height}px;border-radius:var(--radius-md);"
        f"background:linear-gradient(135deg,{ramp[6]} 0%,{ramp[4]} 45%,{ramp[2]} 100%);"
        "position:relative;overflow:hidden'>"
        f"<div style='position:absolute;inset:0;background:"
        f"radial-gradient(60% 80% at 25% 20%,{ramp[1]}55,transparent 60%)'></div>"
        f"<div style='position:absolute;left:12%;bottom:0;width:22%;height:62%;"
        f"background:{ramp[7]}55'></div>"
        f"<div style='position:absolute;left:40%;bottom:0;width:16%;height:84%;"
        f"background:{ramp[8]}44'></div>"
        f"<div style='position:absolute;left:62%;bottom:0;width:26%;height:48%;"
        f"background:{ramp[7]}33'></div></div>"
    )


def _p_imagery(system: dict) -> str:
    t = theme(system)
    return _doc(
        system,
        "Imagery",
        f"""
        <p class="kicker">Foundations</p>
        <h2>Imagery</h2>
        <p class="muted">Figures are treated as <strong>{t["image_treatment"]}</strong> so
        they sit inside the palette rather than beside it. Corners follow
        <code>--radius-md</code>; captions take the caption size in muted ink.</p>
        <figure class="figure">
          {_plate(system, 320)}
          <figcaption>A figure at full measure. The treatment is applied by the
          <code>.figure</code> wrapper, never baked into the asset.</figcaption>
        </figure>
        <hr class="rule">
        <div class="grid g2">
          <figure class="figure">{_plate(system, 170)}
            <figcaption>Half measure, in a row.</figcaption></figure>
          <figure class="figure">{_plate(system, 170)}
            <figcaption>Two figures share the grid gutter.</figcaption></figure>
        </div>
        """,
    )


# ---------------------------------------------------------------- components

def _p_buttons(system: dict) -> str:
    t = theme(system)
    return _doc(
        system,
        "Buttons & tags",
        f"""
        <p class="kicker">Components</p>
        <h2>Buttons &amp; tags</h2>
        <p class="muted">The primary action is {t["buttons"]}. Hover and pressed states
        come from the accent ramp; keyboard focus is the accent
        <code>:focus-visible</code> ring.</p>
        <h3>Buttons</h3>
        <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
          <a class="btn">Continue {_icon("arrow", 18)}</a>
          <a class="btn secondary">Preview</a>
          <a class="btn quiet">Learn more</a>
          <span class="btn disabled">Disabled</span>
        </div>
        <hr class="rule">
        <h3>Icon buttons</h3>
        <div style="display:flex;gap:12px;align-items:center">
          <button class="icon-btn solid">{_icon("spark", 18)}</button>
          <button class="icon-btn">{_icon("layers", 18)}</button>
          <span class="muted">{_icon("arrow", 18)}</span>
        </div>
        <hr class="rule">
        <h3>Full width</h3>
        <a class="btn block">Create project {_icon("arrow", 18)}</a>
        <hr class="rule">
        <h3>Tags</h3>
        <div style="display:flex;gap:10px;align-items:center">
          <span class="tag tint">Accent · tinted</span>
          <span class="tag outline">Accent · outlined</span>
          <span class="tag neutral">Neutral</span>
        </div>
        """,
    )


def _p_cards(system: dict) -> str:
    cards = "".join(
        f"""<div class="card">
              <p class="kicker">{kicker}</p>
              <h3>{title}</h3>
              <p class="muted">{body}</p>
              <div class="muted" style="display:flex;gap:8px;align-items:center;
                   font-size:var(--size-xs)">{_icon("layers", 16)} Updated today</div>
            </div>"""
        for kicker, title, body in [
            ("Getting started", "Pick a direction",
             "Three variants of the current theme, one axis at a time."),
            ("Typography", "Tune the pair",
             "Swap either family and the whole scale re-renders with it."),
            ("Export", "Ship the tokens",
             "Emit the variables and the guide into the project."),
        ]
    )
    return _doc(
        system,
        "Cards",
        f"""
        <p class="kicker">Components</p>
        <h2>Cards</h2>
        <p class="muted">Surface-filled cards with a kicker, title, body and meta row.</p>
        <div class="grid g3">{cards}</div>
        <hr class="rule">
        <h3>Elevation steps</h3>
        <div class="grid g3">
          <div class="card" style="box-shadow:none"><strong>Flat</strong></div>
          <div class="card"><strong>Raised</strong></div>
          <div class="card" style="box-shadow:var(--shadow-lg)"><strong>Top</strong></div>
        </div>
        """,
    )


def _p_dialog(system: dict) -> str:
    return _doc(
        system,
        "Dialog",
        f"""
        <p class="kicker">Components</p>
        <h2>Dialog</h2>
        <div style="position:relative;height:420px;border-radius:var(--radius-md);
             overflow:hidden;border:var(--rule)">
          <div style="position:absolute;inset:0;background:var(--n-800);opacity:.55"></div>
          <div class="card" style="position:absolute;left:50%;top:50%;
               transform:translate(-50%,-50%);width:min(520px,86%);box-shadow:var(--shadow-lg)">
            <h3>Publish this page?</h3>
            <p class="muted">It goes live at its current URL. You can unpublish at any
            time and nothing else on the site changes.</p>
            <div style="display:flex;gap:10px;justify-content:flex-end">
              <a class="btn secondary">Cancel</a>
              <a class="btn">Publish</a>
            </div>
          </div>
        </div>
        <p class="muted" style="margin-top:var(--space-4)">The backdrop is a
        <code>--n-800</code> scrim and the surface is a card at
        <code>--shadow-lg</code>. This page pins it inside a frame so the card can show
        it; in a real page the backdrop covers the viewport.</p>
        """,
    )


def _p_forms(system: dict) -> str:
    return _doc(
        system,
        "Forms",
        f"""
        <p class="kicker">Components</p>
        <h2>Forms</h2>
        <p class="muted">Native elements, themed states. Nothing here is a custom
        control, so keyboard and screen-reader behaviour is the platform's.</p>
        <div class="grid g2" style="margin-bottom:var(--space-5)">
          <div>
            <label for="pn">Project name</label>
            <input id="pn" type="text" value="Untitled project">
          </div>
          <div>
            <label>View</label>
            <div class="segmented">
              <button aria-pressed="true">Grid</button>
              <button aria-pressed="false">List</button>
              <button aria-pressed="false">Board</button>
            </div>
          </div>
        </div>
        <div class="grid g2">
          <div>
            <label for="nt">Notes</label>
            <textarea id="nt" rows="4">Ship the tokens with the guide.</textarea>
          </div>
          <div>
            <label>Apply to</label>
            <p style="display:flex;gap:8px;align-items:center">
              <input type="radio" name="a" checked> <span>Every new page</span></p>
            <p style="display:flex;gap:8px;align-items:center">
              <input type="radio" name="a"> <span>This page only</span></p>
            <label for="sel" style="margin-top:12px">Density</label>
            <select id="sel"><option>Comfortable</option><option>Compact</option></select>
          </div>
        </div>
        """,
    )


def _p_navigation(system: dict) -> str:
    return _doc(
        system,
        "Navigation",
        f"""
        <p class="kicker">Components</p>
        <h2>Navigation</h2>
        <div style="border-radius:var(--radius-md);overflow:hidden;border:var(--rule)">
          <div class="nav">
            <span class="brand">{system.get("name", "System")}</span>
            <a class="active" href="#">Product</a>
            <a href="#">Network</a>
            <a href="#">Docs</a>
            <span class="spacer"></span>
            <a class="btn">Get started</a>
          </div>
          <div style="padding:var(--space-6);background:var(--ground)">
            <p class="muted">The bar sits on the surface colour with a
            {theme(system)["dividers"]} divider beneath it, and the action stays at the
            right edge at every width.</p>
          </div>
        </div>
        """,
    )


def _p_table(system: dict) -> str:
    rows = "".join(
        f"<tr><td>{a}</td><td>{b}</td><td>{c}</td><td>{d}</td></tr>"
        for a, b, c, d in [
            ("Retention", "7 days", "30 days", "Up to 180 days"),
            ("Live streams", "1", "Unlimited", "Unlimited"),
            ("Alerting", "Email", "Email + chat", "Email + chat + pager"),
            ("Compliance", "—", "SOC 2", "SOC 2, HIPAA"),
            ("Support", "Community", "Business hours", "24×7"),
        ]
    )
    return _doc(
        system,
        "Table",
        f"""
        <p class="kicker">Components</p>
        <h2>Table</h2>
        <p class="muted">Ruled rows, an uppercase header in muted ink, and no zebra
        striping — the rule does the separating.</p>
        <table>
          <tr><th>Feature</th><th>Free</th><th>Pro</th><th>Enterprise</th></tr>
          {rows}
        </table>
        """,
    )


# ---------------------------------------------------------------- templates

def _tagline(system: dict) -> str:
    """The first line of a system's notes that actually describes it — the
    distilled form opens with Name:/Fonts:/Swatches:, which is a record, not a
    sentence."""
    if system.get("tagline"):
        return system["tagline"]
    # A distilled system is all `Key: value` lines, so take the palette's
    # description rather than the record's first field.
    for line in (system.get("notes") or "").splitlines():
        text = line.strip()
        if text.lower().startswith("palette:"):
            return text.split(":", 1)[1].strip() or "A system, applied."
    return "A system, applied."


def _p_deck(system: dict) -> str:
    name = system.get("name", "System")
    t = theme(system)
    ramp = _ramp(system)
    bars = "".join(
        f"<div style='flex:1;height:{h}%;background:var(--accent);opacity:{0.35 + i * 0.13};"
        "border-radius:2px 2px 0 0'></div>"
        for i, h in enumerate([38, 55, 47, 72, 90])
    )
    slides = f"""
    <section class="slide">
      <p class="kicker">Design systems</p>
      <h1>{name}</h1>
      <p class="muted" style="max-width:52ch">{_tagline(system)}</p>
      <div style="margin-top:auto" class="muted">Your name · Today</div>
    </section>

    <section class="slide">
      <p class="kicker">Contents</p>
      <h2>What's inside</h2>
      <ol class="muted" style="font-size:var(--size-md);line-height:2">
        <li>Principles</li><li>Foundations</li><li>Components</li><li>Templates</li>
      </ol>
    </section>

    <section class="slide accent">
      <h1 style="font-size:var(--size-2xl)">01</h1>
      <h2>Principles</h2>
    </section>

    <section class="slide">
      <p class="kicker">Working rules</p>
      <h2>Three rules</h2>
      <div class="grid g3" style="margin-top:var(--space-4)">
        <div><h3>Tokens first</h3><p class="muted">Never a literal value in a page.</p></div>
        <div><h3>One accent</h3><p class="muted">Colour marks action, nothing else.</p></div>
        <div><h3>Structure shows</h3><p class="muted">Let the grid be visible.</p></div>
      </div>
    </section>

    <section class="slide">
      <p class="kicker">Two columns</p>
      <h2>Type carries the page</h2>
      <div class="grid g2">
        <p class="muted">The heading face is used above the lead size only, so the
        page has one voice at scale and another at reading size.</p>
        <p class="muted">Body copy holds a 62-character measure. Everything else —
        spacing, rules, radii — follows the token sheet.</p>
      </div>
    </section>

    <section class="slide">
      <p class="kicker">Quadrants</p>
      <h2>Where it applies</h2>
      <div class="grid g2" style="flex:1">
        <div class="card"><strong>Product</strong><p class="muted">Screens and flows.</p></div>
        <div class="card"><strong>Marketing</strong><p class="muted">Landing and campaign.</p></div>
        <div class="card"><strong>Docs</strong><p class="muted">Reference and guides.</p></div>
        <div class="card"><strong>Decks</strong><p class="muted">This one.</p></div>
      </div>
    </section>

    <section class="slide">
      <p class="kicker">Data</p>
      <h2>The table, themed</h2>
      <table>
        <tr><th>Surface</th><th>Pages</th><th>Owner</th></tr>
        <tr><td>Foundations</td><td>5</td><td>Design</td></tr>
        <tr><td>Components</td><td>6</td><td>Design + Eng</td></tr>
        <tr><td>Templates</td><td>2</td><td>Design</td></tr>
      </table>
    </section>

    <section class="slide">
      <p class="kicker">Chart</p>
      <h2>Adoption by surface</h2>
      <div style="display:flex;gap:12px;align-items:flex-end;height:200px">{bars}</div>
      <div class="muted" style="display:flex;gap:12px;font-size:var(--size-xs)">
        <span style="flex:1">Docs</span><span style="flex:1">Marketing</span>
        <span style="flex:1">Support</span><span style="flex:1">Product</span>
        <span style="flex:1">Decks</span>
      </div>
    </section>

    <section class="slide">
      <p class="kicker">Timeline</p>
      <h2>How it lands</h2>
      <div style="display:flex;gap:0;align-items:center;margin-top:var(--space-4)">
        {"".join(
            f"<div style='flex:1'><div style='height:3px;background:var(--accent);"
            f"opacity:{0.3 + i * 0.22}'></div>"
            f"<div style='margin-top:10px'><strong>{q}</strong>"
            f"<div class='muted' style='font-size:var(--size-xs)'>{w}</div></div></div>"
            for i, (q, w) in enumerate(
                [("Week 1", "Tokens"), ("Week 2", "Components"),
                 ("Week 3", "Templates"), ("Week 4", "Rollout")]
            )
        )}
      </div>
    </section>

    <section class="slide" style="justify-content:center">
      <h2 style="font-size:var(--size-xl);max-width:24ch">“A system is what survives
      the second designer.”</h2>
      <p class="muted">— the point of all of this</p>
    </section>

    <section class="slide accent" style="justify-content:center;align-items:flex-start">
      <h1>Thank you</h1>
      <p style="opacity:.8">{name} · tokens, components, templates</p>
    </section>
    """
    return _doc(system, "Deck", slides)


def _p_landing(system: dict) -> str:
    name = system.get("name", "System")
    stats = "".join(
        f"<div><div style='font-family:var(--font-heading);font-size:var(--size-xl);"
        f"color:var(--accent)'>{v}</div>"
        f"<div class='muted' style='font-size:var(--size-xs);text-transform:uppercase;"
        f"letter-spacing:.08em'>{k}</div></div>"
        for v, k in [
            ("99.7%", "Arrivals within three minutes"),
            ("7,340", "Departures orchestrated daily"),
            ("60min", "One pulse, the whole network"),
            ("0", "Excuses per annum"),
        ]
    )
    return _doc(
        system,
        "Landing",
        f"""
        <div class="nav" style="margin:calc(var(--space-7) * -1) calc(var(--space-7) * -1) var(--space-7)">
          <span class="brand">Takt</span>
          <a class="active" href="#">Product</a><a href="#">Network</a><a href="#">Start</a>
          <span class="spacer"></span><a class="btn">Get started</a>
        </div>

        <h1 style="max-width:18ch">The 7:02 leaves at 7:02. Plan on it.</h1>
        <p class="muted" style="max-width:60ch;font-size:var(--size-md)">Takt is timetable
        infrastructure for people who run things: clockface scheduling, guaranteed
        connections, and disruption plans that are ready before the disruption is.</p>
        <div style="display:flex;gap:14px;align-items:center;margin-bottom:var(--space-6)">
          <a class="btn">Start scheduling {_icon("arrow", 18)}</a>
          <a class="btn quiet">Read the timetable</a>
        </div>

        <hr class="rule">
        <div class="grid" style="grid-template-columns:repeat(4,1fr)">{stats}</div>
        <hr class="rule">

        <p class="kicker">What Takt does</p>
        <div class="grid g3">
          <div><h3>Clockface</h3><p class="muted">Every service at the same minute past
          the hour, so the timetable is memorable without being printed.</p></div>
          <div><h3>Connections</h3><p class="muted">Transfers are guaranteed by design,
          not by luck — the plan holds them.</p></div>
          <div><h3>Recovery</h3><p class="muted">The disruption plan exists before the
          disruption, and everyone has already read it.</p></div>
        </div>

        <hr class="rule">
        <figure class="figure">{_plate(system, 300)}
          <figcaption>The network at its evening peak.</figcaption></figure>

        <hr class="rule">
        <div class="card" style="text-align:center;padding:var(--space-7)">
          <h2>Run it like a railway.</h2>
          <p class="muted">Because it is one.</p>
          <a class="btn">Get started {_icon("arrow", 18)}</a>
        </div>
        <p class="muted" style="text-align:center;font-size:var(--size-xs);
           margin-top:var(--space-6)">Built with the {name} design system.</p>
        """,
    )


def _p_parameters(system: dict) -> str:
    t = theme(system)
    rows = "".join(
        f"<tr><td style='width:38%' class='muted'>{k}</td><td><strong>{v}</strong></td></tr>"
        for k, v in [
            ("Ground", t["ground"]),
            ("Fonts", t["fonts"]),
            ("Density", t["density"]),
            ("Radius", t["radius"]),
            ("Layout", t["layout"]),
            ("Dividers", t["dividers"]),
            ("Buttons", t["buttons"]),
            ("Color use", t["color_use"]),
            ("Frame", t["frame"]),
            ("Image treatment", t["image_treatment"]),
            ("Icons", t["icons"]),
        ]
    )
    return _doc(
        system,
        "Parameters",
        f"""
        <p class="kicker">Theme</p>
        <h2>Theme parameters</h2>
        <div class="card"><table>{rows}</table></div>
        <p class="muted" style="margin-top:var(--space-4);max-width:70ch">These values are
        the seed this system was derived from: <code>styles.css</code>, every page in this
        project and the guide in <code>readme.md</code> were built from them (the
        machine-readable copy is <code>theme.json</code>). Retune the system in
        <code>styles.css</code> — the one place tokens live — and keep this page in step
        so the record stays true.</p>
        """,
    )


def readme_md(system: dict) -> str:
    t = theme(system)
    name = system.get("name", "Design system")
    files = "\n".join(
        f"- `{s['file']}` — {s['blurb'][0].lower() + s['blurb'][1:]}."
        for s in SECTIONS
        if s["id"] != "readme"
    )
    return f"""# {name}

{system.get("notes", "").strip()}

## Do

- Take every value from `styles.css`. A literal hex or pixel in a page is a bug.
- Keep one accent. Colour marks action; everything else is a step of the ramp.
- Use the heading face above `--size-md` and the body face below it.
- Let the {t["layout"]} layout show — structure is part of the design here.

## Don't

- Don't invent a radius: `--radius-md` is {t["radius"]} on purpose.
- Don't reach for a second accent to add emphasis; use the ramp.
- Don't soften the {t["dividers"]} dividers into whitespace.
- Don't bake an image treatment into an asset — the `.figure` wrapper does it.

## Files

- `styles.css` — the only stylesheet: the token sheet (`:root` variables, ramps,
  base type) plus the component layer. Link it from every page.
- `readme.md` — this guide.
- `theme.json` — the parameters these files were derived from.
{files}
"""


_BUILDERS = {
    "typography": _p_typography,
    "color": _p_color,
    "layout": _p_layout,
    "icons": _p_icons,
    "imagery": _p_imagery,
    "buttons": _p_buttons,
    "cards": _p_cards,
    "dialog": _p_dialog,
    "forms": _p_forms,
    "navigation": _p_navigation,
    "table": _p_table,
    "deck": _p_deck,
    "landing": _p_landing,
    "parameters": _p_parameters,
}


def page_html(system: dict, section_id: str) -> str:
    """The standalone document for one section of the system."""
    if section_id == "readme":
        return _doc(system, "Readme", _markdown(readme_md(system)))
    builder = _BUILDERS.get(section_id)
    if builder is None:
        raise KeyError(section_id)
    return builder(system)


def _markdown(text: str) -> str:
    """Just enough markdown for the readme — headings, lists, code, bold."""
    import html
    import re

    out: list[str] = []
    in_list = False
    for raw in text.splitlines():
        line = html.escape(raw.rstrip())
        line = re.sub(r"`([^`]+)`", r"<code>\1</code>", line)
        line = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", line)
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{line[2:]}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if line.startswith("### "):
            out.append(f"<h3>{line[4:]}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{line[2:]}</h1>")
        elif line.strip():
            out.append(f"<p>{line}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def theme_json(system: dict) -> str:
    t = theme(system)
    return json.dumps(
        {
            "name": system.get("name"),
            "source": system.get("source"),
            "swatches": _ramp(system),
            **t,
        },
        indent=2,
    )


def tree(system: dict) -> list[dict]:
    """The nav tree and the blurbs, for the document view."""
    return [
        {k: s[k] for k in ("id", "group", "name", "file", "blurb")} for s in SECTIONS
    ]


def system_zip(system: dict) -> bytes:
    """The whole system as a project — the files a developer would receive."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("styles.css", styles_css(system))
        z.writestr("readme.md", readme_md(system))
        z.writestr("theme.json", theme_json(system))
        for section in SECTIONS:
            if section["id"] == "readme":
                continue
            z.writestr(section["file"], page_html(system, section["id"]))
    return buf.getvalue()
