"""Design projects — the store behind Compass's Design section.

A project is one piece of visual work: a prompt, the template it started
from, the generated design (standalone HTML, so it renders in the same
artifact pipeline the rest of Compass uses), and the design system it
should follow.

Storage follows the same config-or-fallback contract as everything else:
Azure Cosmos DB when configured, a local JSON file otherwise.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from compass.common.config import get_settings
from compass.design.skills import TEMPLATE_PROMPTS

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
_HEAVY = ("html", "turns", "versions", "comments", "pages", "clarify")


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
    design_systems: list[str] = field(default_factory=list)
    # The form the project is waiting on, if it is waiting on one. Kept on
    # the project so reopening it shows the question again, not a blank canvas.
    clarify: dict = field(default_factory=dict)  # every system it follows
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
            | {
                "versions": len(r.get("versions") or []),
                # Enough for the table to draw the right tile: a project with
                # nothing rendered yet gets a placeholder, not a broken image.
                "empty": not (r.get("html") or "").strip(),
                "awaiting": bool((r.get("clarify") or {}).get("fields")),
            }
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

    async def pages_with_html(self, project_id: str) -> list[dict]:
        """Every page including its markup — for exporting the whole project.
        The page being edited keeps its markup on the project until it is
        switched away from, so take that one from there."""
        row = await self.get(project_id)
        if row is None:
            return []
        pages = self._pages(row)
        active = row.get("active_page") or (pages[0]["id"] if pages else "")
        out = []
        for page in pages:
            html = row.get("html", "") if page["id"] == active else page.get("html", "")
            out.append({**page, "html": html or page.get("html", "")})
        return out

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


_store: DesignStore | None = None


def get_design_store() -> DesignStore:
    global _store
    if _store is None:
        _store = DesignStore()
    return _store
