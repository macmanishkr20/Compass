"""Finding service lines on disk, checking them, and keeping them in memory.

One folder per service line under `catalog/`, each with a `manifest.yaml`.
Discovery is one directory deep and never recursive — a service line is a
folder with a manifest in it, not any manifest anywhere beneath, which would
pick up fixtures and vendored copies.

FAIL SOFT, LOUDLY. A manifest that does not parse, or that breaks one of the
rules in manifest.py, is skipped with a warning and the others still load. A
broken Tax manifest must not take Talent down with it, and it must certainly
not take the server down — but it also must not load half-valid and be used,
so there is no partial acceptance: a service line either passes every check or
is not there at all.

WHY IT IS CACHED. The catalog is read from the repository, not from a
database, and it changes when somebody deploys. Reading and validating a dozen
YAML files on every request would be waste, so the result is held and
`reload()` exists for the admin screen that edits a manifest. The cache is a
module global rather than per-loop state (which `persistence/catalog.py` needs
and this does not) because nothing here is a network client — it is parsed
text, immutable once loaded, and safe to share.

Nothing in this module reads client data or talks to Azure. It reads files
that ship with Compass.
"""

from __future__ import annotations

import logging
from pathlib import Path

from compass.servicelines.manifest import ServiceLineManifest

logger = logging.getLogger("compass.servicelines")

#: Where the catalog lives, beside this file. Service lines ship with Compass
#: rather than being uploaded at runtime: a manifest is reviewed code, and the
#: review is the point.
CATALOG = Path(__file__).resolve().parent / "catalog"

MANIFEST_NAME = "manifest.yaml"

#: Loaded manifests by id, or None before the first load. A dict and a None
#: rather than an empty dict, so "nothing is installed" and "nothing has been
#: read yet" stay distinguishable.
_lines: dict[str, ServiceLineManifest] | None = None


def _parse(path: Path) -> dict | None:
    """The manifest as a plain dict, or None when it cannot be read.

    PyYAML is imported here rather than at module scope, exactly as
    compass/common/skills.py does it: it arrives transitively today and is not
    in requirements.txt, so a deployment without it loses service lines and
    keeps everything else.
    """
    try:
        import yaml
    except ImportError:
        logger.warning(
            "service lines need PyYAML, which is not installed; none will load"
        )
        return None
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as err:
        logger.warning("cannot read %s: %s", path, err)
        return None
    except yaml.YAMLError as err:
        logger.warning("%s is not valid YAML: %s", path, err)
        return None
    if not isinstance(parsed, dict):
        logger.warning("%s does not describe a service line", path)
        return None
    return parsed


def _load_one(folder: Path) -> ServiceLineManifest | None:
    """The service line in `folder`, or None with a warning saying why."""
    path = folder / MANIFEST_NAME
    if not path.is_file():
        return None

    # A symlink out of the catalog is not a service line of this deployment's.
    # The same check skills.py makes, for the same reason: it keeps "what is
    # installed" an honest answer.
    try:
        if not path.resolve().is_relative_to(CATALOG.resolve()):
            logger.warning("skipping %s: it points outside the catalog", path)
            return None
    except (OSError, ValueError):
        return None

    raw = _parse(path)
    if raw is None:
        return None

    try:
        line = ServiceLineManifest.model_validate(raw)
    except Exception as err:  # noqa: BLE001 — a bad manifest is skipped, not fatal
        logger.warning("skipping %s: %s", path, err)
        return None

    # The folder name is the id. Anything else means two names for one thing,
    # and the one in the URL would not match the one on disk.
    if line.id != folder.name:
        logger.warning(
            "skipping %s: id is %r but the folder is %r",
            path, line.id, folder.name,
        )
        return None

    if problems := line.problems():
        for why in problems:
            logger.warning("%s: %s", path, why)
        return None

    # Prompts are named by the manifest and read by the engine later; a
    # manifest that names one that is not there would fail at the worst
    # possible moment, so it fails here instead.
    for rel in [line.lead_prompt] + [s.prompt for s in line.skills]:
        if missing := _bad_prompt(folder, rel):
            logger.warning("skipping %s: %s", path, missing)
            return None

    return line


def _bad_prompt(folder: Path, rel: str) -> str:
    """Why this prompt path is unusable, or "" when the file is there.

    `..` is refused outright rather than resolved and compared, because a
    manifest has no business reaching outside its own folder and saying so
    plainly is clearer than explaining a path traversal in a log line.
    """
    if Path(rel).is_absolute() or ".." in Path(rel).parts:
        return f"prompt path {rel!r} must stay inside the service line's folder"
    target = folder / rel
    try:
        if not target.resolve().is_relative_to(folder.resolve()):
            return f"prompt path {rel!r} points outside the service line's folder"
    except (OSError, ValueError):
        return f"prompt path {rel!r} cannot be resolved"
    if not target.is_file():
        return f"prompt file {rel!r} is missing"
    return ""


def _scan() -> dict[str, ServiceLineManifest]:
    if not CATALOG.is_dir():
        return {}
    found: dict[str, ServiceLineManifest] = {}
    try:
        folders = sorted(p for p in CATALOG.iterdir() if p.is_dir())
    except OSError:
        return {}
    for folder in folders:
        if folder.name.startswith((".", "_")):
            continue
        if line := _load_one(folder):
            found[line.id] = line
    return found


def _loaded() -> dict[str, ServiceLineManifest]:
    global _lines
    if _lines is None:
        _lines = _scan()
        logger.info(
            "service lines loaded: %s",
            ", ".join(sorted(_lines)) or "none",
        )
    return _lines


def reload() -> dict[str, ServiceLineManifest]:
    """Re-read the catalog. For the admin screen that edits a manifest."""
    global _lines
    _lines = None
    return _loaded()


def all_lines() -> list[ServiceLineManifest]:
    """Every service line that loaded, in a stable order.

    Stable because the picker renders in this order and a list that reshuffles
    between requests is its own small bug.
    """
    return [_loaded()[k] for k in sorted(_loaded())]


def get(line_id: str) -> ServiceLineManifest | None:
    """One service line by id, or None when there is no such thing.

    None covers both "never existed" and "exists but is malformed", and that
    is deliberate: from a caller's point of view a service line that failed
    validation is not available, and inviting the caller to distinguish the
    two would invite it to use the broken one.
    """
    return _loaded().get(line_id)


def prompt_text(line: ServiceLineManifest, rel: str) -> str:
    """One prompt file's contents, by a path the manifest named.

    Re-checks the path rather than trusting that `_load_one` already did. The
    manifest is data, this reads a file from disk, and a check that only runs
    at load time is one refactor away from not running at all.
    """
    folder = CATALOG / line.id
    if why := _bad_prompt(folder, rel):
        raise ValueError(f"{line.id}: {why}")
    return (folder / rel).read_text(encoding="utf-8")
