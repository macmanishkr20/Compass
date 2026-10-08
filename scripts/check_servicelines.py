"""What must hold about the service-line catalog, checked without a model.

A service line is data, so the only claims worth making about this layer are
about what it *refuses*. The registry's job is not to load manifests — any
YAML parser does that — it is to decline the ones that lie about their own
powers, and to decline them completely rather than loading a narrowed version
that nobody asked for.

So most of this file builds deliberately broken manifests in a temporary
catalog and asserts each one does not load. Every refusal below is a rule
written out in compass/servicelines/manifest.py, and the pairing is the point:
if somebody relaxes a rule there, a check here goes red and names it.

The two positive checks matter as much. A control manifest must still load —
otherwise the refusals prove only that the loader is broken — and the real
catalog that ships with Compass must load clean, because a shipped manifest
that fails validation is a service line that silently is not there.

    python3 scripts/check_servicelines.py
"""

from __future__ import annotations

import logging
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The refusals log a warning each, which is correct behaviour and noise here.
logging.disable(logging.WARNING)

from compass.servicelines import registry  # noqa: E402
from compass.servicelines.manifest import WITHHELD_TOOLS  # noqa: E402

FAILURES: list[str] = []


def ok(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(f"{label}{': ' + detail if detail else ''}")


#: A manifest that loads. Every probe below is this with one thing wrong, so a
#: refusal can only be caused by the thing that was changed.
CONTROL = """
id: {id}
name: Probe
description: A fixture.
lead_prompt: prompts/lead.md
governance:
  classification: client-confidential
  residency: india-south
  retention_months: 12
  reviewer: partner
tools: [doc_read, extract_field]
skills:
  - id: ok_skill
    name: Fine
    description: Fine.
    prompt: prompts/s.md
    status: approved
    tools: [doc_read]
{extra}
"""


def _skill(**fields: str) -> str:
    """One extra skill block, for the probes that add a bad one."""
    lines = [
        "  - id: bad",
        "    name: Bad",
        "    description: Bad.",
        "    prompt: prompts/s.md",
    ]
    lines += [f"    {k}" for k in fields.values()]
    return "\n".join(lines) + "\n"


def loads(body: str, folder: str = "probe") -> bool:
    """Whether `body` loads as a service line, in a catalog of its own."""
    tmp = Path(tempfile.mkdtemp())
    before_catalog, before_cache = registry.CATALOG, registry._lines
    try:
        d = tmp / folder
        (d / "prompts").mkdir(parents=True)
        for name in ("lead.md", "s.md"):
            (d / "prompts" / name).write_text("probe")
        (d / "manifest.yaml").write_text(body)
        registry.CATALOG, registry._lines = tmp, None
        return registry.get(folder) is not None
    finally:
        registry.CATALOG, registry._lines = before_catalog, before_cache
        shutil.rmtree(tmp, ignore_errors=True)


def check_a_good_manifest_loads() -> None:
    """Without this the refusals below prove nothing."""
    ok("a valid manifest loads", loads(CONTROL.format(id="probe", extra="")))


def check_a_skill_cannot_reach_past_its_service_line() -> None:
    """The two-layer rule: a skill asks for fewer tools, never more.

    Both halves are checked because they are different mistakes. Asking for
    `bash` misunderstands what a service line is; asking for `doc_classify`
    when the line grants only `doc_read` is usually a missing line in the
    line's own manifest. Neither loads.
    """
    ok("a skill asking for a withheld tool is refused",
       not loads(CONTROL.format(id="probe", extra=_skill(t="tools: [bash]"))))
    ok("a skill asking for a tool its line never granted is refused",
       not loads(CONTROL.format(id="probe", extra=_skill(t="tools: [doc_classify]"))))
    ok("a service line cannot grant a withheld tool either",
       not loads(CONTROL.format(id="probe", extra="")
                 .replace("tools: [doc_read, extract_field]", "tools: [doc_read, bash]")))


def check_the_locks_cannot_be_undone() -> None:
    """Provenance, computed fields and sign-off are confirmable, not optional.

    These three are what make an output defensible, and a manifest is exactly
    where somebody would try to switch one off to make a stubborn skill pass.
    """
    ok("a skill cannot switch off require_source",
       not loads(CONTROL.format(
           id="probe", extra=_skill(e="evidence:\n      require_source: false"))))
    ok("a skill cannot hand computed fields to the model",
       not loads(CONTROL.format(
           id="probe", extra=_skill(e="evidence:\n      computed_fields: model"))))
    ok("a skill cannot switch off human sign-off",
       not loads(CONTROL.format(
           id="probe", extra=_skill(r="review:\n      signoff: false"))))


def check_prompt_bound_text_is_treated_as_data() -> None:
    """A name reaches a system prompt, so markup in one is refused."""
    ok("markup in a name is refused",
       not loads(CONTROL.format(id="probe", extra="")
                 .replace("name: Probe", 'name: "Probe <b>x</b>"')))


def check_a_manifest_stays_in_its_own_folder() -> None:
    """Prompt paths are read off disk later, so they are checked now."""
    ok("a prompt path containing .. is refused",
       not loads(CONTROL.format(id="probe", extra="")
                 .replace("lead_prompt: prompts/lead.md", "lead_prompt: ../../etc/passwd")))
    ok("a prompt file that is not there is refused",
       not loads(CONTROL.format(id="probe", extra="")
                 .replace("lead_prompt: prompts/lead.md", "lead_prompt: prompts/absent.md")))


def check_mistakes_that_would_pass_silently_do_not() -> None:
    """The ones an SME is most likely to make, and least likely to notice."""
    ok("an id that disagrees with its folder is refused",
       not loads(CONTROL.format(id="mismatch", extra="")))
    ok("two skills sharing an id is refused",
       not loads(CONTROL.format(
           id="probe",
           extra="  - id: ok_skill\n    name: Dup\n    description: D.\n    prompt: prompts/s.md\n")))
    ok("an unknown key is refused rather than ignored",
       not loads(CONTROL.format(id="probe", extra="") + "\nretention: 12\n"))
    ok("something that is not YAML at all is refused",
       not loads("}{ not yaml :"))


def check_unfinished_is_not_the_same_as_malformed() -> None:
    """A service line with no governance answers loads, and is unusable.

    This is the state a line is in on its first day. Refusing to load it would
    make it invisible, which is how somebody ends up creating it twice; making
    it usable would let client data into a line whose residency nobody has
    stated. It loads, and it says it is not ready.
    """
    body = CONTROL.format(id="probe", extra="").replace(
        "governance:\n  classification: client-confidential\n"
        "  residency: india-south\n  retention_months: 12\n  reviewer: partner",
        "governance: {}")
    ok("an unconfigured service line still loads", loads(body))

    line = registry.get("sustainability")
    ok("the shipped unconfigured line reports itself unconfigured",
       line is not None and line.unconfigured)
    ok("it names all four unanswered questions",
       line is not None and sorted(line.governance.problems()) ==
       ["classification", "residency", "retention_months", "reviewer"],
       str(line.governance.problems()) if line else "not loaded")


def check_the_shipped_catalog_is_clean() -> None:
    """Every service line that ships with Compass loads and behaves."""
    ids = sorted(line.id for line in registry.all_lines())
    ok("the shipped catalog loads", ids == ["sustainability", "talent", "tax"], str(ids))

    tax, talent = registry.get("tax"), registry.get("talent")
    ok("tax is configured", tax is not None and not tax.unconfigured)
    ok("a draft skill is not runnable",
       tax is not None and "form15ca_15cb" not in {s.id for s in tax.runnable_skills})
    ok("tax and talent answer the four questions differently",
       tax is not None and talent is not None
       and (tax.governance.classification, tax.governance.retention_months)
       != (talent.governance.classification, talent.governance.retention_months))

    every_tool = {t for line in registry.all_lines() for t in line.tools}
    every_tool |= {t for line in registry.all_lines() for s in line.skills for t in s.tools}
    ok("no shipped manifest asks for a withheld tool",
       not (every_tool & WITHHELD_TOOLS), str(sorted(every_tool & WITHHELD_TOOLS)))


def check_nothing_is_mounted_while_the_flag_is_off() -> None:
    """The escape hatch Pipelines and Estimate have, and the promise it makes."""
    from compass.common.config import get_settings

    ok("service lines are off by default", not get_settings().servicelines.enabled)
    ok("a bad id is None rather than an error", registry.get("no-such-line") is None)


def main() -> int:
    print("service lines\n")
    check_a_good_manifest_loads()
    check_a_skill_cannot_reach_past_its_service_line()
    check_the_locks_cannot_be_undone()
    check_prompt_bound_text_is_treated_as_data()
    check_a_manifest_stays_in_its_own_folder()
    check_mistakes_that_would_pass_silently_do_not()
    check_unfinished_is_not_the_same_as_malformed()
    check_the_shipped_catalog_is_clean()
    check_nothing_is_mounted_while_the_flag_is_off()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED")
        for failure in FAILURES:
            print(f"   {failure}")
        return 1
    print("a manifest cannot claim a power it was not granted, and the "
          "catalog that ships says what it means")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
