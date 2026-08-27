"""What must not change while the code is being moved.

A restructure is only safe if "nothing changed" is a measurement rather than a
belief. This records the things a reorganisation could plausibly break — every
prompt the backend sends, every route it serves, and the shape of every reply —
and diffs a later run against an earlier one.

    python3 scripts/structure_snapshot.py save    before touching anything
    python3 scripts/structure_snapshot.py check   after each step

A prompt whose hash moved by one byte is a failed step, not a detail.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SNAPSHOT = HERE / "structure_snapshot.json"

os.environ.setdefault("COMPASS_AUTH_ENABLED", "0")
sys.path.insert(0, str(ROOT))


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def prompts() -> dict[str, str]:
    """Every prompt the backend can send, hashed, with its length.

    Imported by name rather than by module path wherever possible, so the
    lookup keeps working as things move; a name that disappears entirely is
    itself worth failing on.
    """
    out: dict[str, str] = {}

    def take(label: str, value) -> None:
        if isinstance(value, str):
            out[label] = f"{_digest(value)}:{len(value)}"
        elif isinstance(value, dict):
            for key in sorted(value):
                take(f"{label}[{key}]", value[key])
        elif isinstance(value, (list, tuple)):
            out[label] = f"{_digest(json.dumps(value, sort_keys=True, default=str))}:{len(value)}"

    from compass.services import design as _design

    take("design.TEMPLATE_PROMPTS", _design.TEMPLATE_PROMPTS)
    take("design.DESIGN_SYSTEM_PROMPT", _design.DESIGN_SYSTEM_PROMPT)
    take("design.CLARIFY_PROMPT", _design.CLARIFY_PROMPT)
    take("design.FOLLOWUP_PROMPT", _design.FOLLOWUP_PROMPT)
    take("design.FOLLOWUP_FALLBACK", _design.FOLLOWUP_FALLBACK)
    take("design.EXTRACT_PROMPT", _design.EXTRACT_PROMPT)
    take("design.TEMPLATES", _design.TEMPLATES)
    take("design.BUILTIN_SYSTEMS", _design.BUILTIN_SYSTEMS)
    take("design.BLANK_PAGE", _design.BLANK_PAGE)

    from compass.api import server as _server

    take("server.SUGGEST_PROMPT", _server.SUGGEST_PROMPT)
    take("server.REPAIR_PROMPT", _server.REPAIR_PROMPT)
    take("server.CSS_REPAIR_PROMPT", _server.CSS_REPAIR_PROMPT)

    from compass.core import chat_engine as _chat

    take("chat.CHAT_SYSTEM_PROMPT", _chat.CHAT_SYSTEM_PROMPT)

    from compass.context import compaction as _compaction

    take("compaction.SUMMARY_PROMPT", _compaction.SUMMARY_PROMPT)

    from compass.services import work_iq as _work_iq

    take("work_iq.WORK_IQ_SYSTEM_PROMPT", _work_iq.WORK_IQ_SYSTEM_PROMPT)

    from compass.core import query_loop as _loop

    take("query_loop.MAX_OUTPUT_RECOVERY_PROMPT", _loop.MAX_OUTPUT_RECOVERY_PROMPT)

    from compass.core import system_prompt as _sysprompt

    for name in dir(_sysprompt):
        if name.isupper() and isinstance(getattr(_sysprompt, name), str):
            take(f"system_prompt.{name}", getattr(_sysprompt, name))

    return out


def routes() -> list[str]:
    """Every path the app serves, with its methods — the API's outward shape."""
    from compass.api.server import app

    seen = []
    for route in app.routes:
        methods = ",".join(sorted(getattr(route, "methods", []) or ["WS"]))
        seen.append(f"{methods} {route.path}")
    return sorted(seen)


def schema() -> str:
    """The OpenAPI document, which carries the request and response models."""
    from compass.api.server import app

    return _digest(json.dumps(app.openapi(), sort_keys=True))


def gather() -> dict:
    return {"prompts": prompts(), "routes": routes(), "schema": schema()}


def _report(old: dict, new: dict) -> bool:
    ok = True

    moved = [
        k for k in sorted(set(old["prompts"]) | set(new["prompts"]))
        if old["prompts"].get(k) != new["prompts"].get(k)
    ]
    if moved:
        ok = False
        print(f"{len(moved)} PROMPT(S) CHANGED — this is a failed step:")
        for k in moved:
            print(f"   {k}\n      was {old['prompts'].get(k, '(absent)')}"
                  f"\n      now {new['prompts'].get(k, '(absent)')}")
    else:
        print(f"prompts   {len(new['prompts'])} checked, every one byte-identical")

    gone = sorted(set(old["routes"]) - set(new["routes"]))
    added = sorted(set(new["routes"]) - set(old["routes"]))
    if gone or added:
        ok = False
        print("ROUTES CHANGED:")
        for r in gone:
            print(f"   lost  {r}")
        for r in added:
            print(f"   new   {r}")
    else:
        print(f"routes    {len(new['routes'])} checked, the table is unchanged")

    if old["schema"] != new["schema"]:
        ok = False
        print(f"OPENAPI SCHEMA CHANGED: {old['schema']} -> {new['schema']}")
        print("   (a changed request or response model, or a changed summary)")
    else:
        print("schema    unchanged")

    return ok


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else "check"
    now = gather()

    if action == "save":
        SNAPSHOT.write_text(json.dumps(now, indent=1, sort_keys=True))
        print(f"saved {len(now['prompts'])} prompts and {len(now['routes'])} routes"
              f" to {SNAPSHOT.relative_to(ROOT)}")
        return 0

    if not SNAPSHOT.exists():
        print("no snapshot to check against — run `save` first")
        return 2

    before = json.loads(SNAPSHOT.read_text())
    ok = _report(before, now)
    print("\nUNCHANGED" if ok else "\nSOMETHING MOVED — fix or revert")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
