"""Which files a read-only tool can safely open, and how much of one.

Pointed at its own repository a tool can assume a lot. Pointed at somebody
else's it can assume nothing: a backend repository carries checked-in model
weights, database dumps, vendored archives, images, and a `data/` directory
that is source rather than scratch. The search tools were written against the
first case and inherited its assumptions, and both of the ways that went wrong
are the kind a scan cannot report, because one is silent and the other takes
the process with it.

So the rule here is about the nature of a file, never its name. A file is
skipped because it is enormous or because it is not text — facts that hold in
any repository — and the caller is told what was skipped and why, so a gap in
a result is visible as a gap rather than read as an absence.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

#: The largest file a tool will read whole. Chosen against what a read costs
#: rather than what a disk holds: `read_text` materialises the file and then
#: `splitlines` materialises every line again, which measured at ~3.2x the
#: file size in resident memory — 115MB of text cost 374MB. A few of those
#: concurrently is an out-of-memory kill, and a killed process is the one
#: failure an agent cannot work around, because there is nothing left to
#: report it. Anything larger is read head-first instead of whole.
MAX_FILE_BYTES = 8 * 1024 * 1024

#: How much of a file to sniff for binary content. A NUL byte in the first few
#: KB is the same test `grep` and `git` use, and it is right far more often
#: than any extension list, which cannot know about the next format.
SNIFF_BYTES = 8192


def size_of(path: Path) -> int | None:
    """Size in bytes, or None if the file went away or cannot be stat'd.

    Returns rather than raises: every caller is walking a tree that something
    else may be writing to, and a file deleted between listing it and reading
    it is an ordinary event in a repository with a build running.
    """
    try:
        return path.stat().st_size
    except OSError:
        return None


def looks_binary(path: Path) -> bool:
    """Whether the first few KB contain a NUL, i.e. this is not text.

    Read as bytes and sniffed rather than decoded: decoding a 500MB binary
    with `errors="replace"` succeeds, which is the problem — it yields
    megabytes of replacement characters that cost real tokens and say
    nothing.
    """
    try:
        with path.open("rb") as fh:
            return b"\0" in fh.read(SNIFF_BYTES)
    except OSError:
        return True


def read_text_capped(path: Path, cap: int = MAX_FILE_BYTES) -> tuple[str, str]:
    """Up to `cap` bytes of `path` as text, plus a note about what was left.

    The note is empty when the whole file was read. Nothing raises: a file
    that cannot be read comes back as empty text and a note saying why, so a
    caller can carry on through the rest of the tree.
    """
    size = size_of(path)
    if size is None:
        return "", "could not be read"
    if looks_binary(path):
        return "", f"binary file, {_human(size)}"
    try:
        with path.open("rb") as fh:
            raw = fh.read(cap)
    except OSError as err:
        return "", f"could not be read: {err}"
    text = raw.decode("utf-8", errors="replace")
    if size > cap:
        return text, f"read the first {_human(cap)} of {_human(size)}"
    return text, ""


def _human(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f}MB"
    if n >= 1024:
        return f"{n / 1024:.0f}KB"
    return f"{n}B"


#: Ignored-directory lookups are cached per root for this long. A search is
#: often several calls in a row against a tree nobody is restructuring, and
#: asking git once a minute is the difference between a cheap correction and
#: a subprocess on every glob.
_IGNORE_TTL_SECONDS = 60.0
_ignore_cache: dict[str, tuple[float, frozenset[str]]] = {}


async def ignored_dirs(root: Path) -> frozenset[str]:
    """Top-level directory names this repository tells git to ignore.

    The generic skip list can only name things that are never source
    anywhere — `.git`, `node_modules`. What else is noise is a property of
    the repository, and the repository already states it, in the file it
    keeps for exactly that purpose. Asking it is how `data/` gets skipped in
    Compass's own tree, where it holds runtime state and is ignored, while
    staying visible in a backend that keeps schemas there and commits them.

    Best-effort by construction: no git, no repository, a slow or broken
    invocation, all return nothing and leave the search to the generic list.
    A search that works slightly harder is a better failure than one that
    refuses to run.
    """
    key = str(root)
    now = time.monotonic()
    hit = _ignore_cache.get(key)
    if hit and now - hit[0] < _IGNORE_TTL_SECONDS:
        return hit[1]
    names: set[str] = set()
    # Spawned through asyncio rather than `subprocess.run`: this is called
    # from inside a tool, on the event loop, and a blocking wait here stalls
    # every other request the server is serving — including the stream the
    # person is watching. git is fast, but "fast" on an unfamiliar repository
    # is not a promise worth making on a shared loop.
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(root), "ls-files", "--others", "--ignored",
            "--exclude-standard", "--directory",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode == 0:
            for line in out.decode(errors="replace").splitlines():
                line = line.strip().rstrip("/")
                # Only whole top-level directories. A nested or partial match
                # would need real gitignore semantics to be right, and being
                # half-right about what to hide is how files disappear.
                if line and "/" not in line:
                    names.add(line)
    except (OSError, asyncio.TimeoutError, ValueError):
        # A timeout leaves git running; it is read-only and short-lived, but
        # it should not outlive the question it was asked.
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except OSError:
                pass
    frozen = frozenset(names)
    _ignore_cache[key] = (now, frozen)
    return frozen
