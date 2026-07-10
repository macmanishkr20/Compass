"""file_read / file_write / file_edit — ports of FileRead/FileWrite/FileEdit.

The edit tool enforces the same two invariants as Claude Code:
  * you must have read the file this session before editing it
  * the file must not have changed on disk since that read (staleness)
Both are tracked in ToolUseContext.file_state (the fileStateCache analog).
"""

from __future__ import annotations

from typing import AsyncIterator

from pydantic import BaseModel, Field

from compass.policy.permissions import Behavior, PermissionDecision
from compass.tools.base import Tool, ToolOutput, ToolUseContext, ToolYield
from compass.tools.fs_utils import WorkspaceEscapeError, resolve_in_workspace

MAX_READ_CHARS = 100_000


class FileReadInput(BaseModel):
    path: str = Field(description="File path, absolute or workspace-relative")
    offset: int = Field(default=0, ge=0, description="Line to start from")
    limit: int = Field(default=2000, gt=0, description="Max lines to read")


class FileReadTool(Tool):
    name = "file_read"
    description = "Read a text file from the workspace. Returns numbered lines."
    input_model = FileReadInput

    def is_read_only(self, inp: BaseModel) -> bool:
        return True

    def check_tool_permissions(self, inp, ctx):
        return _path_verdict(inp.path, ctx.effective_root())

    async def call(self, inp: FileReadInput, ctx: ToolUseContext) -> AsyncIterator[ToolYield]:
        path = resolve_in_workspace(inp.path, ctx.effective_root())
        if not path.is_file():
            yield ToolOutput(f"file not found: {inp.path}", is_error=True)
            return
        try:
            text = path.read_text(errors="replace")
        except OSError as err:
            yield ToolOutput(f"could not read {inp.path}: {err}", is_error=True)
            return
        ctx.file_state[str(path)] = path.stat().st_mtime
        lines = text.splitlines()[inp.offset : inp.offset + inp.limit]
        numbered = "\n".join(f"{i + 1 + inp.offset}\t{line}" for i, line in enumerate(lines))
        yield ToolOutput(numbered[:MAX_READ_CHARS] or "(empty file)")


class FileWriteInput(BaseModel):
    path: str
    content: str


class FileWriteTool(Tool):
    name = "file_write"
    description = "Create or overwrite a file in the workspace."
    input_model = FileWriteInput

    def check_tool_permissions(self, inp, ctx):
        return _path_verdict(inp.path, ctx.effective_root())

    async def call(self, inp: FileWriteInput, ctx: ToolUseContext) -> AsyncIterator[ToolYield]:
        path = resolve_in_workspace(inp.path, ctx.effective_root())
        known_mtime = ctx.file_state.get(str(path))
        if path.exists() and known_mtime is None:
            yield ToolOutput(
                f"{inp.path} exists but has not been read this session — "
                "read it before overwriting.",
                is_error=True,
            )
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(inp.content)
        ctx.file_state[str(path)] = path.stat().st_mtime
        yield ToolOutput(f"wrote {len(inp.content)} chars to {inp.path}")


class FileEditInput(BaseModel):
    path: str
    old_string: str
    new_string: str
    replace_all: bool = False


class FileEditTool(Tool):
    name = "file_edit"
    description = (
        "Replace an exact string in a file. old_string must match exactly once "
        "unless replace_all is true. Read the file first."
    )
    input_model = FileEditInput

    def check_tool_permissions(self, inp, ctx):
        return _path_verdict(inp.path, ctx.effective_root())

    async def call(self, inp: FileEditInput, ctx: ToolUseContext) -> AsyncIterator[ToolYield]:
        path = resolve_in_workspace(inp.path, ctx.effective_root())
        key = str(path)
        if key not in ctx.file_state:
            yield ToolOutput(
                f"{inp.path} has not been read this session — read before editing.",
                is_error=True,
            )
            return
        if not path.is_file():
            yield ToolOutput(f"file not found: {inp.path}", is_error=True)
            return
        if path.stat().st_mtime > ctx.file_state[key]:
            yield ToolOutput(
                f"{inp.path} changed on disk since it was read — re-read it.",
                is_error=True,
            )
            return
        text = path.read_text()
        count = text.count(inp.old_string)
        if count == 0:
            yield ToolOutput("old_string not found in file", is_error=True)
            return
        if count > 1 and not inp.replace_all:
            yield ToolOutput(
                f"old_string matches {count} times; provide more context or "
                "set replace_all",
                is_error=True,
            )
            return
        replaced = (
            text.replace(inp.old_string, inp.new_string)
            if inp.replace_all
            else text.replace(inp.old_string, inp.new_string, 1)
        )
        path.write_text(replaced)
        ctx.file_state[key] = path.stat().st_mtime
        yield ToolOutput(f"edited {inp.path} ({count if inp.replace_all else 1} replacement)")


def _path_verdict(raw: str, root=None) -> PermissionDecision | None:
    try:
        resolve_in_workspace(raw, root)
        return None
    except WorkspaceEscapeError as err:
        return PermissionDecision(Behavior.DENY, str(err))
