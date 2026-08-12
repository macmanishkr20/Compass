"""memory — save, update or remove what Claude remembers about the user.

The write side of Compass's memory (services/memory.py owns the store, and the
read side is injected into the system prompt each turn). The model calls this
mid-conversation the moment it learns something durable — a preference, a fact
about the project, how the user likes to work — exactly like Claude updating a
memory entry while you chat.
"""

from __future__ import annotations

from typing import AsyncIterator, Literal

from pydantic import BaseModel, Field

from compass.services.memory import CATEGORIES, GLOBAL_SCOPE, get_memory_store
from compass.tools.base import Tool, ToolOutput, ToolUseContext, ToolYield


class MemoryInput(BaseModel):
    action: Literal["save", "update", "delete"] = Field(
        description="save a new entry, update an existing one, or delete one."
    )
    summary: str = Field(
        default="",
        description=(
            "One short line stating the durable fact, e.g. 'Prefers TypeScript "
            "over JavaScript'. Required for save/update."
        ),
    )
    details: str = Field(
        default="", description="Optional extra context for the entry."
    )
    category: str = Field(
        default="Context",
        description=f"One of: {', '.join(CATEGORIES)}.",
    )
    entry_id: str = Field(
        default="", description="The entry to update or delete."
    )


class MemoryTool(Tool):
    name = "memory"
    description = (
        "Remember something durable about the user or this project, or update/"
        "remove what you already remember. Use it the moment you learn a lasting "
        "preference, fact, or working style — not for one-off details of the "
        "current task. Entries are shown to the user in Settings → Memory."
    )
    input_model = MemoryInput

    def is_read_only(self, inp: MemoryInput) -> bool:
        # Writing memory is not a workspace mutation; it never needs approval.
        return True

    async def call(self, inp: MemoryInput, ctx: ToolUseContext) -> AsyncIterator[ToolYield]:
        store = get_memory_store()
        # Each workspace ("project") keeps its own memory; Home shares one.
        scope = getattr(ctx, "workspace_id", None) or GLOBAL_SCOPE

        if inp.action == "delete":
            if not inp.entry_id:
                yield ToolOutput("memory delete needs an entry_id.", is_error=True)
                return
            ok = await store.delete(inp.entry_id)
            yield ToolOutput("Forgot that." if ok else "No such memory entry.")
            return

        if not inp.summary.strip():
            yield ToolOutput("memory needs a `summary`.", is_error=True)
            return

        if inp.action == "update":
            if not inp.entry_id:
                yield ToolOutput("memory update needs an entry_id.", is_error=True)
                return
            row = await store.update(
                inp.entry_id,
                summary=inp.summary,
                details=inp.details or None,
                category=inp.category,
            )
            if row is None:
                yield ToolOutput("No such memory entry.", is_error=True)
                return
            yield ToolOutput(f"Updated memory: {row['summary']}")
            return

        row = await store.add(
            scope=scope,
            category=inp.category,
            summary=inp.summary,
            details=inp.details,
        )
        yield ToolOutput(f"Saved to memory [{row['category']}]: {row['summary']}")
