"""glob / grep — read-only, concurrency-safe search tools."""

from __future__ import annotations

import asyncio
import re
from typing import AsyncIterator

from pydantic import BaseModel, Field

from compass.common.config import get_settings
from compass.common.tools.base import Tool, ToolOutput, ToolUseContext, ToolYield
from compass.code.tools.readable import (
    MAX_FILE_BYTES, ignored_dirs, looks_binary, size_of,
)

#: Directories that hold no source in any repository: a version-control
#: store, and the three places package managers and interpreters put things
#: nobody wrote.
#:
#: `data` used to be on this list, and it was Compass describing itself.
#: Compass keeps its own runtime state in `data/`, so skipping it made its own
#: repository pleasant to search — and made every other repository lie. A
#: backend keeps schemas, fixtures, migrations and seeds in `data/`, and this
#: rule silently dropped all of them: no error, no note, just a search that
#: answered "no matches" about files that were sitting right there. The size
#: and binary guards below are what that entry was really reaching for, and
#: they work on what a file *is* rather than on what a directory is called.
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}
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
        skip = SKIP_DIRS | await ignored_dirs(root)

        # Walked off the event loop. `Path.glob` is synchronous and touches
        # every directory it is pointed at; measured against this repository
        # it held the loop for a second, and an unfamiliar monorepo is not
        # bounded by anything. Every other request — including the stream the
        # person is watching this search from — waits behind it.
        def walk() -> list[str]:
            matches = [
                p
                for p in root.glob(inp.pattern)
                if p.is_file() and not (set(p.parts) & skip)
            ]

            # `stat` through the sort key, not in it: a file removed between
            # the listing and the sort raises, and it would take the whole
            # search with it — a build running alongside is enough.
            def mtime(p) -> float:
                try:
                    return p.stat().st_mtime
                except OSError:
                    return 0.0

            matches.sort(key=mtime, reverse=True)
            out = [str(p.relative_to(root)) for p in matches[:MAX_RESULTS]]
            if len(matches) > MAX_RESULTS:
                out.append(f"... {len(matches) - MAX_RESULTS} more")
            return out

        shown = await asyncio.to_thread(walk)
        yield ToolOutput("\n".join(shown) if shown else "no files matched")


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
        skip = SKIP_DIRS | await ignored_dirs(root)

        # Off the loop, for the same reason as glob, and more so: this one
        # walks the tree *and* reads every text file it finds.
        def search() -> tuple[list[str], int, int]:
            results: list[str] = []
            skipped_big = 0
            skipped_binary = 0
            for path in root.glob(inp.glob):
                if not path.is_file() or (set(path.parts) & skip):
                    continue
                size = size_of(path)
                if size is None:
                    continue
                # Read whole files only while that is cheap. A checked-in dump or
                # model weight costs several times its own size in memory here,
                # and a regex over it cannot match anything a person asked for.
                if size > MAX_FILE_BYTES:
                    skipped_big += 1
                    continue
                if looks_binary(path):
                    skipped_binary += 1
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
            return results, skipped_big, skipped_binary

        results, skipped_big, skipped_binary = await asyncio.to_thread(search)

        # Say what was not looked at. A search that quietly skipped half a
        # repository and answered "no matches" is the failure this whole
        # module just had, and the cure is to make the gap visible.
        notes = []
        if skipped_big:
            notes.append(f"{skipped_big} file(s) larger than "
                         f"{MAX_FILE_BYTES // (1024 * 1024)}MB")
        if skipped_binary:
            notes.append(f"{skipped_binary} binary file(s)")
        note = f"\n\n(skipped {', '.join(notes)})" if notes else ""
        yield ToolOutput(("\n".join(results) if results else "no matches") + note)
