"""Memory — individual, categorized entries the model reads and updates while
you chat (the port of Claude's memory).

Shape, matching Claude's Settings → Memory panel:
  * memory is a set of **individual entries**, each with a `category`, a short
    `summary`, and longer `details`;
  * entries are **scoped**: Home chat shares one scope, and each Code workspace
    ("project") gets its own — so client work never leaks into another project;
  * the model reads them at the start of a turn (injected into the system
    prompt) and writes/updates them mid-conversation via the `memory` tool;
  * the user can view, edit and delete any entry.

Storage follows the same config-or-fallback contract as the rest of Compass:
Azure Cosmos DB when configured, a local JSON file otherwise.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from compass.config import get_settings

logger = logging.getLogger("compass.memory")

# The categories Claude groups entries under in the Memory panel.
CATEGORIES = [
    "Profile",
    "Preferences",
    "Projects",
    "Workflow",
    "Context",
]

GLOBAL_SCOPE = "home"


@dataclass
class MemoryEntry:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    scope: str = GLOBAL_SCOPE  # "home" or a workspace/project id
    category: str = "Context"
    summary: str = ""
    details: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class MemoryStore:
    """Local JSON store — one file, all scopes. Cosmos-backed when configured."""

    def _path(self) -> Path:
        d = get_settings().workspace_root / get_settings().data_dir
        d.mkdir(parents=True, exist_ok=True)
        return d / "memory.json"

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

    async def list(self, scope: str | None = None) -> list[dict]:
        rows = self._read()
        if scope:
            rows = [r for r in rows if r.get("scope") == scope]
        rows.sort(key=lambda r: r.get("updated_at", 0), reverse=True)
        return rows

    async def add(
        self, *, scope: str, category: str, summary: str, details: str = ""
    ) -> dict:
        rows = self._read()
        entry = MemoryEntry(
            scope=scope,
            category=category if category in CATEGORIES else "Context",
            summary=summary.strip(),
            details=details.strip(),
        )
        rows.append(entry.to_dict())
        self._write(rows)
        return entry.to_dict()

    async def update(
        self,
        entry_id: str,
        *,
        summary: str | None = None,
        details: str | None = None,
        category: str | None = None,
    ) -> dict | None:
        rows = self._read()
        for r in rows:
            if r.get("id") == entry_id:
                if summary is not None:
                    r["summary"] = summary.strip()
                if details is not None:
                    r["details"] = details.strip()
                if category is not None and category in CATEGORIES:
                    r["category"] = category
                r["updated_at"] = time.time()
                self._write(rows)
                return r
        return None

    async def delete(self, entry_id: str) -> bool:
        rows = self._read()
        kept = [r for r in rows if r.get("id") != entry_id]
        if len(kept) == len(rows):
            return False
        self._write(kept)
        return True

    async def clear(self, scope: str | None = None) -> int:
        rows = self._read()
        kept = [r for r in rows if scope and r.get("scope") != scope] if scope else []
        removed = len(rows) - len(kept)
        self._write(kept)
        return removed


_store: MemoryStore | None = None


def get_memory_store() -> MemoryStore:
    """Cosmos-backed when configured, else the local JSON store."""
    global _store
    if _store is None:
        cfg = get_settings().storage
        if cfg.backend == "cosmos" and cfg.cosmos_configured:
            from compass.persistence.memory_cosmos import CosmosMemoryStore

            _store = CosmosMemoryStore()
        else:
            _store = MemoryStore()
    return _store


async def memory_prompt(scope: str) -> str:
    """The memory block injected into a turn's system prompt. Entries from the
    given scope plus the global (Home) scope, grouped by category — so the model
    starts every conversation already knowing what it has learned."""
    store = get_memory_store()
    rows = await store.list(scope)
    if scope != GLOBAL_SCOPE:
        rows = rows + await store.list(GLOBAL_SCOPE)
    if not rows:
        return ""
    by_cat: dict[str, list[dict]] = {}
    for r in rows:
        by_cat.setdefault(r.get("category", "Context"), []).append(r)
    lines = ["# Memory", "What you remember about this user (from past chats):"]
    for cat in CATEGORIES:
        items = by_cat.get(cat)
        if not items:
            continue
        lines.append(f"\n## {cat}")
        for r in items:
            detail = f" — {r['details']}" if r.get("details") else ""
            lines.append(f"- {r.get('summary','')}{detail}")
    lines.append(
        "\nUse this naturally; do not recite it. When you learn something "
        "durable about the user or their project, call the `memory` tool to "
        "save or update an entry."
    )
    return "\n".join(lines)
