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

from compass.businessfunctions import (  # noqa: E402
    assistant,
    features,
    notices,
    registry,
)
from compass.businessfunctions.features.base import Scope  # noqa: E402
from compass.businessfunctions.features import form26 as form26_mod  # noqa: E402
from compass.businessfunctions.features import lms as lms_mod  # noqa: E402
from compass.businessfunctions.features import rewardlens as rl_mod  # noqa: E402
from compass.businessfunctions.manifest import (  # noqa: E402
    WITHHELD_TOOLS,
    FeatureManifest,
    Form,
    FormField,
)
from compass.common.config import (  # noqa: E402
    BusinessFunctionSettings,
    get_settings,
)

#: The default as SHIPPED, read off the model rather than off this machine.
#: `get_settings()` would answer with whatever the developer's own .env says —
#: and once you have turned the flag on locally that reads "on", which is a
#: fact about your laptop and not about what anybody else gets.
SHIPPED_DEFAULT = BusinessFunctionSettings.model_fields["enabled"].default

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
    before = (copy.deepcopy(form26_mod._FIXTURE), copy.deepcopy(lms_mod._FIXTURE),
              copy.deepcopy(rl_mod._ITEMS), set(rl_mod._REFERRED),
              set(rl_mod._EXCEPTED), copy.deepcopy(rl_mod._REVIEWED),
              copy.deepcopy(rl_mod._ANSWERS), dict(rl_mod._SUPERSEDED),
              dict(notices._outbox))
    try:
        yield
    finally:
        form26_mod._FIXTURE[:] = before[0]
        lms_mod._FIXTURE[:] = before[1]
        rl_mod._ITEMS[:] = before[2]
        rl_mod._REFERRED.clear(); rl_mod._REFERRED.update(before[3])
        rl_mod._EXCEPTED.clear(); rl_mod._EXCEPTED.update(before[4])
        rl_mod._REVIEWED.clear(); rl_mod._REVIEWED.update(before[5])
        rl_mod._ANSWERS.clear(); rl_mod._ANSWERS.update(before[6])
        rl_mod._SUPERSEDED.clear(); rl_mod._SUPERSEDED.update(before[7])
        notices._outbox.clear(); notices._outbox.update(before[8])


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
    ok("the shipped catalog loads", ids == ["finance", "scs", "talent"], str(ids))

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
    ok("business functions are off by default, however this machine is set up",
       not SHIPPED_DEFAULT)
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


def _client():
    """A test client with the module switched on.

    The flag is set on the settings object rather than in the environment
    because server.py reads it at import time, and this has to be true before
    that import happens.
    """
    get_settings().business_functions.enabled = True
    from fastapi.testclient import TestClient

    from compass.api.server import app

    return TestClient(app)


def check_the_routes_are_mounted_only_when_switched_on() -> None:
    """The escape hatch, measured rather than asserted."""
    import importlib

    import compass.api.server as server

    get_settings().business_functions.enabled = False
    off = {r.path for r in importlib.reload(server).app.routes}
    ok("switched off, no business-function route exists",
       not [p for p in off if "business" in p], str([p for p in off if "business" in p]))

    get_settings().business_functions.enabled = True
    on = {r.path for r in importlib.reload(server).app.routes}
    # The whole surface, spelled out. A count would have let a route be
    # swapped for another without anybody noticing.
    root = "/v1/business-functions"
    feature = f"{root}/{{function_id}}/features/{{feature_id}}"
    ok("switched on, the surface appears — and is exactly this",
       {p for p in on if "business-functions" in p} == {
           root,
           f"{root}/{{function_id}}",
           feature,
           f"{feature}/act",
           f"{feature}/ask",
           f"{feature}/forms/{{form_id}}",
           f"{feature}/forms/{{form_id}}/preview",
           f"{root}/plans/{{plan_id}}/confirm",
           f"{root}/plans/{{plan_id}}/cancel",
       },
       str(sorted(p for p in on if "business-functions" in p)))
    ok("switching it on adds nothing else",
       (on - off) == {p for p in on if "business-functions" in p})


def check_every_route_is_behind_require_user() -> None:
    """All 136 others are. A new surface is not where that lapses."""
    from compass.common.auth import require_user

    import compass.api.server as server

    naked = []
    for route in server.app.routes:
        if "business-functions" not in getattr(route, "path", ""):
            continue
        deps = [d.call for d in getattr(route, "dependant", None).dependencies] \
            if getattr(route, "dependant", None) else []
        if require_user not in deps:
            naked.append(route.path)
    ok("every business-function route requires a signed-in user",
       not naked, str(sorted(set(naked))))


def check_the_surface_answers() -> None:
    """One pass through the screens the mockup actually has."""
    c = _client()

    r = c.get("/v1/business-functions")
    ok("the switcher lists the functions", r.status_code == 200
       and {f["id"] for f in r.json()["functions"]} == {"finance", "scs", "talent"},
       f"{r.status_code} {r.text[:120]}")

    r = c.get("/v1/business-functions/finance",
              params={"entity": "Contoso India", "period": "Q2 FY25"})
    body = r.json()
    ok("the overview carries the function and its features",
       r.status_code == 200 and body["name"] == "Finance"
       and len(body["features"]) == 1, f"{r.status_code}")
    ok("the overview's queue is built from live figures",
       any(q["feature"] == "form26" for q in body["queue"]), str(body["queue"]))
    ok("the rail scopes to the function at the overview",
       body["rail"]["scope_label"] == "Finance" and not body["rail"]["can_act"])

    r = c.get("/v1/business-functions/nope")
    ok("an unknown function is a 404 that names it",
       r.status_code == 404 and "nope" in r.text, str(r.status_code))
    r = c.get("/v1/business-functions/finance/features/nope")
    ok("an unknown feature is a 404 that names the function too",
       r.status_code == 404 and "Finance" in r.text, r.text[:90])


def check_a_feature_says_what_scope_it_is_waiting_on() -> None:
    """Not an error — the UI has to know which selector to light up."""
    c = _client()
    r = c.get("/v1/business-functions/finance/features/form26")
    body = r.json()
    ok("without an entity and period it asks for them",
       r.status_code == 200 and body.get("needs_scope") == ["entity", "period"],
       str(body)[:140])
    ok("and sends no rows while it waits", "rows" not in body)

    r = c.get("/v1/business-functions/finance/features/form26",
              params={"entity": "Contoso India", "period": "Q2 FY25"})
    body = r.json()
    ok("with both, the workbench answers",
       r.status_code == 200 and len(body["rows"]) == 3, str(body)[:140])
    ok("the figures come with it",
       {f["key"]: f["value"] for f in body["figures"]}["at_risk"] == "₹4.8L")
    # A closed period is closed for everybody, so this holds whoever is
    # signed in. `prepared_by_you` is true only for the preparer and is
    # exercised directly against the handler elsewhere.
    r = c.get("/v1/business-functions/finance/features/form26",
              params={"entity": "Contoso India", "period": "Q4 FY24"})
    ok("only the rules in force are sent",
       [x["id"] for x in r.json()["rules"]] == ["period_closed"],
       str([x["id"] for x in r.json()["rules"]]))
    ok("a rule carries the sentence the manifest wrote, not a generated one",
       r.json()["rules"][0]["headline"] == "Read-only.",
       r.json()["rules"][0]["headline"])


def check_the_rail_round_trip_over_http() -> None:
    """Ask, read the plan, confirm — the whole point, through the API."""
    c = _client()
    scope = {"entity": "Contoso India", "period": "Q2 FY25"}

    r = c.post("/v1/business-functions/finance/features/form26/ask",
               json={"text": "Accept the lower credit on all three", **scope})
    body = r.json()
    ok("a sentence comes back as a plan", body.get("kind") == "plan", str(body)[:140])
    plan = body["plan"]
    ok("the plan names its three rows",
       [t["id"] for t in plan["targets"]] == ["0044", "0051", "0058"])
    ok("and carries the manifest's wording", plan["detail"].startswith("Closes the line"))

    r = c.get("/v1/business-functions/finance/features/form26", params=scope)
    ok("asking changed nothing",
       {f["key"]: f["value"] for f in r.json()["figures"]}["mismatches"] == "3")

    r = c.post(f"/v1/business-functions/plans/{plan['id']}/confirm",
               json={"function_id": "finance", "feature_id": "form26"})
    ok("confirming carries it out", r.json()["ok"], r.text[:120])
    r = c.get("/v1/business-functions/finance/features/form26", params=scope)
    ok("and the workbench has moved",
       {f["key"]: f["value"] for f in r.json()["figures"]}["mismatches"] == "0")

    r = c.post(f"/v1/business-functions/plans/{plan['id']}/confirm",
               json={"function_id": "finance", "feature_id": "form26"})
    ok("confirming twice does nothing the second time", not r.json()["ok"])


def check_asking_vaguely_is_a_question_not_an_act() -> None:
    c = _client()
    scope = {"entity": "Contoso India", "period": "Q2 FY25"}
    r = c.post("/v1/business-functions/finance/features/form26/ask",
               json={"text": "accept it", **scope})
    ok("a vague instruction comes back as a clarification",
       r.json().get("kind") == "clarify", str(r.json())[:120])
    r = c.post("/v1/business-functions/finance/features/form26/ask",
               json={"text": "why was 0044 flagged?", **scope})
    body = r.json()
    ok("a question comes back as facts to phrase",
       body.get("kind") == "answer" and body["facts"].get("rows"), str(body)[:120])


def check_a_row_button_needs_no_plan() -> None:
    """A click on a specific row already said which row."""
    c = _client()
    r = c.post("/v1/business-functions/finance/features/form26/act",
               json={"action": "accept", "targets": ["0044"],
                     "entity": "Contoso India", "period": "Q2 FY25"})
    ok("acting on a pointed-at row works", r.json()["ok"], r.text[:120])
    ok("and says what happened", "accepted" in r.json()["said"], r.json()["said"])

    r = c.post("/v1/business-functions/finance/features/form26/act",
               json={"action": "accept", "targets": ["0044"],
                     "entity": "Contoso India", "period": "Q4 FY24"})
    ok("a closed period refuses even a direct click", not r.json()["ok"],
       r.json()["said"])


def check_the_health_endpoint_advertises_the_section() -> None:
    """The nav asks rather than assuming, as it does for the other three."""
    c = _client()
    body = c.get("/healthz").json()
    ok("health reports the section exists", body.get("business_functions") is True,
       str(body.get("business_functions")))


def check_rewardlens_totals_a_person_not_a_purchase() -> None:
    """The arithmetic, against the worked example in the requirement.

        John:   8,000 + 5,000 + 7,000 = 20,000   over a 15,000 limit by 5,000
        Priya:  5,000 + 4,000 + 7,000 = 16,000   over by 1,000

    Both are written down in the document this was built from, which is why
    they are the fixture: the numbers can be checked against something a
    person wrote rather than against a recorded run.
    """
    _, feat, h = registry.feature_of("scs", "rewardlens")
    s = Scope(user="compliance", period="FY 2026-27")
    by_name = {r["recipient"]: r for r in h.rows(s, "all")}

    john = by_name.get("John Mathew")
    ok("John's three gifts come to 20,000", john and john["total"] == 20000,
       str(john and john["total"]))
    ok("which is 5,000 over the limit", john and john["excess"] == 5000,
       str(john and john["excess"]))
    ok("and he is flagged over", john and john["over_limit"])

    priya = by_name.get("Priya Nair")
    ok("Priya's three come to 16,000", priya and priya["total"] == 16000,
       str(priya and priya["total"]))
    ok("over by 1,000", priya and priya["excess"] == 1000)

    vikram = by_name.get("Vikram Rao")
    ok("somebody inside the limit has headroom, not an excess",
       vikram and vikram["excess"] == 0 and vikram["headroom"] == 12500,
       str(vikram and (vikram["excess"], vikram["headroom"])))

    # A limit that silently omits a category is worse than one that says what
    # it covers. Hospitality is recorded against John and left out of his total.
    ok("hospitality is recorded but not counted",
       john and "hospitality" in john["not_counted"] and john["total"] == 20000,
       str(john and john["not_counted"]))

    ok("the row is a person, not a purchase",
       all("recipient" in r and "items_counted" in r for r in h.rows(s, "all")))


def check_rewardlens_figures_follow_the_rows() -> None:
    _, _feat, h = registry.feature_of("scs", "rewardlens")
    s = Scope(user="compliance", period="FY 2026-27")
    fig = {f.key: f for f in h.figures(s)}
    rows = h.rows(s, "all")
    over = [r for r in rows if r["over_limit"]]

    ok("the recipient count is the number of people", fig["recipients"].value == str(len(rows)))
    ok("the over-limit figure matches the rows and reads as bad",
       fig["over"].value == str(len(over)) and fig["over"].tone == "bad",
       f"{fig['over'].value}/{len(over)} {fig['over'].tone}")
    ok("the recorded total is the sum of the per-person totals",
       fig["recorded"].value == rl_mod.rupees(sum(r["total"] for r in rows)),
       fig["recorded"].value)
    ok("the over tab holds exactly the over-limit people",
       len(h.rows(s, "over")) == len(over))


def check_nobody_clears_their_own_breach() -> None:
    """The one rule a system like this exists to enforce.

    Their row is still shown — hiding it would be worse, and the total has to
    be right — but every action against it is refused, in code rather than by
    convention.
    """
    _, feat, h = registry.feature_of("scs", "rewardlens")
    mine = Scope(user="mk", period="FY 2026-27")
    others = Scope(user="compliance", period="FY 2026-27")

    ok("a reviewer who is also a recipient is told so",
       "you_are_a_recipient" in h.rules(mine), str(h.rules(mine)))
    ok("a reviewer who is not, is not told",
       "you_are_a_recipient" not in h.rules(others))
    ok("their own row is still in the list",
       any(r["employee_id"] == "E-1041" for r in h.rows(mine, "all")))

    ok("they cannot refer themselves",
       not h.act(mine, "refer_to_finance", ["E-1041"]).ok)
    ok("they cannot except themselves",
       not h.act(mine, "record_exception", ["E-1041"]).ok)
    ok("they can still act on somebody else",
       h.act(mine, "refer_to_finance", ["E-1007"]).ok)
    ok("and somebody else can act on them",
       h.act(others, "refer_to_finance", ["E-1041"]).ok)


def check_a_referral_is_one_person_at_a_time() -> None:
    _, _feat, h = registry.feature_of("scs", "rewardlens")
    s = Scope(user="compliance", period="FY 2026-27")

    ok("two at once is refused",
       not h.act(s, "refer_to_finance", ["E-1007", "E-1019"]).ok)
    ok("somebody inside the limit is refused, and told the headroom",
       not (o := h.act(s, "refer_to_finance", ["E-1066"])).ok and "inside the limit" in o.said,
       o.said)
    first = h.act(s, "refer_to_finance", ["E-1007"])
    ok("one over the limit succeeds, naming the figures",
       first.ok and "₹20,000" in first.said and "₹5,000" in first.said, first.said)
    ok("referring the same person twice is refused",
       not h.act(s, "refer_to_finance", ["E-1007"]).ok)
    ok("chasing somebody with nothing outstanding is refused",
       not h.act(s, "chase_acknowledgement", ["E-1019"]).ok)


def check_a_signed_off_year_is_closed_to_everyone() -> None:
    _, _feat, h = registry.feature_of("scs", "rewardlens")
    closed = Scope(user="compliance", period="FY 2024-25")
    ok("a signed-off year says only that", h.rules(closed) == ["period_closed"],
       str(h.rules(closed)))
    ok("and refuses the action itself",
       not h.act(closed, "refer_to_finance", ["E-1007"]).ok)


def check_the_rail_can_work_rewardlens() -> None:
    """The assistant reaches it the same way it reaches the other two."""
    fn, feat, h = registry.feature_of("scs", "rewardlens")
    s = Scope(user="compliance", period="FY 2026-27")

    r = assistant.interpret("scs", feat, h, s, "Refer John to Finance", "compliance")
    ok("naming one person becomes a plan", isinstance(r, assistant.Proposal),
       type(r).__name__)
    ok("the plan names only that person",
       isinstance(r, assistant.Proposal)
       and [t.id for t in r.targets] == ["E-1007"],
       str(isinstance(r, assistant.Proposal) and [t.id for t in r.targets]))
    ok("it is marked outward and irreversible",
       isinstance(r, assistant.Proposal) and r.outward and not r.reversible)
    out = assistant.confirm(r.id, "compliance", h)
    ok("confirming it refers them", out.ok and "Finance" in out.said, out.said)

    everyone = assistant.interpret("scs", feat, h, s, "refer all of them", "compliance")
    ok("referring everybody is not proposed",
       isinstance(everyone, assistant.Clarify), type(everyone).__name__)

    unresolved = [st for st in feat.starters
                  if not isinstance(
                      assistant.interpret("scs", feat, h, s, st, "compliance"),
                      (assistant.Proposal, assistant.Answer, assistant.Clarify))]
    ok("every RewardLens starter resolves", not unresolved, str(unresolved))


def check_every_feature_can_be_named() -> None:
    """Every shipped feature's rows can be named in a sentence.

    This is the check that was missing when RewardLens was added: the rail
    fell back to a row key RewardLens does not have, every target came back
    with a blank id, and the symptom was "the assistant says 'which one do
    you mean' and lists nothing". The fallback now raises; this makes sure
    nothing ships that would hit it.
    """
    for fn in registry.all_functions():
        for feat in fn.features:
            h = features.get(feat.handler)
            s = Scope(user="checker",
                      entity="Acme Manufacturing" if "entity" in feat.scope else None,
                      period="FY 2026-27" if "period" in feat.scope else None)
            tabs = h.tabs(s)
            rows = h.rows(s, tabs[0].key) if tabs else []
            named = [assistant._label_for(feat.id, r) for r in rows]
            ok(f"{fn.id}/{feat.id}: every row has an id and words to say it by",
               rows and all(i and l for i, l in named),
               f"{len(rows)} rows, {sum(1 for i, l in named if not (i and l))} unnamed")


def check_a_selector_is_never_offered_the_wrong_values() -> None:
    """Every dimension a person must choose offers values, and says why.

    `entity` and `period` are selectors somebody has to set; `team` and `self`
    are decided by who is signed in and have nothing to pick. So the first two
    need values and the last two must not have any — and the values have to be
    the feature's own, which is the fault this check was written for: the UI
    held one list of quarters for the whole section, and RewardLens, whose
    limit is annual, was offered a quarter to total a year against.
    """
    PICKABLE = {"entity", "period"}
    for fn in registry.all_functions():
        for feat in fn.features:
            need = [d for d in feat.scope if d in PICKABLE]
            for dim in need:
                ok(f"{fn.id}/{feat.id}: there is something to pick for {dim}",
                   feat.choices.get(dim), str(feat.choices.get(dim)))
            ok(f"{fn.id}/{feat.id}: offers nothing for what it is not scoped by",
               not (set(feat.choices) - set(feat.scope)), str(sorted(feat.choices)))
            if need:
                ok(f"{fn.id}/{feat.id}: says why the screen waits",
                   len(feat.scope_why) > 20, feat.scope_why)

    _, form26, _h = registry.feature_of("finance", "form26")
    _, rl, _h2 = registry.feature_of("scs", "rewardlens")
    ok("a quarterly reconciliation and an annual limit do not share a period list",
       set(form26.choices["period"]).isdisjoint(rl.choices["period"]),
       f"{form26.choices['period']} vs {rl.choices['period']}")
    ok("RewardLens is scoped by years",
       all(p.startswith("FY ") for p in rl.choices["period"]),
       str(rl.choices["period"]))

    bad = FeatureManifest(id="x", name="X", short="x", blurb="x", handler="lms",
                          scope=["period"], choices={"entity": ["Contoso"]})
    ok("choices for a dimension it is not scoped by are refused",
       any("not scoped by" in why
           for why in bad.problems(registry.get("talent"))),
       str(bad.problems(registry.get("talent"))))


def check_every_row_can_be_pointed_at() -> None:
    """A row's identity is declared, present and unique — on every tab.

    Two faults wore this shape before the check existed: the rail guessed a
    row's id from a list of field names, and so did the table. Both fell
    through for RewardLens, whose rows are keyed by person, and both failed
    silently — one offered a clarification with blank options, the other gave
    three different people the same empty id. Neither typecheck nor any
    backend check caught either; only opening the screen did.
    """
    for fn in registry.all_functions():
        for feat in fn.features:
            h = features.get(feat.handler)
            s = Scope(user="checker",
                      entity="Contoso India" if "entity" in feat.scope else None,
                      period=(feat.choices.get("period") or [None])[0]
                      if "period" in feat.scope else None)
            key = h.row_key
            ok(f"{fn.id}/{feat.id}: declares which field names a row",
               bool(key), repr(key))
            for tab in h.tabs(s):
                rows = h.rows(s, tab.key)
                # A tab may hold a different kind of thing and name its own key.
                key = tab.key_field or h.row_key
                ids = [str(r.get(key, "")) for r in rows]
                ok(f"{fn.id}/{feat.id}/{tab.key}: every row carries {key}",
                   all(ids), f"{sum(1 for i in ids if not i)} of {len(ids)} blank")
                ok(f"{fn.id}/{feat.id}/{tab.key}: no two rows share it",
                   len(set(ids)) == len(ids), str(ids))

    # And the id the table sends is the id the handler acts on, which is the
    # half that would otherwise only break at the moment somebody clicks.
    _, _f, h = registry.feature_of("scs", "rewardlens")
    s = Scope(user="compliance", period="FY 2026-27")
    row = h.rows(s, "over")[0]
    ok("an action aimed with the declared key reaches the row",
       h.act(s, "refer_to_finance", [row[h.row_key]]).ok)
    ok("and an empty target reaches nothing",
       not h.act(s, "refer_to_finance", [""]).ok)


def check_a_form_and_its_code_agree() -> None:
    """A declared field the handler ignores is worse than a missing one.

    The person believes they declared it. So the catalog refuses to load a
    function whose form asks for anything the handler does not understand,
    and this is that refusal, exercised rather than assumed.
    """
    for fn in registry.all_functions():
        for feat in fn.features:
            h = features.get(feat.handler)
            for form in feat.forms:
                declared = {x.id for x in form.fields}
                ok(f"{fn.id}/{feat.id}/{form.id}: the code understands every field",
                   declared == h.accepts(form.id),
                   f"form {sorted(declared)} vs code {sorted(h.accepts(form.id))}")

    bad = FeatureManifest(
        id="x", name="X", short="x", blurb="x", handler="rewardlens",
        scope=["period"], choices={"period": ["FY 2026-27"]},
        scope_why="because the limit is annual and a year is the unit",
        forms=[Form(id="disclose", label="Declare", blurb="b",
                    submit_label="Record it", confirm="c",
                    fields=[FormField(id="what", label="What"),
                            FormField(id="nickname", label="Nickname")])],
    )
    handler = features.get("rewardlens")
    ok("a form asking for a field nobody coded is a field the code would drop",
       {x.id for x in bad.forms[0].fields} != handler.accepts("disclose"))

    ok("a choice with nothing to choose is refused",
       any("nothing to choose" in why for why in Form(
           id="f", label="L", blurb="b", submit_label="S", confirm="c",
           fields=[FormField(id="k", label="K", kind="choice")]).problems()))
    ok("a form with no fields is refused",
       any("no fields" in why for why in Form(
           id="f", label="L", blurb="b", submit_label="S",
           confirm="c").problems()))


def check_a_declaration_is_about_yourself() -> None:
    """Who it is recorded against comes from the session and nowhere else.

    There is no field for whose record to write to, and no handler argument
    for it either: the only name in play is `scope.user`, which the route
    takes from the signed-in person.
    """
    _, feat, h = registry.feature_of("scs", "rewardlens")
    form = feat.forms[0]
    ok("the form has no field naming somebody else",
       not ({x.id for x in form.fields} & {"employee", "employee_id", "recipient",
                                           "on_behalf_of", "person"}),
       str(sorted(x.id for x in form.fields)))

    entry = {"what": "Vendor hamper", "kind": "gift", "value": "4000",
             "given": "2026-12-02", "source": "Acme Logistics"}

    # admin is Aisha Khan at 12,000; enduser is Vikram Rao at 2,500. The same
    # values recorded by two people land on two different years.
    before = {r["employee_id"]: r["total"] for r in
              h.rows(Scope(user="compliance", period="FY 2026-27"), "all")}
    h.submit(Scope(user="admin", period="FY 2026-27"), "disclose", entry)
    after = {r["employee_id"]: r["total"] for r in
             h.rows(Scope(user="compliance", period="FY 2026-27"), "all")}
    ok("it lands on the declarer's own record",
       after["E-1052"] == before["E-1052"] + 4000, f"{before.get('E-1052')} -> {after.get('E-1052')}")
    ok("and on nobody else's",
       all(after[k] == v for k, v in before.items() if k != "E-1052"))

    ok("somebody Compass cannot place is told so, not given an id",
       not (o := h.preview(Scope(user="stranger", period="FY 2026-27"),
                           "disclose", entry)).ok
       and "which employee record" in o.said, o.said)


def check_the_preview_is_the_number_nobody_could_see() -> None:
    """The point of previewing: ₹4,000 is not the figure that matters.

    Aisha Khan is at ₹12,000 of ₹15,000. A ₹4,000 hamper is unremarkable on
    its own and is the one that takes her over, and she cannot know that
    from the hamper.
    """
    _, _f, h = registry.feature_of("scs", "rewardlens")
    admin = Scope(user="admin", period="FY 2026-27")
    entry = {"what": "Vendor hamper", "kind": "gift", "value": "4000",
             "given": "2026-12-02", "source": "Acme Logistics"}

    said = h.preview(admin, "disclose", entry).said
    ok("the preview gives the resulting total, not the entry",
       "₹16,000" in said and "₹15,000" in said, said)
    ok("and says that this is the one that crosses",
       "takes you over" in said, said)
    ok("previewing changed nothing",
       [r["total"] for r in h.rows(admin, "all") if r["employee_id"] == "E-1052"] == [12000])

    quiet = h.preview(Scope(user="enduser", period="FY 2026-27"), "disclose", entry)
    ok("somebody with room is told the room, not warned",
       quiet.ok and "inside the limit" in quiet.said, quiet.said)

    later = h.preview(admin, "disclose", dict(entry, kind="hospitality"))
    ok("a later-phase kind is recorded and says it is not counted",
       later.ok and "not counted" in later.said and "₹12,000" in later.said,
       later.said)


def check_a_declaration_cannot_be_nonsense() -> None:
    _, _f, h = registry.feature_of("scs", "rewardlens")
    s = Scope(user="admin", period="FY 2026-27")
    base = {"what": "Vendor hamper", "kind": "gift", "value": "4000",
            "given": "2026-12-02", "source": "Acme Logistics"}

    def refused(**over) -> str:
        out = h.preview(s, "disclose", dict(base, **over))
        return "" if out.ok else out.said

    ok("a value that is not a number is refused", refused(value="eight thousand"))
    ok("a value of nothing is refused", refused(value="0"))
    ok("a negative value is refused", refused(value="-500"))
    ok("an absurd value is refused rather than recorded", refused(value="99999999"))
    ok("a date outside the year is refused, and the year is named",
       "FY 2026-27" in refused(given="2025-06-01"))
    ok("a date in the wrong format is refused", refused(given="02/12/2026"))
    ok("a kind nobody recognises is refused", refused(kind="bribe"))
    ok("no description is refused", refused(what="  "))
    ok("no giver is refused", refused(source=""))
    ok("a comma and a rupee sign are accepted, not refused",
       h.preview(s, "disclose", dict(base, value="₹4,000")).ok)

    ok("a signed-off year takes no declarations",
       not h.preview(Scope(user="admin", period="FY 2024-25"),
                     "disclose", base).ok)

    # Submitting twice is the ordinary double-click, and the second one is a
    # refusal rather than a second gift.
    ok("the first one records", h.submit(s, "disclose", base).ok)
    ok("the same one again is refused, saying what it found",
       not (o := h.submit(s, "disclose", base)).ok and "already recorded" in o.said,
       o.said)


def check_what_was_declared_is_what_was_recorded() -> None:
    """A declaration is a statement, so it is kept as made and not improved."""
    _, _f, h = registry.feature_of("scs", "rewardlens")
    s = Scope(user="enduser", period="FY 2026-27")
    entry = {"what": "Cricket tickets", "kind": "voucher", "value": "1,500",
             "given": "2026-09-30", "source": "Vendor · Northwind"}
    ok("it records", h.submit(s, "disclose", entry).ok)

    item = next(i for i in rl_mod._ITEMS if i["what"] == "Cricket tickets")
    ok("the words are the person's own", item["source"] == "Vendor · Northwind")
    ok("the value is whole rupees", item["value"] == 1500)
    ok("it is marked as disclosed, not procured", item["channel"] == "disclosed")
    ok("declaring it is acknowledging it", item["acknowledged"] is True)
    ok("it is against the declarer", item["employee_id"] == "E-1066")
    ok("it has an id of its own", item["id"].startswith("D-"))

    row = next(r for r in h.rows(s, "all") if r["employee_id"] == "E-1066")
    ok("it counts towards the total from the moment it is recorded",
       row["total"] == 4000, str(row["total"]))
    ok("and it shows on the self-disclosed tab",
       any(r["employee_id"] == "E-1066" for r in h.rows(s, "disclosed")))


def check_reviewing_never_changes_what_was_received() -> None:
    """The invariant the whole workflow stands on.

    A review decides whether the record stands, whether to ask the declarer
    something, or whether what they were given was not permitted. It does
    not decide whether it counts. A total that moved because a reviewer
    disagreed would be a total worth arguing with, and would give everybody
    a reason to contest rather than declare.
    """
    _, _f, h = registry.feature_of("scs", "rewardlens")
    c = Scope(user="compliance", period="FY 2026-27")

    def totals() -> dict[str, int]:
        return {r["employee_id"]: r["total"] for r in h.rows(c, "all")}

    before = totals()
    waiting = h.rows(c, "disclosed")
    ok("there are declarations waiting to review", len(waiting) >= 2, str(len(waiting)))

    ok("accepting one leaves every total alone",
       h.act(c, "accept_disclosure", [waiting[0]["id"]]).ok and totals() == before)
    ok("querying one leaves every total alone",
       h.act(c, "query_disclosure", [waiting[1]["id"]],
             "The value looks like the whole dinner.").ok and totals() == before)

    # And the one somebody would most expect to remove it. A fresh
    # declaration, so the breach is tested on something nobody has decided.
    ok("a new declaration arrives",
       h.submit(Scope(user="enduser", period="FY 2026-27"), "disclose", {
           "what": "Cricket tickets", "kind": "voucher", "value": "1500",
           "given": "2026-09-30", "source": "Vendor · Northwind"}).ok)
    with_it = totals()
    fresh = next(r for r in h.rows(c, "disclosed") if r["what"] == "Cricket tickets")
    ok("it counts the moment it is recorded, before anybody reviews it",
       with_it["E-1066"] == before["E-1066"] + 1500,
       f"{before['E-1066']} -> {with_it['E-1066']}")

    ok("recording a breach is allowed with a reason",
       h.act(c, "record_breach", [fresh["id"]], "Vendor is in a live tender.").ok)
    ok("and it STILL counts — a breach does not un-receive a gift",
       totals() == with_it, f"{with_it['E-1066']} -> {totals()['E-1066']}")
    ok("the item is still on the person's breakdown",
       any("Cricket tickets" in r["breakdown"] for r in h.rows(c, "all")))

    reviewed = {r["id"]: r for r in h.rows(c, "disclosed")}
    ok("every row says whether it counts, in every state",
       all(r["counts"] in ("yes", "later phase") for r in reviewed.values()))


def check_a_declaration_is_decided_by_somebody_else() -> None:
    """Two separations, and they are different.

    The person-level rule stops a reviewer acting on their own YEAR. This
    one stops them deciding their own DECLARATION, which is not an action on
    their row and so slips past the first one entirely.
    """
    _, _f, h = registry.feature_of("scs", "rewardlens")
    admin = Scope(user="admin", period="FY 2026-27")       # a reviewer
    other = Scope(user="compliance", period="FY 2026-27")  # another reviewer
    plain = Scope(user="enduser", period="FY 2026-27")     # not a reviewer

    ok("a reviewer declares like anybody else",
       h.submit(admin, "disclose", {
           "what": "Vendor hamper", "kind": "gift", "value": "3000",
           "given": "2026-12-02", "source": "Acme"}).ok)
    mine = next(r for r in h.rows(admin, "disclosed")
                if r["employee_id"] == "E-1052")

    ok("and cannot then decide their own",
       not (o := h.act(admin, "accept_disclosure", [mine["id"]])).ok
       and "your own declaration" in o.said, o.said)
    ok("somebody without the role cannot decide anybody's",
       not (o := h.act(plain, "accept_disclosure", [mine["id"]])).ok
       and "compliance role" in o.said, o.said)
    ok("but they are still shown the declarations",
       h.rows(plain, "disclosed"))
    review = set(rl_mod.RewardLens.REVIEW_ACTIONS)
    ok("and are offered no reviewing button they could not have used",
       not any(set(r["can"]) & review for r in h.rows(plain, "disclosed")))
    ok("a reviewer is offered the ones that are not their own",
       all(set(r["can"]) & review for r in h.rows(other, "disclosed")
           if r["state"] == "awaiting" and r["employee_id"] != "E-1052"))
    ok("and not their own",
       not (set(next(r for r in h.rows(admin, "disclosed")
                     if r["employee_id"] == "E-1052")["can"]) & review))
    ok("and told why the buttons are not theirs",
       "review_is_a_role" in h.rules(plain), str(h.rules(plain)))
    ok("a reviewer who is not the declarer can decide it",
       h.act(other, "accept_disclosure", [mine["id"]]).ok)


def check_a_decision_carries_a_reason_and_a_name() -> None:
    _, feat, h = registry.feature_of("scs", "rewardlens")
    c = Scope(user="compliance", period="FY 2026-27")
    by_id = {a.id: a for a in feat.actions}

    ok("accepting as declared needs no reason", not by_id["accept_disclosure"].note_label)
    ok("querying asks for one", by_id["query_disclosure"].note_label)
    ok("a breach asks for one", by_id["record_breach"].note_label)

    item = h.rows(c, "disclosed")[0]["id"]
    ok("a breach with no reason is refused",
       not (o := h.act(c, "record_breach", [item])).ok and "Say why" in o.said, o.said)
    ok("a reason longer than the cap is refused",
       not h.act(c, "record_breach", [item], "x" * 5000).ok)
    ok("with a reason it is recorded",
       h.act(c, "record_breach", [item], "Vendor is in a live tender.").ok)

    row = next(r for r in h.rows(c, "disclosed") if r["id"] == item)
    ok("the row carries who decided it", row["reviewed_by"] == "compliance")
    ok("and the reason they gave, as they wrote it",
       row["reason"] == "Vendor is in a live tender.", row["reason"])
    ok("deciding it twice is refused, saying who already did",
       not (o := h.act(c, "accept_disclosure", [item])).ok
       and "compliance" in o.said, o.said)
    ok("two at once is refused", not h.act(
        c, "accept_disclosure", [r["id"] for r in h.rows(c, "disclosed")]).ok)
    ok("a signed-off year decides nothing",
       not h.act(Scope(user="compliance", period="FY 2024-25"),
                 "accept_disclosure", [item]).ok)


def check_the_model_does_not_write_the_justification() -> None:
    """An action needing a reason is never proposed from a sentence.

    Not a limitation worked around — the point. "Not permitted because the
    vendor is in a live tender" IS the decision, and a plausible sentence a
    model produced is the last thing that should sit on a compliance record
    under somebody else's name.
    """
    _, feat, h = registry.feature_of("scs", "rewardlens")
    c = Scope(user="compliance", period="FY 2026-27")

    for sentence in ("Mark it not permitted", "record_breach", "Query it"):
        r = assistant.interpret("scs", feat, h, c, sentence, "compliance", "disclosed")
        ok(f"{sentence!r} is not turned into a plan",
           isinstance(r, assistant.Clarify) and "your own words" in r.text,
           f"{type(r).__name__}: {getattr(r, 'text', '')[:60]}")

    # Accepting as declared carries no reason, so the rail may still do it.
    r = assistant.interpret("scs", feat, h, c, "Accept Priya's declaration",
                            "compliance", "disclosed")
    ok("accepting, which needs no reason, is still proposable",
       isinstance(r, assistant.Proposal), type(r).__name__)
    ok("and it resolves against the tab that is open, not the first one",
       isinstance(r, assistant.Proposal)
       and all(t.id.startswith(("G-", "D-")) for t in r.targets),
       str(isinstance(r, assistant.Proposal) and [t.id for t in r.targets]))
    ok("confirming it decides that declaration",
       assistant.confirm(r.id, "compliance", h).ok)

    # The same sentence on the people tab means a person, and still does.
    person = assistant.interpret("scs", feat, h, c, "Refer John to Finance",
                                 "compliance", "over")
    ok("a sentence on the people tab still resolves to a person",
       isinstance(person, assistant.Proposal)
       and [t.id for t in person.targets] == ["E-1007"],
       str(isinstance(person, assistant.Proposal) and [t.id for t in person.targets]))


def check_who_you_are_survives_an_alias() -> None:
    """A deployment that renames its logins must not silently lose the rules.

    `require_user` returns the CANONICAL identity, and a deployment may
    alias a login onto something else — `admin` onto an address. Keyed on
    the login alone, the reviewer role and the conflict-of-interest rule
    both matched nobody: every screen rendered, every figure was right, and
    the two rules that exist to stop somebody deciding their own record
    quietly never fired. Nothing but signing in as a real aliased account
    showed it.
    """
    settings = get_settings()
    kept = dict(settings.auth.identity_aliases)
    try:
        settings.auth.identity_aliases = {"admin": "someone@example.test"}
        _, _f, h = registry.feature_of("scs", "rewardlens")
        aliased = Scope(user="someone@example.test", period="FY 2026-27")

        ok("an aliased login is still the employee it maps to",
           "you_are_a_recipient" in h.rules(aliased), str(h.rules(aliased)))
        ok("and still holds the reviewer role",
           "review_is_a_role" not in h.rules(aliased))
        ok("so the declarations are decidable",
           all("accept_disclosure" in r["can"]
               for r in h.rows(aliased, "disclosed")))
        ok("and their own row is still refused",
           not h.act(aliased, "refer_to_finance", ["E-1052"]).ok)

        ok("somebody the aliases do not name is still nobody",
           not (o := h.preview(Scope(user="stranger@example.test",
                                     period="FY 2026-27"), "disclose", {
               "what": "x", "kind": "gift", "value": "100",
               "given": "2026-12-02", "source": "y"})).ok
           and "which employee record" in o.said, o.said)
    finally:
        settings.auth.identity_aliases = kept


def _queried(h, reviewer, item_id, question="Was this the whole basket?"):
    """Put an item into the state where it can be answered."""
    return h.act(reviewer, "query_disclosure", [item_id], question)


def check_a_correction_is_a_new_record_not_an_edit() -> None:
    """The record does not change. A second one supersedes it.

    "It cannot be edited or withdrawn afterwards, because the point of the
    record is that it did not change after the fact" is what the form
    promises when somebody declares. A correction keeps that promise: the
    original stays, marked superseded, and a new record carries the right
    figure.
    """
    _, _f, h = registry.feature_of("scs", "rewardlens")
    c = Scope(user="compliance", period="FY 2026-27")
    e = Scope(user="enduser", period="FY 2026-27")     # Vikram Rao, E-1066
    total = lambda: next((r["total"] for r in h.rows(c, "all")
                          if r["employee_id"] == "E-1066"), 0)

    item = next(r for r in h.rows(c, "disclosed") if r["employee_id"] == "E-1066")
    ok("it starts at what was declared", total() == item["value"] == 2500, str(total()))

    ok("a reviewer asks about it", _queried(h, c, item["id"]).ok)
    ok("and the total does not move because they asked", total() == 2500)

    ok("the declarer proposes the right figure",
       h.submit(e, "answer_query", {
           "response": "corrected", "value": "1500",
           "note": "That was the whole basket; mine was 1,500."}, item["id"]).ok)
    ok("and the total STILL does not move, because they said so",
       total() == 2500, str(total()))

    out = h.act(c, "accept_answer", [item["id"]])
    ok("a reviewer accepting it is what moves the figure",
       out.ok and total() == 1500, f"{out.said} -> {total()}")
    ok("and the sentence says both numbers",
       "₹1,500" in out.said and "₹2,500" in out.said, out.said)

    rows = {r["id"]: r for r in h.rows(c, "disclosed")}
    ok("the original is still there", item["id"] in rows)
    ok("marked superseded", rows[item["id"]]["state"] == "superseded")
    ok("naming what replaced it",
       rows[item["id"]]["replaced_by"].startswith("C-"),
       rows[item["id"]]["replaced_by"])
    replacement = rows[rows[item["id"]]["replaced_by"]]
    ok("the replacement carries the corrected figure", replacement["value"] == 1500)
    ok("and points back at what it corrects",
       replacement["corrects"] == item["id"], replacement["corrects"])
    ok("only the replacement is in the breakdown",
       next(r["breakdown"] for r in h.rows(c, "all")
            if r["employee_id"] == "E-1066").count("Vendor hamper") == 1)
    ok("nothing was deleted",
       len([i for i in rl_mod._ITEMS if i["employee_id"] == "E-1066"]) == 2)


def check_only_a_mistaken_record_stops_counting() -> None:
    """A breach and a correction look alike and are opposites.

    A breach says you should not have been given it — you were, so it
    counts. A correction says the RECORD was wrong, so what it counted was
    never right. That difference is the lever somebody would reach for
    first, so it is the one worth checking hardest.
    """
    _, _f, h = registry.feature_of("scs", "rewardlens")
    c = Scope(user="compliance", period="FY 2026-27")
    e = Scope(user="enduser", period="FY 2026-27")
    total = lambda: next((r["total"] for r in h.rows(c, "all")
                          if r["employee_id"] == "E-1066"), 0)
    item = next(r for r in h.rows(c, "disclosed") if r["employee_id"] == "E-1066")

    ok("a breach leaves it counting",
       h.act(c, "record_breach", [item["id"]], "Vendor is in a live tender.").ok
       and total() == 2500, str(total()))

    # And the declarer cannot reach for the other lever on their own.
    ok("the declarer cannot answer an item nobody asked about",
       not h.submit(e, "answer_query",
                    {"response": "withdrawn", "note": "I would rather not."},
                    item["id"]).ok)
    ok("it is still counting", total() == 2500)


def check_nobody_corrects_their_own_record_alone() -> None:
    _, _f, h = registry.feature_of("scs", "rewardlens")
    c = Scope(user="compliance", period="FY 2026-27")
    e = Scope(user="enduser", period="FY 2026-27")
    a = Scope(user="admin", period="FY 2026-27")
    item = next(r for r in h.rows(c, "disclosed") if r["employee_id"] == "E-1066")
    _queried(h, c, item["id"])

    ok("somebody else cannot answer for them",
       not (o := h.submit(a, "answer_query",
                          {"response": "stands", "note": "Fine."}, item["id"])).ok
       and "theirs to answer" in o.said, o.said)
    h.submit(e, "answer_query",
             {"response": "withdrawn", "note": "Declared it twice."}, item["id"])
    ok("and the declarer cannot accept their own answer",
       not h.act(e, "accept_answer", [item["id"]]).ok)
    ok("the row offers them no way to",
       not any(x in next(r["can"] for r in h.rows(e, "disclosed")
                         if r["id"] == item["id"])
               for x in ("accept_answer", "record_breach")))

    total = next((r["total"] for r in h.rows(c, "all")
                  if r["employee_id"] == "E-1066"), 0)
    ok("so it keeps counting while it waits", total == 2500, str(total))
    out = h.act(c, "accept_answer", [item["id"]])
    ok("until somebody else agrees", out.ok and "₹0" in out.said, out.said)
    ok("and then it is out of the total",
       next((r["total"] for r in h.rows(c, "all")
             if r["employee_id"] == "E-1066"), 0) == 0)


def check_an_answer_has_to_make_sense() -> None:
    _, _f, h = registry.feature_of("scs", "rewardlens")
    c = Scope(user="compliance", period="FY 2026-27")
    e = Scope(user="enduser", period="FY 2026-27")
    item = next(r for r in h.rows(c, "disclosed") if r["employee_id"] == "E-1066")
    _queried(h, c, item["id"])

    def refused(**values) -> str:
        base = {"response": "corrected", "value": "1500", "note": "because"}
        out = h.preview(e, "answer_query", dict(base, **values), item["id"])
        return "" if out.ok else out.said

    ok("an answer nobody recognises is refused", refused(response="maybe"))
    ok("an answer with no words is refused", refused(note="   "))
    ok("a correction with no figure is refused", refused(value=""))
    ok("a correction to the same figure is refused", refused(value="2500"))
    ok("a correction to nothing says to withdraw it instead",
       "withdrawal" in refused(value="0"))
    ok("a figure with no correction is refused",
       refused(response="stands", value="900"))
    ok("a signed-off year takes no answers",
       not h.preview(Scope(user="enduser", period="FY 2024-25"),
                     "answer_query",
                     {"response": "stands", "note": "x"}, item["id"]).ok)
    ok("previewing it changed nothing",
       next(r["state"] for r in h.rows(c, "disclosed")
            if r["id"] == item["id"]) == "queried")


def check_asking_again_drops_the_old_answer() -> None:
    """A reply to a question nobody is asking any more is not a reply."""
    _, _f, h = registry.feature_of("scs", "rewardlens")
    c = Scope(user="compliance", period="FY 2026-27")
    e = Scope(user="enduser", period="FY 2026-27")
    item = next(r for r in h.rows(c, "disclosed") if r["employee_id"] == "E-1066")

    _queried(h, c, item["id"])
    ok("asking again while it is unanswered is refused",
       not h.act(c, "query_disclosure", [item["id"]], "again?").ok)

    h.submit(e, "answer_query", {"response": "corrected", "value": "1500",
                                 "note": "It was 1,500."}, item["id"])
    row = next(r for r in h.rows(c, "disclosed") if r["id"] == item["id"])
    ok("an answered item is back on the reviewer's desk", row["state"] == "answered")
    ok("and shows what they said",
       row["answered"] == "the value was wrong" and "1,500" in row["answer_note"],
       f"{row['answered']} / {row['answer_note']}")

    ok("asking again is allowed once it is answered",
       h.act(c, "query_disclosure", [item["id"]], "Send the invoice.").ok)
    row = next(r for r in h.rows(c, "disclosed") if r["id"] == item["id"])
    ok("and the old answer is gone with the old question",
       row["state"] == "queried" and row["answered"] == "—", str(row["answered"]))
    ok("so accepting an answer nobody gave is refused",
       not h.act(c, "accept_answer", [item["id"]]).ok)


def check_the_row_and_the_handler_cannot_disagree() -> None:
    """Every button a row offers works, and everything else is refused.

    The row's `can` list is what the table draws and what `act` checks, on
    purpose: two lists would be two chances to disagree, and the symptom
    would be a button that exists and does not work.
    """
    _, feat, h = registry.feature_of("scs", "rewardlens")
    everyone = [Scope(user=u, period="FY 2026-27")
                for u in ("compliance", "admin", "enduser", "stranger")]
    review_actions = set(rl_mod.RewardLens.REVIEW_ACTIONS)

    for scope in everyone:
        for row in h.rows(scope, "disclosed"):
            offered = set(row["can"]) & review_actions
            for action in review_actions - offered:
                out = h.act(scope, action, [row["id"]], "a reason")
                ok(f"{scope.user}: {action} is refused on a row that does not offer it",
                   not out.ok, f"{row['id']} {row['state']}: {out.said}")
            for action in offered:
                ok(f"{scope.user}: {action} works on a row that offers it",
                   h.act(scope, action, [row["id"]], "a reason").ok)
                break  # one is enough; the first changes the state


class _Mailbox:
    """A stand-in mail server, so the sending path can be checked.

    Replaces `send_email` rather than reaching for SMTP: what is being
    checked here is the outbox around it — what is recorded, what is
    attempted, what is said about it — and the SMTP client itself predates
    this feature and is used by routines.
    """

    def __init__(self, accept: bool = True) -> None:
        self.accept, self.sent = accept, []

    def __enter__(self):
        import compass.code.notify as mail
        self._mail = mail
        self._was = (mail.send_email, mail.email_configured)
        mail.email_configured = lambda: True
        def send(subject, body, *, to=None):
            self.sent.append({"subject": subject, "body": body, "to": to})
            return self.accept
        mail.send_email = send
        return self

    def __exit__(self, *_):
        self._mail.send_email, self._mail.email_configured = self._was
        return False


def check_asking_somebody_is_written_down_before_it_is_sent() -> None:
    """A notice is a record. The record is what the screen can prove.

    With no mail server, the ordinary state of a developer's box and of this
    deployment, the question is still asked and still recorded — and the row
    says plainly that nobody was emailed. A reviewer who believes somebody
    was told and nobody was is the failure this exists to prevent.
    """
    notices.clear()
    _, _f, h = registry.feature_of("scs", "rewardlens")
    c = Scope(user="compliance", period="FY 2026-27")
    item = next(r for r in h.rows(c, "disclosed") if r["employee_id"] == "E-1066")

    out = h.act(c, "query_disclosure", [item["id"]], "Was this the whole basket?")
    ok("the question is asked whatever the mail server is doing", out.ok)
    ok("and the outcome says it was not emailed", "not emailed" in out.said, out.said)
    ok("naming what is missing", "no mail server" in out.said, out.said)

    row = next(r for r in h.rows(c, "disclosed") if r["id"] == item["id"])
    ok("the row says so too", "not emailed" in row["notified"], row["notified"])
    ok("the item is queried regardless", row["state"] == "queried")
    ok("one notice was recorded", len(notices.for_item(item["id"])) == 1)
    ok("and it is held, not failed",
       notices.latest_for(item["id"]).state == "held")
    ok("a reviewer is told, once, that somebody was not reached",
       h.rules(c).count("notices_not_sent") == 1, str(h.rules(c)))


def check_a_notice_goes_to_the_person_and_nobody_else() -> None:
    """Never to whoever runs the server, and never about anybody else.

    `send_email` falls back to COMPASS_NOTIFY_EMAIL when given no recipient,
    which would post one employee's gift record to the operator. And a
    notice that helpfully mentioned where somebody sits against their
    colleagues would put one person's record in another's inbox.
    """
    notices.clear()
    _, _f, h = registry.feature_of("scs", "rewardlens")
    c = Scope(user="compliance", period="FY 2026-27")
    item = next(r for r in h.rows(c, "disclosed") if r["employee_id"] == "E-1066")

    with _Mailbox() as box:
        out = h.act(c, "query_disclosure", [item["id"]],
                    "Was the 2,500 the hamper, or the whole basket?")
        ok("with a mail server it is queued", "queued" in out.said, out.said)
        ok("nothing is sent until it is flushed", not box.sent)
        ok("flushing sends it", notices.flush() == (1, 0))

    ok("exactly one message", len(box.sent) == 1, str(len(box.sent)))
    message = box.sent[0]
    ok("addressed to the declarer, explicitly",
       message["to"] == "vikram.rao@example.invalid", str(message["to"]))
    ok("so the operator's own address is never the fallback",
       message["to"] is not None and message["to"] != "")

    body = message["body"]
    ok("it carries the question that was asked",
       "Was the 2,500 the hamper, or the whole basket?" in body)
    ok("and their own figures", "₹2,500" in body and "Vendor hamper" in body)
    ok("and says it keeps counting", "counting towards your annual limit" in body)
    others = [r["recipient"] for r in h.rows(c, "all")
              if r["employee_id"] != "E-1066"]
    ok("and mentions nobody else at all",
       not [who for who in others if who in body],
       str([who for who in others if who in body]))
    ok("and no other figure from the screen",
       "₹76,500" not in body and "₹20,000" not in body)

    row = next(r for r in h.rows(c, "disclosed") if r["id"] == item["id"])
    ok("the row now says they were emailed", row["notified"] == "emailed Vikram Rao",
       row["notified"])
    ok("and the warning is gone", "notices_not_sent" not in h.rules(c))


def check_nobody_is_told_who_has_no_address() -> None:
    """A held notice is better than a confident one."""
    notices.clear()
    _, _f, h = registry.feature_of("scs", "rewardlens")
    c = Scope(user="compliance", period="FY 2026-27")
    item = next(r for r in h.rows(c, "disclosed") if r["employee_id"] == "E-1019")
    ok("Priya has no address on file", not rl_mod._EMAIL.get("E-1019"))

    with _Mailbox() as box:
        out = h.act(c, "query_disclosure", [item["id"]], "Whose dinner was this?")
        ok("the question is still asked", out.ok)
        ok("and says nobody knows where to write",
           "no email address on file" in out.said, out.said)
        ok("flushing sends nothing", notices.flush() == (0, 0))
    ok("nothing was handed to the mail server", not box.sent)

    # No retry is offered, because retrying could never work: the gap is in
    # the directory, not on this screen. What the reviewer gets instead is
    # the row saying exactly who was not reached and why.
    row = next(r for r in h.rows(c, "disclosed") if r["id"] == item["id"])
    ok("no retry is offered, because it could never work",
       "resend_notice" not in row["can"], str(row["can"]))
    ok("and the row names who was not reached",
       "no email address on file for Priya Nair" in row["notified"],
       row["notified"])
    ok("asking for one anyway is refused", not h.act(c, "resend_notice",
                                                     [item["id"]]).ok)


def check_a_held_notice_can_be_tried_again() -> None:
    """Recording it rather than firing it is what makes a retry possible."""
    notices.clear()
    _, _f, h = registry.feature_of("scs", "rewardlens")
    c = Scope(user="compliance", period="FY 2026-27")
    item = next(r for r in h.rows(c, "disclosed") if r["employee_id"] == "E-1066")

    h.act(c, "query_disclosure", [item["id"]], "Was this the whole basket?")
    row = next(r for r in h.rows(c, "disclosed") if r["id"] == item["id"])
    ok("a held notice offers a retry and nothing else",
       row["can"] == ["resend_notice"], str(row["can"]))
    ok("retrying with no mail server still refuses, and says what to set",
       not (o := h.act(c, "resend_notice", [item["id"]])).ok
       and "COMPASS_SMTP_HOST" in o.said, o.said)

    with _Mailbox() as box:
        ok("once there is one, it goes", h.act(c, "resend_notice", [item["id"]]).ok)
        notices.flush()
    ok("and it is the same question, not a new one",
       len(box.sent) == 1 and "Was this the whole basket?" in box.sent[0]["body"])
    ok("the row says it was emailed",
       next(r["notified"] for r in h.rows(c, "disclosed")
            if r["id"] == item["id"]) == "emailed Vikram Rao")
    ok("and there is nothing left to retry",
       not h.act(c, "resend_notice", [item["id"]]).ok)


def check_a_mail_server_that_refuses_does_not_undo_the_decision() -> None:
    notices.clear()
    _, _f, h = registry.feature_of("scs", "rewardlens")
    c = Scope(user="compliance", period="FY 2026-27")
    item = next(r for r in h.rows(c, "disclosed") if r["employee_id"] == "E-1066")

    with _Mailbox(accept=False):
        ok("the query succeeds", h.act(c, "query_disclosure", [item["id"]],
                                       "Was this the whole basket?").ok)
        ok("delivery fails", notices.flush() == (0, 1))

    row = next(r for r in h.rows(c, "disclosed") if r["id"] == item["id"])
    ok("the item is still queried", row["state"] == "queried")
    ok("the row says the email failed", "failed" in row["notified"], row["notified"])
    ok("and it can be tried again", "resend_notice" in row["can"])
    ok("the declarer can still answer it",
       "answer_query" in next(
           r["can"] for r in h.rows(Scope(user="enduser", period="FY 2026-27"),
                                    "disclosed") if r["id"] == item["id"]))


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

    # The route checks come last: they switch the module on, and reloading the
    # app module is the sort of thing that should not be upstream of anything.
    check_the_routes_are_mounted_only_when_switched_on()
    check_every_route_is_behind_require_user()
    with fixtures():
        check_the_surface_answers()
    with fixtures():
        check_a_feature_says_what_scope_it_is_waiting_on()
    with fixtures():
        check_the_rail_round_trip_over_http()
    with fixtures():
        check_asking_vaguely_is_a_question_not_an_act()
    with fixtures():
        check_a_row_button_needs_no_plan()
    check_a_selector_is_never_offered_the_wrong_values()
    check_a_form_and_its_code_agree()
    with fixtures():
        check_a_declaration_is_about_yourself()
    with fixtures():
        check_the_preview_is_the_number_nobody_could_see()
    with fixtures():
        check_a_declaration_cannot_be_nonsense()
    with fixtures():
        check_what_was_declared_is_what_was_recorded()
    with fixtures():
        check_reviewing_never_changes_what_was_received()
    with fixtures():
        check_a_declaration_is_decided_by_somebody_else()
    with fixtures():
        check_a_decision_carries_a_reason_and_a_name()
    with fixtures():
        check_the_model_does_not_write_the_justification()
    with fixtures():
        check_who_you_are_survives_an_alias()
    with fixtures():
        check_asking_somebody_is_written_down_before_it_is_sent()
    with fixtures():
        check_a_notice_goes_to_the_person_and_nobody_else()
    with fixtures():
        check_nobody_is_told_who_has_no_address()
    with fixtures():
        check_a_held_notice_can_be_tried_again()
    with fixtures():
        check_a_mail_server_that_refuses_does_not_undo_the_decision()
    with fixtures():
        check_a_correction_is_a_new_record_not_an_edit()
    with fixtures():
        check_only_a_mistaken_record_stops_counting()
    with fixtures():
        check_nobody_corrects_their_own_record_alone()
    with fixtures():
        check_an_answer_has_to_make_sense()
    with fixtures():
        check_asking_again_drops_the_old_answer()
    with fixtures():
        check_the_row_and_the_handler_cannot_disagree()
    with fixtures():
        check_every_row_can_be_pointed_at()
    with fixtures():
        check_every_feature_can_be_named()
    with fixtures():
        check_rewardlens_totals_a_person_not_a_purchase()
    with fixtures():
        check_rewardlens_figures_follow_the_rows()
    with fixtures():
        check_nobody_clears_their_own_breach()
    with fixtures():
        check_a_referral_is_one_person_at_a_time()
    with fixtures():
        check_a_signed_off_year_is_closed_to_everyone()
    with fixtures():
        check_the_rail_can_work_rewardlens()
    check_the_health_endpoint_advertises_the_section()

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
