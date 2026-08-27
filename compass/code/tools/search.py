"""glob / grep — read-only, concurrency-safe search tools."""

from __future__ import annotations

import re
from typing import AsyncIterator

from pydantic import BaseModel, Field

from compass.common.config import get_settings
from compass.common.tools.base import Tool, ToolOutput, ToolUseContext, ToolYield

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "data"}
MAX_RESULTS = 200


class GlobInput(BaseModel):
    pattern: str = Field(description="Glob pattern, e.g. '**/*.py'")


class GlobTool(Tool):
    name = "glob"
    description = (
        "Find files by a glob pattern on their path, most recently modified "
        "first. Use this to locate files by name, extension or directory when you "
        "do not know exactly where they are — 'src/**/*.ts', '**/test_*.py'. Use "
        "grep instead when you are searching for what is inside files rather than "
        "what they are called. Returns paths only, never contents, and skips the "
        "directories a repository does not track."
    )
    input_model = GlobInput

    def is_read_only(self, inp: BaseModel) -> bool:
        return True

    async def call(self, inp: GlobInput, ctx: ToolUseContext) -> AsyncIterator[ToolYield]:
        root = ctx.effective_root()
        matches = [
            p
            for p in root.glob(inp.pattern)
            if p.is_file() and not (set(p.parts) & SKIP_DIRS)
        ]
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        shown = [str(p.relative_to(root)) for p in matches[:MAX_RESULTS]]
        suffix = f"\n... {len(matches) - MAX_RESULTS} more" if len(matches) > MAX_RESULTS else ""
        yield ToolOutput("\n".join(shown) + suffix if shown else "no files matched")


class GrepInput(BaseModel):
    pattern: str = Field(description="Regular expression to search for")
    glob: str = Field(default="**/*", description="Restrict to files matching this glob")
    case_insensitive: bool = False


class GrepTool(Tool):
    name = "grep"
    description = (
        "Search the contents of files with a regular expression and return each "
        "match as path:line:text. Use this to find where something is defined, "
        "used or mentioned when you know roughly what the text looks like but not "
        "which file it is in. Use glob instead when you are looking for files by "
        "name, and file_read when you already know the file and want the whole "
        "thing. Results are capped, so a very common pattern returns the first "
        "matches rather than all of them — narrow the pattern if the answer "
        "depends on seeing every one."
    )
    input_model = GrepInput

    def is_read_only(self, inp: BaseModel) -> bool:
        return True

    async def call(self, inp: GrepInput, ctx: ToolUseContext) -> AsyncIterator[ToolYield]:
        root = ctx.effective_root()
        try:
            rx = re.compile(inp.pattern, re.IGNORECASE if inp.case_insensitive else 0)
        except re.error as err:
            yield ToolOutput(f"invalid regex: {err}", is_error=True)
            return
        results: list[str] = []
        for path in root.glob(inp.glob):
            if not path.is_file() or (set(path.parts) & SKIP_DIRS):
                continue
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    rel = path.relative_to(root)
                    results.append(f"{rel}:{lineno}:{line.strip()[:200]}")
                    if len(results) >= MAX_RESULTS:
                        break
            if len(results) >= MAX_RESULTS:
                break
        yield ToolOutput("\n".join(results) if results else "no matches")
