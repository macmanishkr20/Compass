"""Skills: expertise kept on disk, read only when it is wanted.

A skill is a directory with a `SKILL.md` in it. The file's frontmatter says
what the skill is for; the body says how to do it, and may point at other
files beside it — a reference, a checklist, a script.

The point is what does *not* get loaded. Every skill's name and description go
into the system prompt, which costs perhaps a hundred tokens each, and nothing
else does. The body is read by the agent, with the file tools it already has,
on the turn it decides the skill is relevant. A skill can carry a thousand
lines of reference material and cost nothing until the day it is needed.

That is why this is worth having rather than one enormous COMPASS.md. Project
memory is loaded on every single turn whether or not it applies; a skill about
migrating the database is free on the days nobody is migrating the database.

Compass needed almost nothing for this. The Code agent already reads files,
globs and greps, which is the whole of levels two and three. The only missing
piece was level one: telling the model which skills exist.

Anthropic's hosted Skills API — upload a bundle, reference it by `skill_id` in
`container.skills` — has no Azure equivalent and is not what this is. This is
the filesystem model, which transfers exactly.

A word on trust. A skill is instructions, and instructions from a repository
someone else wrote are no more trustworthy than any other file in it. The
frontmatter is treated as data: names and descriptions that carry markup are
rejected rather than passed through into the system prompt, both counts are
capped, and the block says plainly that these are descriptions of local files.
None of that makes an unread skill safe to run — read a skill before trusting
it, the same as any other code you did not write.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("compass.skills")

#: Where skills live, relative to the workspace and to the home directory.
#: Two places for the same reason Claude Code has two: some skills belong to a
#: project and travel with it, others belong to the person.
PROJECT_DIR = ".compass/skills"
PERSONAL_DIR = ".compass/skills"

#: The frontmatter rules, as the skill-authoring guidance states them.
NAME_MAX = 64
DESCRIPTION_MAX = 1024
_NAME_OK = re.compile(r"^[a-z0-9-]+$")
#: Reserved because a skill claiming to be from the vendor is exactly the
#: shape a misleading one would take.
_RESERVED = ("anthropic", "claude")
#: Anything that could be read as markup. The guidance forbids it in both
#: fields; here it is also the mitigation that matters, since these strings go
#: into the system prompt and a description full of tags is an injection
#: attempt wearing a description's clothes.
_MARKUP = re.compile(r"<[^>]*>")

#: How many skills to describe. Past this the descriptions stop being a menu
#: and start being a wall, and the model gets worse at picking from them — the
#: same recall problem that makes the tool shelf necessary.
MAX_SKILLS = 24


@dataclass(frozen=True)
class Skill:
    """One skill's metadata, and where the rest of it lives."""

    name: str
    description: str
    #: Absolute path to SKILL.md, so the agent can read it when it wants it.
    path: Path
    #: "project" or "personal", so a reader can tell which is which.
    origin: str


def _frontmatter(text: str) -> dict[str, str] | None:
    """The YAML block at the top of a SKILL.md, or None when there isn't one."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    block = text[3:end]
    try:
        import yaml

        parsed = yaml.safe_load(block)
    except Exception:  # noqa: BLE001 — a malformed skill is skipped, not fatal
        return None
    if not isinstance(parsed, dict):
        return None
    return {str(k): str(v) for k, v in parsed.items() if v is not None}


def _valid(name: str, description: str) -> str:
    """Why this frontmatter is unusable, or "" when it is fine."""
    if not name:
        return "no name"
    if len(name) > NAME_MAX:
        return f"name is longer than {NAME_MAX} characters"
    if not _NAME_OK.match(name):
        return "name may use only lowercase letters, numbers and hyphens"
    if any(word in name for word in _RESERVED):
        return f"name uses a reserved word ({', '.join(_RESERVED)})"
    if not description.strip():
        return "no description"
    if len(description) > DESCRIPTION_MAX:
        return f"description is longer than {DESCRIPTION_MAX} characters"
    if _MARKUP.search(name) or _MARKUP.search(description):
        return "name or description contains markup"
    return ""


def _read(skill_md: Path, origin: str) -> Skill | None:
    try:
        # Only the head is needed: the frontmatter is at the top, and the body
        # is the agent's to read later.
        text = skill_md.read_text(encoding="utf-8", errors="replace")[:8_000]
    except OSError:
        return None
    meta = _frontmatter(text)
    if meta is None:
        return None
    name = (meta.get("name") or "").strip()
    description = (meta.get("description") or "").strip()
    if reason := _valid(name, description):
        logger.warning("skipping skill at %s: %s", skill_md, reason)
        return None
    return Skill(name=name, description=description,
                 path=skill_md.resolve(), origin=origin)


def _scan(root: Path, origin: str) -> list[Skill]:
    """Every valid skill directly under `root`, one directory deep.

    Deliberately not recursive. A skill is a directory with a SKILL.md in it,
    not any SKILL.md anywhere beneath — walking the whole tree would pick up
    fixtures, vendored copies and whatever a dependency happens to ship.
    """
    if not root.is_dir():
        return []
    found: list[Skill] = []
    try:
        entries = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return []
    for entry in entries:
        candidate = entry / "SKILL.md"
        if not candidate.is_file():
            continue
        # A symlink out of the skills directory is not a skill of this
        # project's; refusing it keeps "what is installed" honest.
        try:
            if not candidate.resolve().is_relative_to(root.resolve()):
                logger.warning("skipping skill at %s: it points outside %s",
                               candidate, root)
                continue
        except (OSError, ValueError):
            continue
        if skill := _read(candidate, origin):
            found.append(skill)
    return found


def discover(workspace_root: Path | None = None,
             home: Path | None = None) -> list[Skill]:
    """Every installed skill: the project's first, then the person's.

    Project first because a skill checked into the repository is the one the
    team agreed on, and a personal skill of the same name should not quietly
    displace it. Duplicates are dropped, keeping the first seen.
    """
    roots: list[tuple[Path, str]] = []
    if workspace_root:
        roots.append((Path(workspace_root) / PROJECT_DIR, "project"))
    roots.append(((home or Path.home()) / PERSONAL_DIR, "personal"))

    seen: set[str] = set()
    skills: list[Skill] = []
    for root, origin in roots:
        for skill in _scan(root, origin):
            if skill.name in seen:
                continue
            seen.add(skill.name)
            skills.append(skill)
    return skills[:MAX_SKILLS]


def describe(skills: list[Skill]) -> str:
    """The block for the system prompt, or "" when nothing is installed.

    Empty is the common case and the important one: with no skills the prompt
    is byte-for-byte what it was before any of this existed.
    """
    if not skills:
        return ""
    lines = [
        "# Skills",
        "Instructions kept on disk for particular kinds of work. Only the "
        "names and descriptions below are loaded. When one of them fits what "
        "you have been asked to do, read its SKILL.md with the file tools "
        "before starting, and follow what it says; it may point you at other "
        "files beside it, which you should read only if you need them. Ignore "
        "the ones that do not fit — most turns need none of these.",
        "",
    ]
    for skill in skills:
        lines.append(f"- {skill.name} ({skill.origin}): {skill.description}")
        lines.append(f"  {skill.path}")
    return "\n".join(lines)


def block(workspace_root: Path | None = None) -> str:
    """Discover and render in one call, for the prompt builder."""
    try:
        return describe(discover(workspace_root))
    except Exception:  # noqa: BLE001 — a broken skills dir must not stop a turn
        logger.warning("skill discovery failed", exc_info=True)
        return ""
