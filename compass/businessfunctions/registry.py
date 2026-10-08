"""Finding business functions on disk, checking them, and holding them.

One folder per function under `catalog/`, each with a `manifest.yaml`.
Discovery is one directory deep and never recursive — a function is a folder
with a manifest in it, not any manifest anywhere beneath.

FAIL SOFT, LOUDLY. A manifest that does not parse, or that breaks a rule in
manifest.py, is skipped with a warning and the others still load: a typo in
Talent must not take Finance down with it. There is no partial acceptance —
a function either passes every check or is not there at all, because a feature
that quietly lost half its actions is worse than a feature that is missing.

THE HANDLER CHECK IS WHY THIS IS NOT JUST A YAML LOADER. Every feature names
registered code. The handlers are imported before validation, so a manifest
naming one that nobody registered fails at load rather than on the first
request — and a handler whose declared scope the manifest disagrees with fails
too, since those two have to describe the same screen.

Nothing here reads a store or talks to Azure. It reads files that ship with
Compass.
"""

from __future__ import annotations

import logging
from pathlib import Path

from compass.businessfunctions import features
from compass.businessfunctions.manifest import FunctionManifest

logger = logging.getLogger("compass.businessfunctions")

#: Where the catalog lives, beside this file. Functions ship with Compass
#: rather than being uploaded at runtime: a manifest is reviewed, and the
#: review is the point.
CATALOG = Path(__file__).resolve().parent / "catalog"

MANIFEST_NAME = "manifest.yaml"

#: Loaded functions by id, or None before the first load. None and an empty
#: dict mean different things — "not read yet" and "nothing installed".
_functions: dict[str, FunctionManifest] | None = None


def _parse(path: Path) -> dict | None:
    """The manifest as a plain dict, or None when it cannot be read.

    PyYAML is imported here rather than at module scope, as
    compass/common/skills.py does it, so a deployment without it loses
    business functions and keeps everything else working.
    """
    try:
        import yaml
    except ImportError:
        logger.warning(
            "business functions need PyYAML, which is not installed; "
            "none will load"
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
        logger.warning("%s does not describe a business function", path)
        return None
    return parsed


def _load_one(folder: Path) -> FunctionManifest | None:
    """The business function in `folder`, or None with a warning saying why."""
    path = folder / MANIFEST_NAME
    if not path.is_file():
        return None

    # A symlink out of the catalog is not a function of this deployment's.
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
        fn = FunctionManifest.model_validate(raw)
    except Exception as err:  # noqa: BLE001 — a bad manifest is skipped, not fatal
        logger.warning("skipping %s: %s", path, err)
        return None

    # The folder name is the id. Anything else is two names for one thing, and
    # the one in the URL would not be the one on disk.
    if fn.id != folder.name:
        logger.warning("skipping %s: id is %r but the folder is %r",
                       path, fn.id, folder.name)
        return None

    if problems := fn.problems():
        for why in problems:
            logger.warning("%s: %s", path, why)
        return None

    # Every feature must name code that exists. Checked here so a missing
    # handler is a startup warning rather than a 500 the first time somebody
    # opens the feature.
    for feature in fn.features:
        if features.get(feature.handler) is None:
            logger.warning(
                "skipping %s: feature %r names handler %r, which nothing "
                "registered (known: %s)",
                path, feature.id, feature.handler,
                ", ".join(features.keys()) or "none",
            )
            return None

    return fn


def _scan() -> dict[str, FunctionManifest]:
    if not CATALOG.is_dir():
        return {}
    found: dict[str, FunctionManifest] = {}
    try:
        folders = sorted(p for p in CATALOG.iterdir() if p.is_dir())
    except OSError:
        return {}
    for folder in folders:
        if folder.name.startswith((".", "_")):
            continue
        if fn := _load_one(folder):
            found[fn.id] = fn
    return found


def _loaded() -> dict[str, FunctionManifest]:
    global _functions
    if _functions is None:
        _functions = _scan()
        logger.info("business functions loaded: %s",
                    ", ".join(sorted(_functions)) or "none")
    return _functions


def reload() -> dict[str, FunctionManifest]:
    """Re-read the catalog. For the admin screen that edits a manifest."""
    global _functions
    _functions = None
    return _loaded()


def all_functions() -> list[FunctionManifest]:
    """Every function that loaded, in a stable order.

    Stable because the switcher renders in this order, and a list that
    reshuffles between requests is its own small bug.
    """
    loaded = _loaded()
    return [loaded[k] for k in sorted(loaded)]


def get(function_id: str) -> FunctionManifest | None:
    """One function by id, or None.

    None covers both "no such function" and "exists but is malformed", on
    purpose: from a caller's side a function that failed validation is not
    available, and inviting the caller to tell the two apart invites it to use
    the broken one.
    """
    return _loaded().get(function_id)


def feature_of(function_id: str, feature_id: str):
    """One (function, feature, handler) or (None, None, None).

    The three travel together because every caller needs all three and
    looking them up separately is three chances to mismatch them.
    """
    fn = get(function_id)
    if fn is None:
        return None, None, None
    feature = fn.feature(feature_id)
    if feature is None:
        return fn, None, None
    return fn, feature, features.get(feature.handler)
