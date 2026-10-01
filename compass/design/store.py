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
from compass.common.persistence.catalog import Collection
from compass.design import markup, version_html
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
    #: Who this project belongs to. Empty is legacy and stays visible; see
    #: `compass.common.ownership`.
    owner: str = ""
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


#: Design projects. Their own container rather than the shared catalog: a
#: project carries a rendered design and its pages, which is kilobytes where
#: the other collections are bytes, and it is read one project at a time.
#: Partitioned by the project id — `projectId` in Cosmos, `id` on the
#: document, which is what `partition_source` is for.
_projects = Collection(
    "design", "design.json", shape="list",
    container="designs", partition_field="projectId", partition_source="id",
)


class DesignStore:
    """Design projects, local or in Cosmos.

    Neither the version snapshots nor the markup live in the document — see
    version_html and markup. The snapshots were 21.8MB of a 24.8MB store here,
    and five projects of thirty-seven were already past Cosmos's 2MB item
    limit because of them; the markup was 96% of what remained after that.

    What a document holds now is the project: its name, its pages' names and
    ids, how long each page is, the conversation, the prompt. Everything a
    list or a page switcher asks for, and nothing a canvas does.

    One rule throughout: anything that is going to be saved must be read
    filled first. `_pages` invents a page from `html` when a project has none,
    so an unfilled row saved back would write that page out empty.
    """

    async def _read(self) -> list[dict]:
        """Every project, without its markup. Cheap, and not enough to render."""
        return await _projects.all()

    async def _save(self, row: dict) -> None:
        """Store one project, with its history and markup filed away first."""
        row = dict(row)
        row["versions"] = await version_html.spill(
            str(row.get("id", "")), list(row.get("versions") or [])
        )
        await _projects.put(await markup.spill(row))

    async def _filled(self, project_id: str) -> dict | None:
        """One project with its markup, ready to render or to change.

        Fetched by id rather than by reading all of them and picking: the
        container is partitioned by project id, so this is a point read.
        Filling is only ever for the one project — doing it for all of them to
        answer a question about one would fetch every design's markup.
        """
        row = await _projects.get(project_id)
        return None if row is None else await markup.fill(row)

    async def list(self) -> list[dict]:
        rows = await self._read()
        rows.sort(key=lambda r: r.get("viewed_at") or r.get("updated_at", 0), reverse=True)
        return [
            {k: v for k, v in r.items() if k not in _HEAVY}
            | {
                "versions": len(r.get("versions") or []),
                # Enough for the table to draw the right tile: a project with
                # nothing rendered yet gets a placeholder, not a broken image.
                # Asked of the recorded length rather than the markup, which is
                # the point of recording it — the list fetches nothing.
                "empty": markup.length(r) == 0,
                "awaiting": bool((r.get("clarify") or {}).get("fields")),
            }
            for r in rows
        ]

    async def get(self, project_id: str) -> dict | None:
        return await self._filled(project_id)

    async def touch(self, project_id: str) -> dict | None:
        """Record that the project was opened — the table sorts on this, the way
        claude.ai's "Last viewed" column does."""
        row = await self._filled(project_id)
        if row is None:
            return None
        row["viewed_at"] = time.time()
        await self._save(row)
        return row

    @staticmethod
    def _pages(row: dict) -> list[dict]:
        """A project's pages, inventing the first from the design it already
        has — every project had exactly one page before this existed.

        The invented page *is* the project's markup, so it carries however the
        project is holding it: inline, or a reference plus a length. Copying
        only `html` would report a filed-away design as a blank page, which is
        what the page switcher would then show.
        """
        pages = list(row.get("pages") or [])
        if not pages:
            held = {"html": row.get("html", "")} if row.get("html") is not None else {}
            if row.get(markup.SPILLED):
                held = {markup.SPILLED: row[markup.SPILLED]}
            pages = [
                {
                    "id": "p1",
                    "name": f"{row.get('name', 'Design')}.html",
                    markup.LENGTH: markup.length(row),
                    "updated_at": row.get("updated_at", time.time()),
                    **held,
                }
            ]
        return pages

    async def pages(self, project_id: str) -> list[dict]:
        """The page switcher's list: names and sizes, no markup.

        Deliberately the unfilled row — this is the one page question that can
        be answered without the pages themselves, because the length of each
        was written down when it was filed away.
        """
        row = await _projects.get(project_id)
        if row is None:
            return []
        return [
            {k: v for k, v in p.items() if k not in ("html", markup.SPILLED)}
            | {"chars": markup.length(p)}
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
        r = await self._filled(project_id)
        if r is None:
            return None
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
        await self._save(r)
        return r

    async def open_page(self, project_id: str, page_id: str) -> dict | None:
        """Switch pages: the outgoing one keeps what is on the canvas."""
        r = await self._filled(project_id)
        if r is None:
            return None
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
        await self._save(r)
        return r

    async def delete_page(self, project_id: str, page_id: str) -> dict | None:
        """Drop a page. The last one stays — a project without a page has
        nothing to show."""
        r = await self._filled(project_id)
        if r is None:
            return None
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
        await self._save(r)
        # The dropped page's markup goes with it, or it would sit in blob
        # storage forever with nothing able to reach it.
        await markup.drop(project_id, page_id)
        return r

    async def save_html(
        self, project_id: str, html: str, *, label: str = "Edited"
    ) -> dict | None:
        """Write a new design, keeping the outgoing one as a version. Every path
        that changes the html goes through here, so history is never partial.

        Filled first, and that matters here more than anywhere: the outgoing
        markup becomes the version, so reading it unfilled would file an empty
        snapshot and the undo step would restore a blank page.
        """
        r = await self._filled(project_id)
        if r is not None:
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
                # What falls off the end takes its snapshot with it. Trimmed
                # versions used to leave their markup in blob storage with
                # nothing able to reach it — one orphan per edit, forever.
                for stale in versions[MAX_VERSIONS:]:
                    await version_html.drop(stale)
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
            await self._save(r)
            return r
        return None

    async def duplicate(self, project_id: str) -> dict | None:
        # Filled, so the copy carries the markup itself and gets blobs of its
        # own under its new id. An unfilled source would have handed the copy
        # the *original's* references, and then editing either would have
        # changed both.
        source = await self._filled(project_id)
        if source is None:
            return None
        copy = dict(source)
        copy["id"] = uuid.uuid4().hex[:12]
        copy["name"] = f"{source.get('name', 'Untitled')} copy"
        copy["versions"] = []  # a copy starts its own history
        copy["starred"] = False
        copy["created_at"] = copy["updated_at"] = copy["viewed_at"] = time.time()
        await self._save(copy)
        return copy

    async def create(
        self,
        *,
        name: str,
        template: str,
        prompt: str,
        design_system: str = "",
        design_systems: list[str] | None = None,
        owner: str = "",
    ) -> dict:
        # Nothing is read here. There used to be a `rows = await self._read()`
        # on this line whose result was never used — every new project paid for
        # a full read of every existing one.
        systems = list(design_systems or ([design_system] if design_system else []))
        p = DesignProject(
            name=name or "Untitled",
            template=template if template in TEMPLATE_PROMPTS else "blank",
            prompt=prompt,
            design_system=systems[0] if systems else "",
            design_systems=systems,
            owner=owner,
        ).to_dict()
        await self._save(p)
        return p

    async def update(self, project_id: str, **fields) -> dict | None:
        # Filled even when the caller is only renaming: a save rewrites the
        # whole document, so a row that arrived without its markup would be
        # written back without it. `PATCH` can also set `html` directly, which
        # is why this cannot derive the markup from the pages afterwards.
        r = await self._filled(project_id)
        if r is None:
            return None
        for k, v in fields.items():
            if v is not None:
                r[k] = v
        r["updated_at"] = time.time()
        await self._save(r)
        return r

    async def delete(self, project_id: str) -> bool:
        row = await _projects.get(project_id)
        if row is None:
            return False
        gone = await _projects.remove(project_id)
        if gone:
            # The markup and the history go with the project. Together they are
            # nearly all of its bytes, so a delete that left them behind would
            # free almost nothing.
            await markup.discard(project_id)
            for version in row.get("versions") or []:
                await version_html.drop(version)
        return gone


_store: DesignStore | None = None


def get_design_store() -> DesignStore:
    global _store
    if _store is None:
        _store = DesignStore()
    return _store
