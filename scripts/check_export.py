"""Does an exported pipeline behave the same as the one it came from?

The export carries a second implementation of the walk — Compass's engine is
async, persists every step, resolves secrets and can park a run for hours, and
none of that belongs in a library dropped into someone else's application. Two
implementations is a real cost, and the only thing that makes it safe is
checking they agree.

So this generates a package from a real graph, installs nothing, runs it in a
subprocess with no Compass on the path, and compares the result against
Compass's own engine node by node. If the two ever drift, this fails.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("COMPASS_PIPELINES", "1")
os.environ.setdefault("COMPASS_AUTH_ENABLED", "0")
os.environ.setdefault("COMPASS_WORKSPACE", tempfile.mkdtemp(prefix="plexport-"))
# Locally, like the temp workspace above and for the same reason: this compares
# two implementations of a graph walk, which is the same answer whatever holds
# the graph. On the cosmos backend it instead talked to the cloud across
# several event loops, which is slower, needs credentials, and left "Unclosed
# client session" printed under the result — noise exactly where a real
# warning would go unread.
os.environ.setdefault("COMPASS_STORAGE_BACKEND", "local")

FAILED = 0


def ok(condition: bool, label: str) -> None:
    global FAILED
    print(("   ok    " if condition else "   FAIL  ") + label)
    if not condition:
        FAILED += 1


def build_graph():
    from compass.pipelines.store import Edge, Node, Pipeline

    # Deliberately exercises the parts most likely to drift: a branch on a
    # false condition, a loop with two body nodes, a node after the loop, a
    # deactivated node, and an expression reaching an upstream result.
    pipeline = Pipeline(id="pl_x", name="Export Check", version=3)
    pipeline.nodes = [
        Node(id="start", type="flow.start", name="Start"),
        Node(id="seed", type="flow.set_variable", name="Seed",
             config={"name": "greeting", "value": "hello"}),
        Node(id="gate", type="flow.if", name="Gate",
             config={"condition": False}),
        Node(id="never", type="flow.set_variable", name="Never",
             config={"name": "nope", "value": 1}),
        Node(id="loop", type="flow.foreach", name="Each",
             config={"items": ["a", "b", "c"]}),
        Node(id="mark", type="flow.set_variable", name="Mark",
             config={"name": "cur", "value": "@item()"}),
        Node(id="pos", type="flow.set_variable", name="Pos",
             config={"name": "at", "value": "@index()"}),
        Node(id="after", type="flow.set_variable", name="After",
             config={"name": "seen", "value": "@nodes('seed').data.value"}),
        Node(id="off", type="flow.set_variable", name="Off",
             config={"name": "x", "value": 1}, state="deactivated",
             mark_as="success"),
    ]
    pipeline.edges = [
        Edge("start", "seed"),
        Edge("seed", "gate"),
        Edge("gate", "never", port="out"),
        Edge("gate", "loop", port="false"),
        Edge("loop", "mark", port="each"),
        Edge("mark", "pos"),
        Edge("loop", "after", port="out"),
        Edge("after", "off"),
    ]
    return pipeline


async def run_in_compass(pipeline):
    from compass.pipelines import store as pstore
    from compass.pipelines.engine import engine

    pstore.pipelines._write(pipeline.id, pipeline.to_dict())
    run = await engine.start(pipeline)
    return {
        "status": run.status,
        "nodes": {k: v.status for k, v in run.nodes.items()},
        "iterations": len(run.iterations.get("loop", [])),
        "items": [it["mark"]["output"].get("value")
                  for it in run.iterations.get("loop", [])],
    }


def run_exported(pipeline) -> dict:
    from compass.pipelines import export as pexport

    files = pexport.python_package(pipeline)
    name = pexport.package_name(pipeline)
    with tempfile.TemporaryDirectory(prefix="plpkg-") as tmp:
        root = Path(tmp)
        for rel, content in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        driver = root / "drive.py"
        driver.write_text(
            "import json, sys\n"
            f"from {name} import Pipeline\n"
            "p = Pipeline()\n"
            "r = p.run()\n"
            "print(json.dumps({\n"
            "  'status': r.status,\n"
            "  'nodes': {k: v.status for k, v in r.nodes.items()},\n"
            "  'iterations': len(r.iterations.get('loop', [])),\n"
            "  'items': [it['mark'].data.get('value')\n"
            "            for it in r.iterations.get('loop', [])],\n"
            "}))\n",
            encoding="utf-8",
        )
        # No Compass on the path: PYTHONPATH is the package directory only,
        # so an accidental import of Compass would fail loudly here rather
        # than making the export look more portable than it is.
        env = {**os.environ, "PYTHONPATH": str(root)}
        env.pop("COMPASS_WORKSPACE", None)
        proc = subprocess.run(
            [sys.executable, str(driver)], capture_output=True, text=True,
            env=env, cwd=tmp, timeout=120,
        )
        if proc.returncode != 0:
            print(proc.stdout[-2000:])
            print(proc.stderr[-3000:])
            raise SystemExit("the exported package did not run")
        return json.loads(proc.stdout.strip().splitlines()[-1])


def main() -> int:
    pipeline = build_graph()

    print("\nthe export produces a package that runs on its own")
    from compass.pipelines import export as pexport

    files = pexport.python_package(pipeline)
    name = pexport.package_name(pipeline)
    ok(name == "export_check", f"the package is named for the pipeline ({name})")
    for expected in (f"{name}/__init__.py", f"{name}/runtime.py",
                     f"{name}/handlers.py", f"{name}/graph.py",
                     "pyproject.toml", "README.md", "ARCHITECTURE.md"):
        ok(expected in files, f"it ships {expected}")
    ok("dependencies = []" in files["pyproject.toml"],
       "with no third-party dependencies, so it drops into anything")
    # The word "Compass" appears in the docstrings, which is fine. What must
    # not appear is an import of it — that is the difference between a package
    # that mentions where it came from and one that cannot leave.
    import re as _re
    blob = files[f"{name}/runtime.py"] + files[f"{name}/handlers.py"]
    ok(not _re.search(r"^\s*(from|import)\s+compass", blob, _re.M),
       "and imports nothing from Compass")

    exported = run_exported(pipeline)
    native = asyncio.get_event_loop().run_until_complete(run_in_compass(pipeline))

    print("\nand agrees with Compass's own engine, node by node")
    ok(exported["status"] == native["status"] == "done",
       f"both finish with the same status ({native['status']})")
    ok(exported["nodes"] == native["nodes"],
       "every node reaches the same state in both")
    if exported["nodes"] != native["nodes"]:
        for key in sorted(set(exported["nodes"]) | set(native["nodes"])):
            a, b = native["nodes"].get(key), exported["nodes"].get(key)
            if a != b:
                print(f"          {key}: compass={a} exported={b}")
    ok(native["nodes"]["never"] == "skipped",
       "the branch the condition did not take is skipped in both")
    ok(native["nodes"]["off"] == "inactive",
       "a deactivated node reports what it was told to")
    ok(exported["iterations"] == native["iterations"] == 3,
       "the loop ran the same number of times")
    ok(exported["items"] == native["items"] == ["a", "b", "c"],
       "@item() resolved to the same values in both")

    print("\nthe architecture note is honest about what did not come")
    arch = files["ARCHITECTURE.md"]
    ok("```mermaid" in arch, "it carries a diagram that renders where it lands")
    ok("once per item" in arch, "and says which steps run more than once")
    ok("never (deactivated" in arch, "and which never run at all")
    bundle = pexport.bundle(pipeline)
    ok(bundle["needs_host"] == [],
       "this graph needed nothing from the host, and says so")

    # A graph that does need the host must name it rather than fail quietly.
    from compass.pipelines.store import Node

    pipeline.nodes.append(Node(id="sh", type="tool.bash", name="Shell",
                               config={"command": "echo hi"}))
    needy = pexport.bundle(pipeline)
    ok([n["type"] for n in needy["needs_host"]] == ["tool.bash"],
       "a step that cannot be exported is named")
    ok("p.register('tool.bash'" in needy["architecture"],
       "with the line you need to supply it")

    print("\n" + ("EXPORT DRIFTED" if FAILED else "the export matches"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
