"""What must hold about the business-function catalog, without a model.

Two halves, and both matter.

The first is about what the loader REFUSES. A manifest is data edited by
people who are not reviewing it as code, so the only interesting claim about
this layer is that it declines the ones that lie about their own powers — and
declines them completely, rather than loading a narrowed version nobody asked
for. Every refusal below is a rule written out in manifest.py, and the pairing
is the point: relax a rule there and a check here goes red and names it.

The second is about the handlers, where the arithmetic lives. A reconciliation
whose figures disagree with its rows is worse than no reconciliation, so the
numbers are asserted against the fixture rather than against a recorded
output: 520000 − 380000 is 140000, and the comment above each check says so.

    python3 scripts/check_businessfunctions.py
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Refusals log a warning each, which is correct behaviour and noise here.
logging.disable(logging.WARNING)

from compass.businessfunctions import assistant, features, registry  # noqa: E402
from compass.businessfunctions.features.base import Scope  # noqa: E402
from compass.businessfunctions.features import form26 as form26_mod  # noqa: E402
from compass.businessfunctions.features import lms as lms_mod  # noqa: E402
from compass.businessfunctions.manifest import WITHHELD_TOOLS  # noqa: E402

FAILURES: list[str] = []


@contextlib.contextmanager
def fixtures():
    """Run a check against untouched rows, whatever ran before it.

    The handlers still carry fixtures, and approving leave is deliberately
    irreversible — there is no undo to put it back with. So the rows are
    deep-copied in and restored out, which makes every check below
    order-independent. When the store lands this becomes a transaction that
    rolls back, and the checks do not change.
    """
    before = (copy.deepcopy(form26_mod._FIXTURE), copy.deepcopy(lms_mod._FIXTURE))
    try:
        yield
    finally:
        form26_mod._FIXTURE[:] = before[0]
        lms_mod._FIXTURE[:] = before[1]


def ok(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(f"{label}{': ' + detail if detail else ''}")


#: A manifest that loads. Every probe is this with one thing changed, so a
#: refusal can only have been caused by the thing that was changed.
CONTROL = """
id: {id}
name: Probe
subtitle: A fixture
blurb: A fixture function.
tools: [read_rows]
features:
  - id: ok_feature
    name: Fine
    short: Fine
    blurb: Fine.
    handler: form26
    scope: [entity, period]
    tools: [read_rows]
{extra}
"""


def loads(body: str, folder: str = "probe") -> bool:
    """Whether `body` loads as a business function, in a catalog of its own."""
    tmp = Path(tempfile.mkdtemp())
    before = (registry.CATALOG, registry._functions)
    try:
        d = tmp / folder
        d.mkdir(parents=True)
        (d / "manifest.yaml").write_text(body)
        registry.CATALOG, registry._functions = tmp, None
        return registry.get(folder) is not None
    finally:
        registry.CATALOG, registry._functions = before
        shutil.rmtree(tmp, ignore_errors=True)


def check_a_good_manifest_loads() -> None:
    """Without this the refusals prove only that the loader is broken."""
    ok("a valid manifest loads", loads(CONTROL.format(id="probe", extra="")))


def check_a_feature_must_name_code_that_exists() -> None:
    """A manifest is data; the thing it points at has to be registered.

    Named by key and never by import path, so a manifest cannot make Compass
    import an arbitrary module.
    """
    ok("a feature naming an unregistered handler is refused",
       not loads(CONTROL.format(id="probe", extra="")
                 .replace("handler: form26", "handler: nope")))
    ok("both shipped handlers are registered",
       set(features.keys()) >= {"form26", "lms"}, str(features.keys()))


def check_a_feature_cannot_reach_past_its_function() -> None:
    """The two-layer rule: narrow the function's grant, never widen it."""
    ok("a feature asking for a withheld tool is refused",
       not loads(CONTROL.format(id="probe", extra="")
                 .replace("    tools: [read_rows]\n", "    tools: [bash]\n")))
    ok("a feature asking for a tool its function never granted is refused",
       not loads(CONTROL.format(id="probe", extra="")
                 .replace("    tools: [read_rows]\n", "    tools: [explain_row]\n")))
    ok("a function cannot grant a withheld tool either",
       not loads(CONTROL.format(id="probe", extra="")
                 .replace("tools: [read_rows]\n", "tools: [read_rows, bash]\n", 1)))


def check_a_scope_must_be_one_compass_can_apply() -> None:
    """A scope nobody can apply is a filter that silently shows everything."""
    ok("an unknown scope is refused",
       not loads(CONTROL.format(id="probe", extra="")
                 .replace("scope: [entity, period]", "scope: [whatever]")))


def check_prompt_bound_text_is_treated_as_data() -> None:
    """Names and starters reach a system prompt, so markup is refused."""
    ok("markup in a name is refused",
       not loads(CONTROL.format(id="probe", extra="")
                 .replace("name: Probe", 'name: "Probe <b>x</b>"')))
    ok("markup in a starter is refused",
       not loads(CONTROL.format(id="probe", extra="")
                 + '\nstarters:\n  - "<script>x</script>"\n'))


def check_mistakes_that_would_pass_silently_do_not() -> None:
    ok("an id that disagrees with its folder is refused",
       not loads(CONTROL.format(id="mismatch", extra="")))
    ok("two features sharing an id is refused",
       not loads(CONTROL.format(id="probe", extra=(
           "  - id: ok_feature\n    name: Dup\n    short: D\n    blurb: D.\n"
           "    handler: lms\n"))))
    ok("two actions sharing an id is refused",
       not loads(CONTROL.format(id="probe", extra="") .replace(
           "    tools: [read_rows]\n",
           "    tools: [read_rows]\n    actions:\n"
           "      - id: a\n        label: A\n        confirm: A.\n"
           "      - id: a\n        label: B\n        confirm: B.\n")))
    ok("an unknown key is refused rather than ignored",
       not loads(CONTROL.format(id="probe", extra="") + "\nretention: 12\n"))
    ok("something that is not YAML at all is refused", not loads("}{ not yaml :"))


def check_the_shipped_catalog_is_clean() -> None:
    ids = sorted(fn.id for fn in registry.all_functions())
    ok("the shipped catalog loads", ids == ["finance", "talent"], str(ids))

    fin, tal = registry.get("finance"), registry.get("talent")
    ok("finance holds form26", fin is not None and fin.feature("form26") is not None)
    ok("talent holds lms", tal is not None and tal.feature("lms") is not None)
    # The argument for manifests: nothing in the code knows that a
    # reconciliation is scoped by period and leave by reporting line.
    ok("the two functions scope their features differently",
       fin is not None and tal is not None
       and fin.feature("form26").scope != tal.feature("lms").scope)
    ok("leave approval is marked outward and irreversible",
       (a := tal.feature("lms").actions[0]) and a.outward and not a.reversible)

    every = {t for fn in registry.all_functions() for t in fn.tools}
    every |= {t for fn in registry.all_functions()
              for f in fn.features for t in f.tools}
    ok("no shipped manifest asks for a withheld tool",
       not (every & WITHHELD_TOOLS), str(sorted(every & WITHHELD_TOOLS)))


def check_form26_arithmetic() -> None:
    """The figures are subtraction, and they move when the rows do.

    Three short lines in the fixture:
       0044  520000 − 380000 = 140000
       0051  410000 − 290000 = 120000
       0058  730000 − 510000 = 220000
                               ------
                               480000  = ₹4.8L
    """
    _, feat, h = registry.feature_of("finance", "form26")
    s = Scope(user="mk", entity="Contoso India", period="Q2 FY25")
    fig = {f.key: f.value for f in h.figures(s)}
    ok("three mismatches are counted", fig["mismatches"] == "3", str(fig))
    ok("the amount at risk is the sum of the three differences",
       fig["at_risk"] == "₹4.8L", fig["at_risk"])

    short = [r["line"] for r in h.rows(s, "mismatches")]
    ok("the mismatch tab holds exactly those three",
       short == ["0044", "0051", "0058"], str(short))
    ok("a row carries its difference",
       h.rows(s, "mismatches")[0]["difference"] == 140000)

    out = h.act(s, "accept", short)
    ok("accepting three lines succeeds", out.ok, out.said)
    ok("the figures move with the rows",
       {f.key: f.value for f in h.figures(s)}["mismatches"] == "0")
    ok("undo puts them back", h.act(s, "undo", short).ok
       and {f.key: f.value for f in h.figures(s)}["mismatches"] == "3")


def check_a_rule_speaks_only_when_it_applies() -> None:
    """Both rules exist in the manifest; at most one is true at a time."""
    _, feat, h = registry.feature_of("finance", "form26")
    declared = {r.id for r in feat.rules}
    ok("both rules are declared", declared == {"prepared_by_you", "period_closed"},
       str(declared))

    open_period = Scope(user="mk", entity="Contoso India", period="Q2 FY25")
    closed = Scope(user="mk", entity="Contoso India", period="Q4 FY24")
    ok("an open period prepared by you says so",
       h.rules(open_period) == ["prepared_by_you"], str(h.rules(open_period)))
    ok("a closed period says only that", h.rules(closed) == ["period_closed"],
       str(h.rules(closed)))
    ok("someone who did not prepare it is told nothing",
       h.rules(Scope(user="pr", entity="Contoso India", period="Q2 FY25")) == [])
    ok("a closed period refuses the action itself",
       not h.act(closed, "accept", ["0044"]).ok)


def check_an_outward_action_is_taken_one_at_a_time() -> None:
    """Approving leave notifies a person and books days. It cannot be bulked.

    The refusal is the feature, not a limitation: a proposal covering the
    whole queue is exactly what somebody should have to restate request by
    request.
    """
    _, feat, h = registry.feature_of("talent", "lms")
    s = Scope(user="mk")
    ok("approving three at once is refused",
       not h.act(s, "approve", ["a1", "a2", "a3"]).ok)
    out = h.act(s, "approve", ["a1"])
    ok("approving one succeeds and names the person", out.ok and "Arjun" in out.said,
       out.said)
    ok("approving it twice is refused", not h.act(s, "approve", ["a1"]).ok)
    ok("an unknown action is refused, not raised",
       not h.act(s, "teleport", ["a2"]).ok)


def check_nothing_is_mounted_while_the_flag_is_off() -> None:
    from compass.common.config import get_settings

    ok("business functions are off by default",
       not get_settings().business_functions.enabled)
    ok("a bad id is None rather than an error", registry.get("no-such-function") is None)
    ok("a bad feature id is three Nones",
       registry.feature_of("finance", "nope") == (registry.get("finance"), None, None))


def check_a_proposal_changes_nothing_until_confirmed() -> None:
    """The gap between deciding and acting is the feature.

    A single call that both resolved the sentence and performed it would be
    this same design with the safety taken out.
    """
    _, feat, h = registry.feature_of("finance", "form26")
    s = Scope(user="mk", entity="Contoso India", period="Q2 FY25")
    before = {f.key: f.value for f in h.figures(s)}["mismatches"]

    r = assistant.interpret("finance", feat, h, s,
                            "Accept the lower credit on all three", "mk")
    ok("a sentence becomes a proposal", isinstance(r, assistant.Proposal),
       type(r).__name__)
    ok("the proposal names the rows it would touch",
       [t.id for t in r.targets] == ["0044", "0051", "0058"],
       str([t.id for t in r.targets]))
    ok("its wording is the manifest's, not generated",
       r.detail == feat.actions[0].confirm)
    ok("nothing moved while it waited",
       {f.key: f.value for f in h.figures(s)}["mismatches"] == before)

    out = assistant.confirm(r.id, "mk", h)
    ok("confirming carries it out", out.ok, out.said)
    ok("the figures moved with it",
       {f.key: f.value for f in h.figures(s)}["mismatches"] == "0")


def check_a_proposal_belongs_to_one_person() -> None:
    """Holding the id is not the same as being allowed to use it.

    The non-owner's attempt must also leave the plan intact — consuming it
    would let anybody cancel somebody else's work by guessing an id.
    """
    _, feat, h = registry.feature_of("finance", "form26")
    s = Scope(user="mk", entity="Contoso India", period="Q2 FY25")
    r = assistant.interpret("finance", feat, h, s, "accept all", "mk")

    ok("somebody else cannot confirm it", not assistant.confirm(r.id, "pr", h).ok)
    ok("somebody else cannot cancel it", not assistant.cancel(r.id, "pr").ok)
    ok("their attempt did not consume it",
       assistant.pending(r.id, "mk") is not None)
    ok("the owner can still confirm it", assistant.confirm(r.id, "mk", h).ok)


def check_a_proposal_is_single_use_and_expires() -> None:
    _, feat, h = registry.feature_of("finance", "form26")
    s = Scope(user="mk", entity="Contoso India", period="Q2 FY25")

    r = assistant.interpret("finance", feat, h, s, "accept all", "mk")
    assistant.confirm(r.id, "mk", h)
    ok("confirming twice does nothing the second time",
       not assistant.confirm(r.id, "mk", h).ok)

    # Back to undecided, so the next two steps have rows to act on. The
    # wrapper restores at the end of the check; this is mid-check.
    h.act(s, "undo", ["0044", "0051", "0058"])

    r2 = assistant.interpret("finance", feat, h, s, "accept all", "mk")
    ok("cancelling says plainly that nothing changed",
       assistant.cancel(r2.id, "mk").said == "Cancelled — nothing changed.")
    ok("a cancelled plan cannot then be confirmed",
       not assistant.confirm(r2.id, "mk", h).ok)

    # Expiry is checked by ageing the record rather than by waiting ten
    # minutes: the clock is the thing under test, not asyncio.
    r3 = assistant.interpret("finance", feat, h, s, "accept all", "mk")
    stale = dataclasses.replace(
        r3, made_at=time.time() - assistant.TTL_SECONDS - 1)
    assistant._pending[r3.id] = stale
    ok("a plan left too long expires", stale.expired)
    ok("an expired plan cannot be confirmed",
       not assistant.confirm(r3.id, "mk", h).ok)
    ok("expired plans are swept", assistant.open_count() == 0,
       str(assistant.open_count()))


def check_an_ambiguous_sentence_is_not_guessed() -> None:
    """An assistant that picks the likeliest row is wrong exactly when it hurts."""
    _, feat, h = registry.feature_of("finance", "form26")
    s = Scope(user="mk", entity="Contoso India", period="Q2 FY25")

    vague = assistant.interpret("finance", feat, h, s, "accept it", "mk")
    ok("'accept it' with three candidates asks which",
       isinstance(vague, assistant.Clarify), type(vague).__name__)
    ok("the clarification offers the candidates",
       isinstance(vague, assistant.Clarify) and len(vague.options) == 3)

    miscount = assistant.interpret("finance", feat, h, s, "accept all five", "mk")
    ok("a stated count that disagrees with the screen asks rather than acts",
       isinstance(miscount, assistant.Clarify)
       and "3 here" in miscount.text, getattr(miscount, "text", ""))

    named = assistant.interpret("finance", feat, h, s, "accept line 0044", "mk")
    ok("an explicit line number is unambiguous",
       isinstance(named, assistant.Proposal)
       and [t.id for t in named.targets] == ["0044"])
    assistant.cancel(named.id, "mk")


def check_an_outward_action_is_never_bulk_proposed() -> None:
    """The person should never be shown a button that cannot work.

    The handler refuses a bulk approval anyway; refusing at proposal time
    means nobody reads a plan covering four people and then finds out.
    """
    _, feat, h = registry.feature_of("talent", "lms")
    s = Scope(user="mk")
    r = assistant.interpret("talent", feat, h, s, "approve all of them", "mk")
    ok("approving everybody is not proposed", isinstance(r, assistant.Clarify),
       type(r).__name__)
    ok("it says why, and offers the names",
       isinstance(r, assistant.Clarify) and "one at a time" in r.text
       and len(r.options) == 3)

    one = assistant.interpret("talent", feat, h, s, "approve Arjun's request", "mk")
    ok("naming one person is proposed", isinstance(one, assistant.Proposal))
    ok("it is marked outward and irreversible",
       isinstance(one, assistant.Proposal) and one.outward and not one.reversible)
    out = assistant.confirm(one.id, "mk", h)
    ok("confirming it names who was told", out.ok and "Arjun" in out.said, out.said)


def check_a_question_is_answered_from_facts() -> None:
    """The model phrases; it does not choose rows and it does not do sums.

    An Answer carries what was read off the feature, so a phrasing layer
    cannot introduce a figure that was never in the data.
    """
    _, feat, h = registry.feature_of("finance", "form26")
    s = Scope(user="mk", entity="Contoso India", period="Q2 FY25")
    a = assistant.interpret("finance", feat, h, s, "why was line 0044 flagged?", "mk")
    ok("a question is an answer, not a proposal", isinstance(a, assistant.Answer),
       type(a).__name__)
    ok("it carries the figures it would be phrased from",
       isinstance(a, assistant.Answer) and a.facts.get("figures"))
    ok("and the rows", isinstance(a, assistant.Answer) and a.facts.get("rows"))
    ok("asking changed nothing",
       {f.key: f.value for f in h.figures(s)}["mismatches"] == "3")


def check_the_rail_says_what_it_can_read() -> None:
    """The scope chip is the visible half of the scoping rule."""
    fin = registry.get("finance")
    _, feat, _h = registry.feature_of("finance", "form26")

    over = assistant.view(fin, None, Scope(user="mk"))
    ok("at the overview the rail scopes to the function",
       over.scope_label == "Finance" and not over.can_act, over.scope_label)
    ok("and offers the function's starters",
       over.starters == fin.starters)

    inside = assistant.view(fin, feat,
                            Scope(user="mk", entity="Contoso India", period="Q2 FY25"))
    ok("inside a feature it scopes to the feature",
       inside.scope_label == "Form 26" and inside.can_act)
    ok("and names the entity and period chosen",
       inside.subtitle == "Contoso India · Q2 FY25", inside.subtitle)


def check_every_starter_the_manifest_offers_actually_works() -> None:
    """A suggestion that does nothing when clicked is worse than no suggestion."""
    unresolved = []
    for fn in registry.all_functions():
        for feature in fn.features:
            _, fm, h = registry.feature_of(fn.id, feature.id)
            s = Scope(user="mk", entity="Contoso India", period="Q2 FY25")
            for starter in fm.starters:
                r = assistant.interpret(fn.id, fm, h, s, starter, "mk")
                if isinstance(r, assistant.Proposal):
                    assistant.cancel(r.id, "mk")
                elif not isinstance(r, (assistant.Answer, assistant.Clarify)):
                    unresolved.append(f"{fn.id}/{feature.id}: {starter}")
    ok("every feature starter resolves to something", not unresolved,
       str(unresolved))
    ok("no plans were left open", assistant.open_count() == 0,
       str(assistant.open_count()))


def main() -> int:
    print("business functions\n")
    check_a_good_manifest_loads()
    check_a_feature_must_name_code_that_exists()
    check_a_feature_cannot_reach_past_its_function()
    check_a_scope_must_be_one_compass_can_apply()
    check_prompt_bound_text_is_treated_as_data()
    check_mistakes_that_would_pass_silently_do_not()
    check_the_shipped_catalog_is_clean()
    with fixtures():
        check_form26_arithmetic()
    with fixtures():
        check_a_rule_speaks_only_when_it_applies()
    with fixtures():
        check_an_outward_action_is_taken_one_at_a_time()
    with fixtures():
        check_a_proposal_changes_nothing_until_confirmed()
    with fixtures():
        check_a_proposal_belongs_to_one_person()
    with fixtures():
        check_a_proposal_is_single_use_and_expires()
    with fixtures():
        check_an_ambiguous_sentence_is_not_guessed()
    with fixtures():
        check_an_outward_action_is_never_bulk_proposed()
    with fixtures():
        check_a_question_is_answered_from_facts()
    check_the_rail_says_what_it_can_read()
    with fixtures():
        check_every_starter_the_manifest_offers_actually_works()
    check_nothing_is_mounted_while_the_flag_is_off()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED")
        for failure in FAILURES:
            print(f"   {failure}")
        return 1
    print("a manifest cannot claim a power it was not granted, a plan changes "
          "nothing until its owner confirms it, and the model only phrases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
