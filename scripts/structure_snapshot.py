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

    # These three are looked up by name across the modules they could live in,
    # so a prompt that MOVES still has to prove its bytes did not change. The
    # label stays put, because the label is what the baseline was saved under.
    def wherever(label: str, name: str, *modules: str) -> None:
        import importlib

        for mod in modules:
            try:
                value = getattr(importlib.import_module(mod), name)
            except (ImportError, AttributeError):
                continue
            take(label, value)
            return
        raise SystemExit(f"{name} has vanished: not in any of {modules}")

    wherever("server.SUGGEST_PROMPT", "SUGGEST_PROMPT",
             "compass.api.server", "compass.code.routes")
    wherever("server.REPAIR_PROMPT", "REPAIR_PROMPT",
             "compass.api.server", "compass.design.routes", "compass.design.review")
    wherever("server.CSS_REPAIR_PROMPT", "CSS_REPAIR_PROMPT",
             "compass.api.server", "compass.design.routes", "compass.design.review")

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


def smoke() -> dict[str, str]:
    """Actually call every route that can be called without arguments.

    The route table and the schema can both be perfectly intact while a
    handler raises the moment it runs — a module-level singleton left behind
    in the wrong file, say. Only calling them finds that.
    """
    from fastapi.testclient import TestClient

    from compass.api.server import app

    out: dict[str, str] = {}
    with TestClient(app, raise_server_exceptions=False) as client:
        for route in app.routes:
            path = route.path
            methods = getattr(route, "methods", set()) or set()
            if "GET" not in methods or "{" in path:
                continue
            try:
                status = client.get(path).status_code
            except Exception as err:  # noqa: BLE001
                status = f"raised {type(err).__name__}"
            out[path] = str(status)
    return out


def gather() -> dict:
    return {"prompts": prompts(), "routes": routes(),
            "schema": schema(), "smoke": smoke()}


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

    broke = [
        p for p in sorted(set(old.get("smoke", {})) | set(new.get("smoke", {})))
        if old.get("smoke", {}).get(p) != new.get("smoke", {}).get(p)
    ]
    if broke:
        ok = False
        print(f"{len(broke)} ROUTE(S) NOW ANSWER DIFFERENTLY:")
        for p in broke:
            print(f"   {p}: was {old.get('smoke', {}).get(p, '-')}"
                  f" now {new.get('smoke', {}).get(p, '-')}")
    elif new.get("smoke"):
        print(f"calls     {len(new['smoke'])} GET routes answered as before")

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
