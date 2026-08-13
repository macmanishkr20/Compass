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
    {"id": "blank", "name": "Blank", "hint": "Start from nothing"},
    {"id": "mobile", "name": "Mobile app design", "hint": "Screens for a phone app"},
    {"id": "slides", "name": "Slides", "hint": "A deck to present"},
    {"id": "document", "name": "Document", "hint": "A formatted written page"},
    {"id": "wireframe", "name": "Wireframe", "hint": "Low-fidelity structure"},
    {"id": "animation", "name": "Animation", "hint": "Something that moves"},
    {"id": "mockups", "name": "UI mockups", "hint": "High-fidelity screens"},
    {"id": "resume", "name": "Résumé", "hint": "A one-page CV"},
    {"id": "object3d", "name": "3D object", "hint": "A rendered object"},
    {"id": "research", "name": "Research", "hint": "Findings, written up"},
    {"id": "email", "name": "HTML email", "hint": "An email that renders"},
    {"id": "colortype", "name": "Color + type pairing", "hint": "A palette and typefaces"},
]

# Per-template guidance appended to the generation prompt.
TEMPLATE_PROMPTS = {
    "blank": "Design whatever the request describes.",
    "mobile": "Design mobile app screens at 390x844, shown side by side in a row.",
    "slides": (
        "Design a slide deck: each slide a 16:9 section with class=\"slide\", "
        "stacked vertically. The class is what the PowerPoint and PDF exports "
        "cut on, so every slide needs it."
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


@dataclass
class DesignProject:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = "Untitled"
    template: str = "blank"
    prompt: str = ""
    html: str = ""              # the generated design (standalone HTML)
    turns: list[dict] = field(default_factory=list)  # the design conversation
    design_system: str = ""     # design-system id, "" = None
    starred: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    def card(self) -> dict:
        """Row shape for the projects table (no html — it can be large)."""
        d = self.to_dict()
        d.pop("html", None)
        d.pop("turns", None)
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
        rows.sort(key=lambda r: r.get("updated_at", 0), reverse=True)
        return [{k: v for k, v in r.items() if k not in ("html", "turns")} for r in rows]

    async def get(self, project_id: str) -> dict | None:
        return next((r for r in self._read() if r.get("id") == project_id), None)

    async def create(
        self, *, name: str, template: str, prompt: str, design_system: str = ""
    ) -> dict:
        rows = self._read()
        p = DesignProject(
            name=name or "Untitled",
            template=template if template in TEMPLATE_PROMPTS else "blank",
            prompt=prompt,
            design_system=design_system,
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
    source: str = "pasted"  # pasted | file | url
    notes: str = ""         # the distilled system, in prose
    css: str = ""           # verbatim tokens, when the source had them
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


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
        rows = self._read()
        rows.sort(key=lambda r: r.get("updated_at", 0), reverse=True)
        return rows

    async def get(self, system_id: str) -> dict | None:
        return next((r for r in self._read() if r.get("id") == system_id), None)

    async def create(self, *, name: str, source: str, notes: str, css: str = "") -> dict:
        rows = self._read()
        s = DesignSystem(
            name=name or "Untitled system", source=source, notes=notes, css=css
        ).to_dict()
        rows.append(s)
        self._write(rows)
        return s

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
    "You are reading a style guide, stylesheet, or brand page. Distil it into a "
    "design system another designer could follow.\n\n"
    "Reply as plain text, no preamble, in this shape:\n"
    "Name: <a short name for the system>\n"
    "Palette: <the colours, with hex values and what each is for>\n"
    "Type: <typefaces, weights, and the size scale>\n"
    "Spacing & shape: <spacing rhythm, radii, borders, shadows>\n"
    "Components: <how buttons, cards, inputs, and tables look>\n"
    "Voice: <the tone the writing takes>\n\n"
    "Only state what the source actually shows. Say nothing about what is absent."
)


def system_prompt_block(system: dict | None) -> str:
    """The instruction appended to a generation when a system is attached."""
    if not system:
        return ""
    parts = [
        "Follow this design system exactly — it outranks your own taste:",
        system.get("notes", "").strip(),
    ]
    css = (system.get("css") or "").strip()
    if css:
        parts.append("Use these tokens verbatim:\n\n```css\n" + css[:6_000] + "\n```")
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
    "- It must render correctly standalone in a browser at 1280px wide."
)
