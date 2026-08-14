"""Design projects — the store behind Compass's Design section (the port of
Claude Design).

A project is one piece of visual work: a prompt, the template it started from,
the generated design (standalone HTML, so it renders in the same artifact
pipeline the rest of Compass uses), and the design system it should follow.

Storage follows the same config-or-fallback contract as everything else: Azure
Cosmos DB when configured, a local JSON file otherwise.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from compass.config import get_settings

# The templates Claude Design offers on its landing screen.
TEMPLATES = [
    {"id": "blank", "name": "Blank", "hint": "Start from nothing", "stem": ""},
    {
        "id": "mobile",
        "name": "Mobile app design",
        "hint": "Screens for a phone app",
        "stem": "Design a mobile app for ",
    },
    {"id": "slides", "name": "Slides", "hint": "A deck to present", "stem": "Make a deck about "},
    {
        "id": "document",
        "name": "Document",
        "hint": "A formatted written page",
        "stem": "Write and lay out a document on ",
    },
    {
        "id": "wireframe",
        "name": "Wireframe",
        "hint": "Low-fidelity structure",
        "stem": "Wireframe the flow for ",
    },
    {
        "id": "animation",
        "name": "Animation",
        "hint": "Something that moves",
        "stem": "Animate ",
    },
    {
        "id": "mockups",
        "name": "UI mockups",
        "hint": "High-fidelity screens",
        "stem": "Mock up the screens for ",
    },
    {"id": "resume", "name": "Résumé", "hint": "A one-page CV", "stem": "A résumé for "},
    {
        "id": "object3d",
        "name": "3D object",
        "hint": "A rendered object",
        "stem": "Model a 3D object: ",
    },
    {
        "id": "research",
        "name": "Research",
        "hint": "Findings, written up",
        "stem": "Write up research on ",
    },
    {
        "id": "email",
        "name": "HTML email",
        "hint": "An email that renders",
        "stem": "An HTML email announcing ",
    },
    {
        "id": "colortype",
        "name": "Color + type pairing",
        "hint": "A palette and typefaces",
        "stem": "A colour and type pairing for ",
    },
]

# Per-template guidance appended to the generation prompt.
TEMPLATE_PROMPTS = {
    "blank": "Design whatever the request describes.",
    "mobile": "Design mobile app screens at 390x844, shown side by side in a row.",
    "slides": (
        "Design a slide deck: each slide a 16:9 section with class=\"slide\". "
        "The class is what the PowerPoint and PDF exports cut on, so every "
        "slide needs it. If the deck presents one slide at a time, put the "
        "navigation chrome in an element with data-export-hide so it stays out "
        "of the exported file."
    ),
    "document": "Design a formatted document page with clear typographic hierarchy.",
    "wireframe": "Design a low-fidelity greyscale wireframe: boxes, placeholder text, no colour.",
    "animation": "Design an animated piece using CSS keyframes; it must move on load.",
    "mockups": "Design high-fidelity UI mockups with realistic content and states.",
    "resume": "Design a one-page résumé with strong typography.",
    "object3d": "Render a 3D-looking object using CSS 3D transforms.",
    "research": "Design a research write-up: findings, evidence, and a conclusion.",
    "email": "Design an HTML email using table layout and inline styles for client support.",
    "colortype": "Design a colour palette and type-pairing specimen sheet with swatches and samples.",
}


# What a new blank page starts as — enough to render and be edited, nothing
# that pretends to be a design.
BLANK_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Blank page</title>
<style>
  body { margin: 0; font-family: system-ui, sans-serif; color: #1D1D1F;
         background: #fff; padding: 64px; }
  h1 { font-size: 2rem; font-weight: 600; margin: 0 0 12px; }
  p { color: #6b6b70; max-width: 60ch; }
</style></head>
<body><h1>Blank page</h1><p>Describe what this page should be, or edit it here.</p></body>
</html>
"""

# How many past versions a project keeps. Deep enough to undo a session's
# worth of edits, shallow enough that the store stays a readable file.
MAX_VERSIONS = 25

# Fields the projects table never needs — a design and its history are large.
_HEAVY = ("html", "turns", "versions", "comments", "pages")


@dataclass
class DesignProject:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = "Untitled"
    template: str = "blank"
    prompt: str = ""
    html: str = ""              # the active page's document
    pages: list[dict] = field(default_factory=list)  # {id, name, html, updated_at}
    active_page: str = ""
    turns: list[dict] = field(default_factory=list)  # the design conversation
    versions: list[dict] = field(default_factory=list)  # past html, newest first
    comments: list[dict] = field(default_factory=list)  # pins left on the canvas
    design_system: str = ""     # the first system, kept for older rows
    design_systems: list[str] = field(default_factory=list)  # every system it follows
    starred: bool = False
    viewed_at: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    def card(self) -> dict:
        """Row shape for the projects table (no html — it can be large)."""
        d = self.to_dict()
        for k in _HEAVY:
            d.pop(k, None)
        return d


class DesignStore:
    def _path(self) -> Path:
        d = get_settings().workspace_root / get_settings().data_dir
        d.mkdir(parents=True, exist_ok=True)
        return d / "design.json"

    def _read(self) -> list[dict]:
        p = self._path()
        if not p.is_file():
            return []
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return []

    def _write(self, rows: list[dict]) -> None:
        self._path().write_text(json.dumps(rows, indent=2))

    async def list(self) -> list[dict]:
        rows = self._read()
        rows.sort(key=lambda r: r.get("viewed_at") or r.get("updated_at", 0), reverse=True)
        return [
            {k: v for k, v in r.items() if k not in _HEAVY}
            | {"versions": len(r.get("versions") or [])}
            for r in rows
        ]

    async def get(self, project_id: str) -> dict | None:
        return next((r for r in self._read() if r.get("id") == project_id), None)

    async def touch(self, project_id: str) -> dict | None:
        """Record that the project was opened — the table sorts on this, the way
        claude.ai's "Last viewed" column does."""
        rows = self._read()
        for r in rows:
            if r.get("id") == project_id:
                r["viewed_at"] = time.time()
                self._write(rows)
                return r
        return None

    @staticmethod
    def _pages(row: dict) -> list[dict]:
        """A project's pages, inventing the first from the design it already
        has — every project had exactly one page before this existed."""
        pages = list(row.get("pages") or [])
        if not pages:
            pages = [
                {
                    "id": "p1",
                    "name": f"{row.get('name', 'Design')}.html",
                    "html": row.get("html", ""),
                    "updated_at": row.get("updated_at", time.time()),
                }
            ]
        return pages

    async def pages(self, project_id: str) -> list[dict]:
        row = await self.get(project_id)
        if row is None:
            return []
        return [
            {k: v for k, v in p.items() if k != "html"} | {"chars": len(p.get("html") or "")}
            for p in self._pages(row)
        ]

    async def add_page(self, project_id: str, name: str = "") -> dict | None:
        """A new blank page, and the project switches to it."""
        rows = self._read()
        for r in rows:
            if r.get("id") != project_id:
                continue
            pages = self._pages(r)
            page = {
                "id": uuid.uuid4().hex[:8],
                "name": name or f"Page {len(pages) + 1}.html",
                "html": BLANK_PAGE,
                "updated_at": time.time(),
            }
            pages.append(page)
            r["pages"] = pages
            r["active_page"] = page["id"]
            r["html"] = page["html"]
            r["updated_at"] = time.time()
            self._write(rows)
            return r
        return None

    async def open_page(self, project_id: str, page_id: str) -> dict | None:
        """Switch pages: the outgoing one keeps what is on the canvas."""
        rows = self._read()
        for r in rows:
            if r.get("id") != project_id:
                continue
            pages = self._pages(r)
            current = r.get("active_page") or pages[0]["id"]
            for p in pages:
                if p["id"] == current:
                    p["html"] = r.get("html", "")
            target = next((p for p in pages if p["id"] == page_id), None)
            if target is None:
                return None
            r["pages"] = pages
            r["active_page"] = page_id
            r["html"] = target.get("html", "")
            self._write(rows)
            return r
        return None

    async def delete_page(self, project_id: str, page_id: str) -> dict | None:
        """Drop a page. The last one stays — a project without a page has
        nothing to show."""
        rows = self._read()
        for r in rows:
            if r.get("id") != project_id:
                continue
            pages = self._pages(r)
            if len(pages) < 2:
                return None
            kept = [p for p in pages if p["id"] != page_id]
            if len(kept) == len(pages):
                return None
            r["pages"] = kept
            if (r.get("active_page") or pages[0]["id"]) == page_id:
                r["active_page"] = kept[0]["id"]
                r["html"] = kept[0].get("html", "")
            r["updated_at"] = time.time()
            self._write(rows)
            return r
        return None

    async def save_html(
        self, project_id: str, html: str, *, label: str = "Edited"
    ) -> dict | None:
        """Write a new design, keeping the outgoing one as a version. Every path
        that changes the html goes through here, so history is never partial."""
        rows = self._read()
        for r in rows:
            if r.get("id") != project_id:
                continue
            previous = r.get("html") or ""
            if previous and previous != html:
                versions = list(r.get("versions") or [])
                versions.insert(
                    0,
                    {
                        "id": uuid.uuid4().hex[:12],
                        "at": r.get("updated_at", time.time()),
                        "label": r.get("version_label") or "Previous version",
                        "html": previous,
                    },
                )
                r["versions"] = versions[:MAX_VERSIONS]
            r["html"] = html
            r["version_label"] = label
            r["updated_at"] = time.time()
            pages = self._pages(r)
            active = r.get("active_page") or pages[0]["id"]
            for p in pages:
                if p["id"] == active:
                    p["html"] = html
                    p["updated_at"] = r["updated_at"]
            r["pages"] = pages
            r["active_page"] = active
            self._write(rows)
            return r
        return None

    async def duplicate(self, project_id: str) -> dict | None:
        rows = self._read()
        source = next((r for r in rows if r.get("id") == project_id), None)
        if source is None:
            return None
        copy = dict(source)
        copy["id"] = uuid.uuid4().hex[:12]
        copy["name"] = f"{source.get('name', 'Untitled')} copy"
        copy["versions"] = []  # a copy starts its own history
        copy["starred"] = False
        copy["created_at"] = copy["updated_at"] = copy["viewed_at"] = time.time()
        rows.append(copy)
        self._write(rows)
        return copy

    async def create(
        self,
        *,
        name: str,
        template: str,
        prompt: str,
        design_system: str = "",
        design_systems: list[str] | None = None,
    ) -> dict:
        rows = self._read()
        systems = list(design_systems or ([design_system] if design_system else []))
        p = DesignProject(
            name=name or "Untitled",
            template=template if template in TEMPLATE_PROMPTS else "blank",
            prompt=prompt,
            design_system=systems[0] if systems else "",
            design_systems=systems,
        ).to_dict()
        rows.append(p)
        self._write(rows)
        return p

    async def update(self, project_id: str, **fields) -> dict | None:
        rows = self._read()
        for r in rows:
            if r.get("id") == project_id:
                for k, v in fields.items():
                    if v is not None:
                        r[k] = v
                r["updated_at"] = time.time()
                self._write(rows)
                return r
        return None

    async def delete(self, project_id: str) -> bool:
        rows = self._read()
        kept = [r for r in rows if r.get("id") != project_id]
        if len(kept) == len(rows):
            return False
        self._write(kept)
        return True


@dataclass
class DesignSystem:
    """A house style a design should follow — colours, type, spacing, the
    components and the tone. Held as prose because that is what the model
    consumes; `css` keeps any literal tokens that came with it."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = "Untitled system"
    source: str = "pasted"  # pasted | upload | url | repo | included
    notes: str = ""         # the distilled system, in prose
    css: str = ""           # verbatim tokens, when the source had them
    fonts: str = ""         # the typefaces, for the card
    swatches: list[str] = field(default_factory=list)  # the ramp, for the card
    origin: str = ""        # where it came from — a URL, a repo path, a filename
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


# The systems that ship with Design. They are real, usable systems — a design
# told to follow one comes back in that voice — and they double as the examples
# on the Design systems tab, which is why each carries its fonts and ramp.
BUILTIN_SYSTEMS: list[dict] = [
    {
        "id": "builtin-modernist",
        "tagline": (
        "Swiss-school modernism: a strict grid, Archivo everywhere, square corners, "
        "strong rules and one red accent on a near-white ground."
        ),
        "params": {
            "ground": "light (scheme: mono, hue 8°, saturation 0.85)",
            "fonts": "Archivo / Archivo",
            "density": "1.00×",
            "radius": "0px",
            "layout": "grid",
            "dividers": "strong",
            "buttons": "solid, flush-left labels",
            "color_use": "fill",
            "frame": "none",
            "image_treatment": "grayscale",
            "icons": "geometric",
        },
        "name": "Modernist",
        "source": "included",
        "builtin": True,
        "fonts": "Archivo",
        "font_display": "Archivo",
        "font_body": "Archivo",
        "swatches": [
            "#FDECE7", "#FBD5CB", "#F7AE9B", "#F2775B", "#E8482A",
            "#C7331A", "#9C2814", "#701C0E", "#451108",
        ],
        "notes": (
            "Name: Modernist\n\n"
            "Palette: a single warm red ramp — #FDECE7 tints for surfaces, "
            "#E8482A as the one accent, #451108 for ink. Greys are the red "
            "desaturated, never neutral grey.\n"
            "Type: Archivo throughout, 700 for headings and 400 for body. "
            "Headings are tight (-0.02em) and large; the scale is 13/15/18/24/34/48.\n"
            "Spacing & shape: an 8px rhythm, 2px radii, 1px rules, no shadows. "
            "Layouts are grids with visible structure.\n"
            "Components: buttons are square and solid accent; cards are outlined, "
            "not filled; tables have ruled rows and no zebra striping.\n"
            "Voice: declarative and unadorned. Short sentences. No exclamation marks."
        ),
        "css": (
            ":root{--surface:#FDECE7;--accent:#E8482A;--ink:#451108;\n"
            "  --radius:2px;--rule:1px solid #F2775B;--font:'Archivo',sans-serif}"
        ),
    },
    {
        "id": "builtin-classical",
        "tagline": (
        "Book typography: parchment and aged gold, Cormorant for display and Lora "
        "for reading, wide margins, hairline rules and no radii."
        ),
        "params": {
            "ground": "light (scheme: warm, hue 40°, saturation 0.50)",
            "fonts": "Cormorant Garamond / Lora",
            "density": "1.15×",
            "radius": "0px",
            "layout": "single column",
            "dividers": "hairline",
            "buttons": "text with a rule",
            "color_use": "accent only",
            "frame": "none",
            "image_treatment": "sepia",
            "icons": "geometric",
        },
        "name": "Classical",
        "source": "included",
        "builtin": True,
        "fonts": "Cormorant Garamond / Lora",
        "font_display": "Cormorant Garamond",
        "font_body": "Lora",
        "swatches": [
            "#FBF3E0", "#F4E4BF", "#E8CE92", "#D9B364", "#C2963F",
            "#9E772C", "#78591F", "#523C14", "#2E210B",
        ],
        "notes": (
            "Name: Classical\n\n"
            "Palette: warm parchment #FBF3E0 pages, aged gold #C2963F for accents "
            "and rules, #2E210B ink. Colour is used sparingly — the page is mostly "
            "paper and text.\n"
            "Type: Cormorant Garamond for display at generous sizes, Lora for body "
            "at 17px/1.7. Small caps and letterspacing for eyebrows; the scale is "
            "14/17/21/28/40/56.\n"
            "Spacing & shape: wide margins, long measure (65-75 characters), hairline "
            "rules, no radii, no shadows.\n"
            "Components: buttons are text with a rule beneath; cards are separated by "
            "rules rather than boxes; tables are ruled top and bottom only.\n"
            "Voice: considered and formal, full sentences, the occasional em dash."
        ),
        "css": (
            ":root{--page:#FBF3E0;--accent:#C2963F;--ink:#2E210B;\n"
            "  --radius:0;--display:'Cormorant Garamond',serif;--body:'Lora',serif}"
        ),
    },
    {
        "id": "builtin-nocturne",
        "tagline": (
        "Dark by default: Inter throughout, violet accents, soft glows, and surfaces "
        "that lift off a near-black ground."
        ),
        "dark": True,
        "params": {
            "ground": "dark (scheme: cool, hue 258°, saturation 0.60)",
            "fonts": "Inter / Inter",
            "density": "0.95×",
            "radius": "10px",
            "layout": "grid",
            "dividers": "subtle",
            "buttons": "solid with a glow",
            "color_use": "fill",
            "frame": "card",
            "image_treatment": "none",
            "icons": "geometric",
        },
        "name": "Nocturne",
        "source": "included",
        "builtin": True,
        "fonts": "Inter",
        "font_display": "Inter",
        "font_body": "Inter",
        "swatches": [
            "#EEEAFB", "#D6CCF6", "#B7A6EF", "#957FE6", "#7458DA",
            "#5B3FC0", "#452F96", "#2F206B", "#1A1140",
        ],
        "notes": (
            "Name: Nocturne\n\n"
            "Palette: dark by default — #12101C ground, #1A1140 raised surfaces, "
            "#957FE6 accent, #EEEAFB text. Light-on-dark is the design, not a mode.\n"
            "Type: Inter throughout, 600 headings and 400 body at 15px/1.6, "
            "tabular numerals for data. The scale is 12/14/15/18/24/32.\n"
            "Spacing & shape: a 4px rhythm, 10px radii, 1px borders at 10% white, "
            "soft glows instead of drop shadows.\n"
            "Components: buttons are filled accent with a subtle glow; cards are "
            "raised surfaces with a hairline border; inputs sit darker than the card.\n"
            "Voice: precise and technical, sentence case, no marketing adjectives."
        ),
        "css": (
            ":root{--ground:#12101C;--surface:#1A1140;--accent:#957FE6;\n"
            "  --ink:#EEEAFB;--radius:10px;--font:'Inter',sans-serif}"
        ),
    },
    {
        "id": "builtin-organic",
        "tagline": (
        "Warm and round: Caprasimo display over Figtree text, terracotta on cream, "
        "generous padding and soft edges everywhere."
        ),
        "params": {
            "ground": "light (scheme: warm, hue 24°, saturation 0.70)",
            "fonts": "Caprasimo / Figtree",
            "density": "1.05×",
            "radius": "18px",
            "layout": "stacked",
            "dividers": "none",
            "buttons": "pill",
            "color_use": "fill",
            "frame": "rounded",
            "image_treatment": "warm",
            "icons": "geometric",
        },
        "name": "Organic",
        "source": "included",
        "builtin": True,
        "fonts": "Caprasimo / Figtree",
        "font_display": "Caprasimo",
        "font_body": "Figtree",
        "swatches": [
            "#FEF1E6", "#FBDCC4", "#F5BE95", "#EC9A63", "#DE7638",
            "#BC5A23", "#93441A", "#6A3013", "#3F1C0B",
        ],
        "notes": (
            "Name: Organic\n\n"
            "Palette: sunbaked terracotta — #FEF1E6 surfaces, #DE7638 accent, "
            "#3F1C0B ink, with #6A3013 for depth. Warmth in every neutral.\n"
            "Type: Caprasimo for display (big, round, playful), Figtree for body at "
            "16px/1.65. The scale is 14/16/20/26/36/52.\n"
            "Spacing & shape: generous padding, 18px radii, soft edges everywhere, "
            "shadows are warm and low.\n"
            "Components: buttons are pill-shaped solid accent with white text; cards "
            "are filled and rounded; imagery is masked into organic shapes.\n"
            "Voice: friendly and human, contractions welcome, never corporate."
        ),
        "css": (
            ":root{--surface:#FEF1E6;--accent:#DE7638;--ink:#3F1C0B;\n"
            "  --radius:18px;--display:'Caprasimo',cursive;--body:'Figtree',sans-serif}"
        ),
    },
    {
        "id": "builtin-broadsheet",
        "tagline": (
        "Newspaper typography: Source Serif at reading sizes, a cyan spot colour, "
        "ruled columns and figures that sit inside the grid."
        ),
        "name": "Broadsheet",
        "params": {
            "ground": "light (scheme: cool, hue 200°, saturation 0.75)",
            "fonts": "Source Serif 4 / Source Serif 4",
            "density": "1.05×",
            "radius": "2px",
            "layout": "columns",
            "dividers": "ruled",
            "buttons": "solid, square",
            "color_use": "spot",
            "frame": "none",
            "image_treatment": "duotone",
            "icons": "geometric",
        },
        "source": "included",
        "builtin": True,
        "fonts": "Source Serif 4",
        "font_display": "Source Serif 4",
        "font_body": "Source Serif 4",
        "swatches": [
            "#E8F6FD", "#C7EAFA", "#94D8F5", "#54BFEA", "#22A2D6",
            "#1183B4", "#0C6389", "#08455F", "#052A3A",
        ],
        "notes": (
            "Name: Broadsheet\n\n"
            "Palette: a cool cyan ramp — #E8F6FD tints, #22A2D6 as the single spot "
            "colour, #052A3A ink. Colour marks a link or a lead, never a mood.\n"
            "Type: Source Serif 4 for both display and body, 600 headings and 400 "
            "text at 17px/1.65. The scale is 13/15/17/22/30/44.\n"
            "Spacing & shape: a measured column grid, 2px radii, ruled dividers "
            "above every section, no shadows.\n"
            "Components: buttons are square and solid; cards are ruled rather than "
            "boxed; tables have a heavy rule under the header.\n"
            "Voice: reported and specific. Facts first, adjectives last."
        ),
        "css": (
            ":root{--surface:#FFFFFF;--accent:#22A2D6;--ink:#052A3A;\n"
            "  --radius:2px;--font:'Source Serif 4',Georgia,serif}"
        ),
    },
    {
        "id": "builtin-industry",
        "tagline": (
        "Engineering drawing: Barlow Condensed over Barlow, steel blues, tick "
        "marks at the corners and photographs printed in duotone."
        ),
        "name": "Industry",
        "params": {
            "ground": "light (scheme: cool, hue 215°, saturation 0.35)",
            "fonts": "Barlow Condensed / Barlow",
            "density": "0.95×",
            "radius": "0px",
            "layout": "grid",
            "dividers": "tick marks",
            "buttons": "solid, square",
            "color_use": "fill",
            "frame": "outline",
            "image_treatment": "duotone",
            "icons": "geometric",
        },
        "source": "included",
        "builtin": True,
        "fonts": "Barlow Condensed / Barlow",
        "font_display": "Barlow Condensed",
        "font_body": "Barlow",
        "swatches": [
            "#EDF2F8", "#D3E0EE", "#AFC5DC", "#89A6C4", "#6788AC",
            "#4C6A8C", "#374F6B", "#25364A", "#161F2B",
        ],
        "notes": (
            "Name: Industry\n\n"
            "Palette: steel blues — #EDF2F8 surfaces, #6788AC accent, #161F2B ink. "
            "Everything reads like a drawing sheet.\n"
            "Type: Barlow Condensed for headings (uppercase, tight), Barlow for body "
            "at 15px/1.55. The scale is 12/14/15/20/28/40.\n"
            "Spacing & shape: a strict grid, 0 radii, tick marks at panel corners "
            "instead of borders, no shadows.\n"
            "Components: buttons are square and solid; cards are outlined panels; "
            "tables are dense with ruled rows.\n"
            "Voice: technical and terse. Specifications, not sentences."
        ),
        "css": (
            ":root{--surface:#EDF2F8;--accent:#6788AC;--ink:#161F2B;\n"
            "  --radius:0;--display:'Barlow Condensed',sans-serif;--body:'Barlow',sans-serif}"
        ),
    },
]

class DesignSystemStore:
    def _path(self) -> Path:
        d = get_settings().workspace_root / get_settings().data_dir
        d.mkdir(parents=True, exist_ok=True)
        return d / "design_systems.json"

    def _read(self) -> list[dict]:
        p = self._path()
        if not p.is_file():
            return []
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return []

    def _write(self, rows: list[dict]) -> None:
        self._path().write_text(json.dumps(rows, indent=2))

    async def list(self) -> list[dict]:
        """The user's systems, newest first. The included ones are returned
        separately so the tab can show them as examples under their own heading."""
        rows = self._read()
        rows.sort(key=lambda r: r.get("updated_at", 0), reverse=True)
        return rows

    async def get(self, system_id: str) -> dict | None:
        builtin = next((s for s in BUILTIN_SYSTEMS if s["id"] == system_id), None)
        if builtin is not None:
            return builtin
        return next((r for r in self._read() if r.get("id") == system_id), None)

    async def create(
        self,
        *,
        name: str,
        source: str,
        notes: str,
        css: str = "",
        fonts: str = "",
        swatches: list[str] | None = None,
        origin: str = "",
    ) -> dict:
        rows = self._read()
        s = DesignSystem(
            name=name or "Untitled system",
            source=source,
            notes=notes,
            css=css,
            fonts=fonts,
            swatches=swatches or [],
            origin=origin,
        ).to_dict()
        rows.append(s)
        self._write(rows)
        return s

    async def duplicate(self, system: dict) -> dict:
        """Copy a system into the user's own, so an included one can be
        annotated and retuned without being edited in place."""
        rows = self._read()
        copy = dict(system)
        copy["id"] = uuid.uuid4().hex[:12]
        copy["name"] = f"{system.get('name', 'Untitled system')} copy"
        copy["source"] = "copied"
        copy["builtin"] = False
        copy["created_at"] = copy["updated_at"] = time.time()
        rows.append(copy)
        self._write(rows)
        return copy

    async def delete(self, system_id: str) -> bool:
        rows = self._read()
        kept = [r for r in rows if r.get("id") != system_id]
        if len(kept) == len(rows):
            return False
        self._write(kept)
        return True


_store: DesignStore | None = None
_systems: DesignSystemStore | None = None


def get_design_store() -> DesignStore:
    global _store
    if _store is None:
        _store = DesignStore()
    return _store


def get_system_store() -> DesignSystemStore:
    global _systems
    if _systems is None:
        _systems = DesignSystemStore()
    return _systems


# Distilling a pasted style guide / stylesheet into a system the model can
# follow. Kept short on purpose — a long system crowds out the actual request.
EXTRACT_PROMPT = (
    "You are reading a style guide, stylesheet, brand page, or the source of a "
    "component library. Distil it into a design system another designer could "
    "follow.\n\n"
    "Reply as plain text, no preamble, in this shape:\n"
    "Name: <a short name for the system>\n"
    "Fonts: <the display typeface / the body typeface>\n"
    "Swatches: <exactly 9 hex values, lightest to darkest, comma separated>\n"
    "Palette: <the colours, with hex values and what each is for>\n"
    "Type: <typefaces, weights, and the size scale>\n"
    "Spacing & shape: <spacing rhythm, radii, borders, shadows>\n"
    "Components: <how buttons, cards, inputs, and tables look>\n"
    "Voice: <the tone the writing takes>\n\n"
    "Only state what the source actually shows. Say nothing about what is absent. "
    "For Swatches, build the ramp from the colours the source actually uses."
)


def parse_extract(notes: str) -> tuple[str, str, list[str]]:
    """Pull the name, fonts, and colour ramp back out of a distilled system so
    the card can show them without re-reading the prose."""
    import re

    name = fonts = ""
    swatches: list[str] = []
    for line in notes.splitlines():
        low = line.lower()
        if low.startswith("name:") and not name:
            name = line.split(":", 1)[1].strip()
        elif low.startswith("fonts:") and not fonts:
            fonts = line.split(":", 1)[1].strip()
        elif low.startswith("swatches:") and not swatches:
            swatches = re.findall(r"#[0-9a-fA-F]{6}", line)
    if len(swatches) < 3:  # no usable ramp on its own line — take the prose's
        swatches = list(dict.fromkeys(re.findall(r"#[0-9a-fA-F]{6}", notes)))[:9]
    return name, fonts, swatches[:9]


# When a brief is too thin to design from, Compass asks rather than guesses —
# and asks in the shape of a short form, because a wall of questions in prose
# is worse than four fields.
CLARIFY_PROMPT = (
    "You are Compass Design deciding whether a request is specific enough to "
    "design from.\n\n"
    "If it is — there is a subject and enough intent to make something real — "
    'reply exactly: {"ready": true}\n\n'
    "If it is not — the subject is missing, or the sentence trails off — reply "
    "with a short form that would settle it, as JSON:\n"
    '{"ready": false, "title": "What should I model?", '
    '"waiting": "Waiting on object selection", '
    '"note": "Waiting on the form — mainly which object you want.", '
    '"subtitle": "Your message cut off before the object — tell me what to '
    'build and I\'ll start.", "fields": [\n'
    '  {"id":"object","label":"The object","hint":"Be as specific as you like '
    '— brand-free, but details help","type":"textarea","max":400,'
    '"placeholder":"e.g. a 1960s desk fan, a moka pot, a hand plane"},\n'
    '  {"id":"detail","label":"How detailed?","hint":"Detail costs nothing to '
    'view, but simpler reads cleaner","type":"segmented",'
    '"options":["Clean and simple","Balanced","Highly detailed"],'
    '"value":"Balanced"},\n'
    '  {"id":"style","label":"Modeling style","type":"radio",'
    '"options":["Photoreal proportions","Stylized / toy-like",'
    '"Technical / product-shot neutral","Low-poly faceted"]},\n'
    '  {"id":"use","label":"What\'s it for?","hint":"Shapes how I trade off '
    'polish vs. export cleanliness","type":"checkbox",'
    '"options":["Just to look at / spin around","Import into Blender or a 3D '
    'tool","Game or AR asset"]}\n]}\n\n'
    "Rules: four or five fields, never more. The first is always the missing "
    "subject, and the last asks for anything that must be present — the "
    "specific parts or sections the person cares about. Write the questions "
    "for THIS request: a deck asks about audience and length, a résumé about "
    "the role, a landing page about the product.\n"
    '"waiting" is a four-word status for the project while it waits '
    '("Waiting on object selection"). "note" is one sentence saying what is '
    "being waited on. Reply with JSON and nothing else."
)


# Asked again after a first round: what else would sharpen this?
FOLLOWUP_PROMPT = (
    "You already asked about this design and got the answers below. The person "
    "then pressed \"Ask me follow-up questions\" — they asked for another round, "
    "so there is always one to give. Return three or four NEW questions in the "
    "same JSON shape ({\"ready\": false, \"title\", \"waiting\", \"note\", "
    "\"subtitle\", \"fields\"}), with the same field types. Never repeat a "
    "question already answered, and never re-ask the subject. Go one level "
    "finer than the first round: materials and finish, colour and palette, "
    "lighting or mood, scale and proportion, setting or background, motion, "
    "labelling, what to leave out. Never reply {\"ready\": true} — they asked to "
    "be asked. JSON only."
)


# What the second round falls back to when the model has nothing to add: the
# questions that sharpen almost any design. Asked-for questions get asked.
FOLLOWUP_FALLBACK = {
    "ready": False,
    "title": "A few more details",
    "waiting": "Waiting on the details",
    "note": "A second round — finish, colour, mood, and what to leave out.",
    "subtitle": "Nothing here is required; anything you skip, I'll choose.",
    "fields": [
        {
            "id": "materials",
            "label": "Materials and finish",
            "hint": "Surfaces, textures, how new or worn it should read",
            "type": "textarea",
            "max": 300,
            "placeholder": "e.g. satin painted steel, light patina, chrome trim",
        },
        {
            "id": "palette",
            "label": "Colour direction",
            "type": "radio",
            "options": [
                "Muted and natural",
                "Bold and saturated",
                "Mostly monochrome",
                "Follow the design system",
            ],
        },
        {
            "id": "mood",
            "label": "Mood",
            "type": "radio",
            "options": [
                "Warm and inviting",
                "Clean and technical",
                "Playful",
                "Serious and premium",
            ],
        },
        {
            "id": "avoid",
            "label": "Anything to leave out",
            "hint": "Details, elements, or clichés you would rather not see",
            "type": "textarea",
            "max": 300,
            "placeholder": "e.g. no gradients, no stock-photo people, no drop shadows",
        },
    ],
}


# The form only knows five field types. A model asked for JSON will now and
# then reach for a sixth ("select", "multiselect", "short_text") or hand back
# options as objects rather than strings, so every form is put through this
# before it reaches the canvas.
_FIELD_TYPES = {
    "textarea": "textarea",
    "text": "text",
    "string": "text",
    "input": "text",
    "short_text": "text",
    "segmented": "segmented",
    "toggle": "segmented",
    "radio": "radio",
    "select": "radio",
    "dropdown": "radio",
    "choice": "radio",
    "single_select": "radio",
    "checkbox": "checkbox",
    "multiselect": "checkbox",
    "multi_select": "checkbox",
    "checkboxes": "checkbox",
}


def normalize_clarify(form: dict) -> dict:
    """Coerce a model's form into the shapes the canvas can render."""
    fields = []
    for raw in form.get("fields") or []:
        if not isinstance(raw, dict):
            continue
        label = str(raw.get("label") or raw.get("title") or "").strip()
        if not label:
            continue
        options = []
        for opt in raw.get("options") or []:
            if isinstance(opt, dict):
                text = opt.get("label") or opt.get("title") or opt.get("value")
            else:
                text = opt
            text = str(text or "").strip()
            if text:
                options.append(text)

        kind = _FIELD_TYPES.get(str(raw.get("type") or "").strip().lower(), "")
        if not kind:
            kind = "radio" if options else "textarea"
        if kind in {"radio", "checkbox", "segmented"} and not options:
            kind = "text"
        # Three short options read better as a segmented control; more than
        # that, or anything wordy, wants the stacked list.
        if kind == "segmented" and (len(options) > 3 or max(map(len, options)) > 22):
            kind = "radio"

        field = {
            "id": str(raw.get("id") or label.lower().replace(" ", "_"))[:48],
            "label": label,
            "type": kind,
        }
        for key in ("hint", "placeholder"):
            if raw.get(key):
                field[key] = str(raw[key])
        if options:
            field["options"] = options
        if raw.get("max"):
            try:
                field["max"] = int(raw["max"])
            except (TypeError, ValueError):
                pass
        value = raw.get("value")
        if isinstance(value, str) and (not options or value in options):
            field["value"] = value
        fields.append(field)

    form = dict(form)
    form["fields"] = fields
    return form


def clarify_answers_block(answers: dict) -> str:
    """Fold a filled-in form back into the brief."""
    lines = []
    for label, value in answers.items():
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        if str(value).strip():
            lines.append(f"- {label}: {value}")
    return "\n".join(lines)


def system_prompt_block(*systems: dict | None) -> str:
    """The instruction appended to a generation when systems are attached.

    More than one is allowed — a team's brand plus a product system, say — in
    which case the first leads and the rest are named as compatible."""
    kept = [s for s in systems if s]
    if not kept:
        return ""

    parts: list[str] = []
    lead = kept[0]
    parts.append("Follow this design system exactly — it outranks your own taste:")
    parts.append(lead.get("notes", "").strip())
    css = (lead.get("css") or "").strip()
    if css:
        parts.append("Use these tokens verbatim:\n\n```css\n" + css[:6_000] + "\n```")

    for extra in kept[1:]:
        parts.append(
            "Also stay compatible with " + (extra.get("name") or "this system") + ":\n\n"
            + extra.get("notes", "").strip()
        )
    return "\n\n".join(p for p in parts if p)


DESIGN_SYSTEM_PROMPT = (
    "You are Compass Design — you turn a described idea into a finished, "
    "self-contained visual design.\n\n"
    "Reply with ONE ```html code block and nothing else: a complete standalone "
    "document (doctype, <style> in the head, any script inline). No commentary "
    "before or after.\n\n"
    "Design rules:\n"
    "- Make it look designed, not templated: considered type scale, generous "
    "spacing, a deliberate palette, real-feeling content (never lorem ipsum).\n"
    "- Use system fonts or Google Fonts via @import; never reference local files.\n"
    "- Embed any imagery as inline SVG or a data: URI — never hotlink.\n"
    "- Lay out with flexbox/grid; never absolute-position a whole page.\n"
    "- It must render correctly standalone in a browser at 1280px wide.\n"
    "- Drive every colour, face and spacing from CSS custom properties on "
    ":root, so one change retunes the whole document.\n\n"
    "End the document with a tweak sheet — the knobs a person should be able to "
    "turn afterwards — as:\n"
    '<script type="application/json" id="tweaks">[{"name":"accent",'
    '"type":"color","var":"--accent","value":"#1A7F5A",'
    '"options":["#1A7F5A","#1E3A8A","#8B2C2C","#333333"]},'
    '{"name":"sectionStyle","type":"select","var":"--section-style",'
    '"value":"Hairline","options":["Hairline","Ruled","None"]},'
    '{"name":"density","type":"select","var":"--density",'
    '"value":"Standard","options":["Compact","Standard","Roomy"]}]</script>\n'
    "Name the knobs after what they change in this design, give each the "
    "custom property it sets, and make sure the document actually responds to "
    "every value offered."
)
