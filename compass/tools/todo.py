"""todo_write — session-scoped task tracking (TodoWriteTool port)."""

from __future__ import annotations

from typing import AsyncIterator, Literal

from pydantic import BaseModel

from compass.tools.base import Tool, ToolOutput, ToolUseContext, ToolYield


class TodoItem(BaseModel):
    content: str
    status: Literal["pending", "in_progress", "completed"] = "pending"


class TodoWriteInput(BaseModel):
    todos: list[TodoItem]


class TodoWriteTool(Tool):
    name = "todo_write"
    description = (
        "Replace the session todo list. Use for multi-step work so progress "
        "is visible; keep at most one item in_progress."
    )
    input_model = TodoWriteInput

    def is_read_only(self, inp: BaseModel) -> bool:
        return True  # session-state only; no filesystem or network effects

    async def call(self, inp: TodoWriteInput, ctx: ToolUseContext) -> AsyncIterator[ToolYield]:
        ctx.todos.clear()
        ctx.todos.extend(item.model_dump() for item in inp.todos)
        lines = [f"[{t['status']}] {t['content']}" for t in ctx.todos]
        yield ToolOutput("todo list updated:\n" + "\n".join(lines))
