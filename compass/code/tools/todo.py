"""todo_write — session-scoped task tracking (TodoWriteTool port)."""

from __future__ import annotations

from typing import AsyncIterator, Literal

from pydantic import BaseModel

from compass.common.tools.base import Tool, ToolOutput, ToolUseContext, ToolYield


class TodoItem(BaseModel):
    content: str
    status: Literal["pending", "in_progress", "completed"] = "pending"


class TodoWriteInput(BaseModel):
    todos: list[TodoItem]


class TodoWriteTool(Tool):
    name = "todo_write"
    description = (
        "Replace the session's todo list with a new one. Use this at the start of "
        "work that has several distinct steps, and again after each step, so the "
        "person watching can see what is planned and what is done. It writes the "
        "whole list every time, so include the items already finished rather than "
        "only the new ones. Keep at most one item in_progress: two in progress "
        "means the list no longer says what is actually being worked on. Skip it "
        "for a single-step request, where the list is noise."
    )
    input_model = TodoWriteInput

    def is_read_only(self, inp: BaseModel) -> bool:
        return True  # session-state only; no filesystem or network effects

    async def call(self, inp: TodoWriteInput, ctx: ToolUseContext) -> AsyncIterator[ToolYield]:
        ctx.todos.clear()
        ctx.todos.extend(item.model_dump() for item in inp.todos)
        lines = [f"[{t['status']}] {t['content']}" for t in ctx.todos]
        yield ToolOutput("todo list updated:\n" + "\n".join(lines))
