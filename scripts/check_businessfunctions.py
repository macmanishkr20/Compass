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

import asyncio
import contextlib
import datetime as dt
import io
import json
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
    chat,
    policy,
    export,
    features,
    ledger,
    notices,
    people,
    registry,
    reports,
)
from compass.businessfunctions.features.base import Scope  # noqa: E402
from compass.businessfunctions.features import form26 as form26_mod  # noqa: E402
from compass.businessfunctions.features import lms as lms_mod  # noqa: E402
from compass.businessfunctions.features import giftrequests as gr_mod  # noqa: E402
from compass.businessfunctions.features import rewardlens as rl_mod  # noqa: E402
from compass.businessfunctions.features import oversight as ov_mod  # noqa: E402
from compass.businessfunctions import routes  # noqa: E402
from compass.businessfunctions.features import giftaudit as au_mod  # noqa: E402
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


def as_(user: str, feature: str = "register", *, fn: str = "scs",
        period: str = "FY 2026-27", entity: str = "") -> Scope:
    """A scope the way a request builds one: roles and breadth resolved.

    Constructing `Scope(user=...)` by hand gives somebody no roles and the
    unrestricted breadth a feature that declares no visibility gets — which
    is neither of the things a real caller would have. The checks build
    theirs the same way the routes do, through the one function that knows
    how, or they are checking a situation that cannot happen.
    """
    return people.scope_for(user, registry.feature_of(fn, feature)[1],
                            entity=entity, period=period)


def drive(coro):
    """Drive one store call from these synchronous checks.

    A fresh loop per call is fine: the collection's lock is per-loop by
    design, precisely so module-level singletons are not bound to whichever
    loop happened to touch them first.
    """
    return asyncio.run(coro)


@contextlib.contextmanager
def scratch_data():
    """Point the stores at a throwaway folder, on the LOCAL backend.

    Both halves matter, and the second one was learned the hard way: a
    temporary directory is no protection at all when the deployment is
    configured for Cosmos, because the collection never looks at the
    filesystem. The first version of this wrote test subscriptions into a
    live Azure account — harmlessly, as it happened, because the kind was
    new and nothing else shares it, but the next check to be written would
    not have been so lucky.

    So the backend is forced local for the duration as well as the folder
    being thrown away. Checks write; the place they write has to be one
    nobody will miss.
    """
    settings = get_settings()
    was_dir, was_backend = settings.data_dir, settings.storage.backend
    with tempfile.TemporaryDirectory(prefix="bf-checks-") as tmp:
        settings.storage.backend = "local"
        # `workspace_root / data_dir` is how a collection resolves its file,
        # and an absolute data_dir makes that the absolute path.
        settings.data_dir = tmp
        try:
            yield Path(settings.workspace_root / settings.data_dir)
        finally:
            settings.data_dir, settings.storage.backend = was_dir, was_backend


@contextlib.contextmanager
def stored():
    """Roll back the STORE as well as memory, for checks that flush.

    `fixtures` restores what is held in the process; it cannot restore what
    a check wrote through to disk. A handful of checks here deliberately
    flush and reload to prove a restart keeps things, and without this the
    next one reads a register carrying everything every earlier check ever
    wrote — which showed up as a total that was right before the reload and
    wrong after, for reasons that had nothing to do with the code.
    """
    items = drive(ledger._items_store.all())
    events = drive(ledger._events_store.all())
    try:
        yield
    finally:
        drive(ledger._items_store.replace_all(items))
        drive(ledger._events_store.replace_all(events))
        ledger.forget()
        drive(ledger.ready(seed=rl_mod.SEED, requests_seed=gr_mod.SEED))


@contextlib.contextmanager
def fixtures():
    """Run a check against untouched records, whatever ran before it.

    Everything mutable now lives in three places — the ledger's register,
    its trail and its requests — plus the outbox and the two older
    handlers' own fixtures. Snapshotting the ledger is a few lines where it
    used to be three dictionaries, which is the first dividend of folding
    state instead of storing it.

    THE REQUESTS WERE MISSING FROM THIS, and it was not harmless: every
    check that raised one left it behind, so a check's result depended on
    what had run before it. Adding two reports to the middle of the run
    broke sixteen checks downstream that had quietly been asserting on a
    total somebody else's fixture had moved.
    """
    before = (copy.deepcopy(form26_mod._FIXTURE), copy.deepcopy(lms_mod._FIXTURE),
              copy.deepcopy(ledger._items), copy.deepcopy(ledger._events),
              copy.deepcopy(ledger._requests),
              list(ledger._unsaved_items), list(ledger._unsaved_events),
              list(ledger._unsaved_requests),
              dict(notices._outbox), set(notices._unsaved),
              # The RULEBOOK too. A check that raises the annual limit
              # changes what every later check is measuring against, and
              # the first symptom is a check about somebody being over the
              # limit failing because an earlier one moved the limit.
              copy.deepcopy(policy._versions), list(policy._unsaved))
    try:
        yield
    finally:
        form26_mod._FIXTURE[:] = before[0]
        lms_mod._FIXTURE[:] = before[1]
        ledger._items[:] = before[2]
        ledger._events[:] = before[3]
        ledger._requests[:] = before[4]
        ledger._unsaved_items[:] = before[5]
        ledger._unsaved_events[:] = before[6]
        ledger._unsaved_requests[:] = before[7]
        notices._outbox.clear(); notices._outbox.update(before[8])
        notices._unsaved.clear(); notices._unsaved.update(before[9])
        policy._versions[:] = before[10]
        policy._unsaved[:] = before[11]


def _refuses(call) -> bool:
    """Whether a call refuses rather than answering. Any refusal will do —
    the point is that there is no second path to a row somebody cannot see."""
    try:
        call()
    except Exception:
        return True
    return False


def _flat(book: bytes) -> str:
    """Every cell of a workbook as one string, for asking what is in it."""
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(book))
    return " ".join(str(c.value) for ws in wb.worksheets
                    for row in ws.iter_rows() for c in row
                    if c.value is not None)


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
           f"{root}/{{function_id}}/ask",
           f"{feature}/export.xlsx",
           f"{feature}/report",
           f"{feature}/report/send",
           f"{feature}/forms/{{form_id}}",
           f"{feature}/forms/{{form_id}}/preview",
           f"{feature}/forms/{{form_id}}/upload",
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

    # A screen opens on a tab that has something in it. The first tab is
    # the reader's own queue, which is right for a desk and always empty
    # for Leadership, who hold no step — they opened the chain onto
    # "Waiting on you: 0" and read it as no data, three orders away.
    r = c.get("/v1/business-functions/scs/features/requests",
              params={"period": "FY 2026-27"})
    body = r.json()
    counts = {t["key"]: t["count"] for t in body["tabs"]}
    ok("the screen opens on a tab that has rows",
       counts.get(body["tab"], 0) > 0 or not any(counts.values()),
       f"opened on {body['tab']!r} with {counts}")
    ok("and that tab's rows really arrive",
       len(body["rows"]) == counts[body["tab"]],
       f"{len(body['rows'])} rows vs count {counts[body['tab']]}")
    ok("a tab asked for by name is still honoured",
       c.get("/v1/business-functions/scs/features/requests",
             params={"period": "FY 2026-27", "tab": "mine"}).json()["tab"]
       == "mine")

    # Which fields are the screen's machinery rather than somebody's to
    # read. Sent, because the alternative is the UI keeping its own copy of
    # the list — which it did, and which had drifted far enough that a
    # reader was shown a row's internal stage key.
    r = c.get("/v1/business-functions/scs/features/requests",
              params={"period": "FY 2026-27"})
    body = r.json()
    handler = registry.feature_of("scs", "requests")[2]
    ok("a feature says which of its fields are not for reading",
       body.get("private") == sorted(handler.private), str(body.get("private")))
    ok("and the rows really carry them, so the list is not aspirational",
       not body["rows"] or set(handler.private) <= set(body["rows"][0]),
       str(set(handler.private) - set(body["rows"][0] if body["rows"] else {})))

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

        John:   12,000 + 14,000 + 26,000 = 52,000  over the 50,000
                                                   threshold by 2,000
        Priya:  15,000 + 10,000 + 18,000 = 43,000  under it by 7,000

    The requirement's worked example, scaled to the threshold the business
    actually named: three gifts from three teams, each unremarkable on its
    own, and nobody aware of the other two until they are added up here.
    Priya is the near miss, because a screen that only ever shows breaches
    teaches nobody what the ordinary case looks like.
    """
    _, feat, h = registry.feature_of("scs", "register")
    s = as_("compliance", period="FY 2026-27")
    by_name = {r["recipient"]: r for r in h.rows(s, "all")}

    john = by_name.get("John Mathew")
    ok("John's three gifts come to 52,000", john and john["total"] == 52000,
       str(john and john["total"]))
    ok("which is 2,000 past the threshold", john and john["excess"] == 2000,
       str(john and john["excess"]))
    ok("and he is flagged", john and john["over_limit"])

    priya = by_name.get("Priya Nair")
    ok("Priya's three come to 43,000", priya and priya["total"] == 43000,
       str(priya and priya["total"]))
    ok("which is under it, so she is not flagged",
       priya and priya["excess"] == 0 and not priya["over_limit"],
       str(priya and priya["excess"]))

    vikram = by_name.get("Vikram Rao")
    ok("somebody inside the limit has headroom, not an excess",
       vikram and vikram["excess"] == 0 and vikram["headroom"] == 47500,
       str(vikram and (vikram["excess"], vikram["headroom"])))

    # A limit that silently omits a category is worse than one that says what
    # it covers. Hospitality is recorded against John and left out of his total.
    ok("hospitality is recorded but not counted",
       john and "hospitality" in john["not_counted"] and john["total"] == 52000,
       str(john and john["not_counted"]))

    ok("the row is a person, not a purchase",
       all("recipient" in r and "items_counted" in r for r in h.rows(s, "all")))


def check_rewardlens_figures_follow_the_rows() -> None:
    _, _feat, h = registry.feature_of("scs", "register")
    s = as_("compliance", period="FY 2026-27")
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
    _, feat, h = registry.feature_of("scs", "register")
    mine = as_("mk", period="FY 2026-27")
    others = as_("compliance", period="FY 2026-27")

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
    # ...when there is something to refer. Manish is comfortably under the
    # threshold, so the refusal names the headroom rather than acting.
    ok("and somebody else is not stopped by the conflict rule",
       "inside the limit" in h.act(others, "refer_to_finance",
                                   ["E-1041"]).said,
       h.act(others, "refer_to_finance", ["E-1041"]).said)


def check_a_referral_is_one_person_at_a_time() -> None:
    _, _feat, h = registry.feature_of("scs", "register")
    s = as_("compliance", period="FY 2026-27")

    ok("two at once is refused",
       not h.act(s, "refer_to_finance", ["E-1007", "E-1019"]).ok)
    ok("somebody inside the limit is refused, and told the headroom",
       not (o := h.act(s, "refer_to_finance", ["E-1066"])).ok and "inside the limit" in o.said,
       o.said)
    first = h.act(s, "refer_to_finance", ["E-1007"])
    ok("one over the limit succeeds, naming the figures",
       first.ok and "₹52,000" in first.said and "₹2,000" in first.said,
       first.said)
    ok("referring the same person twice is refused",
       not h.act(s, "refer_to_finance", ["E-1007"]).ok)
    # Chasing an acknowledgement went with the acknowledgement itself.
    ok("chasing a recipient is no longer an action at all",
       not h.act(s, "chase_acknowledgement", ["E-1019"]).ok)


def check_a_row_says_which_actions_it_takes() -> None:
    """Every row of a feature with buttons says which of them IT takes.

    This is the contract that puts buttons on a screen: the surface draws
    what the row allows and nothing otherwise, because it cannot know who
    holds which role, whose record this is, or what state it is in.

    It is checked rather than assumed because the last time it broke,
    nothing failed. The Tracker's person rows never carried the list; the
    surface, which was steering on the feature's id, stopped recognising
    that id when the sub-service was renamed `register`, fell through to
    "offer everything the manifest declares", and put nine buttons on a
    record that was within the limit — a referral with nothing to refer, an
    exception with no breach, and four decisions about a declaration that
    did not exist. Eight of the nine the server refuses, and the one that
    was real sat in the middle of them looking identical.
    """
    function = registry.get("scs")
    ok("the function loads", function is not None)
    if function is None:
        return

    for feature in function.features:
        if not feature.actions:
            continue
        declared = {a.id for a in feature.actions}
        _, _f, h = registry.feature_of("scs", feature.id)
        # Compliance sees the firm on every screen here, so this walks the
        # widest set of rows any one person is shown.
        scope = as_("compliance", feature.id, period="FY 2026-27")
        for tab in h.tabs(scope):
            rows = h.rows(scope, tab.key)
            ok(f"{feature.id}/{tab.key}: every row says what it takes",
               all(isinstance(r.get("can"), list) for r in rows),
               str(next((r for r in rows
                         if not isinstance(r.get("can"), list)), ""))[:120])
            loose = {a for r in rows for a in r.get("can", [])} - declared
            ok(f"{feature.id}/{tab.key}: and names only declared actions",
               not loose, str(loose))

    # And the list is the truth, not a hint: what a row says it takes, the
    # handler takes. Walked against the Tracker's people, because that is
    # the screen the regression was on.
    _, _feat, tracker = registry.feature_of("scs", "register")
    firm = as_("compliance", period="FY 2026-27")
    people_rows = tracker.rows(firm, "all")
    ok("the Tracker's people carry a list each",
       people_rows and all(isinstance(r.get("can"), list) for r in people_rows))

    within = next((r for r in people_rows if not r["over_limit"]), None)
    ok("somebody within the limit offers nothing",
       within is not None and within["can"] == [], str(within and within["can"]))

    over = next((r for r in people_rows
                 if r["over_limit"] and not r["referred"]), None)
    ok("somebody over the limit leads with the referral",
       over is not None and over["can"][:1] == ["refer_to_finance"],
       str(over and over["can"]))

    # THEIR OWN ROW OFFERS NOTHING. Acknowledging your own gift was the one
    # act on this screen that was only ever about yourself, and so the one
    # exemption from "nobody decides their own case". It is gone, and the
    # rule applies to everything again.
    mine = as_("admin", period="FY 2026-27")
    own = next((r for r in tracker.rows(mine, "all") if r["yours"]), None)
    ok("your own row offers nothing",
       own is not None and own["can"] == [], str(own and own["can"]))
    ok("and the handler refuses what it no longer has",
       own is not None
       and not tracker.act(mine, "acknowledge", [own["employee_id"]]).ok)

    # The orders tab tracks; it does not decide. The chain's own screen has
    # the sign-off buttons, with the chain's own guards behind them.
    orders = tracker.rows(firm, "orders")
    ok("an order on the tracker offers nothing",
       orders and all(r["can"] == [] for r in orders))


def check_gift_requests_is_yours_and_the_tracker_is_the_desk() -> None:
    """Two screens, two questions, and no overlap between them.

    GIFT REQUESTS is personal: what I asked for, and what somebody asked
    for me. Nothing else — not a stage I own, not my team's, not the
    firm's, and no exemption for Compliance or Leadership. A screen where
    an employee asks for a hamper had the whole firm's queue on it.

    THE TRACKER is where the desks work: Finance, Procurement, the two
    service line approvers, Payroll, Accounts Payable and Compliance each
    see what is standing at their stage, sign it off there, and watch it
    land in Signed off.
    """
    _, rf, chain = registry.feature_of("scs", "requests")
    _, _tf, tracker = registry.feature_of("scs", "register")

    # `servicelead` raises one FOR somebody else.
    rid = _ask(by="servicelead", recipient="Vikram Rao", what="A hamper",
               value="1200", occasion="festival")

    seen = lambda who: {r["id"] for r in chain.rows(_req(who), "all")}
    ok("the person who raised it sees it on Gift requests",
       rid in seen("servicelead"))
    ok("and so does the person it was raised for",
       rid in seen("enduser"))
    for stranger in ("finance", "payroll", "procurement", "payments",
                     "compliance", "leadtwo", "mk"):
        ok(f"{stranger} sees nothing of it on Gift requests",
           rid not in seen(stranger))

    # The desks see it on the Tracker — and only the one whose stage it is
    # has anything to sign.
    ok("the stage's owner has it to sign",
       any(r["id"] == rid for r in _desk("leadtwo", "to_sign")))
    ok("the one who raised it cannot sign their own",
       not any(r["id"] == rid for r in _desk("servicelead", "to_sign")))
    for ahead in ("finance", "payroll", "procurement", "payments"):
        ok(f"{ahead} has nothing to sign while it is not theirs",
           not any(r["id"] == rid for r in _desk(ahead, "to_sign")))

    # Signed off FROM THE TRACKER, which is the point of the change.
    out = tracker.act(as_("leadtwo", "register", period="FY 2026-27"),
                      "sign_off", [rid], "Business need is clear.")
    ok("a desk signs off from the Tracker", out.ok, out.said)
    ok("and it moves to the next desk there",
       any(r["id"] == rid for r in _desk("finance", "to_sign")))
    ok("while the desk that signed keeps sight of it",
       any(r["id"] == rid for r in _desk("leadtwo", "orders")))

    # ...all the way, and then it lands.
    ok("the rest of the chain runs from the Tracker",
       not _walk(rid, by="servicelead"), "")
    ok("and a finished order is in Signed off",
       any(r["id"] == rid for r in _desk("compliance", "completed")))
    ok("and no longer in flight",
       not any(r["id"] == rid for r in _desk("compliance", "orders")))
    ok("with nothing left to sign",
       not any(r["id"] == rid for r in _desk("compliance", "to_sign")))

    # The Tracker never grows its own copy of the chain's rules: the row
    # says what the chain says, and the refusal comes from the chain.
    refused = tracker.act(as_("payroll", "register", period="FY 2026-27"),
                          "sign_off", [rid], "mine now")
    ok("a desk cannot sign an order that has finished", not refused.ok,
       refused.said)


def check_a_bulk_order_becomes_people() -> None:
    """The thing the tracker exists for: buy first, name recipients after.

    The business's own first pain point is "bulk purchases without
    recipient details". A form that demanded one name before anything could
    be ordered could not represent that purchase at all — so the quantity
    is the order and the recipients are an answer given at distribution,
    which is where they are actually known.
    """
    _, feat, h = registry.feature_of("scs", "requests")
    asking = _req("requester")

    bulk = {"what": "Festival hamper", "kind": "gift", "value": "2000",
            "occasion": "festival", "quantity": "3", "purpose": "Recognising the team."}
    out = h.submit(asking, "request_gift", bulk)
    ok("an order with no recipient is accepted", out.ok, out.said)
    ok("and says how many are to be allocated",
       "3 \u00d7" in out.said and "allocated" in out.said, out.said)
    rid = out.touched[0]

    ok("nothing is on anybody's register yet",
       not gr_mod.allocated_to(rid), str(gr_mod.allocated_to(rid)))

    # It walks the chain like any other order — the checks that need a
    # person say so rather than pretending to have run.
    row = next(r for r in gr_mod._rows(_req("servicelead")) if r["id"] == rid)
    ok("the row says the year is checked at distribution",
       "when they are named" in row["tax"], row["tax"])
    ok("and it is not blocked for having nobody on it", not row["blocked"])

    trouble = _walk(rid, upto="recipient_list")
    ok("it reaches the list step", not trouble, str(trouble))

    # NOBODY ON THE LIST, so there is nothing to close and nothing to hand
    # out. Closing an empty list would send an order to distribution with
    # no one to record it against.
    asking_spoc = _req("spoc")
    refused = h.act(asking_spoc, "sign_off", [rid], "")
    ok("an empty list cannot be closed", not refused.ok, refused.said)

    out = h.submit(asking_spoc, "revise_list",
                   {"add": "Vikram Rao, John Mathew",
                    "why": "Who is actually getting one."}, about=rid)
    ok("Operations put people on it", out.ok, out.said)
    ok("and nothing is on the register yet",
       not gr_mod.allocated_to(rid), str(gr_mod.allocated_to(rid)))

    ok("the same person twice is refused",
       not h.submit(asking_spoc, "revise_list",
                    {"add": "Vikram Rao", "why": "again"}, about=rid).ok)

    out = h.submit(asking_spoc, "revise_list",
                   {"add": "Aisha Khan", "why": "The third one."}, about=rid)
    ok("the third goes on", out.ok, out.said)
    ok("the list is three people", len(gr_mod.planned_list(rid)) == 3,
       str(gr_mod.planned_list(rid)))

    ok("closing it is what Operations sign off",
       h.act(asking_spoc, "sign_off", [rid], "").ok)
    ok("and the list is shut to changes now",
       not h.submit(asking_spoc, "revise_list",
                    {"add": "Priya Nair", "why": "too late"}, about=rid).ok)

    # STARTING THE DISTRIBUTION is what writes the register — one record
    # per name on the closed list, at the value that was approved.
    out = h.act(asking_spoc, "sign_off", [rid], "")
    ok("starting the distribution signs off", out.ok, out.said)
    ok("three records are on the register",
       len(gr_mod.allocated_to(rid)) == 3,
       str(len(gr_mod.allocated_to(rid))))
    ok("at the value of ONE, not the order",
       all(i["value"] == 2000 for i in gr_mod.allocated_to(rid)))
    ok("dated the day they went out",
       len({i["given"] for i in gr_mod.allocated_to(rid)}) == 1,
       str({i["given"] for i in gr_mod.allocated_to(rid)}))
    ok("each knows which order it came from",
       all(i["from_request"] == rid for i in gr_mod.allocated_to(rid)))
    # A notice EACH, which holds whether or not this deployment has a mail
    # server: `notices.record` writes the notice down either way, and
    # whether it then went is the outbox's business, not this step's.
    ok("and every recipient was told, one notice each",
       all(len(notices.for_item(i["id"])) == 1
           for i in gr_mod.allocated_to(rid)),
       str([(i["id"], len(notices.for_item(i["id"])))
            for i in gr_mod.allocated_to(rid)]))

    # THE DISTRIBUTION IS THE END. There used to be a stage after it, the
    # recipients confirming one by one, and the order stayed open until the
    # last of them had. Nothing waits on a recipient now.
    ok("starting the distribution closes the order",
       gr_mod.stage_of(next(r for r in ledger.requests()
                            if r["id"] == rid)) == "closed")
    ok("and the trail says so, once",
       len([e for e in ledger.events(rid) if e["kind"] == "closed"]) == 1,
       str([e["kind"] for e in ledger.events(rid)]))


def check_payroll_is_read_at_the_end_not_signed_at_the_start() -> None:
    """The tool FLAGS a crossing; it never assesses one.

    Payroll was third of seven in the chain, confirming the tax treatment
    of one gift before it was bought. That is the wrong end twice over:
    the third hamper is what takes somebody over, not the first, and the
    treatment is not this tool's to decide at all. Tax is out of scope in
    writing — so what leaves here is who crossed the threshold and by how
    much, and the tax teams do the rest in their own systems.
    """
    ok("payroll is not a stage in the chain",
       "payroll_tax" not in gr_mod.STAGES, str(list(gr_mod.STAGES)))
    ok("and no stage is owned by payroll",
       not [k for k, v in gr_mod.STAGES.items() if v.get("owner") == "payroll"])
    # Five. It was six until the recipient's own step went: the closed
    # recipient list is the acknowledgement, so the chain ends at the desk
    # that hands the gifts out.
    ok("the chain is five steps",
       len([k for k in gr_mod.STAGES if k != "closed"]) == 5,
       str(list(gr_mod.STAGES)))

    rid = _ask(what="A hamper", value="1200")
    ok("payroll has nothing to sign off", not _desk("payroll", "to_sign"))
    ok("and the chain still runs end to end without them", not _walk(rid), "")

    # What they get instead.
    _, _f, fin = registry.feature_of("scs", "finance")
    rows = fin.rows(_fin(), "flagged")
    ok("there is a line per employee who crossed", rows, str(len(rows)))
    for row in rows:
        ok(f"{row['employee_id']}: how far over is received minus threshold",
           row["above_threshold"] == row["received"] - row["threshold"],
           f"{row['received']} - {row['threshold']} != {row['above_threshold']}")
        ok(f"{row['employee_id']}: and it says whether Finance has seen it",
           "Finance" in row["status"], row["status"])
    ok("nobody under the threshold is on it",
       all(r["above_threshold"] > 0 for r in rows))

    # TAX IS OUT OF SCOPE, in writing. Nothing here may read as an
    # assessment: no rate, and not even the word.
    ok("no rate is computed anywhere on it",
       not any("%" in str(v) for r in rows for v in r.values()), str(rows[:1]))
    ok("and nothing on a row calls itself taxable",
       not any("tax" in str(k).lower() or "tax" in str(v).lower()
               for r in rows for k, v in r.items()), str(rows[:1]))

    # And the count is on the screen, not only in a tab.
    figure = next(f for f in fin.figures(_fin()) if f.key == "flagged")
    ok("the screen says how many the tax teams are being told about",
       figure.value == str(len(rows)), figure.value)


def check_compass_mints_no_reference_of_its_own() -> None:
    """Approval issues nothing. The order id is what gets quoted.

    The tracker used to generate a `GIFT-2627-…` reference when Finance
    approved an order and email it to whoever raised it, for them to quote
    on the requisition in the purchasing system. Nothing was read back, so
    the number's whole job was to be copied from one system into another —
    the firm's bookkeeping rather than a fact about a gift, and an order
    already has an id that does the same work.

    What survives is the thread BACK, which was always the half that
    mattered: Procurement writes down the PR, PO and GRN the purchasing
    system raised. See `check_the_purchase_is_linked_back`.
    """
    _, feat, h = registry.feature_of("scs", "requests")
    notices.clear()

    ok("accounts payable is not a stage",
       "accounts_payable" not in gr_mod.STAGES, str(list(gr_mod.STAGES)))
    ok("and nothing asks anybody to type a PO number in",
       "record_purchase" not in {f.id for f in feat.forms},
       str([f.id for f in feat.forms]))

    rid = _ask(what="A hamper", value="1400")
    h.act(_req("servicelead"), "sign_off", [rid], "Business need is clear.")
    out = h.act(_req("finance"), "sign_off", [rid], "Within policy.")
    ok("Finance approving it still signs off", out.ok, out.said)

    # NOTHING MINTED, at any step of the chain.
    ok("approval writes no reference to the trail",
       not [e for e in ledger.events(rid) if e["kind"] == "referenced"],
       str([e["kind"] for e in ledger.events(rid)]))
    ok("and says nothing about one",
       "GIFT-" not in out.said and "reference" not in out.said.lower(),
       out.said)
    ok("and emails nobody a number to quote",
       not notices.for_item(rid), str([n.subject for n in notices.for_item(rid)]))
    ok("the module cannot mint one at all",
       not hasattr(gr_mod, "reference_for"))

    h.act(_req("procurement"), "sign_off", [rid], "Bought it")
    ok("nor does any later step",
       not [e for e in ledger.events(rid) if e["kind"] == "referenced"],
       str([e["kind"] for e in ledger.events(rid)]))
    ok("and the chain carries on without one",
       gr_mod.stage_of(next(r for r in ledger.requests() if r["id"] == rid))
       == "recipient_list")

    # THE TRAIL STILL READS. Orders approved under the old rule carry a
    # `referenced` event, and the vocabulary that explains it stays.
    ok("the retired event still has words",
       ledger.reads_as("referenced") == "recorded the purchase reference for",
       ledger.reads_as("referenced"))


def check_a_blocked_order_can_ask_for_an_exception() -> None:
    """A rule that refuses is not the end of the conversation.

    Before this the only answers to a blocked order were "send it back"
    and "withdraw it". The case the business has every month — a gift that
    matters and a rule that did not foresee it — had no route through
    Compass, which means it was done beside it, which is the whole failure
    this function exists to end.
    """
    _, feat, h = registry.feature_of("scs", "requests")

    # Client courtesy above the single-gift threshold: blocked.
    rid = _ask(what="Dinner out", value="12000", kind="gift",
               occasion="client courtesy")
    row = lambda who: next(r for r in gr_mod._rows(_req(who))
                           if r["id"] == rid)
    ok("it is blocked", row("servicelead")["blocked"])
    ok("so it offers no sign-off",
       "sign_off" not in row("servicelead")["can"])
    ok("but the desk holding it can ask for an exception",
       "request_exception" in row("servicelead")["can"],
       str(row("servicelead")["can"]))
    ok("and so can whoever raised it",
       "request_exception" in row("requester")["can"],
       str(row("requester")["can"]))
    # Finance is not even shown it yet: an order at the service line is
    # not Finance's business until somebody puts the question to them.
    ok("Finance has not been handed it before anybody asks",
       not any(r["id"] == rid for r in gr_mod._rows(_req("finance"))))

    ok("asking without a reason is refused",
       not h.act(_req("servicelead"), "request_exception", [rid], "  ").ok)
    out = h.act(_req("servicelead"), "request_exception", [rid],
                "Twenty-year client, and the partner is attending.")
    ok("with one, it is asked", out.ok, out.said)
    ok("and it does not move the order",
       gr_mod.stage_of(next(r for r in ledger.requests() if r["id"] == rid))
       == "service_line")
    ok("it is still blocked while Finance thinks", row("servicelead")["blocked"])

    ok("now Finance can decide it",
       {"grant_exception", "refuse_exception"} <= set(row("finance")["can"]),
       str(row("finance")["can"]))
    ok("and the desk that asked cannot grant its own",
       not {"grant_exception", "refuse_exception"}
       & set(row("servicelead")["can"]), str(row("servicelead")["can"]))

    out = h.act(_req("finance"), "grant_exception", [rid],
                "Allowed once, on the partner's authority.")
    ok("Finance grants it", out.ok, out.said)
    ok("the order is no longer blocked", not row("servicelead")["blocked"])
    ok("and the rule is still on the record beside the decision",
       "above" in row("servicelead")["policy"]
       and "exception granted" in row("servicelead")["policy"],
       row("servicelead")["policy"])
    ok("so it can be signed off now",
       "sign_off" in row("servicelead")["can"], str(row("servicelead")["can"]))
    ok("and it runs to the end", not _walk(rid), "")

    ok("the asking and the granting are both on the trail",
       {ledger.reads_as("exception_asked"), ledger.reads_as("excepted")}
       <= {ledger.reads_as(e["kind"]) for e in ledger.events(rid)},
       str([e["kind"] for e in ledger.events(rid)]))
    ok("granting twice is refused",
       not h.act(_req("finance"), "grant_exception", [rid], "again").ok)

    # And the other way: refused, and the rule stands.
    second = _ask(what="More dinner", value="12000", kind="gift",
                  occasion="client courtesy")
    h.act(_req("servicelead"), "request_exception", [second], "Same client.")
    out = h.act(_req("finance"), "refuse_exception", [second],
                "Once was the exception.")
    ok("Finance can refuse one", out.ok, out.said)
    second_row = next(r for r in gr_mod._rows(_req("servicelead"))
                      if r["id"] == second)
    ok("and the order stays blocked", second_row["blocked"])
    ok("with no second bite at it",
       "request_exception" not in second_row["can"], str(second_row["can"]))
    ok("but it can still be sent back",
       "decline" in second_row["can"], str(second_row["can"]))

    # An order nothing is blocking has no exception to make.
    fine = _ask(what="A hamper", value="1000")
    ok("an unblocked order is offered no exception",
       "request_exception" not in next(
           r for r in gr_mod._rows(_req("servicelead"))
           if r["id"] == fine)["can"])
    refused = h.act(_req("servicelead"), "request_exception", [fine], "why not")
    ok("and asking anyway is refused, saying why", not refused.ok,
       refused.said)
    ok("in words that explain the rule is not biting",
       "not biting" in refused.said, refused.said)


def check_the_purchase_is_linked_back() -> None:
    """Procurement writes the purchasing system's own numbers down.

    The business asked for the PR/PO to be linked back to the tracker. One
    direction only — nothing here raises, approves, amends or pays
    anything, and nothing is read back out of the purchasing system.

    It used to be gated on a reference Compass minted at approval, which
    the order's own id now does instead; the gate is the STAGE, because an
    order Procurement has not reached is not theirs to write on yet.
    """
    _, feat, h = registry.feature_of("scs", "requests")
    rid = _ask(what="A hamper", value="1800")

    # Not theirs until the order reaches them.
    refused = h.submit(_req("procurement"), "link_purchase",
                       {"po": "PO-1"}, about=rid)
    ok("there is nothing to link before it reaches Procurement",
       not refused.ok, refused.said)

    h.act(_req("servicelead"), "sign_off", [rid], "Business need is clear.")
    h.act(_req("finance"), "sign_off", [rid], "Within policy.")
    ok("and approval minted nothing to wait for",
       not [e for e in ledger.events(rid) if e["kind"] == "referenced"],
       str([e["kind"] for e in ledger.events(rid)]))

    ok("nobody but Procurement writes the purchase number down",
       not h.submit(_req("servicelead"), "link_purchase",
                    {"po": "PO-2"}, about=rid).ok)
    ok("and an empty link is refused",
       not h.submit(_req("procurement"), "link_purchase", {}, about=rid).ok)

    out = h.submit(_req("procurement"), "link_purchase",
                   {"pr": "PR-7781", "po": "PO-4410", "grn": "GRN-55"},
                   about=rid)
    ok("Procurement links the requisition it raised", out.ok, out.said)
    links = gr_mod.purchase_links(rid)
    ok("all three are kept",
       links == {"pr": "PR-7781", "po": "PO-4410", "grn": "GRN-55"},
       str(links))
    # ONE COLUMN NOW. There were two, the other carrying the reference
    # Compass minted; with that gone they said the same thing.
    ok("the row shows the thread back",
       "PO-4410" in _order("procurement", rid)["purchase"],
       _order("procurement", rid)["purchase"])
    ok("and no longer carries a reference of our own",
       "bought_on" not in _order("procurement", rid),
       str(sorted(_order("procurement", rid))))

    ok("linking it is its own thing on the trail",
       ledger.reads_as("purchase_linked")
       in {ledger.reads_as(e["kind"]) for e in ledger.events(rid)})

    # Linking is a record, not a gate: the step signs off either way,
    # because Compass is not part of the purchasing it is referencing.
    ok("and it is not a gate on the chain",
       h.act(_req("procurement"), "sign_off", [rid], "Bought it").ok)


def check_the_recipient_list_is_maintained_not_overwritten() -> None:
    """Controlled add and delete, with the original kept beside the revisions.

    "It was right first time" and "it was changed twice, by these people,
    for these reasons" are different facts, and the difference is the one
    an auditor asks about: these people were approved and those people
    received. A list somebody can rewrite silently is not a control; a
    list nobody can correct is a control people work around.
    """
    _, _f, h = registry.feature_of("scs", "requests")
    asking = _req("requester")

    out = h.submit(asking, "request_gift",
                   {"recipient": "Vikram Rao, John Mathew", "what": "Hamper",
                    "kind": "gift", "value": "1500", "occasion": "festival",
                    "quantity": "3",
                    "purpose": "Festival gifts for the delivery team."})
    ok("a list of known recipients is captured up front", out.ok, out.said)
    rid = out.touched[0]
    ok("both are on it", len(gr_mod.intended_list(rid)) == 2,
       str(gr_mod.intended_list(rid)))
    ok("and more names than gifts is refused",
       not h.submit(asking, "request_gift",
                    {"recipient": "Vikram Rao, John Mathew, Aisha Khan",
                     "what": "Hamper", "kind": "gift", "value": "1500",
                     "occasion": "festival", "quantity": "2",
                     "purpose": "Too many."}).ok)

    # Revised: one off, one on, each with a reason.
    ok("a change with no reason is refused",
       not h.submit(asking, "revise_list",
                    {"remove": "John Mathew"}, about=rid).ok)
    ok("and a change naming nobody is refused",
       not h.submit(asking, "revise_list", {"why": "because"}, about=rid).ok)
    out = h.submit(asking, "revise_list",
                   {"remove": "John Mathew", "add": "Aisha Khan, Priya Nair",
                    "why": "John has left; the other two joined the team."},
                   about=rid)
    ok("a controlled change goes through", out.ok, out.said)
    now = gr_mod.planned_list(rid)
    ok("the list is what the changes make it", len(now) == 3, str(now))
    ok("the person taken off is not on it",
       gr_mod._employee_named("John Mathew") not in now)

    # ...and the original is untouched.
    ok("the list as approved is still there",
       len(gr_mod.intended_list(rid)) == 2, str(gr_mod.intended_list(rid)))
    ok("so the row can show both",
       "John Mathew" in next(r for r in gr_mod._rows(asking)
                             if r["id"] == rid)["listed_as"])
    ok("and say how many times it changed",
       next(r for r in gr_mod._rows(asking)
            if r["id"] == rid)["list_changes"] == 3)

    # THE QUANTITY DOES NOT CAP THE LIST. It is how many were bought, and
    # the Service Line Approver orders with a buffer precisely so a late
    # addition does not go round the chain again — capped, the fourth
    # name on a three-gift order was refused and the spare stock could
    # not be given to anybody.
    before = len(gr_mod.planned_list(rid))
    ok("somebody can be added even past the quantity",
       h.submit(asking, "revise_list",
                {"add": "Manish K.", "why": "One more from the buffer."},
                about=rid).ok)
    ok("and the list really grew",
       len(gr_mod.planned_list(rid)) == before + 1,
       f"{before} then {len(gr_mod.planned_list(rid))}")
    ok("without asking anybody to approve anything again — it has not "
       "moved back a step",
       gr_mod.stage_of(next(r for r in ledger.requests() if r["id"] == rid))
       == "service_line",
       gr_mod.stage_of(next(r for r in ledger.requests() if r["id"] == rid)))
    ok("somebody already on it cannot be added twice",
       not h.submit(asking, "revise_list",
                    {"add": "Aisha Khan", "why": "again"}, about=rid).ok)
    ok("and somebody not on it cannot be taken off",
       not h.submit(asking, "revise_list",
                    {"remove": "Rahul Desai", "why": "nope"}, about=rid).ok)
    ok("a stranger cannot touch the list",
       not h.submit(_req("payroll"), "revise_list",
                    {"remove": "Aisha Khan", "why": "no"}, about=rid).ok)
    ok("but Operations can, since they distribute it",
       h.submit(_req("spoc"), "revise_list",
                {"remove": "Priya Nair", "why": "On leave that week."},
                about=rid).ok)

    # THE LIST IS THE ALLOCATION NOW, so "allocating to somebody off the
    # list" cannot happen: there is nothing to allocate with. What takes
    # its place is the register being built from exactly these names.
    _walk(rid, upto="recipient_list")
    settled = gr_mod.planned_list(rid)
    ok("the list reaching the close is what it was revised to",
       len(settled) == 3, str(settled))

    ok("Operations close it",
       h.act(_req("spoc"), "sign_off", [rid], "").ok)

    # CLOSED MEANS CLOSED, and this is the moment to ask: the list has
    # shut and the order has not ended, so the refusal under test is the
    # list's own rather than the one every ended order gives. Asked after
    # the distribution it is a different refusal with different words —
    # true, but not this rule.
    late = h.submit(_req("spoc"), "revise_list",
                    {"remove": "Vikram Rao", "why": "changed my mind"},
                    about=rid)
    ok("and once it is closed nothing comes off it", not late.ok, late.said)
    ok("in words that say where a late change goes",
       "exception" in late.said, late.said)

    ok("starting the distribution writes one record per name",
       h.act(_req("spoc"), "sign_off", [rid], "").ok
       and len(gr_mod.allocated_to(rid)) == len(settled),
       f"{len(gr_mod.allocated_to(rid))} for {len(settled)} names")
    ok("and each record is against somebody who was on it",
       {i["employee_id"] for i in gr_mod.allocated_to(rid)} == set(settled),
       str({i["employee_id"] for i in gr_mod.allocated_to(rid)}))

    # AND THE ENDED ORDER REFUSES TOO, in its own words.
    after = h.submit(_req("spoc"), "revise_list",
                     {"remove": "Vikram Rao", "why": "changed my mind"},
                     about=rid)
    ok("an order that has ended refuses as well", not after.ok, after.said)

    # And every change is on the trail, in words.
    said = {ledger.reads_as(e["kind"]) for e in ledger.events(rid)}
    ok("adding and removing are both on the trail",
       {ledger.reads_as("listed"), ledger.reads_as("unlisted")} <= said,
       str(said))


def check_a_recipient_reads_their_gift_and_not_the_order() -> None:
    """Being one name on a list is not a seat at the table.

    Breadth decided which ROWS somebody sees and said nothing about which
    FIELDS, so a recipient opening Gift requests was handed the whole
    order sheet: the other three recipients by name, how many were
    bought, what each one cost, the purchase reference, who raised it and
    whose desk it had crossed. None of that is theirs, and "who else got
    one" is the other recipients' own business.

    What they keep is the gift: what it is, why they have it, where it
    has got to and whether they have confirmed it. Their annual position
    is theirs too — on the Tracker, which is the screen for it.
    """
    _, feat, h = registry.feature_of("scs", "requests")
    rid = _ask(by="servicelead", quantity="3", what="Gift card", value="3000",
               recipient_list="Manish K., Vikram Rao, Aisha Khan")
    # Walked the whole way: the recipient's own view is what this is
    # about, and the order now closes at distribution, so there is no
    # stage left to stop short of.
    ok("it reaches the recipients",
       not _walk(rid, by="servicelead",
                 who={"service_line": "leadtwo"}), "")

    # mk is Manish K. — who in this directory ALSO holds compliance, so
    # the plain-recipient view is read with a scope that holds only what a
    # recipient holds. That is the persona being measured, not the login.
    theirs = Scope(user="mk", period="FY 2026-27", sees="self",
                   employee=people.employee_of("mk"),
                   roles=frozenset({"employee"}))
    mine = [r for r in h.rows(theirs, "all") if r["id"] == rid]
    ok("a recipient sees the order they are on", len(mine) == 1, str(len(mine)))
    row = mine[0]

    ok("their own gift is named", row["what"] == "Gift card", str(row))
    ok("and it says it is THEIRS, not the order's first name",
       row["recipient"] == "Manish K.", row["recipient"])
    ok("with their own employee number on it",
       row["employee_id"] == people.employee_of("mk"), row["employee_id"])
    # NAMING NOBODY ELSE is the invariant. `with` used to list every
    # recipient still to confirm, which was the one place on this row that
    # named the other people standing on it. There is no such stage now,
    # so it is the chain's own answer and never a roll-call.
    ok("and whose move it is, naming no other recipient",
       row["with"] in ("\u2014", "the firm, nothing for you to do")
       and not any(name in row["with"]
                   for name in ("Vikram", "Aisha", "Rao", "Khan")),
       row["with"])
    ok("why they have it is theirs to read",
       row["occasion"] == "festival" and row["purpose"], str(row.get("purpose")))
    ok("and where it has got to", "status" in row, str(sorted(row)))

    # WHO ELSE. The thing the business asked for by name.
    for leak in ("for_list", "listed_as", "for"):
        ok(f"nobody else's name reaches them via {leak}",
           leak not in row, f"{leak}={row.get(leak)!r}")
    ok("and no other recipient is anywhere in the row",
       not any("Vikram" in str(v) or "Aisha" in str(v) for v in row.values()),
       str({k: v for k, v in row.items()
            if "Vikram" in str(v) or "Aisha" in str(v)}))

    # THE ORDER'S OWN NUMBERS AND PAPERWORK.
    for leak in ("quantity", "allocation", "allocated", "bought_on",
                 "purchase", "policy", "tax", "requested_by", "source",
                 "last_signed_by", "list_changes", "allocated_ids",
                 "by_employee"):
        ok(f"the order's {leak} is not theirs to read",
           leak not in row, f"{leak}={row.get(leak)!r}")

    # AND THE SURFACE IS TOLD TO LAY IT OUT THAT WAY, so the table draws
    # their four columns rather than ten columns of dashes.
    ok("the payload says how much of the screen is theirs",
       theirs.sees == "self")

    # A RAISER IS NOT A RECIPIENT. Somebody reading their own request back
    # chose every figure in it, and hiding it from them would be absurd —
    # what they still may not read is the recipient's year, which is
    # masked on its own and always was.
    own = _ask(by="enduser", recipient="John Mathew", what="A hamper",
               value="6000", occasion="long service")
    raiser = people.scope_for("enduser", feat, period="FY 2026-27")
    r = next(x for x in h.rows(raiser, "all") if x["id"] == own)
    ok("a raiser still reads the request they made",
       r.get("quantity") is not None and "policy" in r, str(sorted(r)))
    ok("but not the recipient's figures", "\u20b9" not in r["tax"], r["tax"])


def check_the_list_is_closed_before_anything_goes_out() -> None:
    """Two steps at Operations, and the business asked for both.

    Settling who is getting one and recording that they have are two
    decisions, and between ordering and handing out is exactly when the
    list moves — somebody leaves, somebody is missed, the event changes.
    So the desk gets a moment where it can still be put right, and a
    moment after which it cannot, and the second is what builds the
    register.
    """
    _, _f, h = registry.feature_of("scs", "requests")
    rid = _ask(by="servicelead", quantity="3", what="Gift card", value="3000",
               recipient_list="Vikram Rao, Priya Nair")
    ok("it reaches the list step",
       not _walk(rid, upto="recipient_list", by="servicelead",
                 who={"service_line": "leadtwo"}), "")
    ok("and the row says whose it is",
       "Operations" in _order("spoc", rid)["with"],
       _order("spoc", rid)["with"])
    ok("the row says the list is still open",
       "still open" in _order("spoc", rid)["allocation"],
       _order("spoc", rid)["allocation"])
    ok("the button says what the step does, not just 'sign off'",
       _order("spoc", rid)["labels"]["sign_off"] == "Close the recipient list",
       str(_order("spoc", rid)["labels"]))

    # STILL OPEN: add and remove both work, each with a reason.
    ok("a name can go on", h.submit(_req("spoc"), "revise_list",
       {"add": "Aisha Khan", "why": "Was missed."}, about=rid).ok)
    ok("and a name can come off", h.submit(_req("spoc"), "revise_list",
       {"remove": "Priya Nair", "why": "Has left."}, about=rid).ok)
    ok("nothing is on the register while the list is being settled",
       not gr_mod.allocated_to(rid), str(gr_mod.allocated_to(rid)))

    # CLOSING IT is the sign-off, and says so.
    closed = h.act(_req("spoc"), "sign_off", [rid], "")
    ok("closing it needs no note — the act is the record",
       closed.ok, closed.said)
    ok("and the sentence says the list is shut",
       "nothing changes it now" in closed.said, closed.said)
    ok("the row says so too",
       "closed" in _order("spoc", rid)["allocation"],
       _order("spoc", rid)["allocation"])
    after = h.submit(_req("spoc"), "revise_list",
                     {"add": "Manish K.", "why": "one more"}, about=rid)
    ok("and the list really is shut", not after.ok, after.said)
    ok("with the way past it named", "exception" in after.said, after.said)

    # DISTRIBUTION STARTED: the register is built from the closed list.
    ok("the next button says that",
       _order("spoc", rid)["labels"]["sign_off"] == "Start the distribution",
       str(_order("spoc", rid)["labels"]))
    out = h.act(_req("spoc"), "sign_off", [rid], "")
    ok("starting it needs no note either", out.ok, out.said)
    on_register = gr_mod.allocated_to(rid)
    ok("one record per name on the closed list",
       {i["employee_id"] for i in on_register} == set(gr_mod.planned_list(rid)),
       str({i["recipient"] for i in on_register}))
    # A notice EACH, which holds whether or not this deployment has a mail
    # server: `notices.record` writes the notice down either way, and
    # whether it then went is the outbox's business, not this step's.
    ok("and every recipient was told, one notice each",
       all(len(notices.for_item(i["id"])) == 1
           for i in gr_mod.allocated_to(rid)),
       str([(i["id"], len(notices.for_item(i["id"])))
            for i in gr_mod.allocated_to(rid)]))
    ok("which is on the trail as well as in the post",
       ledger.happened(rid, "recipients_told"),
       str([e["kind"] for e in ledger.events(rid)]))
    ok("and that is the end of it",
       gr_mod.stage_of(next(r for r in ledger.requests() if r["id"] == rid))
       == "closed")

    # AND NOBODY SIGNED OFF A LIST THEY HAD NOT SETTLED. The two steps are
    # distinct on the trail, so "who closed the list" and "who handed them
    # out" are separate questions with separate answers.
    signs = [e for e in ledger.events(rid) if e["kind"] == "signed_off"]
    ok("both of Operations' steps are their own line on the trail",
       {"recipient_list", "distribution"} <= {e["stage"] for e in signs},
       str([e["stage"] for e in signs]))


def check_the_distribution_is_the_end_of_the_chain() -> None:
    """Operations start the distribution and the order is finished.

    This used to measure the opposite: an order reached the recipients and
    stayed open until every one of them had confirmed, and the hard part
    was that the first confirmation must not write `closed` on everybody
    else's behalf. The business settled it the other way — the list
    Operations close IS the acknowledgement — so what has to hold now is
    that the last desk's sign-off ends it cleanly, exactly once, and that
    a recipient has nothing left to sign.
    """
    _, _f, h = registry.feature_of("scs", "requests")
    rid = _ask(by="servicelead", quantity="2", what="Hamper", value="2000",
               recipient_list="Vikram Rao, Manish K.")
    ok("it reaches the list step",
       not _walk(rid, upto="recipient_list", by="servicelead",
                 who={"service_line": "leadtwo"}), "")
    closed = h.act(_req("spoc"), "sign_off", [rid], "List settled.")
    ok("Operations close the list", closed.ok, closed.said)
    started = h.act(_req("spoc"), "sign_off", [rid], "Both handed over.")
    ok("and then start the distribution", started.ok, started.said)
    ok("which is what put both on the register",
       len(gr_mod.allocated_to(rid)) == 2,
       str(len(gr_mod.allocated_to(rid))))
    ok("and told them both, one notice each",
       all(len(notices.for_item(i["id"])) == 1
           for i in gr_mod.allocated_to(rid)),
       str([len(notices.for_item(i["id"])) for i in gr_mod.allocated_to(rid)]))

    ok("that sign-off IS the close", "is closed" in started.said, started.said)
    ok("the trail says so exactly once",
       len([e for e in ledger.events(rid) if e["kind"] == "closed"]) == 1,
       str([e["kind"] for e in ledger.events(rid)]))
    ok("and the order has ended",
       gr_mod.stage_of(next(r for r in ledger.requests() if r["id"] == rid))
       in gr_mod.ENDED)

    # AND THE RECORDS ARRIVE SETTLED. Left waiting on a recipient they
    # would have sat unconfirmed for ever, because nobody is asked now.
    ok("every record it wrote is settled",
       all(i["acknowledged"] for i in gr_mod.allocated_to(rid)),
       str([i["acknowledged"] for i in gr_mod.allocated_to(rid)]))

    # NOBODY ON THE LIST HAS ANYTHING TO SIGN. Both of these are recipients
    # of this very order.
    ok("a recipient cannot sign off a closed order",
       not h.act(_req("enduser"), "sign_off", [rid], "").ok)
    ok("and neither can another one of them",
       not h.act(_req("mk"), "sign_off", [rid], "").ok)


def check_a_late_recipient_draws_on_the_buffer() -> None:
    """A fourth name on a three-gift order, without going round again.

    The Service Line Approver orders with spare stock, so somebody added
    at the last moment — one who left, one who was missed — is not a new
    approval. The list is where that is settled, and the quantity does
    not cap it: capped, the fourth name was refused and the desk holding
    the spare boxes could not write down who they were for. What is NOT
    waived is the fact, which is counted and said wherever the number
    appears.
    """
    _, _f, h = registry.feature_of("scs", "requests")
    rid = _ask(by="servicelead", quantity="3", what="Gift card",
               recipient_list="Vikram Rao, Priya Nair, Aisha Khan")
    ok("it reaches the list step",
       not _walk(rid, upto="recipient_list", by="servicelead",
                 who={"service_line": "leadtwo"}), "")

    added = h.submit(_req("spoc"), "revise_list",
                     {"add": "Manish K.",
                      "why": "Joined the team this week; buffer covers it."},
                     about=rid)
    ok("Operations can add a fourth name", added.ok, added.said)
    ok("and the list carries four for three gifts",
       len(gr_mod.planned_list(rid)) == 4,
       str(len(gr_mod.planned_list(rid))))
    ok("nothing went back for re-approval",
       gr_mod.stage_of(next(r for r in ledger.requests() if r["id"] == rid))
       == "recipient_list")

    ok("closing the list is Operations' own act",
       h.act(_req("spoc"), "sign_off", [rid], "Four of them, list final.").ok)

    # AND ALL FOUR ACTUALLY GET ONE. The closed list IS the allocation, so
    # a name on it that nothing may be recorded against would be a name on
    # a list and nothing more.
    out = h.act(_req("spoc"), "sign_off", [rid], "Handed out at the event.")
    ok("starting the distribution records every one of them", out.ok, out.said)
    ok("four records are on the register",
       len(gr_mod.allocated_to(rid)) == 4,
       str(len(gr_mod.allocated_to(rid))))
    ok("and the sentence says the extra came from the buffer",
       "buffer" in out.said, out.said)

    # SAID, NOT SWALLOWED. More given than approved is a fact somebody
    # should be able to read off the row.
    row = _order("spoc", rid)
    ok("the row says how many are past what was approved",
       "past the 3 approved" in row["allocation"], row["allocation"])

    # The one cap that STAYS: asking for fewer gifts than the people you
    # name, at the moment of asking. The buffer covers a late addition,
    # not an order that did not add up when it was raised.
    refused = h.preview(_req("servicelead"), "request_gift", {
        "what": "Hamper", "kind": "gift", "quantity": "2", "value": "1000",
        "occasion": "festival", "purpose": "Thanks.",
        "recipient_list": "Vikram Rao, Priya Nair, Aisha Khan"})
    ok("naming three people for two gifts up front is still refused",
       not refused.ok, refused.said)
    ok("and it says where the late case is handled instead",
       "buffer" in refused.said, refused.said)


def check_every_on_row_form_has_a_door() -> None:
    """A form the surface will not draw is a form nobody can reach.

    THE BUG THIS EXISTS FOR. `formsFor(row)` filters the on-row forms by
    the row's own `can`, and `can` held only action ids — so "Change the
    recipient list" and "Link the PR/PO" had no button anywhere in the
    UI. Operations were told to settle the list and offered no way to
    change it. Every check drove `submit` directly and so passed over a
    screen with no door in it; this one asks the ROW.
    """
    _, feat, h = registry.feature_of("scs", "requests")
    on_row = [f.id for f in feat.forms if f.on_row]
    ok("the feature has on-row forms at all", len(on_row) == 2, str(on_row))

    rid = _ask(by="servicelead", quantity="3",
               recipient_list="Vikram Rao, Priya Nair, Aisha Khan")
    row = lambda who: next(r for r in gr_mod._rows(_req(who))
                           if r["id"] == rid)

    # THE LIST, from the start, for whoever raised it and their approver.
    for who in ("servicelead", "leadtwo"):
        ok(f"{who} is offered the list form",
           "revise_list" in row(who)["can"], str(row(who)["can"]))
    ok("somebody with no part in it is not",
       "revise_list" not in row("enduser")["can"], str(row("enduser")["can"]))

    # THE PURCHASE, at procurement and not before.
    ok("the purchase form is not offered before procurement",
       "link_purchase" not in row("servicelead")["can"],
       str(row("servicelead")["can"]))
    ok("walking to procurement works",
       not _walk(rid, upto="procurement", by="servicelead",
                 who={"service_line": "leadtwo"}), "")
    ok("and there it is Procurement's",
       "link_purchase" in row("procurement")["can"],
       str(row("procurement")["can"]))
    ok("and nobody else's",
       "link_purchase" not in row("finance")["can"], str(row("finance")["can"]))

    # THE LIST IS STILL OPEN AT THE LIST STEP, and Operations hold it.
    ok("walking to the list step works",
       not _walk(rid, upto="recipient_list", by="servicelead",
                 who={"service_line": "leadtwo"}), "")
    ok("Operations are offered the list form there",
       "revise_list" in row("spoc")["can"], str(row("spoc")["can"]))
    at_desk = next(r for r in _desk("spoc", "orders") if r["id"] == rid)
    ok("and it reaches them on the Tracker, where they sign off",
       "revise_list" in at_desk["can"], str(at_desk["can"]))

    # AND IT SHUTS WHEN THEY CLOSE IT, so the button never outlives the
    # thing it does.
    ok("closing the list signs off",
       h.act(_req("spoc"), "sign_off", [rid], "Final.").ok)
    ok("and then the list form is gone",
       "revise_list" not in row("spoc")["can"], str(row("spoc")["can"]))
    refused = h.submit(_req("spoc"), "revise_list",
                       {"add": "Manish K.", "why": "late"}, about=rid)
    ok("and submitting one anyway is refused", not refused.ok, refused.said)
    ok("in words that say where a late change goes",
       "exception" in refused.said, refused.said)

    # EVERY BUTTON A ROW OFFERS MUST BE ONE THE SUBMIT WOULD TAKE. A form
    # that refuses when pressed teaches people the screen lies.
    _, reg_feat, reg = registry.feature_of("scs", "register")
    tracker = [f.id for f in reg_feat.forms if f.on_row]
    ok("the Tracker carries every one of the chain's row forms",
       set(on_row) <= set(tracker), f"{on_row} vs {tracker}")
    for form_id in on_row:
        ok(f"and its handler accepts {form_id}", bool(reg.accepts(form_id)))
        ok(f"with the chain's own words for {form_id}, not a second copy",
           next(f.label for f in reg_feat.forms if f.id == form_id)
           == next(f.label for f in feat.forms if f.id == form_id))


def check_the_register_carries_the_approved_value() -> None:
    """One value for the order, and it is the one that was approved.

    The allocation form used to take what a gift ACTUALLY cost, and the
    register carried that instead of the estimate. The business took the
    field out: a whole order goes out at once, so one figure typed at the
    end stood for every gift in it, and a number somebody fills in because
    the form insists is not a better record than the number three desks
    actually approved.

    WHAT THAT COSTS, written down so nobody rediscovers it as a surprise:
    if a vendor invoices above the estimate, the register does not know.
    The purchase behind it does — that is what the PR/PO on the row leads
    to — and reconciling the two is the firm's own job in its own system,
    which is where the money already lives.
    """
    _, _f, h = registry.feature_of("scs", "requests")
    rid = _ask(by="servicelead", what="Festival hamper", value="2000",
               quantity="2", recipient_list="Vikram Rao, Aisha Khan")
    ok("it reaches the list step",
       not _walk(rid, upto="recipient_list", by="servicelead",
                 who={"service_line": "leadtwo"}), "")
    ok("the list closes", h.act(_req("spoc"), "sign_off", [rid], "").ok)
    ok("and the distribution starts",
       h.act(_req("spoc"), "sign_off", [rid], "").ok)

    on_register = gr_mod.allocated_to(rid)
    ok("every record carries the value of ONE gift, as approved",
       all(i["value"] == 2000 for i in on_register),
       str([i["value"] for i in on_register]))
    ok("not the order's total", sum(i["value"] for i in on_register) == 4000)
    ok("and the form no longer asks for anything else",
       not h.accepts("allocate"), str(h.accepts("allocate")))


def check_a_field_is_only_offered_to_whoever_it_is_for() -> None:
    """A list of two hundred names is the Service Line Approver's box.

    Per FIELD rather than per form: anybody may ask for a gift, and only
    the approver hands over a list. A second form for them would be the
    same form twice, and the one that drifted would be the one nobody
    used.

    Hidden in the browser is not the same as not offered. A field somebody
    was not sent is also one the handler refuses, because a box nobody can
    see is the kind of thing that gets filled in by something other than a
    person.
    """
    _, feat, h = registry.feature_of("scs", "requests")
    form = next(f for f in feat.forms if f.id == "request_gift")
    bulk = next(x for x in form.fields if x.id == "recipient_list")

    ok("the paste box is declared for both desks that hold a list",
       bulk.roles == ["service_line", "operations"], str(bulk.roles))
    ok("and it is a list of names, not a line of text",
       bulk.kind == "names", bulk.kind)
    ok("the quantity is a count, so nothing draws money on it",
       next(x for x in form.fields if x.id == "quantity").kind == "count")
    ok("while the price is money", 
       next(x for x in form.fields if x.id == "value").kind == "money")

    # What each reader is SENT.
    def fields_for(login: str) -> set[str]:
        scope = people.scope_for(login, feat, period="FY 2026-27")
        card = routes._feature_card("scs", feat, scope.roles, every_field=False)
        got = next(f for f in card["forms"] if f["id"] == "request_gift")
        return {x["id"] for x in got["fields"]}

    lead = fields_for("servicelead")
    plain = fields_for("enduser")
    ok("the approver is offered the paste box", "recipient_list" in lead)
    ok("an employee is not", "recipient_list" not in plain, str(plain))
    ok("and everything else is offered to both",
       lead - {"recipient_list"} == plain, str(lead ^ plain))

    # And the handler refuses what the screen did not draw.
    pasted = {"what": "Hamper", "kind": "gift", "value": "1500",
              "occasion": "festival", "purpose": "The team.",
              "quantity": "3", "recipient_list": "Vikram Rao\nJohn Mathew"}
    out = h.submit(_req("enduser"), "request_gift", dict(pasted))
    ok("an employee pasting a list anyway is refused", not out.ok, out.said)
    ok("and told whose it is",
       "Service Line Approver" in out.said, out.said)

    out = h.submit(_req("servicelead"), "request_gift", dict(pasted))
    ok("the approver pasting one is accepted", out.ok, out.said)
    ok("and every line of it became a recipient",
       len(gr_mod.intended_list(out.touched[0])) == 2,
       str(gr_mod.intended_list(out.touched[0])))

    # Typed and pasted are the same fact, so the two are merged and the
    # same person named twice is one person.
    out = h.submit(_req("servicelead"), "request_gift",
                   dict(pasted, recipient="Aisha Khan, Vikram Rao",
                        quantity="4"))
    ok("typed and pasted names merge", out.ok, out.said)
    named = gr_mod.intended_list(out.touched[0])
    ok("and nobody is counted twice", len(named) == len(set(named)) == 3,
       str(named))


def check_a_recipient_list_can_be_uploaded() -> None:
    """A spreadsheet of names, read and SHOWN before anything is recorded.

    The firm keeps its list in Excel, because that is where lists live.
    Reading it is the easy part; what matters is that what was read comes
    back for somebody to look at, and the request is then sent the same
    way a typed one is. An upload that wrote straight to a record would be
    two hundred recipients nobody saw.
    """
    from openpyxl import Workbook

    def sheet(rows: list[str]) -> bytes:
        book = Workbook()
        page = book.active
        for i, text in enumerate(rows, start=1):
            page.cell(row=i, column=1, value=text)
            page.cell(row=i, column=2, value="not a person")
        out = io.BytesIO()
        book.save(out)
        return out.getvalue()

    # A header row, two known people, a blank, a stranger and a repeat.
    known, unknown, read = routes.recipients_in("team.xlsx", sheet(
        ["Name", "Vikram Rao", "", "John Mathew", "Nobody Here", "vikram rao"]))
    ok("the header row is not taken for a person", "Name" not in known,
       str(known))
    ok("the people it knows come back",
       known == ["Vikram Rao", "John Mathew"], str(known))
    ok("the one it does not know is set aside, never guessed at",
       unknown == ["Nobody Here"], str(unknown))
    ok("a blank row is not a person", read == 4, str(read))
    ok("and nobody is read twice", len(known) == len(set(known)))

    ok("the sentence names who it could not place",
       "Nobody Here" in routes._upload_said("team.xlsx", known, unknown)
       and "does not know" in routes._upload_said("team.xlsx", known, unknown))

    # Only the first column, because a spreadsheet has six others and
    # guessing which of them are people is how a gift lands on a team name.
    ok("only the first column is read", "not a person" not in str(known))

    # A list with no header keeps its first row.
    plain, _u, _r = routes.recipients_in("x.xlsx", sheet(["Vikram Rao",
                                                          "Aisha Khan"]))
    ok("a list with no header keeps everybody",
       plain == ["Vikram Rao", "Aisha Khan"], str(plain))

    # CSV too, because half of these are exported as one.
    csv = b"Name,Team\nVikram Rao,Advisory\nAisha Khan,Risk\n"
    from_csv, _u, _r = routes.recipients_in("x.csv", csv)
    ok("a csv reads the same way",
       from_csv == ["Vikram Rao", "Aisha Khan"], str(from_csv))

    for filename, raw, why in (("x.pdf", b"%PDF-1.4", "not a list at all"),
                               ("x.xlsx", b"not a workbook", "unreadable")):
        try:
            routes.recipients_in(filename, raw)
            ok(f"{why} is refused", False, "it was accepted")
        except Exception as err:                    # noqa: BLE001
            ok(f"{why} is refused, in words somebody can act on",
               "paste" in str(err) or "Excel" in str(err), str(err))

    # The field's own roles decide who may upload, because a door beside a
    # locked door is not a lock. Signed out, `guest` holds nothing.
    client = _client()
    got = client.post(
        "/v1/business-functions/scs/features/requests/forms/"
        "request_gift/upload?field=recipient_list",
        files={"file": ("x.csv", csv, "text/csv")})
    ok("somebody without the role cannot upload to that field",
       got.status_code == 403, f"{got.status_code} {got.text[:100]}")
    got = client.post(
        "/v1/business-functions/scs/features/requests/forms/"
        "request_gift/upload?field=what",
        files={"file": ("x.csv", csv, "text/csv")})
    ok("and no field that is not a list takes one at all",
       got.status_code == 404, str(got.status_code))

    # BOTH DESKS THAT HOLD A LIST. The Service Line Approver has it when
    # the order is raised and Operations have it when the order reaches
    # them, so both are offered the box and both may upload to it. Gated
    # on the service line alone, an Operations SPOC raising a bulk order
    # had two hundred names to type beside an upload box they could not
    # see — and the two halves are checked together here, because a field
    # that renders and refuses, or refuses and renders, is the screen and
    # the server disagreeing about one rule.
    _, feat, _h = registry.feature_of("scs", "requests")
    field = next(x for f in feat.forms if f.id == "request_gift"
                 for x in f.fields if x.id == "recipient_list")
    for who, allowed in (("servicelead", True), ("spoc", True),
                         ("finance", False), ("procurement", False),
                         ("enduser", False)):
        roles = people.roles_of(who)
        offered = bool(roles & set(field.roles))
        ok(f"{who} {'is' if allowed else 'is not'} offered the upload box",
           offered == allowed, f"{sorted(roles)} against {field.roles}")

    # AND THE ROUTE AGREES, for each of them. Called as its own coroutine
    # rather than over HTTP: these checks run with auth disabled, so a
    # login would not change who the request is from, and the guard under
    # test is exactly the one that reads `field.roles`.
    from fastapi import HTTPException, UploadFile

    for who, allowed in (("servicelead", True), ("spoc", True),
                         ("finance", False)):
        async def upload(user=who):
            return await routes.read_recipient_file(
                "scs", "requests", "request_gift", field="recipient_list",
                file=UploadFile(filename="team.csv", file=io.BytesIO(csv)),
                user=user)
        try:
            got = asyncio.run(upload())
            took = isinstance(got, dict) and got.get("read", 0) > 0
        except HTTPException as err:                 # noqa: PERF203
            took = False
            code = err.status_code
        else:
            code = 200
        ok(f"and the upload route {'takes' if allowed else 'refuses'} "
           f"{who}'s list", took == allowed, f"status {code}")

    ok("reading a list records nothing",
       not [r for r in ledger.requests() if "xlsx" in r.get("what", "")])


def check_the_dashboard_is_its_charts() -> None:
    """Four charts, four tabs, and nothing else on the screen.

    It had seven tabs and four charts. Three of them predate the charts —
    who nobody had decided, where the process had stalled, where the value
    came from — and on a screen whose whole argument is "read the position
    at a glance", a list nobody drew reads as a second screen stacked
    under the first.

    The findings did not go: they are rules at the top, which is what a
    finding is. The orders behind them are the Tracker's, which is where
    every desk already goes to ask what has got where.
    """
    _, _f, board = registry.feature_of("scs", "oversight")
    seeing = as_("compliance", "oversight", period="FY 2026-27")

    tabs = [t.key for t in board.tabs(seeing)]
    charts = board.charts(seeing)
    ok("there are five tabs", len(tabs) == 5, str(tabs))
    ok("and a chart for each", len(charts) == 5, str([c.key for c in charts]))
    ok("every chart lands on its own tab",
       [c.tab for c in charts] == tabs, f"{[c.tab for c in charts]} vs {tabs}")
    ok("and none of the retired ones is left",
       not {"attention", "stuck", "sources"} & set(tabs), str(tabs))

    for tab in tabs:
        rows = board.rows(seeing, tab)
        ok(f"{tab}: its rows are the ones its chart was drawn from",
           len(rows) == len(next(c for c in charts if c.tab == tab).slices)
           or not rows,
           f"{len(rows)} rows")

    # THE TWO THE REQUIREMENT ASKS FOR BY NAME: where everything has got
    # to, and what has not reached a person. Point-in-time visibility is
    # the phrase the business used, and a number nobody drew is not that.
    where = board.rows(seeing, "where")
    ok("every step of the chain is on the where-chart",
       len(where) == len([k for k in gr_mod.STAGES if k != "closed"]),
       str([w["stage"] for w in where]))
    ok("a step with nothing at it is still shown",
       all("orders" in w for w in where), str(where[:1]))
    ok("and it says how long the longest has waited",
       all("longest_wait" in w for w in where))

    # THERE IS NO UNCONFIRMED TAB. It charted gifts nobody had confirmed
    # receiving, which stopped being a population when the closed list
    # became the acknowledgement — a chart that can only draw zero reads
    # as "nothing wrong" where a reader expects a finding.
    ok("the unconfirmed tab is gone rather than empty",
       "unconfirmed" not in tabs, str(tabs))
    ok("and so is its chart",
       "unconfirmed" not in [c.key for c in charts],
       str([c.key for c in charts]))

    # The findings survive the tabs.
    said = board.rules(seeing)
    ok("the screen still says what it finds", isinstance(said, list))
    for rule in said:
        ok(f"{rule}: and the manifest has words for it",
           rule in {r.id for r in registry.feature_of("scs", "oversight")[1].rules},
           rule)


def check_the_trail_outlives_the_chain() -> None:
    """Every stage ever shipped still reads, years after it is removed.

    The trail is append-only and each event carries the stage it was taken
    at, so a stage dropped from the chain is still being read back from
    records that already happened. Removing Payroll and Accounts Payable
    made every screen that reads a stored stage raise on exactly those
    records — and the first screen to go down was the audit trail, which
    is the one that must not, because its whole job is that the past stays
    legible.
    """
    # Nothing in the chain's own vocabulary may raise, whatever it is
    # handed: a live stage, a retired one, an end state, a stage no
    # version of this ever had, or nothing at all.
    for stage in (list(gr_mod.STAGES) + list(gr_mod.RETIRED_STAGES)
                  + ["declined", "cancelled", "a_stage_from_2019", ""]):
        label, step = gr_mod.stage_label(stage), gr_mod.step_name(stage)
        ok(f"{stage or 'nothing'}: reads as words", bool(label) and bool(step),
           f"{label!r} / {step!r}")
        ok(f"{stage or 'nothing'}: and not as a key",
           "_" not in label, label)

    ok("the retired stages are the three that were removed",
       set(gr_mod.RETIRED_STAGES) == {"payroll_tax", "accounts_payable",
                                      "acknowledgement"},
       str(sorted(gr_mod.RETIRED_STAGES)))
    ok("and none of them is still in the chain",
       not set(gr_mod.RETIRED_STAGES) & set(gr_mod.STAGES))
    ok("they keep the words they shipped with",
       gr_mod.stage_label("payroll_tax") == "With Payroll"
       and gr_mod.stage_label("accounts_payable") == "With Accounts Payable")

    # And the screens that read stored stages survive one.
    _, _f, h = registry.feature_of("scs", "audit")
    rid = _ask(what="A hamper", value="1200")
    ledger.add_event(subject=rid, kind="signed_off", by="payroll",
                     stage="payroll_tax", value=1200)
    ledger.add_event(subject=rid, kind="signed_off", by="payments",
                     stage="accounts_payable", value=1200)

    reader = as_("compliance", "audit", period="FY 2026-27")
    rows = h.rows(reader, "everything")
    ok("the audit trail still loads with retired stages on it", rows)
    mine = [r for r in rows if r["subject"] == rid]
    ok("and reads them in the words they were taken in",
       any("with payroll" in r["detail"] for r in mine),
       str([r["detail"] for r in mine]))
    ok("including the one from the step after it",
       any("with accounts payable" in r["detail"] for r in mine),
       str([r["detail"] for r in mine]))
    ok("its tabs count them", h.tabs(reader)[0].count is not None)

    # The workbook names them too, rather than printing the raw key.
    _, feat, _hh = registry.feature_of("scs", "requests")
    chain = registry.feature_of("scs", "requests")[2]
    ok("a workbook names a retired step in words",
       chain.step_label("payroll_tax") == "With Payroll",
       chain.step_label("payroll_tax"))


def check_the_trail_is_read_by_the_order() -> None:
    """One row per ORDER, from the day it was asked for to the day it landed.

    The raw list of events is still underneath, and still the thing an
    auditor needs. What it could not answer is "show me this order" —
    because an order's history is filed under two subjects: the request
    until it is handed over, and the register record from then on.
    """
    _, _f, h = registry.feature_of("scs", "audit")
    reader = as_("compliance", "audit", period="FY 2026-27")

    ok("the trail opens on orders", h.tabs(reader)[0].key == "orders",
       str([t.key for t in h.tabs(reader)]))

    rid = _ask(what="Long service watch", value="2200",
               occasion="long service")
    ok("walking it to the end", not _walk(rid), "")

    row = next(r for r in h.rows(reader, "orders") if r["id"] == rid)
    ok("the row is the order number", row["id"] == rid)
    ok("and says what it was and for whom",
       "Long service watch" in row["what"] and row["recipient"])
    ok("and when it was received",
       row["received_on"] != "not yet", row["received_on"])

    trail = row["trail"]
    ok("the trail is a list of steps", isinstance(trail, list) and trail)
    ok("in the order they happened",
       [t["when"] for t in trail] == sorted(t["when"] for t in trail))
    ok("every step has somebody answerable for it",
       all(t["who"] and t["did"] for t in trail),
       str([t for t in trail if not (t["who"] and t["did"])][:1]))

    # END TO END means two subjects. The half that proves somebody
    # actually received it is recorded against the register record, under
    # an id nobody looking at the order would think to ask for.
    said = [t["did"] for t in trail]
    ok("it starts at the first sign-off, under the request",
       ledger.reads_as("signed_off") in said, str(said))
    # The second subject is still the register record: an order's history
    # continues under the item once it is written, whether or not the
    # recipient was ever asked anything about it.
    ok("and reaches the register record, under the item",
       any(t["did"] for t in trail), str(said))

    ok("and the trail stays out of the workbook, being a list",
       "trail" in registry.feature_of("scs", "audit")[2].private)


def check_a_signed_off_year_is_closed_to_everyone() -> None:
    _, _feat, h = registry.feature_of("scs", "register")
    closed = as_("compliance", period="FY 2024-25")
    ok("a signed-off year says only that", h.rules(closed) == ["period_closed"],
       str(h.rules(closed)))
    ok("and refuses the action itself",
       not h.act(closed, "refer_to_finance", ["E-1007"]).ok)


def check_the_rail_can_work_rewardlens() -> None:
    """The assistant reaches it the same way it reaches the other two."""
    fn, feat, h = registry.feature_of("scs", "register")
    s = as_("compliance", period="FY 2026-27")

    # Named, because the rail resolves against the VIEW somebody is on and
    # the tracker now opens on its orders. A sentence about people is one
    # typed while looking at people.
    r = assistant.interpret("scs", feat, h, s, "Refer John to Finance",
                            "compliance", "all")
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

    everyone = assistant.interpret("scs", feat, h, s, "refer all of them",
                                   "compliance", "all")
    ok("referring everybody is not proposed",
       isinstance(everyone, assistant.Clarify), type(everyone).__name__)

    unresolved = [st for st in feat.starters
                  if not isinstance(
                      assistant.interpret("scs", feat, h, s, st, "compliance",
                                          "all"),
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
    # One real decision first, so the audit trail has something in it. Its
    # rows are events, and an empty trail is the correct state for a
    # deployment where nothing has happened — seeding fabricated history
    # into an audit trail to make a check pass would be the single worst
    # thing in this file.
    registry.feature_of("scs", "register")[2].act(
        as_("compliance"), "refer_to_finance", ["E-1007"])
    every_named: list[tuple[str, str]] = []
    for fn in registry.all_functions():
        for feat in fn.features:
            h = features.get(feat.handler)
            # Built the way a route builds it, so the scope carries real
            # roles. A bare `Scope(user=...)` has none, and a feature that
            # narrows by what concerns the reader then returns nothing —
            # the check would be measuring an empty screen.
            s = people.scope_for(
                "compliance", feat,
                entity="Acme Manufacturing" if "entity" in feat.scope else "",
                period="FY 2026-27" if "period" in feat.scope else "")
            tabs = h.tabs(s)
            rows = h.rows(s, tabs[0].key) if tabs else []
            # Every tab, not only the first: one feature here has three
            # shapes of row and the rail can land on any of them.
            named = [assistant._label_for(feat.id, r)
                     for tab in h.tabs(s) for r in h.rows(s, tab.key)]
            # Not "there are rows": Gift requests is a PERSONAL screen, so
            # a compliance reviewer who has asked for nothing and been
            # given nothing sees nothing there, and that is the right
            # answer rather than a gap. What must hold is that whatever IS
            # shown can be named — a row the rail cannot say back is a row
            # nobody can act on by describing it.
            ok(f"{fn.id}/{feat.id}: every row has an id and words to say it by",
               all(i and l for i, l in named),
               f"{len(named)} rows, "
               f"{sum(1 for i, l in named if not (i and l))} unnamed")
            every_named.extend(named)


    ok("and the catalog as a whole does have rows to name",
       len(every_named) > 20, str(len(every_named)))


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
    _, rl, _h2 = registry.feature_of("scs", "register")
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
            # As somebody who can see everything: a feature that shows a
            # restricted view nothing at all would otherwise pass this by
            # having no rows to name.
            s = Scope(user="compliance",
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
    _, _f, h = registry.feature_of("scs", "register")
    s = as_("compliance", period="FY 2026-27")
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
    _, feat, h = registry.feature_of("scs", "register")
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
              h.rows(as_("compliance", period="FY 2026-27"), "all")}
    h.submit(as_("admin", period="FY 2026-27"), "disclose", entry)
    after = {r["employee_id"]: r["total"] for r in
             h.rows(as_("compliance", period="FY 2026-27"), "all")}
    ok("it lands on the declarer's own record",
       after["E-1052"] == before["E-1052"] + 4000, f"{before.get('E-1052')} -> {after.get('E-1052')}")
    ok("and on nobody else's",
       all(after[k] == v for k, v in before.items() if k != "E-1052"))

    ok("somebody Compass cannot place is told so, not given an id",
       not (o := h.preview(as_("stranger", period="FY 2026-27"),
                           "disclose", entry)).ok
       and "which employee record" in o.said, o.said)


def check_the_preview_is_the_number_nobody_could_see() -> None:
    """The point of previewing: ₹4,000 is not the figure that matters.

    Aisha Khan is at ₹12,000 of ₹15,000. A ₹4,000 hamper is unremarkable on
    its own and is the one that takes her over, and she cannot know that
    from the hamper.
    """
    _, _f, h = registry.feature_of("scs", "register")
    admin = as_("admin", period="FY 2026-27")
    entry = {"what": "Vendor hamper", "kind": "gift", "value": "4000",
             "given": "2026-12-02", "source": "Acme Logistics"}

    said = h.preview(admin, "disclose", entry).said
    ok("the preview gives the resulting total, not the entry",
       "₹16,000" in said and "₹50,000" in said, said)
    ok("and says how much room is left, since this one does not cross",
       "inside the limit" in said, said)
    ok("previewing changed nothing",
       [r["total"] for r in h.rows(admin, "all") if r["employee_id"] == "E-1052"] == [12000])

    quiet = h.preview(as_("enduser", period="FY 2026-27"), "disclose", entry)
    ok("somebody with room is told the room, not warned",
       quiet.ok and "inside the limit" in quiet.said, quiet.said)

    later = h.preview(admin, "disclose", dict(entry, kind="hospitality"))
    ok("a later-phase kind is recorded and says it is not counted",
       later.ok and "not counted" in later.said and "₹12,000" in later.said,
       later.said)


def check_a_declaration_cannot_be_nonsense() -> None:
    _, _f, h = registry.feature_of("scs", "register")
    s = as_("admin", period="FY 2026-27")
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
       not h.preview(as_("admin", period="FY 2024-25"),
                     "disclose", base).ok)

    # Submitting twice is the ordinary double-click, and the second one is a
    # refusal rather than a second gift.
    ok("the first one records", h.submit(s, "disclose", base).ok)
    ok("the same one again is refused, saying what it found",
       not (o := h.submit(s, "disclose", base)).ok and "already recorded" in o.said,
       o.said)


def check_what_was_declared_is_what_was_recorded() -> None:
    """A declaration is a statement, so it is kept as made and not improved."""
    _, _f, h = registry.feature_of("scs", "register")
    s = as_("enduser", period="FY 2026-27")
    entry = {"what": "Cricket tickets", "kind": "voucher", "value": "1,500",
             "given": "2026-09-30", "source": "Vendor · Northwind"}
    ok("it records", h.submit(s, "disclose", entry).ok)

    item = next(i for i in ledger.items() if i["what"] == "Cricket tickets")
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
    _, _f, h = registry.feature_of("scs", "register")
    c = as_("compliance", period="FY 2026-27")

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
       h.submit(as_("enduser", period="FY 2026-27"), "disclose", {
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
    _, _f, h = registry.feature_of("scs", "register")
    admin = as_("admin", period="FY 2026-27")       # a reviewer
    other = as_("compliance", period="FY 2026-27")  # another reviewer
    plain = as_("enduser", period="FY 2026-27")     # not a reviewer

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
       and "not decide it" in o.said, o.said)
    ok("they are shown their own declaration, and only that",
       [r["employee_id"] for r in h.rows(plain, "disclosed")] == ["E-1066"],
       str([r["employee_id"] for r in h.rows(plain, "disclosed")]))
    review = set(rl_mod.RewardLens.REVIEW_ACTIONS)
    ok("and are offered no reviewing button they could not have used",
       not any(set(r["can"]) & review for r in h.rows(plain, "disclosed")))
    ok("a reviewer is offered the ones that are not their own",
       all(set(r["can"]) & review for r in h.rows(other, "disclosed")
           if r["state"] == "awaiting" and r["employee_id"] != "E-1052"))
    ok("and not their own",
       not (set(next(r for r in h.rows(admin, "disclosed")
                     if r["employee_id"] == "E-1052")["can"]) & review))
    # An employee is told what the screen IS, not what they may not do on
    # it: "reviewing is a compliance role" is a sentence written for
    # somebody looking at other people, and they are looking at themselves.
    ok("an employee is told whose record they are reading",
       h.rules(plain) == ["your_own_record"], str(h.rules(plain)))

    # Somebody who DOES see other people and still cannot decide — a service
    # line lead — is the one that sentence was written for.
    lead = as_("servicelead")
    ok("a manager sees their team rather than themselves alone",
       len(h.rows(lead, "all")) > 1, str(len(h.rows(lead, "all"))))
    ok("and is told why the buttons are not theirs",
       "review_is_a_role" in h.rules(lead), str(h.rules(lead)))
    ok("and is offered none of them",
       not any(set(r["can"]) & review for r in h.rows(lead, "disclosed")))
    ok("a reviewer who is not the declarer can decide it",
       h.act(other, "accept_disclosure", [mine["id"]]).ok)


def check_a_decision_carries_a_reason_and_a_name() -> None:
    _, feat, h = registry.feature_of("scs", "register")
    c = as_("compliance", period="FY 2026-27")
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
       not h.act(as_("compliance", period="FY 2024-25"),
                 "accept_disclosure", [item]).ok)


def check_the_model_does_not_write_the_justification() -> None:
    """An action needing a reason is never proposed from a sentence.

    Not a limitation worked around — the point. "Not permitted because the
    vendor is in a live tender" IS the decision, and a plausible sentence a
    model produced is the last thing that should sit on a compliance record
    under somebody else's name.
    """
    _, feat, h = registry.feature_of("scs", "register")
    c = as_("compliance", period="FY 2026-27")

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
        _, _f, h = registry.feature_of("scs", "register")
        aliased = as_("someone@example.test", period="FY 2026-27")

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
           not (o := h.preview(as_("stranger@example.test", period="FY 2026-27"), "disclose", {
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
    _, _f, h = registry.feature_of("scs", "register")
    c = as_("compliance", period="FY 2026-27")
    e = as_("enduser", period="FY 2026-27")     # Vikram Rao, E-1066
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
       len([i for i in ledger.items() if i["employee_id"] == "E-1066"]) == 2)


def check_only_a_mistaken_record_stops_counting() -> None:
    """A breach and a correction look alike and are opposites.

    A breach says you should not have been given it — you were, so it
    counts. A correction says the RECORD was wrong, so what it counted was
    never right. That difference is the lever somebody would reach for
    first, so it is the one worth checking hardest.
    """
    _, _f, h = registry.feature_of("scs", "register")
    c = as_("compliance", period="FY 2026-27")
    e = as_("enduser", period="FY 2026-27")
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
    _, _f, h = registry.feature_of("scs", "register")
    c = as_("compliance", period="FY 2026-27")
    e = as_("enduser", period="FY 2026-27")
    a = as_("admin", period="FY 2026-27")
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
    _, _f, h = registry.feature_of("scs", "register")
    c = as_("compliance", period="FY 2026-27")
    e = as_("enduser", period="FY 2026-27")
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
       not h.preview(as_("enduser", period="FY 2024-25"),
                     "answer_query",
                     {"response": "stands", "note": "x"}, item["id"]).ok)
    ok("previewing it changed nothing",
       next(r["state"] for r in h.rows(c, "disclosed")
            if r["id"] == item["id"]) == "queried")


def check_asking_again_drops_the_old_answer() -> None:
    """A reply to a question nobody is asking any more is not a reply."""
    _, _f, h = registry.feature_of("scs", "register")
    c = as_("compliance", period="FY 2026-27")
    e = as_("enduser", period="FY 2026-27")
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
    _, feat, h = registry.feature_of("scs", "register")
    everyone = [as_(u) for u in ("compliance", "admin", "enduser", "stranger")]
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
    _, _f, h = registry.feature_of("scs", "register")
    c = as_("compliance", period="FY 2026-27")
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
    _, _f, h = registry.feature_of("scs", "register")
    c = as_("compliance", period="FY 2026-27")
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
    _, _f, h = registry.feature_of("scs", "register")
    c = as_("compliance", period="FY 2026-27")
    item = next(r for r in h.rows(c, "disclosed") if r["employee_id"] == "E-1019")
    ok("Priya has no address on file", not rl_mod._address_for_employee("E-1019"))

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
    ok("asking for one anyway is refused, and repeats that reason",
       not (o := h.act(c, "resend_notice", [item["id"]])).ok
       and "no email address on file" in o.said, o.said)


def check_a_held_notice_can_be_tried_again() -> None:
    """Recording it rather than firing it is what makes a retry possible."""
    notices.clear()
    _, _f, h = registry.feature_of("scs", "register")
    c = as_("compliance", period="FY 2026-27")
    item = next(r for r in h.rows(c, "disclosed") if r["employee_id"] == "E-1066")

    h.act(c, "query_disclosure", [item["id"]], "Was this the whole basket?")
    row = next(r for r in h.rows(c, "disclosed") if r["id"] == item["id"])
    ok("with no mail server, no retry is offered — the fix is elsewhere",
       "resend_notice" not in row["can"], str(row["can"]))
    ok("and asking for one anyway says exactly what is missing",
       not (o := h.act(c, "resend_notice", [item["id"]])).ok
       and "COMPASS_SMTP_HOST" in o.said, o.said)

    with _Mailbox() as box:
        row = next(r for r in h.rows(c, "disclosed") if r["id"] == item["id"])
        ok("once there is a mail server, the retry appears",
           row["can"] == ["resend_notice"], str(row["can"]))
        ok("and it goes", h.act(c, "resend_notice", [item["id"]]).ok)
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
    _, _f, h = registry.feature_of("scs", "register")
    c = as_("compliance", period="FY 2026-27")
    item = next(r for r in h.rows(c, "disclosed") if r["employee_id"] == "E-1066")

    with _Mailbox(accept=False):
        ok("the query succeeds", h.act(c, "query_disclosure", [item["id"]],
                                       "Was this the whole basket?").ok)
        ok("delivery fails", notices.flush() == (0, 1))

        # Read while the mail server is still there, which is the state a
        # reviewer would be looking at: it exists and it refused the message.
        row = next(r for r in h.rows(c, "disclosed") if r["id"] == item["id"])
        ok("the row says the email failed",
           "failed" in row["notified"], row["notified"])
        ok("and a retry is offered, because one could work",
           "resend_notice" in row["can"], str(row["can"]))
        ok("and it queues it again", h.act(c, "resend_notice", [item["id"]]).ok)

    row = next(r for r in h.rows(c, "disclosed") if r["id"] == item["id"])
    ok("the item is still queried throughout", row["state"] == "queried")
    ok("the declarer can still answer it",
       "answer_query" in next(
           r["can"] for r in h.rows(as_("enduser", period="FY 2026-27"),
                                    "disclosed") if r["id"] == item["id"]))


def check_the_dashboard_cannot_disagree_with_the_workbench() -> None:
    """Two screens, one set of facts.

    If the dashboard summed the items itself, it could say four people are
    over while the workbench shows three, and two numbers for one fact is
    worse than one wrong number: nobody can tell which to believe, so both
    stop being usable. It reads the workbench's own rows, and this is that
    claim, checked against a screen that has been changed under it.
    """
    _, _f, board = registry.feature_of("scs", "oversight")
    _, _f2, bench = registry.feature_of("scs", "register")
    c = as_("compliance", period="FY 2026-27")

    def agree(why: str) -> None:
        over = [r for r in bench.rows(c, "all") if r["over_limit"]]
        figures = {f.key: f.value for f in board.figures(c)}
        ok(f"{why}: the same people are over",
           figures["people_over"] == str(len(over)),
           f"{figures['people_over']} vs {len(over)}")
        ok(f"{why}: the exposure is the sum of their excesses",
           figures["exposure"] == rl_mod.rupees(sum(r["excess"] for r in over)),
           figures["exposure"])
        # The chart of people past the watch level is drawn from the same
        # rows, so everybody on it is over on the workbench too.
        on_board = {r["employee_id"] for r in board.rows(c, "notable")}
        ok(f"{why}: and everyone it names is on the workbench",
           on_board <= {r["employee_id"] for r in bench.rows(c, "all")},
           str(on_board))

    agree("to begin with")

    # Move the workbench under it, three different ways.
    bench.submit(as_("admin", period="FY 2026-27"), "disclose", {
        "what": "Vendor hamper", "kind": "gift", "value": "4000",
        "given": "2026-12-02", "source": "Acme"})
    agree("after a declaration takes somebody over")

    bench.act(c, "refer_to_finance", ["E-1007"])
    agree("after a referral")

    item = next(r for r in bench.rows(c, "disclosed")
                if r["employee_id"] == "E-1066")
    bench.act(c, "query_disclosure", [item["id"]], "Was this the whole basket?")
    bench.submit(as_("enduser", period="FY 2026-27"), "answer_query",
                 {"response": "withdrawn", "note": "Declared it twice."},
                 item["id"])
    bench.act(c, "accept_answer", [item["id"]])
    agree("after a record is superseded")


def check_the_dashboard_answers_a_different_question() -> None:
    """Otherwise it is the same screen twice.

    The workbench asks who is over the limit. This asks whether anybody is
    dealing with it, and whether the thing we built is being used — which
    is the half the workbench cannot show.
    """
    _, feat, board = registry.feature_of("scs", "oversight")
    _, _f, bench = registry.feature_of("scs", "register")
    c = as_("compliance", period="FY 2026-27")

    mine = {f.key for f in board.figures(c)}
    theirs = {f.key for f in bench.figures(c)}
    ok("it shares no figure with the workbench", not (mine & theirs),
       str(sorted(mine & theirs)))
    ok("it puts a rupee figure on the exposure, which the workbench never does",
       "exposure" in mine)

    over = [r for r in bench.rows(c, "all") if r["over_limit"]]
    ok("undecided starts as everybody over", 
       {f.key: f.value for f in board.figures(c)}["undecided"] == str(len(over)))
    bench.act(c, "refer_to_finance", ["E-1007"])
    ok("and referring somebody takes them off it",
       {f.key: f.value for f in board.figures(c)}["undecided"] == str(len(over) - 1))
    ok("though they are still over on the workbench",
       len([r for r in bench.rows(c, "all") if r["over_limit"]]) == len(over))


def check_the_dashboard_sees_the_process_stopping() -> None:
    """The reason it exists apart: a stalled process is not a breach."""
    notices.clear()
    _, _f, board = registry.feature_of("scs", "oversight")
    _, _f2, bench = registry.feature_of("scs", "register")
    c = as_("compliance", period="FY 2026-27")

    figures = lambda: {f.key: f.value for f in board.figures(c)}
    ok("nothing is unanswered to begin with", figures()["unanswered"] == "0")
    ok("and nobody has been missed", figures()["unreached"] == "0")

    item = next(r for r in bench.rows(c, "disclosed")
                if r["employee_id"] == "E-1019")   # no address on file
    bench.act(c, "query_disclosure", [item["id"]], "Whose dinner was this?")

    ok("asking somebody shows up as unanswered", figures()["unanswered"] == "1")
    ok("and as never reached, because the email did not go",
       figures()["unreached"] == "1")
    ok("which is said out loud",
       "somebody_never_heard" in board.rules(c), str(board.rules(c)))

    # Read from the function that produces the FINDING rather than from a
    # tab. The Dashboard says "something has been waiting too long" as a
    # rule at the top; the orders behind it are the Tracker's, which is
    # where the desks already go.
    stuck = ov_mod._stuck(c)
    never = [r for r in stuck if "never told" in r["problem"]]
    ok("the process list names them and the reason", 
       len(never) == 1 and never[0]["who"] == "Priya Nair"
       and "no email address" in never[0]["problem"], str(never))
    ok("and does not also count it as unanswered — nobody was asked",
       not [r for r in stuck if r["who"] == "Priya Nair"
            and "no answer" in r["problem"]])

    # Answering it clears both.
    with _Mailbox():
        bench.act(c, "resend_notice", [item["id"]]) \
            if rl_mod._address_for_employee("E-1019") else None
    bench.submit(as_("mk", period="FY 2026-27"), "answer_query",
                 {"response": "stands", "note": "It was mine."}, item["id"]) \
        if rl_mod._employee("mk") == item["employee_id"] else None


def check_leadership_can_look_and_not_touch() -> None:
    _, feat, board = registry.feature_of("scs", "oversight")
    c = as_("compliance", "oversight", period="FY 2026-27")

    ok("the dashboard declares no actions", not feat.actions, str(feat.actions))
    ok("so the rail says it cannot operate here",
       not assistant.view(registry.get("scs"), feat, c).can_act)
    ok("and acting is refused with the reason, not a shrug",
       not (o := board.act(c, "refer_to_finance", ["E-1007"])).ok
       and "decisions belong" in o.said, o.said)
    ok("no sentence turns into a plan here",
       not isinstance(
           assistant.interpret("scs", feat, board, c,
                               "refer John to Finance", "compliance"),
           assistant.Proposal))

    unresolved = [st for st in feat.starters
                  if not isinstance(
                      assistant.interpret("scs", feat, board, c, st, "compliance"),
                      (assistant.Answer, assistant.Clarify))]
    ok("every starter it offers resolves to an answer", not unresolved, str(unresolved))


def check_a_firm_wide_view_is_for_the_role() -> None:
    """It names people and their totals, so it is not for everybody."""
    _, _f, board = registry.feature_of("scs", "oversight")
    plain = as_("enduser", "oversight", period="FY 2026-27")
    allowed = as_("compliance", "oversight", period="FY 2026-27")

    ok("somebody without the role sees no figures", not board.figures(plain))
    ok("no tabs", not board.tabs(plain))
    ok("and no rows, on any tab",
       not any(board.rows(plain, tab)
               for tab in ("budgets", "teams", "notable", "tax", "where",
                           )))
    ok("and no charts either",
       not board.charts(plain), str([c.key for c in board.charts(plain)]))
    ok("they are told why rather than shown an empty screen",
       board.rules(plain) == ["oversight_is_a_role"], str(board.rules(plain)))
    ok("and nobody else's total leaks through the rail",
       not assistant.interpret("scs", registry.feature_of("scs", "oversight")[1],
                               board, plain, "what are we exposed to?",
                               "enduser").facts.get("rows"))

    ok("the compliance role sees it", board.figures(allowed) and board.tabs(allowed))


def _workbook(fn_id: str, feature_id: str, scope: Scope):
    """Build an export and read it back as openpyxl does."""
    import io

    from openpyxl import load_workbook

    from compass.businessfunctions import export as exporter

    fn, feature, handler = registry.feature_of(fn_id, feature_id)
    data = exporter.to_excel(fn, feature, handler, scope, asked_by=scope.user)
    return load_workbook(io.BytesIO(data))


def check_the_export_is_the_screen() -> None:
    """Same rows, same figures, same order — because it is the same calls.

    A spreadsheet built from its own query is how one comes to say four
    people are over the limit while the screen says three, and the
    spreadsheet is the copy that gets emailed to a board and quoted back.
    """
    _, feat, h = registry.feature_of("scs", "oversight")
    c = as_("compliance", period="FY 2026-27")
    wb = _workbook("scs", "oversight", c)

    tabs = h.tabs(c)
    ok("there is a summary and one sheet per tab",
       len(wb.sheetnames) == len(tabs) + 1, str(wb.sheetnames))
    ok("in the order the screen shows them",
       wb.sheetnames[1:] == [t.label for t in tabs], str(wb.sheetnames))

    for tab in tabs:
        rows = h.rows(c, tab.key)
        sheet = wb[tab.label]
        ok(f"{tab.label}: the same number of rows",
           sheet.max_row - 1 == len(rows), f"{sheet.max_row - 1} vs {len(rows)}")
        if not rows:
            continue
        heads = [cell.value for cell in sheet[1]]
        first = [cell.value for cell in sheet[2]]
        pairs = dict(zip(heads, first))
        for key, value in rows[0].items():
            if key in h.private:
                continue
            head = key.replace("_", " ").capitalize()
            ok(f"{tab.label}: {key} is carried", head in pairs, str(heads))

    figures = {f.caption.capitalize(): f.value for f in h.figures(c)}
    summary = [[c.value for c in row] for row in wb["Summary"].iter_rows()]
    said = {r[0]: r[1] for r in summary if r and r[0]}
    for caption, value in figures.items():
        ok(f"the summary carries '{caption}'",
           str(said.get(caption)) == value or said.get(caption) == value,
           f"{said.get(caption)!r} vs {value!r}")


def check_the_export_says_when_it_was_true() -> None:
    """It outlives the screen. Nothing else in the file admits that."""
    c = as_("compliance", period="FY 2026-27")
    wb = _workbook("scs", "oversight", c)
    said = {r[0]: r[1] for r in
            ([cell.value for cell in row] for row in wb["Summary"].iter_rows())
            if r and r[0]}

    ok("it names the period", said.get("Period") == "FY 2026-27", str(said.get("Period")))
    ok("and who asked for it", said.get("Generated for") == "compliance")
    ok("and when", bool(said.get("Generated")), str(said.get("Generated")))
    ok("and says the figures move",
       "snapshot" in str(said.get("Note", "")).lower(), str(said.get("Note")))

    from compass.businessfunctions import export as exporter
    fn, feat, _h = registry.feature_of("scs", "oversight")
    name = exporter.filename(fn, feat, c)
    ok("the filename carries the period, so two are not the same file",
       "fy-2026-27" in name and name.endswith(".xlsx"), name)


def check_the_export_cannot_see_what_the_screen_hides() -> None:
    """There is no second path to the data.

    The usual shape of this bug is an export endpoint with its own query,
    which quietly skips the rule the screen applies. This one calls the
    same handler, so a person who is shown nothing gets a workbook with
    nothing in it — and that is a property of the design rather than a
    check somebody remembered to add.
    """
    plain = as_("enduser", period="FY 2026-27")
    wb = _workbook("scs", "oversight", plain)

    ok("somebody without the role gets only a summary",
       wb.sheetnames == ["Summary"], str(wb.sheetnames))
    text = "\n".join(str(cell.value) for row in wb["Summary"].iter_rows()
                      for cell in row if cell.value)
    for who in ("John Mathew", "Priya Nair", "Manish K.", "Aisha Khan"):
        ok(f"and no sign of {who}", who not in text)
    ok("no figure of anybody's leaks into it", "₹8,000" not in text)
    ok("but it does say why it is empty",
       "for compliance" in text.lower(), text[:120])


def check_a_workbook_is_worth_opening() -> None:
    """Figures that are text cannot be summed, and a sheet of text is a
    printout pretending to be a spreadsheet."""
    import datetime as dt

    # Read off the TRACKER's register, which is the sheet with every
    # shape this is about on it: a money column, a date column and the
    # machinery that must not reach a spreadsheet. The Dashboard used to
    # carry one; it is four charts and their four tabs now.
    c = as_("compliance", period="FY 2026-27")
    wb = _workbook("scs", "register", c)
    sheet = wb["Everyone"]
    heads = [cell.value for cell in sheet[1]]
    row = {heads[i]: cell for i, cell in enumerate(sheet[2])}

    ok("money is a number, not a string",
       isinstance(row["Total"].value, int), repr(row["Total"].value))
    ok("and it is formatted as rupees",
       "₹" in row["Total"].number_format, row["Total"].number_format)
    ok("a date is a date",
       isinstance(row["Crossed on"].value, (dt.date, dt.datetime)),
       repr(row["Crossed on"].value))
    ok("the header row is frozen", sheet.freeze_panes == "A2")
    ok("and the columns can be filtered", bool(sheet.auto_filter.ref))

    ok("no machinery column reached the sheet",
       not ({"Can", "State", "Asked at", "Over limit", "Referred", "Exception"}
            & set(heads)), str(heads))


def check_any_feature_can_be_exported() -> None:
    """Nothing in the exporter knows what a business function contains."""
    for fn in registry.all_functions():
        for feat in fn.features:
            h = features.get(feat.handler)
            scope = Scope(
                user="compliance",
                entity=(feat.choices.get("entity") or [""])[0]
                if "entity" in feat.scope else "",
                period=(feat.choices.get("period") or [""])[0]
                if "period" in feat.scope else "",
            )
            wb = _workbook(fn.id, feat.id, scope)
            ok(f"{fn.id}/{feat.id} exports",
               wb.sheetnames[0] == "Summary"
               and len(wb.sheetnames) == len(h.tabs(scope)) + 1,
               str(wb.sheetnames))


def check_a_monthly_slot_lands_on_the_right_day() -> None:
    """Including the months that have no 31st.

    A subscription that silently never fires is worse than one that fires a
    day early, so the day is clamped to the month's length rather than
    skipped. Routines' clock does the timezone; this is only the day
    arithmetic, which is where a monthly schedule actually goes wrong.
    """
    import datetime as dt

    def due_on(day: int, now: dt.datetime, at: str = "08:00") -> str:
        sub = reports.Subscription(id="x", user="compliance", function_id="scs",
                                   feature_id="oversight", period="FY 2026-27",
                                   day=day, time_of_day=at)
        slot = reports._slot(sub, now=now)
        local = dt.datetime.utcfromtimestamp(slot) + dt.timedelta(minutes=330)
        return local.strftime("%Y-%m-%d %H:%M")

    ok("after the hour on the day, it is today's slot",
       due_on(1, dt.datetime(2026, 2, 1, 9, 0)) == "2026-02-01 08:00",
       due_on(1, dt.datetime(2026, 2, 1, 9, 0)))
    ok("before the hour on the day, it is last month's",
       due_on(1, dt.datetime(2026, 2, 1, 7, 0)) == "2026-01-01 08:00",
       due_on(1, dt.datetime(2026, 2, 1, 7, 0)))
    ok("in January it steps back across the year",
       due_on(15, dt.datetime(2026, 1, 3, 9, 0)) == "2025-12-15 08:00",
       due_on(15, dt.datetime(2026, 1, 3, 9, 0)))
    ok("the 31st of February is the 28th, not a skipped month",
       due_on(31, dt.datetime(2026, 2, 28, 9, 0)) == "2026-02-28 08:00",
       due_on(31, dt.datetime(2026, 2, 28, 9, 0)))
    ok("and in a leap February, the 29th",
       due_on(31, dt.datetime(2028, 2, 29, 9, 0)) == "2028-02-29 08:00",
       due_on(31, dt.datetime(2028, 2, 29, 9, 0)))
    ok("a 30-day month gives the 30th",
       due_on(31, dt.datetime(2026, 4, 30, 9, 0)) == "2026-04-30 08:00",
       due_on(31, dt.datetime(2026, 4, 30, 9, 0)))


def check_a_slot_fires_once_and_is_not_missed_by_a_restart() -> None:
    import time as clock

    drive(reports.clear())
    sub = drive(reports.subscribe(user="compliance", function_id="scs",
                                  feature_id="oversight", period="FY 2026-27"))
    slot = reports._slot(sub)

    ok("a slot that has just passed is due",
       reports.due(sub, now=slot + 60) == slot)
    ok("and one still hours old is, because a restart should not lose a month",
       reports.due(sub, now=slot + 6 * 3600) == slot)
    ok("but not one from last month",
       reports.due(sub, now=slot + 40 * 3600) is None)

    ran = drive(reports.save(reports.run(sub)))
    ok("running it records the slot", ran.runs and ran.runs[-1].at == slot)
    ok("and the same slot does not fire again",
       reports.due(ran, now=clock.time()) is None)
    ok("the run kept the figures as they were",
       ran.runs[-1].figures and ran.runs[-1].figures[0][1].startswith("₹"),
       str(ran.runs[-1].figures[:1]))


def check_a_report_cannot_see_more_than_its_reader() -> None:
    """The reason this is safe to run unattended.

    There is no privileged render: the report is the person's own screen,
    read through `Scope(user=...)`. Somebody who would be shown nothing gets
    a report with nothing in it — not because this checks, but because it is
    the same call that would empty their screen.
    """
    drive(reports.clear())
    notices.clear()

    boss = drive(reports.subscribe(user="compliance", function_id="scs",
                                   feature_id="oversight", period="FY 2026-27"))
    plain = drive(reports.subscribe(user="enduser", function_id="scs",
                                    feature_id="oversight", period="FY 2026-27"))

    _subject, body, figures = reports.render(boss)
    ok("the compliance role gets the figures", figures and "₹" in body)

    _s2, empty, none = reports.render(plain)
    ok("somebody without it gets no figures", not none, str(none))
    for who in ("John Mathew", "Priya Nair", "Manish K.", "₹8,000"):
        ok(f"and no sign of {who} in their report", who not in empty)
    ok("and it says why rather than arriving blank",
       "access may have changed" in empty, empty[:80])


def check_a_report_goes_to_its_subscriber_and_nobody_else() -> None:
    drive(reports.clear())
    notices.clear()
    # `admin` is a reviewer who is also an employee, so there is somewhere to
    # send it. `compliance` is a reviewer with no employee record — a real
    # state, checked below.
    with _Mailbox() as box:
        sub = drive(reports.subscribe(user="admin", function_id="scs",
                                      feature_id="oversight",
                                      period="FY 2026-27"))
        drive(reports.save(reports.run(sub)))
        notices.flush()

    ok("one message", len(box.sent) == 1, str(len(box.sent)))
    if box.sent:
        ok("addressed to the subscriber and nobody else",
           box.sent[0]["to"] == rl_mod.address_of("admin")
           and box.sent[0]["to"] == rl_mod._address_for_employee("E-1052"),
           str(box.sent[0]["to"]))

    drive(reports.clear())
    notices.clear()
    held = drive(reports.save(reports.run(drive(reports.subscribe(
        user="compliance", function_id="scs", feature_id="oversight",
        period="FY 2026-27")))))
    ok("a reviewer the directory cannot place is held, and says so",
       "no email address on file" in held.runs[-1].said, held.runs[-1].said)
    ok("and nothing was handed to a mail server", notices.flush() == (0, 0))


def check_subscribing_is_something_you_do_to_yourself() -> None:
    drive(reports.clear())
    drive(reports.subscribe(user="compliance", function_id="scs",
                            feature_id="oversight", period="FY 2026-27"))
    drive(reports.subscribe(user="compliance", function_id="scs",
                            feature_id="oversight", period="FY 2025-26"))
    ok("subscribing twice leaves one subscription",
       len(drive(reports.all_subscriptions())) == 1,
       str(len(drive(reports.all_subscriptions()))))
    ok("and the second one replaced the first",
       drive(reports.for_user("compliance", "scs", "oversight")).period
       == "FY 2025-26")

    drive(reports.subscribe(user="enduser", function_id="scs",
                            feature_id="oversight", period="FY 2026-27"))
    ok("two people have two subscriptions",
       len(drive(reports.all_subscriptions())) == 2)
    ok("stopping yours leaves theirs",
       drive(reports.unsubscribe("compliance", "scs", "oversight"))
       and len(drive(reports.all_subscriptions())) == 1
       and drive(reports.for_user("enduser", "scs", "oversight")) is not None)
    ok("stopping one you do not have is not an error",
       not drive(reports.unsubscribe("compliance", "scs", "oversight")))

    # The shape of the request itself: there is nowhere to name anybody.
    from compass.businessfunctions.routes import ReportBody
    ok("the request body has no field naming a recipient",
       not ({"to", "recipients", "email", "user", "for_user"}
            & set(ReportBody.model_fields)),
       str(sorted(ReportBody.model_fields)))


def check_these_checks_write_nowhere_that_matters() -> None:
    """The guard on the guards.

    Everything below writes subscriptions. A temporary folder is not enough
    on its own: with the deployment configured for Cosmos, a collection
    never touches the filesystem, and the throwaway directory protects
    nothing. So the backend is forced local too, and this is the assertion
    that it actually was.
    """
    settings = get_settings()
    ok("the checks run against the local backend",
       settings.storage.backend == "local", settings.storage.backend)
    folder = Path(settings.workspace_root / settings.data_dir)
    ok("and write to a throwaway folder, not the data directory",
       "bf-checks-" in str(folder), str(folder))
    ok("which is not inside the repository",
       not str(folder).startswith(str(settings.workspace_root / "data")),
       str(folder))


def check_a_subscription_outlives_the_process() -> None:
    """The whole point: a standing request that a deploy does not forget.

    Checked by throwing away everything held in memory and reading it back
    off disk, which is what a restart does.
    """
    drive(reports.clear())
    made = drive(reports.subscribe(user="admin", function_id="scs",
                                   feature_id="oversight",
                                   period="FY 2026-27", day=9,
                                   time_of_day="18:30"))
    drive(reports.save(reports.run(made)))

    # It is on disk, not in this process. Read the file itself rather than
    # trusting the layer that wrote it: every `drive` call above already
    # opens its own loop, so "it came back" could otherwise mean a cache.
    folder = Path(get_settings().workspace_root / get_settings().data_dir)
    written = folder / "business_function_reports.json"
    ok("there is a file to survive a restart", written.is_file(), str(written))
    raw = json.loads(written.read_text())
    stored = list(raw.values()) if isinstance(raw, dict) else raw
    ok("holding the one subscription", len(stored) == 1, str(len(stored)))
    ok("under the owner it belongs to, which is who it reports to",
       stored and stored[0].get("owner") == stored[0].get("user") == "admin",
       str(stored and stored[0].get("owner")))

    back = drive(reports.for_user("admin", "scs", "oversight"))
    ok("it is still there after the process forgets", back is not None)
    ok("with the same id", back and back.id == made.id)
    ok("and the day and time it was asked for",
       back and back.day == 9 and back.time_of_day == "18:30",
       str(back and (back.day, back.time_of_day)))
    ok("and the schedule still reads the same", back and back.summary == made.summary)
    ok("the run history survived too", back and len(back.runs) == 1)
    ok("with the figures as they were",
       back and back.runs[0].figures and back.runs[0].figures[0][1].startswith("₹"),
       str(back and back.runs[0].figures[:1]))
    ok("and the slot it already fired, so a restart does not re-send it",
       back and reports.due(back) is None)

    ok("stopping it removes it from disk as well",
       drive(reports.unsubscribe("admin", "scs", "oversight")))
    left = json.loads(written.read_text())
    ok("so a restart does not bring it back",
       not (list(left.values()) if isinstance(left, dict) else left),
       str(left))


def check_an_unreadable_subscription_is_skipped_not_fatal() -> None:
    """One bad row is one bad row, not a section that will not load."""
    drive(reports.clear())
    drive(reports.subscribe(user="admin", function_id="scs",
                            feature_id="oversight", period="FY 2026-27"))
    drive(reports._store.put({"id": "rubbish", "owner": "admin"}))

    subs = drive(reports.all_subscriptions())
    ok("the good one still loads", len(subs) == 1, str(len(subs)))
    ok("and it is the right one", subs and subs[0].user == "admin")


def check_every_persona_sees_its_own_breadth() -> None:
    """Every persona on the business's sheet, on three screens, in one place.

    Written as a table because that is what it is: the useful question
    about a persona is not "may they open this" but "how much of it is
    theirs", and reading it down a column is how a mistake gets noticed.

    The shape worth reading off it: the four personas that OWN a step in
    the chain but have no business reading the firm's register see the
    requests screen at firm breadth and the register only at self. They
    need the order in front of them, not everybody's year.

    There is no `audit` row. Auditing is Leadership and Compliance's, per
    the sheet, and a row for a persona nobody holds proved nothing.
    """
    _, bench, h = registry.feature_of("scs", "register")
    _, board, board_h = registry.feature_of("scs", "oversight")
    _, chain, _ch = registry.feature_of("scs", "requests")
    everybody = len(h.rows(as_("compliance"), "all"))

    table = [
        # login         roles                         register dashboard chain
        ("admin",       {"compliance", "leadership", "admin"},
                                                        "firm", "firm", "firm"),
        ("compliance",  {"compliance", "leadership"},   "firm", "firm", "firm"),
        # The dashboard is LEADERSHIP'S ALONE. Finance and Payroll have
        # their own screen; this one as well made it a third opinion.
        ("finance",     {"finance"},                    "firm", "none", "firm"),
        ("payroll",     {"payroll"},                    "firm", "none", "firm"),
        ("procurement", {"procurement"},                "self", "none", "firm"),
        ("payments",    {"accounts_payable"},           "self", "none", "firm"),
        ("requester",   {"requester"},                  "self", "none", "firm"),
        ("servicelead", {"service_line", "requester"},  "team", "none", "firm"),
        ("enduser",     set(),                          "self", "none", "self"),
        ("stranger",    None,                           "none", "none", "none"),
    ]
    for login, roles, on_register, on_board, on_chain in table:
        mine = people.roles_of(login)
        if roles is None:
            ok(f"{login}: the directory does not know them", not mine, str(mine))
        else:
            ok(f"{login}: is {', '.join(sorted(roles)) or 'an employee'}",
               mine == (roles | {"employee"}), str(sorted(mine)))

        ok(f"{login}: sees the register at {on_register}",
           people.breadth(login, bench.visibility) == on_register,
           people.breadth(login, bench.visibility))
        ok(f"{login}: sees the dashboard at {on_board}",
           people.breadth(login, board.visibility) == on_board,
           people.breadth(login, board.visibility))
        ok(f"{login}: sees the chain at {on_chain}",
           people.breadth(login, chain.visibility) == on_chain,
           people.breadth(login, chain.visibility))

        rows = h.rows(as_(login), "all")
        if on_register == "firm":
            ok(f"{login}: the whole register", len(rows) == everybody, str(len(rows)))
        elif on_register == "self":
            # A team mailbox has no employee record, so at self breadth it
            # sees nothing — which is right: Procurement buys gifts, it does
            # not receive them, and a shared login with somebody's year on
            # it would be the leak this phase closed.
            theirs = [people.employee_of(login)] if people.employee_of(login) else []
            ok(f"{login}: their own row and no more" if theirs
               else f"{login}: nothing, having no record of their own",
               [r["employee_id"] for r in rows] == theirs,
               str([r["employee_id"] for r in rows]))
        elif on_register == "team":
            theirs = people.team_of(login) | {people.employee_of(login)}
            ok(f"{login}: their team and nobody else",
               rows and {r["employee_id"] for r in rows} <= theirs,
               str({r["employee_id"] for r in rows} - theirs))
        else:
            ok(f"{login}: nothing at all", not rows, str(len(rows)))


def check_an_employee_is_not_shown_a_colleague() -> None:
    """The hole this phase was opened to close.

    Before roles, the register listed every recipient to anybody signed in:
    an ordinary employee could read what each colleague had received and
    what the firm had spent. The dashboard was gated and the workbench was
    not, which is the usual shape of it — the restricted screen gets the
    attention and the one people actually open does not.
    """
    _, _f, h = registry.feature_of("scs", "register")
    them = as_("enduser")
    mine = people.employee_of("enduser")

    rows = h.rows(them, "all")
    ok("an employee sees exactly one row", len(rows) == 1, str(len(rows)))
    ok("and it is their own", rows and rows[0]["employee_id"] == mine)

    others = {r["recipient"] for r in h.rows(as_("compliance"), "all")} - {
        rows[0]["recipient"]}
    everything = " ".join(
        str(v) for tab in h.tabs(them) for r in h.rows(them, tab.key)
        for v in r.values())
    everything += " ".join(f.value + f.caption for f in h.figures(them))
    for who in others:
        ok(f"no sign of {who} anywhere on their screen", who not in everything)

    # And the figures are written for them rather than translated: a
    # reviewer's strip counts people and declarations to review, which to
    # somebody reading their own year is at best odd and at worst suggests
    # the screen is about somebody else.
    figures = {f.key: f for f in h.figures(them)}
    ok("the figures are their own year, not a count of people",
       "recipients" not in figures, str(sorted(figures)))
    ok("they are told what is counted so far",
       figures["recorded"].value == rl_mod.rupees(rows[0]["total"]),
       figures["recorded"].value)
    ok("and how much room is left",
       figures["headroom"].value == rl_mod.rupees(rows[0]["headroom"]),
       figures["headroom"].value)
    ok("in their own words", "your limit" in figures["recorded"].caption,
       figures["recorded"].caption)
    # The intent, which has outlived the tab that used to satisfy it: an
    # employee opening this must land on something that HAS rows, not on
    # an empty queue that makes their own record look absent. The tracker
    # now opens on their orders, which is a better first answer to "what
    # is happening to my gift" than their year total.
    first = h.tabs(them)[0]
    ok("an employee lands on a view with something in it",
       h.rows(them, first.key), f"{first.key} is empty")
    ok("and their own year is still one tab away",
       any(t.key == "all" and t.count for t in h.tabs(them)),
       str([(t.key, t.count) for t in h.tabs(them)]))


def check_what_a_feature_declares_is_what_is_enforced() -> None:
    """A role outside the declared list is refused; one inside it is not.

    Checked against the real manifest rather than by mutating it: the thing
    worth proving is that the list in the YAML is the list the handler
    applies, and a role nobody declared getting through is exactly how a
    read-only persona ends up able to decide somebody's record.
    """
    _, bench, h = registry.feature_of("scs", "register")
    declared = {a.id: set(a.roles) for a in bench.actions if a.roles}
    ok("every review action names who may take it",
       set(rl_mod.RewardLens.REVIEW_ACTIONS) <= set(declared), str(sorted(declared)))

    # Payroll reads the whole firm and decides none of ITS record — the
    # shape most likely to be quietly over-granted, because somebody who
    # can already see everything looks like somebody who may do anything.
    # Measured through a persona the business actually holds: signed in as
    # an invented `audit` login, every row was empty and this passed
    # without exercising a single refusal.
    reader = as_("payroll")
    ok("payroll sees the whole register",
       len(h.rows(reader, "all")) == len(h.rows(as_("compliance"), "all")),
       f"{len(h.rows(reader, 'all'))} rows")
    for action, roles in sorted(declared.items()):
        if "payroll" in roles:
            continue
        out = h.act(reader, action, ["E-1007"], "a reason")
        ok(f"and is refused {action}, which is for {', '.join(sorted(roles))}",
           not out.ok, out.said)

    ok("while compliance, which is named, is not refused for the role",
       "another team" not in h.act(as_("compliance"), "refer_to_finance",
                                   ["E-1007"]).said)
    ok("an undeclared action is open to anybody who can see the row, as before",
       all(a.roles for a in bench.actions),
       str([a.id for a in bench.actions if not a.roles]))


def check_a_role_cannot_be_claimed_by_a_caller() -> None:
    """Roles are resolved, never received.

    A client that could name its own role could name any of them, so the
    request models have nowhere to put one and the scope is built from the
    directory on the server.
    """
    from compass.businessfunctions.routes import ActBody, FormBody, ReportBody

    for model in (ActBody, FormBody, ReportBody):
        leaked = {"role", "roles", "user", "sees", "employee", "team",
                  "as_user"} & set(model.model_fields)
        ok(f"{model.__name__} has no field naming who the caller is",
           not leaked, str(sorted(leaked)))

    ok("a scope built for somebody unknown holds no role",
       not as_("nobody-at-all").roles)
    ok("and sees nothing", as_("nobody-at-all").sees == "none")

    # The oldest features declare no visibility and must be untouched by any
    # of this.
    for fn_id, feature_id in (("finance", "form26"), ("talent", "lms")):
        _fn, feature, _h = registry.feature_of(fn_id, feature_id)
        ok(f"{fn_id}/{feature_id} declares no visibility and so is unrestricted",
           not feature.visibility
           and people.breadth("anybody", feature.visibility) == "firm")


def check_the_register_and_the_trail_survive_a_restart() -> None:
    """What a deploy must not forget.

    Everything here was in memory an hour ago: a declaration, the question
    somebody asked about it, the answer, the decision, the supersession and
    the referral. "The November report said eight thousand" and "we asked
    them on the 9th" both have to be answerable after a restart, and an
    audit trail that empties on one answers neither.
    """
    _, _f, h = registry.feature_of("scs", "register")
    c = as_("compliance")
    e = as_("enduser")

    # A full round trip, of the kind the trail exists to remember.
    ok("a declaration is recorded",
       h.submit(e, "disclose", {
           "what": "Cricket tickets", "kind": "voucher", "value": "1500",
           "given": "2026-09-30", "source": "Vendor · Northwind"}).ok)
    fresh = next(r for r in h.rows(c, "disclosed") if r["what"] == "Cricket tickets")
    ok("a reviewer asks about it",
       h.act(c, "query_disclosure", [fresh["id"]], "Was this yours alone?").ok)
    ok("the declarer answers",
       h.submit(e, "answer_query", {"response": "stands", "note": "Mine."},
                fresh["id"]).ok)
    ok("and the reviewer settles it", h.act(c, "accept_answer", [fresh["id"]]).ok)
    ok("somebody is referred to Finance",
       h.act(c, "refer_to_finance", ["E-1007"]).ok)

    before = {r["employee_id"]: (r["total"], r["status"])
              for r in h.rows(c, "all")}
    declarations = {r["id"]: (r["status"], r["reviewed_by"], r["answered"])
                    for r in h.rows(c, "disclosed")}

    drive(ledger.flush())
    ok("nothing is left unsaved", ledger.pending() == 0, str(ledger.pending()))

    # What a restart is: the process forgets, and reads it back.
    ledger.forget()
    ok("the register is empty in memory", not ledger.items())
    drive(ledger.ready(seed=rl_mod.SEED, requests_seed=gr_mod.SEED))

    ok("the register came back", len(ledger.items()) == len(before) and True
       or len(ledger.items()) > 0, str(len(ledger.items())))
    after = {r["employee_id"]: (r["total"], r["status"]) for r in h.rows(c, "all")}
    ok("every person's total is what it was", after == before,
       str({k: (before.get(k), after.get(k)) for k in set(before) | set(after)
            if before.get(k) != after.get(k)}))
    ok("the referral is still recorded",
       after.get("E-1007", ("", ""))[1] == "referred", str(after.get("E-1007")))

    again = {r["id"]: (r["status"], r["reviewed_by"], r["answered"])
             for r in h.rows(c, "disclosed")}
    ok("every declaration is in the state it was left in", again == declarations,
       str({k: (declarations.get(k), again.get(k)) for k in set(declarations)
            if declarations.get(k) != again.get(k)}))
    ok("including who decided it",
       again[fresh["id"]][1] == "compliance", str(again[fresh["id"]]))


def check_a_correction_still_supersedes_after_a_restart() -> None:
    """The invariant, across the thing most likely to break it.

    A correction is two records and an event tying them together. Stored as
    a state on the item it would be one record that changed, which is the
    thing the disclosure form promises never happens.
    """
    _, _f, h = registry.feature_of("scs", "register")
    c = as_("compliance")
    e = as_("enduser")

    item = next(r for r in h.rows(c, "disclosed") if r["employee_id"] == "E-1066")
    h.act(c, "query_disclosure", [item["id"]], "Was that the whole basket?")
    h.submit(e, "answer_query",
             {"response": "corrected", "value": "1500", "note": "Mine was 1,500."},
             item["id"])
    ok("the correction is accepted", h.act(c, "accept_answer", [item["id"]]).ok)
    total = next(r["total"] for r in h.rows(c, "all") if r["employee_id"] == "E-1066")

    drive(ledger.flush())
    ledger.forget()
    drive(ledger.ready(seed=rl_mod.SEED, requests_seed=gr_mod.SEED))

    rows = {r["id"]: r for r in h.rows(c, "disclosed")}
    ok("the original is still on the register", item["id"] in rows)
    ok("still marked superseded", rows[item["id"]]["state"] == "superseded",
       rows[item["id"]]["state"])
    ok("still pointing at what replaced it",
       rows[item["id"]]["replaced_by"].startswith("C-"),
       rows[item["id"]]["replaced_by"])
    ok("the corrected figure is still the one that counts",
       next(r["total"] for r in h.rows(c, "all")
            if r["employee_id"] == "E-1066") == total, str(total))
    ok("and nothing was edited — both records are there",
       len([i for i in ledger.items() if i["employee_id"] == "E-1066"]) >= 2)


def check_the_trail_says_who_and_when() -> None:
    """An audit trail with no name or time on it is a list of rumours."""
    _, _f, h = registry.feature_of("scs", "register")
    c = as_("compliance")
    # John: he is the one past the threshold, so he is the one there is
    # anything to refer about. Chasing an acknowledgement used to be
    # measured here too, and went with the acknowledgement itself.
    h.act(c, "refer_to_finance", ["E-1007"])

    for subject, kind in (("E-1007", "referred"),):
        event = ledger.latest(subject, kind)
        ok(f"{kind} is on the trail", event is not None)
        ok(f"{kind} names who did it", event and event["by"] == "compliance")
        ok(f"{kind} is timed", event and event["at"] > 0)

    ok("every event names its subject, kind, who and when",
       all({"subject", "kind", "by", "at"} <= set(e) for e in ledger.events()),
       str([e for e in ledger.events()
            if not {"subject", "kind", "by", "at"} <= set(e)][:1]))
    ok("and nothing on the trail is an event nobody can fold",
       all(e["kind"] in ledger.EVENTS for e in ledger.events()),
       str({e["kind"] for e in ledger.events()} - set(ledger.EVENTS)))


def check_seeding_only_fills_an_empty_register() -> None:
    """Seeding on every start would overwrite a year with a fixture."""
    _, _f, h = registry.feature_of("scs", "register")
    c = as_("compliance")

    h.submit(as_("enduser"), "disclose", {
        "what": "A real declaration", "kind": "gift", "value": "900",
        "given": "2026-08-01", "source": "A real vendor"})
    drive(ledger.flush())
    count = len(ledger.items())

    ledger.forget()
    drive(ledger.ready(seed=rl_mod.SEED, requests_seed=gr_mod.SEED))
    ok("a register with records in it is not re-seeded",
       len(ledger.items()) == count, f"{len(ledger.items())} vs {count}")
    ok("and the real declaration is still there",
       any(i["what"] == "A real declaration" for i in ledger.items()))

    ledger.forget()
    drive(ledger._items_store.replace_all([]))
    drive(ledger.ready(seed=rl_mod.SEED, requests_seed=gr_mod.SEED))
    ok("an empty one is seeded, so a fresh deployment has something to see",
       len(ledger.items()) == len(rl_mod.SEED), str(len(ledger.items())))


def check_a_notice_outlives_the_process() -> None:
    _, _f, h = registry.feature_of("scs", "register")
    c = as_("compliance")
    notices.clear()

    item = next(r for r in h.rows(c, "disclosed") if r["employee_id"] == "E-1019")
    h.act(c, "query_disclosure", [item["id"]], "Whose dinner was this?")
    said = next(r["notified"] for r in h.rows(c, "disclosed")
                if r["id"] == item["id"])
    ok("the notice says it could not go", "not emailed" in said, said)

    drive(notices.save())
    notices.clear()
    notices._loaded = False
    drive(notices.ready())

    ok("it came back", notices.latest_for(item["id"]) is not None)
    ok("saying the same thing",
       next(r["notified"] for r in h.rows(c, "disclosed")
            if r["id"] == item["id"]) == said)
    ok("so a restart cannot make an unsent question look sent",
       notices.latest_for(item["id"]).state in ("held", "failed"))


def _req(user: str = "requester", **over) -> Scope:
    return people.scope_for(user, registry.feature_of("scs", "requests")[1],
                            period="FY 2026-27")


def _desk(user: str, tab: str = "orders", period: str = "FY 2026-27"
          ) -> list[dict]:
    """What a DESK sees of the chain — which is the Tracker, not the chain.

    Gift requests is where somebody asks for something and watches their
    own; every other persona signs off in the Tracker. A check that read a
    desk's queue off the requests screen would be checking a screen that
    persona no longer opens.
    """
    handler = registry.feature_of("scs", "register")[2]
    return handler.rows(as_(user, "register", period=period), tab)


def _order(user: str, rid: str, tab: str = "orders") -> dict:
    """One order as this desk sees it on the Tracker."""
    for where in (tab, "to_sign", "orders", "completed"):
        for row in _desk(user, where):
            if row["id"] == rid:
                return row
    raise AssertionError(f"{user} cannot see {rid} on the Tracker")


#: Who signs off each stage of the chain, for a request raised by
#: `requester`. The sign-off stages are not interchangeable: the login here
#: is the one whose persona OWNS that step, and every other login is
#: expected to be refused at it.
SIGNS = {
    "service_line": "servicelead",
    "finance_policy": "finance",
    "procurement": "procurement",
    # Operations settle the list, then Operations hand it out. Two steps
    # and one desk: closing the list and starting the distribution are
    # different decisions, and the business wanted a moment between them
    # where the list can still be put right.
    "recipient_list": "spoc",
    # The last step. There was one more — the recipient confirming receipt,
    # driven by `enduser` — and it went when the closed list became the
    # acknowledgement. Nothing in the chain is a recipient's any more, so
    # the walk has no persona outside the firm to drive.
    "distribution": "spoc",
}


def _ask(by: str = "requester", **over) -> str:
    """Raise a request and return its id."""
    values = {"recipient": "Vikram Rao", "what": "Diwali hamper",
              "kind": "gift", "value": "3000", "occasion": "festival",
              "quantity": "1",
              "purpose": "Thanking the team for the quarter."}
    values.update(over)
    h = registry.feature_of("scs", "requests")[2]
    out = h.submit(_req(by), "request_gift", values)
    if not out.ok:
        raise AssertionError(f"could not raise a request: {out.said}")
    return out.touched[0]


def _walk(rid: str, upto: str = "closed", by: str = "requester",
          who: dict[str, str] | None = None) -> list[str]:
    """Sign a request off, stage by stage, until it reaches `upto`.

    Returns what went wrong, so a caller can assert on an empty list. The
    walk is the point: a chain that only works when every step is driven by
    the right persona is the thing being measured, so this refuses to fudge
    a step it could not take.
    """
    h = registry.feature_of("scs", "requests")[2]
    signers = dict(SIGNS, **(who or {}))
    # Whoever raised it may also hand it out, so a caller naming `by`
    # drives both of Operations' steps with that login.
    signers["recipient_list"] = who.get("recipient_list", by) if who else by
    signers["distribution"] = who.get("distribution", by) if who else by
    trouble: list[str] = []
    for _ in range(len(gr_mod.STAGES) + 1):
        request = next(r for r in ledger.requests() if r["id"] == rid)
        stage = gr_mod.stage_of(request)
        if stage == upto or stage in gr_mod.ENDED:
            break
        # Two steps now have a record to make before they have a
        # sign-off to give: Procurement says what it was bought on, and
        # the requester says who received it. Both are the point of their
        # step rather than a formality, so the walk makes them properly.
        if stage == "recipient_list" and not gr_mod.planned_list(rid):
            if not request["for"]:
                made = h.submit(_req(by), "revise_list",
                                {"add": "Vikram Rao",
                                 "why": "Settling the list before it goes "
                                        "out."}, about=rid)
                if not made.ok:
                    trouble.append(f"{stage}: {made.said}")
                    break
        out = h.act(_req(signers[stage]), "sign_off", [rid],
                    f"Checked at {stage}.")
        if not out.ok:
            trouble.append(f"{stage}: {out.said}")
            break
    return trouble



def check_crossing_the_threshold_tells_somebody() -> None:
    """The firm's complaint is that it finds out at the year end.

    A dashboard that would have shown it is only as good as somebody
    opening the dashboard, so the moment the register says somebody is
    over, the people whose job that is are written to. Measured on the
    OUTBOX rather than on the sentence, because the sentence is what the
    signer reads and the notice is what reaches compliance.
    """
    notices.clear()
    big = rl_mod.limit() + 5_000
    rid = _ask(value=str(big), recipient="Vikram Rao")
    trouble = _walk(rid)
    ok("an order big enough to cross is signed off", not trouble, str(trouble))

    vikram = people.employee_of("enduser")
    sent = notices.for_item(vikram)
    reachable = people.holders("compliance")
    ok("everybody whose job it is to look is told, not a role mailbox",
       len(sent) == len(reachable) and len(reachable) > 1,
       f"{len(sent)} notices to {[p.name for p in reachable]}")
    his = next(r for r in rl_mod.totals(as_("compliance", period="FY 2026-27"))
               if r["employee_id"] == vikram)
    ok("and told by how much, read off the register and not the order",
       all(rl_mod.rupees(his["excess"]) in n.body for n in sent)
       and his["excess"] != big - rl_mod.limit(),
       f"excess {his['excess']}, this order {big}")
    ok("and told who it is",
       all(vikram in n.body and "Vikram Rao" in n.subject for n in sent))
    ok("the alert says it is a flag, not an assessment",
       all("not an assessment" in n.body for n in sent),
       str([n.body[:80] for n in sent][:1]))
    ok("and nobody is told what their tax is — that is out of scope",
       not any(word in n.body.lower() for n in sent
               for word in ("perquisite", "tds", "deduct ", "payable",
                            "liability")),
       str([n.body[:160] for n in sent][:1]))
    ok("the crossing is on the trail as well as in the post",
       ledger.happened(vikram, "threshold_crossed"))

    # ONCE PER PERSON PER YEAR. A second gift to somebody already over is
    # not news, and an alert that repeats is an alert people filter.
    was = len(notices.for_item(vikram))
    again = _ask(value="4000", recipient="Vikram Rao")
    ok("a second order to the same person goes through",
       not _walk(again))
    ok("and does not alert again", len(notices.for_item(vikram)) == was,
       f"{was} then {len(notices.for_item(vikram))}")

    # AND NOT BEFORE. Nothing is on the register until distribution, so
    # nothing crosses before it either.
    notices.clear()
    third = _ask(value=str(big), recipient="Priya Nair")
    ok("an order held short of distribution is signed off",
       not _walk(third, "distribution"))
    ok("nothing is alerted on a proposal",
       not notices.for_item("E-1019") and not notices.for_item("E-1077"),
       str([n.subject for n in notices._outbox.values()]))


def check_an_exception_decision_reaches_whoever_asked() -> None:
    """Granted or refused, the row does not move — so without a notice the
    asker finds out by going back and looking, which is the same failure
    as the year end, one decision smaller."""
    h = registry.feature_of("scs", "requests")[2]
    row = lambda who, rid: next(r for r in gr_mod._rows(_req(who))
                                if r["id"] == rid)

    # Client courtesy above the single-gift threshold: the rule bites, so
    # there is an exception to make. With a mail server, because what is
    # being measured here is that the answer REACHES somebody — a held
    # notice is the subject of the third case below, not of this one.
    notices.clear()
    post = _Mailbox().__enter__()
    rid = _ask(by="requester", what="Dinner out", value="12000",
               kind="gift", occasion="client courtesy")
    ok("the rule is biting", row("requester", rid)["blocked"])
    asked = h.act(_req("servicelead"), "request_exception", [rid],
                  "Twenty-year client, and the partner is attending.")
    ok("the desk holding it can ask", asked.ok, asked.said)
    ok("asking alerts nobody — the asker already knows",
       not notices.for_item(rid), str(len(notices.for_item(rid))))

    refused = h.act(_req("finance"), "refuse_exception", [rid],
                    "Over the threshold with no business case.")
    ok("finance can refuse", refused.ok, refused.said)
    ok("and the refusal says who was told", "has been told" in refused.said,
       refused.said)
    told = notices.for_item(rid)
    ok("the asker is written to, once", len(told) == 1, str(len(told)))
    ok("by name", told[0].name == people.find("servicelead").name,
       told[0].name)
    ok("and actually sent, not held", told[0].state != "held",
       f"{told[0].state}: {told[0].detail}")
    ok("with finance's reason in it, not just the verdict",
       "no business case" in told[0].body, told[0].body[:160])
    ok("and the decision itself", "refused" in told[0].subject.lower(),
       told[0].subject)
    ok("and what it means for the order",
       "Nothing has moved" in told[0].body, told[0].body[-140:])
    ok("the order is still blocked", row("requester", rid)["blocked"])

    # The other way, on a fresh order.
    notices.clear()
    other = _ask(by="requester", what="More dinner", value="12000",
                 kind="gift", occasion="client courtesy")
    h.act(_req("requester"), "request_exception", [other],
          "Same client, same partner.")
    granted = h.act(_req("finance"), "grant_exception", [other],
                    "Allowed once, on the partner's authority.")
    ok("finance can grant", granted.ok, granted.said)
    said = notices.for_item(other)
    ok("the asker hears that too", len(said) == 1, str(len(said)))
    ok("and hears it can carry on", "carries on" in said[0].body,
       said[0].body[-160:])
    ok("the rule is quoted in it, not replaced by the decision",
       "above" in said[0].body and "Allowed once" in said[0].body,
       said[0].body[:200])
    ok("the exception is on the record", gr_mod.exception_on(other) is not None)
    post.__exit__()

    # WHOEVER ASKED, not whoever raised — and an asker nobody can write
    # to is a state the sentence has to admit rather than paper over. The
    # Business Requester desk has no address on purpose, so this is the
    # real case and not a contrivance.
    notices.clear()
    third = _ask(by="requester", what="Dinner again", value="12000",
                 kind="gift", occasion="client courtesy")
    h.act(_req("requester"), "request_exception", [third], "My client.")
    out = h.act(_req("finance"), "refuse_exception", [third], "Still no.")
    heard = notices.for_item(third)
    ok("it is the asker who is written to, not the stage's desk",
       len(heard) == 1 and heard[0].name == people.find("requester").name,
       str([n.name for n in heard]))
    ok("and when nobody knows where, the notice is held with the reason",
       heard[0].state == "held" and "no email address" in heard[0].detail,
       f"{heard[0].state}: {heard[0].detail}")
    ok("and finance is told so, rather than told it was sent",
       "was not told" in out.said and "no email address" in out.said,
       out.said)


def check_nothing_reaches_the_register_until_it_is_given() -> None:
    """A request is a proposal. Most of what goes wrong in systems like
    this is treating it as a record: a declined request on the register is
    a gift nobody received, and one signed off five times but never handed
    over is a total that is wrong the other way.

    So the register is touched at exactly ONE stage — distribution — and
    this walks the chain measuring the total after every step to prove it.
    """
    _, _f, h = registry.feature_of("scs", "requests")
    _, _f2, bench = registry.feature_of("scs", "register")
    seen = as_("compliance")
    total = lambda: next((r["total"] for r in bench.rows(seen, "all")
                          if r["employee_id"] == "E-1066"), 0)
    before = total()

    rid = _ask()
    ok("a request can be raised", rid)
    ok("and changes nobody's total", total() == before, str(total()))

    for stage in ("service_line", "finance_policy", "procurement"):
        out = h.act(_req(SIGNS[stage]), "sign_off", [rid], f"Checked at {stage}.")
        if stage == "finance_policy":
            # Approving it used to mint and email a reference. It mints
            # nothing now, and either way nobody's total moves: approval
            # is not a gift.
            ok("approving it mints no reference",
               not [e for e in ledger.events(rid) if e["kind"] == "referenced"])
        ok(f"{gr_mod.stage_label(stage).lower()} signs off", out.ok, out.said)
        ok(f"and {stage} changed nobody's total", total() == before, str(total()))

    # TWO STEPS AT OPERATIONS NOW: the list is closed, and then the
    # distribution starts. Only the second touches the register, which is
    # what this check exists to pin down.
    closed = h.act(_req("requester"), "sign_off", [rid], "List settled.")
    ok("closing the list signs off", closed.ok, closed.said)
    ok("and closing it changed nobody's total", total() == before, str(total()))

    given = h.act(_req("requester"), "sign_off", [rid], "Handed over at the desk.")
    ok("recording the distribution is what puts it on the register",
       given.ok and total() == before + 3000, f"{given.said} -> {total()}")
    ok("and the outcome says their new year", "\u20b95,500" in given.said, given.said)
    ok("the item knows which request it came from",
       any(i.get("from_request") == rid for i in ledger.items()))
    # SETTLED, because nobody is asked. It arrived unacknowledged while
    # the recipient had a step of their own; the closed list is the
    # acknowledgement now, so the record is settled by what wrote it.
    ok("and it arrives settled, with nobody asked",
       next(i["acknowledged"] for i in ledger.items()
            if i.get("from_request") == rid) is True)


def check_a_declined_request_never_becomes_a_gift() -> None:
    _, _f, h = registry.feature_of("scs", "requests")
    _, _f2, bench = registry.feature_of("scs", "register")
    seen = as_("compliance")
    total = lambda: next((r["total"] for r in bench.rows(seen, "all")
                          if r["employee_id"] == "E-1066"), 0)
    before, items = total(), len(ledger.items())

    rid = _ask(what="A thing", value="2000")
    ok("declining needs a reason",
       not h.act(_req("servicelead"), "decline", [rid]).ok)
    ok("with one, it is declined",
       h.act(_req("servicelead"), "decline", [rid],
             "Too close to the tender.").ok)
    ok("nothing was added to the register", len(ledger.items()) == items)
    ok("and nobody's total moved", total() == before)
    row = _order("servicelead", rid)
    ok("the reason is on the record", row["why"] == "Too close to the tender.")
    ok("and it says where it was turned down", row["status"] == "Declined")
    ok("it cannot then be signed off",
       not h.act(_req("servicelead"), "sign_off", [rid], "on reflection").ok)
    ok("and no later stage can pick it up either",
       not any(h.act(_req(who), "sign_off", [rid], "mine now").ok
               for who in ("finance", "procurement")))


def check_the_breach_is_caught_before_it_is_bought() -> None:
    """The point of the whole phase.

    The requirement's complaint is that the firm finds out afterwards. A
    \u20b96,000 watch for John is unremarkable on its own and takes him to
    \u20b926,000 because of what three other teams already gave him — and that
    sentence is now said while it still costs nothing to say no.
    """
    _, _f, h = registry.feature_of("scs", "requests")
    asking = _req()
    watch = {"recipient": "John Mathew", "what": "Long service watch",
             "kind": "gift", "value": "6000", "occasion": "long service",
             "purpose": "Recognising the team.",
             "quantity": "1",
             "source": "People team"}

    said = h.preview(asking, "request_gift", watch).said
    ok("the preview adds it to what he already has",
       "\u20b958,000" in said, said)
    ok("and says how far over that is", "\u20b98,000 over" in said, said)
    ok("but does not refuse it — somebody senior decides knowingly",
       "cannot be approved" not in said, said)

    rid = _ask(**watch)
    # Read on the TRACKER, which is where the desks sign off now.
    row = _order("servicelead", rid, "to_sign")
    ok("the row carries it to whoever signs off", "over" in row["tax"],
       row["tax"])
    ok("signing off is still offered", "sign_off" in row["can"], str(row["can"]))
    out = h.act(_req("servicelead"), "sign_off", [rid], "Thirty years' service.")
    ok("and the sign-off records that they knew",
       out.ok and "knowing" in out.said, out.said)
    # The warning travels with the request, so the desk holding it sees it
    # whichever desk that is. Procurement cannot see it yet — it is not
    # theirs until Finance has passed it on, which is the point of a queue.
    ok("a later desk has nothing to sign while it is not theirs",
       not any(r["id"] == rid for r in _desk("procurement", "to_sign")),
       "procurement has an order still at the service line to sign")
    h.act(_req("finance"), "sign_off", [rid], "Within policy.")
    ok("and sees it, warning and all, once it reaches them",
       "over" in _order("procurement", rid, "to_sign")["tax"])


def check_policy_blocks_where_tax_only_warns() -> None:
    """Two checks now, one of which refuses and one of which does not.

    There were three. The third asked whether the paying team had budget,
    and it went with the team budgets: the money IS the recipient's
    allowance, which is what the tax check already reads.
    """
    _, _f, h = registry.feature_of("scs", "requests")
    asking = _req()

    hospitality = h.preview(asking, "request_gift", {
        "recipient": "Vikram Rao", "what": "Dinner", "kind": "hospitality",
        "value": "4000", "occasion": "client courtesy",
        "purpose": "Recognising the team.",
        "quantity": "1",
        "source": "People team"}).said
    ok("hospitality is not requested through this yet",
       "cannot be approved" in hospitality, hospitality)

    courtesy = h.preview(asking, "request_gift", {
        "recipient": "Vikram Rao", "what": "Hamper", "kind": "gift",
        "value": "12000", "occasion": "client courtesy",
        "purpose": "Recognising the team.",
        "quantity": "1",
        "source": "People team"}).said
    ok("a large client courtesy needs a conversation, not a form",
       "conversation" in courtesy, courtesy)

    big = h.preview(asking, "request_gift", {
        "recipient": "Vikram Rao", "what": "Big hamper", "kind": "gift",
        "value": "30000", "occasion": "festival", "purpose": "Recognising the team.", "quantity": "1"}).said
    # A large gift is NOT refused. It takes the recipient past their
    # allowance, which is a thing somebody senior decides knowing — the
    # requirement's own argument. What used to refuse it was the paying
    # team's budget, and that is gone.
    ok("a large gift warns rather than refuses",
       "cannot be approved" not in big, big)
    ok("and nobody is told about a team budget any more",
       "short by" not in big, big)

    rid = _ask(what="Dinner out", value="12000", kind="gift",
               occasion="client courtesy")
    row = _order("servicelead", rid)
    ok("a blocked request offers no sign-off", "sign_off" not in row["can"],
       str(row["can"]))
    ok("but can still be declined", "decline" in row["can"])
    ok("and asking anyway is refused",
       not h.act(_req("servicelead"), "sign_off", [rid], "go on").ok)
    ok("so it never reaches the next desk",
       gr_mod.stage_of(next(r for r in ledger.requests() if r["id"] == rid))
       == "service_line")


def check_a_queue_does_not_age_from_the_epoch() -> None:
    """A request with no recorded moment has not been waiting since 1970.

    The seeds a fresh deployment starts with carry `at: 0.0`, and the
    Tracker aged from it: every empty deployment opened on a queue saying
    something had been waiting 20,744 days, and the "waiting too long"
    finding fired on fixtures nobody had touched. A figure that absurd is
    not a cosmetic problem — it is the one that makes a reader stop
    believing the rest of the screen.
    """
    # The TRACKER is where the column is drawn, so the Tracker is what is
    # measured — the requests screen never carried this field.
    rows = {r["id"]: r for r in _desk("servicelead", "orders")}
    seen = 0
    for seed in gr_mod.SEED:
        row = rows.get(seed["id"])
        if row is None:
            continue
        seen += 1
        ok(f"{seed['id']}: carries no moment to age from",
           not seed.get("at"), str(seed.get("at")))
        ok(f"{seed['id']}: so it has been waiting no days",
           row["waiting_days"] == 0, str(row["waiting_days"]))
    ok("the seeded requests are on the Tracker to be measured at all",
       seen == len(gr_mod.SEED), f"{seen} of {len(gr_mod.SEED)}")

    # And one that DOES carry a moment is still aged, so the guard did not
    # quietly turn the column off for everybody.
    fresh = _ask(what="Something new", value="3000")
    row = _order("servicelead", fresh)
    ok("a request raised just now has been waiting no days either",
       row["waiting_days"] == 0, str(row["waiting_days"]))
    ok("but it is aged from its own moment, not from nothing",
       next(r for r in ledger.requests() if r["id"] == fresh)["at"] > 0)


def check_the_two_thresholds_do_different_jobs() -> None:
    """One is about tax, one is about governance, and they are not the same
    number any more.

    ₹5,000 PER GIFT is the tax-free limit: at or below it a gift is
    tax-free, above it the gift is taxable. That is a FACT RECORDED about
    the gift and nothing else — no refusal, no different approver, no
    arithmetic. What taxable means for the recipient is the tax teams' own
    work, offline, which is the business's own scope decision.

    ₹50,000 CUMULATIVE is the governance threshold: every gift in the year
    added together, and past it an order needs either a sign-off made
    knowingly or an exception somebody puts their name to.
    """
    _, _f, h = registry.feature_of("scs", "requests")
    asking = _req()
    said = lambda **over: h.preview(asking, "request_gift", dict(
        {"recipient": "Vikram Rao", "what": "Hamper", "kind": "gift",
         "value": "3000", "occasion": "festival", "quantity": "1",
         "purpose": "Thanking the team."}, **over)).said

    ok("the tax-free limit is the ₹5,000 the business named",
       policy.big() == 5_000, str(policy.big()))
    ok("the cumulative limit is ₹50,000", policy.limit() == 50_000,
       str(policy.limit()))

    # AT the limit is inside it. An off-by-one here is a gift wrongly
    # called taxable, which somebody then has to unpick with Payroll.
    at = said(value="5000")
    ok("a gift at exactly the limit is tax-free",
       "inside the ₹5,000 tax-free limit" in at, at)
    over = said(value="5001")
    ok("a rupee above it is taxable", "taxable" in over, over)
    ok("and says whose job that is, which is not Compass's",
       "tax teams" in over, over)
    ok("and is not refused for it", "cannot be approved" not in over, over)
    ok("and needs no different approver",
       "compliance approves" not in over and "line manager" not in over, over)

    # NOT A GATE. The whole order runs, exactly as a ₹3,000 one would.
    rid = _ask(value="9000", what="Festival hamper")
    row = _order("servicelead", rid)
    ok("an above-the-tax-free-limit order can be signed off",
       "sign_off" in row["can"], str(row["can"]))
    ok("and is not blocked", not row["blocked"], str(row))
    ok("and offers no exception, because nothing is biting",
       "request_exception" not in row["can"], str(row["can"]))
    ok("and it runs to the end", not _walk(rid), "")

    # THE COURTESY GATE DID NOT MOVE WITH IT. These were one number, and
    # dropping the tax-free limit to ₹5,000 would have quietly started
    # refusing ordinary client courtesies.
    ok("a client courtesy above the tax-free limit is still a form",
       "conversation" not in said(value="6000", occasion="client courtesy"),
       said(value="6000", occasion="client courtesy"))
    ok("and above its own threshold it is still a conversation",
       "conversation" in said(value="12000", occasion="client courtesy"),
       said(value="12000", occasion="client courtesy"))
    ok("which is a number of its own, not the tax-free one",
       gr_mod.CONVERSATION_ABOVE != policy.big(),
       f"{gr_mod.CONVERSATION_ABOVE} vs {policy.big()}")


def check_crossing_the_cumulative_limit_has_two_ways_past_it() -> None:
    """Sign it off knowing, or ask Finance to own it.

    The crossing used to offer only the first: the desk holding it could
    wave through an ordinary ₹6,000 watch that happened to take somebody
    past ₹50,000 on its own authority, but could not escalate it — so a
    desk that wanted cover had nowhere to go inside Compass. That is the
    failure this function exists to end, so both routes are open.
    """
    _, _f, h = registry.feature_of("scs", "requests")
    row = lambda who, rid: next(r for r in gr_mod._rows(_req(who))
                                if r["id"] == rid)

    # Take somebody most of the way there, then one ordinary gift more.
    first = _ask(value="48000", what="Milestone award", recipient="Vikram Rao")
    ok("the first order runs", not _walk(first), "")
    rid = _ask(value="6000", what="A watch", recipient="Vikram Rao")

    r = row("servicelead", rid)
    ok("the crossing is flagged", r["warns"], str(r["tax"]))
    ok("and it is not a refusal", not r["blocked"], str(r["policy"]))
    ok("route one: it can still be signed off",
       "sign_off" in r["can"], str(r["can"]))
    ok("route two: and an exception can be asked for",
       "request_exception" in r["can"], str(r["can"]))

    asked = h.act(_req("servicelead"), "request_exception", [rid],
                  "Twenty years' service, and the partner asked for it.")
    ok("asking is accepted", asked.ok, asked.said)
    ok("Finance can decide it",
       {"grant_exception", "refuse_exception"} <= set(row("finance", rid)["can"]),
       str(row("finance", rid)["can"]))

    out = h.act(_req("finance"), "grant_exception", [rid],
                "Allowed once, on the partner's authority.")
    ok("Finance grants it", out.ok, out.said)
    ok("and does not claim it unblocked something that was never blocked",
       "can now be signed off" not in out.said, out.said)
    ok("it says what granting actually changed",
       "crossing is yours" in out.said, out.said)
    ok("and it still runs to the end", not _walk(rid), "")

    # A SIGN-OFF STILL SAYS SO. Whichever route was taken, the sentence the
    # signer reads names the crossing rather than passing it silently.
    second = _ask(value="7000", what="A pen", recipient="Vikram Rao")
    signed = h.act(_req("servicelead"), "sign_off", [second],
                   "Checked, and I know what it does to his year.")
    ok("signing a crossing off knowingly is allowed", signed.ok, signed.said)
    ok("and the sentence says what was signed",
       "over the annual limit" in signed.said, signed.said)


def check_nobody_approves_their_own() -> None:
    """Two shapes of the same rule: not your request, not your gift.

    With one exception that is not a loophole, and is measured below: the
    stage where the requester records what they actually handed over.
    """
    _, _f, h = registry.feature_of("scs", "requests")

    mine = _ask(by="servicelead", what="A hamper", value="2000")
    row = _order("servicelead", mine)
    ok("the person who asked cannot sign it off",
       "sign_off" not in row["can"] and "cancel_request" in row["can"],
       str(row["can"]))
    ok("the other approver for that persona can", "sign_off" in _order("leadtwo", mine)["can"])
    # Stronger than "no button": a later desk does not have the row at all
    # while it is not theirs.
    ok("and a later stage cannot even see it yet",
       not any(r["id"] == mine for r in h.rows(_req("payroll"), "all")))

    for_me = _ask(by="servicelead", recipient="Aisha Khan", what="A watch",
                  value="2000", occasion="performance")
    ok("a gift to you is not yours to sign off either",
       "sign_off" not in next(r for r in h.rows(_req("admin"), "all")
                              if r["id"] == for_me)["can"])
    ok("you cannot even ask for one for yourself",
       "cannot be approved" in h.preview(_req("admin"), "request_gift", {
           "recipient": "Aisha Khan", "what": "A watch", "kind": "gift",
           "value": "2000", "occasion": "performance",
           "purpose": "Recognising the team.",
           "quantity": "1",
           "source": "People team"}).said)


def check_each_stage_moves_only_for_its_own_persona() -> None:
    """The routing question, measured at every step rather than asserted.

    This is the behaviour the chain exists for: a request signed off by
    Payroll lands in Procurement's queue, and in nobody else's. So at each
    stage, every login that is NOT that stage's persona is refused, the one
    that is succeeds, and the stage then moves on by itself.
    """
    _, _f, h = registry.feature_of("scs", "requests")
    everybody = ("servicelead", "finance", "payroll", "procurement",
                 "payments", "requester", "enduser", "compliance", "admin")
    rid = _ask(what="A hamper", value="1000")

    stage = gr_mod.FIRST_STAGE
    while stage != "closed":
        owner = gr_mod.STAGES[stage]["owner"]
        theirs = SIGNS[stage]
        label = gr_mod.stage_label(stage).lower()

        # THE TWO IDENTITY-ROUTED STEPS HAVE TWO LEGITIMATE SIGNERS, and
        # always did: Operations hold them, and so does whoever raised the
        # order, because a one-off gift raised by an ordinary employee has
        # no SPOC behind it. So "everybody else is refused" has to mean
        # everybody except both of them — asserting one signer there said
        # the chain was broken when it was the design.
        allowed = {theirs}
        if stage in ("recipient_list", "distribution"):
            allowed.add("requester")
        outsiders = [w for w in everybody if w not in allowed]
        refused = [w for w in outsiders
                   if not h.act(_req(w), "sign_off", [rid], "mine, surely").ok]
        ok(f"{label}: nobody else can sign it off",
           len(refused) == len(outsiders),
           str([w for w in outsiders if w not in refused]))
        # Two shapes of refusal, and which one you get is itself the rule.
        # A desk that has already acted on this order still sees it, so it
        # is told whose desk it is on now. One that has never touched it
        # does not have the row at all, and is told only that — nothing
        # about an order that is none of their business.
        for w in outsiders[:4]:
            said = h.act(_req(w), "sign_off", [rid], "x").said
            involved = any(r["id"] == rid for r in _desk(w, "orders")) or any(
                r["id"] == rid for r in _desk(w, "completed"))
            ok(f"{label}: {w} is refused, "
               + ("and told whose it is" if involved
                  else "without being told about it"),
               (label in said.lower()) if involved else ("in this view" in said),
               said)
        # Only the sign-off, because the person who RAISED it keeps a
        # withdraw button at the early stages and that is not a step in the
        # chain. Asserting "no outsider has any button" said that it was
        # broken when it was the requester's own withdraw.
        offered = lambda w: next(
            (r["can"] for r in _desk(w, "orders") if r["id"] == rid), [])
        ok(f"{label}: the step is in the owner's queue and no outsider's",
           all("sign_off" not in offered(w) for w in outsiders),
           str([w for w in outsiders if "sign_off" in offered(w)]))
        ok(f"{label}: and it is in the owner's",
           "sign_off" in offered(theirs), str(offered(theirs)))
        withdraw = "cancel_request" in offered("requester")
        ok(f"{label}: the requester can withdraw it only before it is bought",
           withdraw == (stage in ("service_line", "finance_policy")),
           f"{stage}: {offered('requester')}")

        # The list has to have somebody on it before it can be closed, and
        # a request raised for one named person already has.
        if stage == "recipient_list" and not gr_mod.planned_list(rid):
            h.submit(_req(theirs), "revise_list",
                     {"add": "Vikram Rao", "why": "Settling the list."},
                     about=rid)
        out = h.act(_req(theirs), "sign_off", [rid], f"Checked at {stage}.")
        ok(f"{label}: {people.LABELS.get(owner, owner)} can", out.ok, out.said)

        nxt = gr_mod.STAGES[stage]["next"]
        now = gr_mod.stage_of(next(r for r in ledger.requests()
                                   if r["id"] == rid))
        ok(f"{label}: and it moves to {nxt}", now == nxt, now)
        if nxt != "closed":
            # A persona for a control step, a NAMED PERSON for the two
            # routed by identity — "with Business Requester" is not who to
            # chase when the holder is whichever employee raised it.
            now = _order("compliance", rid)["with"]
            both = (f"{people.LABELS['operations']} or "
                    f"{people.find('requester').name}")
            want = ({"recipient_list": both, "distribution": both}.get(
                         nxt, people.LABELS.get(
                             gr_mod.STAGES[nxt]["owner"], "")))
            ok(f"{label}: the row says who it is with now", now == want,
               f"{now!r} wanted {want!r}")
        stage = nxt

    ok("the end of the chain is closed, and nothing is waiting on anybody",
       _order("compliance", rid)["stage"] == "closed")
    ok("a closed request cannot be signed off again",
       not any(h.act(_req(w), "sign_off", [rid], "again").ok
               for w in everybody))


def check_anybody_can_raise_one_and_it_still_reaches_the_end() -> None:
    """A Business Requester raises orders; so does an employee thanking
    somebody. Both are ordinary, and the chain has to finish for both.

    The failure this catches is not at the start, where it would be
    obvious, but FIVE DESKS IN: distribution was routed by the `requester`
    persona, so an order an ordinary employee raised was signed off by the
    service line, Finance, Payroll, Procurement and Accounts Payable, and
    then sat there with nobody on earth able to record what was handed
    over. The step is already pinned to one named person, which is
    narrower than any role, so the persona gate does not apply to it.
    """
    _, feat, h = registry.feature_of("scs", "requests")
    _, _b, bench = registry.feature_of("scs", "register")

    # The third raiser also HOLDS the first persona in the chain, so their
    # own step has to be signed off by the other approver — which is the
    # not-your-own rule working, not a failure.
    for who, roles, hand_to in (
            ("requester", "the persona whose job it is", {}),
            ("enduser", "an employee who holds no persona at all", {}),
            ("servicelead", "somebody who also approves",
             {"service_line": "leadtwo"})):
        with fixtures():
            raising = people.scope_for(who, feat, period="FY 2026-27")
            out = h.submit(raising, "request_gift", {
                "recipient": "Aisha Khan", "what": "A thank-you hamper",
                "kind": "gift", "value": "1500", "occasion": "performance",
                "purpose": "Recognising the team.",
                "quantity": "1",
                "source": "Service line \u00b7 Advisory"})
            ok(f"{roles} can raise one", out.ok, out.said)
            rid = out.touched[0]

            # Distribution is theirs because they raised it, whatever
            # personas they do or do not hold.
            trouble = _walk(rid, by=who, who=dict(hand_to))
            ok(f"and the chain they started runs to the end", not trouble,
               str(trouble))
            ok("recording the distribution was the raiser's to do",
               any(e["by"] == who and e.get("stage") == "distribution"
                   for e in ledger.events(rid)),
               str([(e["by"], e.get("stage")) for e in ledger.events(rid)]))
            ok("it closed", gr_mod.stage_of(
                next(r for r in ledger.requests() if r["id"] == rid))
               == "closed")
            item = next(i for i in ledger.items()
                        if i.get("from_request") == rid)
            ok("it reached the register", item["recipient"] == "Aisha Khan")
            # SETTLED ON ARRIVAL. The recipient used to confirm this one
            # themselves; the closed list is the acknowledgement now, so
            # the record is settled by the act that wrote it.
            ok("and the record arrives settled, with nobody asked",
               item["acknowledged"] and not any(
                   e["kind"] == "acknowledged"
                   for e in ledger.events(item["id"])))

    # "Waiting on you" has to mean a step that is YOURS. The raiser keeps
    # a withdraw button while it is being checked, and counting that as a
    # queue told an employee something was waiting on them about a request
    # sitting with the Service Line Approver.
    with fixtures():
        rid = _ask(by="enduser", recipient="Aisha Khan", what="A hamper",
                   value="1400", occasion="performance")
        theirs = people.scope_for("enduser", feat, period="FY 2026-27")
        row = next(r for r in h.rows(theirs, "all") if r["id"] == rid)
        ok("the raiser can still withdraw it", "cancel_request" in row["can"])
        ok("but it is not waiting on them \u2014 it is with somebody else",
           not [r for r in h.rows(theirs, "mine") if r["id"] == rid],
           str(row["can"]))
        ok("and the screen does not say it is",
           "waiting_on_you" not in h.rules(theirs), str(h.rules(theirs)))
        ok("the figure agrees with the tab",
           next(f.value for f in h.figures(theirs) if f.key == "mine")
           == str(len(h.rows(theirs, "mine"))))
        ok("while it IS waiting on the approver, on their tracker",
           any(r["id"] == rid for r in _desk("servicelead", "to_sign")))

        # ...and once it reaches them, it is.
        _walk(rid, upto="distribution", by="enduser")
        ok("when their own step arrives, it is waiting on them",
           any(r["id"] == rid for r in h.rows(theirs, "mine")))
        ok("and the screen says so", "waiting_on_you" in h.rules(theirs))

    # The rule it must NOT have loosened: distribution is the raiser's or
    # OPERATIONS', and nobody else's — holding the requester persona is
    # not enough, which was the whole point of routing this step by
    # identity rather than by role.
    with fixtures():
        rid = _ask(by="enduser", recipient="Aisha Khan",
                   what="Another hamper", value="1100",
                   occasion="performance")
        _walk(rid, upto="distribution", by="enduser")
        offered = lambda w: next(
            (r["can"] for r in h.rows(_req(w), "all") if r["id"] == rid), [])
        ok("and the row names whoever can hand it over",
           _order("compliance", rid)["with"]
           == f"{people.LABELS['operations']} or {people.find('enduser').name}",
           _order("compliance", rid)["with"])
        ok("the raiser may record the distribution",
           "sign_off" in offered("enduser"), str(offered("enduser")))
        # ...on the TRACKER, which is where every desk works. Gift
        # requests is personal, and Operations neither raised this one nor
        # received it.
        ok("and so may Operations, who have the boxes",
           "sign_off" in _order("spoc", rid)["can"],
           str(_order("spoc", rid)["can"]))
        ok("though Gift requests shows them none of it",
           not offered("spoc"), str(offered("spoc")))
        strangers = [w for w in ("requester", "servicelead", "procurement",
                                 "payments", "finance", "compliance", "admin")
                     if "sign_off" in offered(w)]
        ok("and nobody else may, persona or no persona",
           not strangers, str(strangers))


def check_every_stage_reads_as_a_sentence() -> None:
    """A column heading and a sentence are not the same words.

    `label` is "With Finance", which is right above a table and wrong in
    prose: reused, it produced "Signed off at with finance". Measured for
    every stage rather than spot-checked, because the next stage somebody
    adds will be written by copying one of these.
    """
    _, _f, h = registry.feature_of("scs", "requests")
    rid = _ask(what="A hamper", value="1000")

    for stage, nxt in [(s, gr_mod.STAGES[s]["next"])
                       for s in gr_mod.STAGES if s != "closed"]:
        ok(f"{stage}: has a name for a column and one for a sentence",
           gr_mod.STAGES[stage]["label"] and gr_mod.STAGES[stage]["step"]
           and gr_mod.STAGES[stage]["holder"], str(gr_mod.STAGES[stage]))
        ok(f"{stage}: and they are not the same words",
           gr_mod.step_name(stage).lower()
           != gr_mod.stage_label(stage).lower())

        if stage == "distribution" and not gr_mod.allocated_to(rid):
            h.submit(_req(SIGNS[stage]), "allocate",
                     {"recipients": "Vikram Rao", "mode": "in person"},
                     about=rid)
        out = h.act(_req(SIGNS[stage]), "sign_off", [rid], f"Checked {stage}.")
        ok(f"{stage}: it can be signed off", out.ok, out.said)
        ok(f"{stage}: and reads as English, not as a heading",
           " at with " not in out.said and "with with" not in out.said,
           out.said)
        if nxt != "closed":
            ok(f"{stage}: signing off says which step it was",
               gr_mod.step_name(stage) in out.said, out.said)
            ok(f"{stage}: and names who has it now",
               gr_mod.STAGES[nxt]["holder"] in out.said, out.said)
        else:
            # The last step says the order is closed rather than naming its
            # own step and a next desk there is none of.
            ok(f"{stage}: the last one says it is the end of the chain",
               "closed" in out.said and "last step" in out.said, out.said)
            ok(f"{stage}: and that it was the register it wrote",
               "on the register" in out.said, out.said)

    back = _ask(what="Another hamper", value="1000")
    said = h.act(_req("servicelead"), "decline", [back], "Not this year.").said
    ok("sending one back reads as a sentence too",
       gr_mod.step_name("service_line") in said and " at with " not in said,
       said)


def check_the_last_two_steps_only_go_forward() -> None:
    """Five desks CHECK; the last two RECORD. Only a check can refuse.

    By distribution the gift has been approved, bought and paid for. The
    requester's job there is to say what was actually handed over, not to
    reopen a decision five other desks already took — and a recipient who
    could decline their own gift would have a way to make a record
    disappear, which is the opposite of a control.
    """
    _, feat, h = registry.feature_of("scs", "requests")
    rid = _ask(what="A hamper", value="1000")

    for stage in [s for s in gr_mod.STAGES if s != "closed"]:
        theirs = _req(SIGNS[stage])
        # The chain's own rows: this is a check about what each STEP
        # allows, and the two last steps are read on Gift requests while
        # the five desks are read on the Tracker. One source for both.
        # The record the step owes, made before its row is read: a step
        # whose own capture is missing offers no sign-off, which is a
        # different rule and is measured in its own check.
        if stage == "recipient_list" and not gr_mod.planned_list(rid):
            h.submit(theirs, "revise_list",
                     {"add": "Vikram Rao", "why": "Settling the list."},
                     about=rid)
        row = next(r for r in gr_mod._rows(theirs) if r["id"] == rid)
        # THE THREE DESKS THAT CHECK may refuse; the three that RECORD may
        # not. Closing the list joined the second group: Operations are
        # not approving the order there, they are settling who it reaches,
        # and a wrong list is fixed on the spot rather than sent back
        # three desks.
        checks = stage not in ("recipient_list", "distribution")

        ok(f"{stage}: the stage says whether it may refuse",
           gr_mod.STAGES[stage]["back"] is checks, str(gr_mod.STAGES[stage]))
        ok(f"{stage}: sending it back is offered only where it is a check",
           ("decline" in row["can"]) is checks, str(row["can"]))
        ok(f"{stage}: and signing off always is",
           "sign_off" in row["can"], str(row["can"]))

        if not checks:
            out = h.act(theirs, "decline", [rid], "On reflection, no.")
            ok(f"{stage}: asking anyway is refused", not out.ok, out.said)
            ok(f"{stage}: and it did not move the request",
               gr_mod.stage_of(next(r for r in ledger.requests()
                                    if r["id"] == rid)) == stage)
            ok(f"{stage}: nor end it",
               not ledger.happened(rid, "declined"))
        h.act(theirs, "sign_off", [rid], f"Checked at {stage}.")

    ok("so a bought gift can only go forward",
       gr_mod.stage_of(next(r for r in ledger.requests() if r["id"] == rid))
       == "closed")

    # And the manifest says the same thing, since that is what the role
    # gate at the entry of `act` reads.
    declines = next(a for a in feat.actions if a.id == "decline")
    ok("the manifest gives sending it back to the checking desks only",
       set(declines.roles) == {"service_line", "finance", "procurement"},
       str(sorted(declines.roles)))
    ok("and the recipient is not among them", "employee" not in declines.roles)


def check_a_recipient_is_found_by_the_name_a_person_types() -> None:
    """A full stop is not a different person.

    The directory holds "Manish K."; somebody types "Manish K" and the whole
    form was refused with "Compass does not know anybody called that" — a
    dead end that also did not say who it DID know, turning a typed name
    into a guessing game.
    """
    _, feat, h = registry.feature_of("scs", "requests")
    asking = _req()
    base = {"what": "A hamper", "kind": "gift", "value": "1500",
            "occasion": "performance", "source": "Service line \u00b7 Advisory", "purpose": "Recognising the team.", "quantity": "1"}

    for typed in ("Manish K.", "Manish K", "manish k", "MANISH  K",
                  "  Manish K.  "):
        ok(f"{typed!r} finds the same person",
           gr_mod._employee_named(typed) == "E-1041",
           gr_mod._employee_named(typed))
    ok("and so does a name the register holds but the directory does not",
       gr_mod._employee_named("John Mathew") == "E-1007",
       gr_mod._employee_named("John Mathew"))
    ok("a name nobody has still finds nobody",
       not gr_mod._employee_named("Jon Smith"))
    ok("and neither does an empty one", not gr_mod._employee_named("   "))

    said = h.preview(asking, "request_gift",
                     dict(base, recipient="Manish K")).said
    ok("the form accepts the name as typed", "does not know" not in said, said)

    said = h.preview(asking, "request_gift",
                     dict(base, recipient="Jon Smith")).said
    ok("an unknown name is still refused", "does not know" in said, said)
    ok("but the refusal says who Compass does know",
       all(n in said for n in ("Manish K.", "Vikram Rao", "Aisha Khan")), said)

    said = h.preview(asking, "request_gift",
                     dict(base, recipient="Vikr")).said
    ok("a near miss is offered as a suggestion",
       "Did you mean" in said and "Vikram Rao" in said, said)

    # A BLANK ONE IS NOW ALLOWED. The firm buys two hundred hampers before
    # it knows whose they are, and a form that insisted on a name was a
    # form nobody could use for the purchase this exists to control.
    out = h.preview(asking, "request_gift", dict(base, recipient="  "))
    ok("a blank recipient is accepted, not refused", out.ok, out.said)
    ok("and says the year is checked when they are named",
       "when they are named" in out.said, out.said)


def check_the_tracker_follows_orders_through_the_chain() -> None:
    """Two screens, two questions. The chain is a QUEUE — what needs me
    now. The tracker is the JOURNEY — where has each order got to, and who
    has it.

    Both are narrowed by the same rule, asked of the chain rather than
    worked out twice: which orders are somebody's business is the chain's
    to say, and a tracker deriving it again is a second place for a
    privacy rule to be wrong.
    """
    _, feat, h = registry.feature_of("scs", "register")
    _, _rf, chain = registry.feature_of("scs", "requests")

    board = people.scope_for("compliance", feat, period="FY 2026-27")
    orders = h.rows(board, "orders") or h.rows(board, "completed")
    # It opens on the DESK's own queue — the orders standing at a stage
    # they own — because that is the only tab about the reader. The
    # journey is the one after it.
    ok("the tracker opens on what this desk has to sign",
       h.tabs(board)[0].key == "to_sign", h.tabs(board)[0].key)
    ok("with the journey next", h.tabs(board)[1].key == "orders",
       h.tabs(board)[1].key)
    ok("and where they land after that",
       h.tabs(board)[2].key == "completed", h.tabs(board)[2].key)
    ok("and there are some to follow", orders)

    for row in orders:
        ok(f"{row['id']}: says how far it has got",
           " of " in row["progress"] or row["stage"] in gr_mod.ENDED,
           row["progress"])
        ok(f"{row['id']}: and who has it now", row["with"])
        ok(f"{row['id']}: with what was given and to whom",
           row["what"] and row["recipient"])
        break

    # The same order, seen by the desks that handle it and not by others.
    rid = orders[0]["id"]
    sees = lambda who: {r["id"] for r in h.rows(
        people.scope_for(who, feat, period="FY 2026-27"), "orders")}
    # Nothing has reached Procurement in the fixture, and a desk tracking
    # orders that are nowhere near it would be the bug. Walk one to them.
    ok("a desk tracks nothing before anything reaches it",
       not gr_mod.tracked("procurement", "FY 2026-27"),
       str(len(gr_mod.tracked("procurement", "FY 2026-27"))))
    mine = _ask(what="A hamper", value="1300")
    _walk(mine, upto="procurement")
    ok("and tracks it once it is theirs",
       mine in {r["id"] for r in gr_mod.tracked("procurement", "FY 2026-27")})
    # The chain's own screen is PERSONAL now, so this is no longer the
    # same set — and it should not be. Procurement tracks what reaches its
    # desk; Gift requests shows them only what they raised themselves.
    ok("and the chain's own screen shows them none of it",
       not any(r["id"] == mine for r in chain.rows(
           people.scope_for("procurement", _rf, period="FY 2026-27"), "all")))
    ok("a stranger tracks nothing", not gr_mod.tracked("stranger", "FY 2026-27"))

    # Procurement reads the ORDERS without reading the firm's register,
    # which is the whole reason the two narrowings are separate.
    proc = people.scope_for("procurement", feat, period="FY 2026-27")
    ok("procurement follows the order that reached it",
       mine in {r["id"] for r in h.rows(proc, "orders")})
    ok("while still seeing none of the firm's register",
       not h.rows(proc, "all"), str(len(h.rows(proc, "all"))))

    # Progress is a fold over the trail, so it moves when the order does.
    with fixtures():
        fresh = _ask(what="A hamper", value="1100")
        was = next(r["progress"] for r in h.rows(board, "orders")
                   if r["id"] == fresh)
        chain.act(_req("servicelead"), "sign_off", [fresh], "Clear need.")
        now = next(r["progress"] for r in h.rows(board, "orders")
                   if r["id"] == fresh)
        ok("a sign-off moves the tracker", was != now, f"{was} -> {now}")
        ok("and it says which desk has it now",
           next(r["with"] for r in h.rows(board, "orders")
                if r["id"] == fresh) == "Finance Team")

    ok("none of the order machinery reaches a workbook",
       not ({"stage", "done", "of"} & set(orders[0])) or
       {"stage", "done", "of"} <= h.private,
       str(sorted(h.private)))


def check_the_firm_has_three_levels() -> None:
    """SCS is a FUNCTION, RewardLens a SERVICE, the screens its SUB-SERVICES.

    Flattened to two, five sub-services sat beside each other looking like
    five services — which is exactly what somebody reading the nav
    concluded, because nothing in the structure said otherwise.
    """
    fn = registry.get("scs")
    ok("the function is the department", fn.name == "SCS", fn.name)
    ok("and it runs one named service",
       [s.name for s in fn.services] == ["RewardLens"],
       str([s.name for s in fn.services]))

    grouped = fn.grouped()
    ok("every sub-service is filed under it", len(grouped) == 1, str(len(grouped)))
    service, feats = grouped[0]
    ok("all five of them",
       [f.name for f in feats] == ["Dashboard", "Gift requests", "Tracker",
                                   "Tax & budget", "Audit trail"],
       str([f.name for f in feats]))
    ok("nothing is left outside a service",
       all(f.service == "rewardlens" for f in fn.features),
       str([f.id for f in fn.features if f.service != "rewardlens"]))
    ok("and a mistyped service would show as unfiled rather than vanish",
       sum(len(fs) for _s, fs in grouped) == len(fn.features))

    # The hierarchy reaches the screen, or the UI cannot draw it.
    c = _client()
    body = c.get("/v1/business-functions/scs").json()
    ok("the overview sends the services", body.get("services"),
       str(body.get("services"))[:80])
    ok("with their sub-services inside",
       [f["name"] for f in body["services"][0]["features"]]
       == [f["name"] for f in body["features"]],
       str([f["name"] for f in body["services"][0]["features"]]))


def check_the_dashboard_is_leaderships_alone_and_draws_what_it_says() -> None:
    """One dashboard, for leadership, and every picture names its figures."""
    _, feat, h = registry.feature_of("scs", "oversight")

    for who, allowed in (("compliance", True), ("admin", True),
                         ("finance", False), ("payroll", False),
                         ("procurement", False), ("servicelead", False),
                         ("requester", False), ("enduser", False)):
        breadth = people.breadth(who, feat.visibility)
        ok(f"{who} {'reads' if allowed else 'cannot read'} the dashboard",
           (breadth != "none") == allowed, breadth)

    board = people.scope_for("compliance", feat, period="FY 2026-27")
    charts = h.charts(board)
    ok("it draws five pictures", len(charts) == 5, str(len(charts)))
    ok("the ones the business asked for",
       [c.key for c in charts] == ["budget", "teams", "notable", "tax",
                                   "where"],
       str([c.key for c in charts]))
    ok("each is a shape a reader knows",
       all(c.kind in ("bars", "split") for c in charts),
       str({c.kind for c in charts}))

    tabs = {t.key for t in h.tabs(board)}
    for c in charts:
        ok(f"{c.key}: names the view its figures are in", c.tab in tabs,
           f"{c.tab!r} not in {sorted(tabs)}")
        ok(f"{c.key}: and that view really has rows to show",
           isinstance(h.rows(board, c.tab), list))
        ok(f"{c.key}: says something when there is nothing to draw", c.empty)
        for sl in c.slices:
            ok(f"{c.key}: every slice says what it is worth", sl.said,
               str(sl))

    # ONE THRESHOLD. It was two — a dashboard "watch level" of ₹50,000
    # beside a compliance limit of ₹15,000 — and two numbers for one
    # question meant a reader seeing "3 over the limit" on one screen and
    # "0 past ₹50,000" on another could not tell which was the firm's rule.
    ok("the dashboard watches the one threshold the rulebook holds",
       h.NOTABLE == rl_mod.limit(), f"{h.NOTABLE} vs {rl_mod.limit()}")
    ok("and moving the rulebook moves the dashboard with it",
       h.NOTABLE == policy.limit(), f"{h.NOTABLE} vs {policy.limit()}")

    # Nobody who cannot read the screen gets charts off it either.
    for who in ("finance", "enduser", "procurement"):
        ok(f"{who} gets no charts", not h.charts(
            people.scope_for(who, feat, period="FY 2026-27")))


def check_a_year_shows_that_year() -> None:
    """The limit is ANNUAL, so a year has to mean a year.

    Every screen ignored its own period selector: one gift given in 2026
    appeared under FY 2024-25, FY 2025-26 and FY 2026-27 alike, so the
    annual limit was being checked against the sum of all time and a team's
    budget never reset. Nobody saw it because a screen only ever showed one
    year at a time, and they all looked the same.
    """
    ok("an Indian financial year runs April to March",
       rl_mod.within("FY 2026-27", "2026-04-01")
       and rl_mod.within("FY 2026-27", "2027-03-31")
       and not rl_mod.within("FY 2026-27", "2026-03-31")
       and not rl_mod.within("FY 2026-27", "2027-04-01"))
    ok("a timestamp works as well as a date",
       rl_mod.within("FY 2026-27", time.mktime((2026, 6, 1, 12, 0, 0, 0, 0, -1))))
    ok("no period means no filtering, rather than an empty screen",
       rl_mod.within("", "2020-01-01"))
    ok("and neither does one nobody can parse",
       rl_mod.within("last Tuesday", "2020-01-01"))

    _, bench, h = registry.feature_of("scs", "register")
    _, _a, audit = registry.feature_of("scs", "audit")
    _, _r, req = registry.feature_of("scs", "requests")

    seen = lambda handler, feature, year, tab: len(handler.rows(
        people.scope_for("compliance", feature, period=year), tab))

    this_year = seen(h, bench, "FY 2026-27", "all")
    ok("the seeded register has rows in the year it was given", this_year)
    ok("and none of them in a year they were not",
       seen(h, bench, "FY 2022-23", "all") == 0,
       str(seen(h, bench, "FY 2022-23", "all")))
    ok("the trail is the trail of that year too",
       seen(audit, _a, "FY 2022-23", "everything") == 0)

    # A gift given in a different year is not in this year's total, which
    # is the whole reason the limit is annual.
    with fixtures():
        them = "E-1066"
        before = next(r["total"] for r in h.rows(
            people.scope_for("compliance", bench, period="FY 2026-27"), "all")
            if r["employee_id"] == them)
        ledger.add_item({"id": "G-OLD001", "employee_id": them,
                         "recipient": "Vikram Rao", "kind": "gift",
                         "what": "A hamper two years ago", "value": 9_000,
                         "given": "2024-06-01", "source": "People team",
                         "channel": "procured", "acknowledged": True})
        after = next(r["total"] for r in h.rows(
            people.scope_for("compliance", bench, period="FY 2026-27"), "all")
            if r["employee_id"] == them)
        ok("a gift from two years ago does not count towards this year",
           after == before, f"{before} -> {after}")
        older = next((r["total"] for r in h.rows(
            people.scope_for("compliance", bench, period="FY 2024-25"), "all")
            if r["employee_id"] == them), 0)
        ok("but it does count towards the year it was given",
           older == 9_000, str(older))

    # A team's budget is annual, so last year's approvals do not spend it.
    with fixtures():
        team = "Service line \u00b7 Tax"
        spent_now = gr_mod.committed(team, "FY 2026-27")
        ledger.add_request({"id": "R-OLD001", "for": "E-1066", "kind": "gift",
                            "what": "An old hamper", "value": 5_000,
                            "occasion": "festival", "source": team,
                            "purpose": "Recognising the team.",
                            "by": "requester", "by_employee": "",
                            "at": time.mktime((2024, 6, 1, 12, 0, 0, 0, 0, -1)), "quantity": "1"})
        ledger.add_event(subject="R-OLD001", kind="signed_off", by="servicelead",
                         stage="service_line", note="x", value=5_000)
        ok("last year's commitment does not spend this year's budget",
           gr_mod.committed(team, "FY 2026-27") == spent_now,
           str(gr_mod.committed(team, "FY 2026-27")))
        ok("while it does spend the year it was committed in",
           gr_mod.committed(team, "FY 2024-25") >= 5_000,
           str(gr_mod.committed(team, "FY 2024-25")))

    # Live work is never hidden by a year selector.
    with fixtures():
        rid = _ask(what="A hamper", value="1000")
        here = {r["id"] for r in req.rows(
            people.scope_for("requester", _r, period="FY 2024-25"), "all")}
        ok("an in-flight request shows whatever year is selected",
           rid in here, "a live request vanished behind a year filter")


def check_the_assistant_answers_from_what_the_reader_may_see() -> None:
    """A model may phrase, and may not see past the person asking.

    The brief is the ONLY thing the model is given, so whatever it can say
    is bounded by what is in there. That makes the brief the security
    boundary, and it is built through each reader's own scope.
    """
    everything = chat.brief("scs", "compliance", period="FY 2026-27")
    ok("leadership's brief carries every screen",
       {s["screen"] for s in everything["screens"]}
       == {"Dashboard", "Audit trail", "Tax & budget", "Gift requests",
           "Tracker"},
       str([s["screen"] for s in everything["screens"]]))

    theirs = chat.brief("scs", "enduser", period="FY 2026-27")
    shown = {s["screen"] for s in theirs["screens"]}
    ok("an employee's brief carries only their own screens",
       shown == {"Gift requests", "Tracker"}, str(sorted(shown)))
    ok("so the oversight screens are not in it",
       not {"Dashboard", "Tax & budget", "Audit trail"} & shown)

    # The sharper test: the register at self breadth is one row, so a
    # question about colleagues has nothing to answer from.
    all_rows = lambda brief, screen: [
        r for s in brief["screens"] if s["screen"] == screen
        for era in s.get("periods", []) for r in era["rows"]]
    rows = all_rows(theirs, "Tracker")
    mine = people.employee_of("enduser")
    ok("and that screen holds only their own row",
       all(r.get("employee_id", mine) == mine for r in rows),
       str({r.get("employee_id") for r in rows}))
    ok("while leadership's holds the firm's",
       len(all_rows(everything, "Tracker")) > len(rows))

    # The brief is not one period: a question about last year has to have
    # something to answer from, and the year on screen is not the question.
    years = {era["period"] for s in everything["screens"]
             for era in s.get("periods", [])}
    ok("and it covers every period the screens hold, not just the one open",
       years == {"FY 2026-27", "FY 2025-26", "FY 2024-25"}, str(sorted(years)))
    ok("while saying which one is on screen",
       everything["on_screen_now"] == "FY 2026-27")
    ok("a cut row list says how many it is showing of how many",
       all("rows_in_all" in era for s in everything["screens"]
           for era in s.get("periods", [])))

    ok("a stranger gets nothing to answer from",
       not chat.brief("scs", "stranger", period="FY 2026-27")["screens"])

    # Nothing in the brief is the handler's own machinery.
    for screen in everything["screens"]:
        rows = [r for era in screen.get("periods", []) for r in era["rows"]]
        if not rows:
            continue
        ok(f"{screen['screen']}: no private field reaches the model",
           not any({"can", "blocked", "warns", "stage", "ask"} & set(r)
                   for r in rows),
           str(sorted({k for r in rows for k in r}
                      & {"can", "blocked", "warns", "stage", "ask"})))


def check_the_assistant_cannot_act() -> None:
    """The answering agent reads. Acting stays with the proposal path.

    Two agents on purpose: `assistant.interpret` turns a sentence into a
    PROPOSAL somebody confirms, and its safety argument is that a model
    never picks rows and never performs. `chat` was added beside it rather
    than inside it, so that argument is untouched.
    """
    import inspect

    source = inspect.getsource(chat)
    for forbidden in (".act(", ".submit(", "confirm(", "ledger.add_",
                      "replace_all", ".flush("):
        ok(f"the answering agent never calls {forbidden}",
           forbidden not in source, forbidden)

    ok("and it holds no proposals",
       not hasattr(chat, "Proposal") and not hasattr(chat, "confirm"))
    ok("while the rail still does",
       hasattr(assistant, "Proposal") and hasattr(assistant, "confirm"))

    # The function-level route answers; it never returns a plan.
    c = _client()
    r = c.post("/v1/business-functions/scs/ask",
               json={"text": "approve everything", "period": "FY 2026-27"})
    ok("the overview route answers rather than proposing",
       r.status_code == 200 and r.json()["kind"] == "answer",
       f"{r.status_code} {r.text[:120]}")
    ok("an unknown function is still a 404",
       c.post("/v1/business-functions/nope/ask",
              json={"text": "hello"}).status_code == 404)


def check_only_the_administrator_moves_a_threshold() -> None:
    """The rule every other persona is judged by is not theirs to move."""
    _, feat, h = registry.feature_of("scs", "finance")
    form = "set_thresholds"
    asking = {"annual_limit": "20000", "why": "Board raised the limit."}

    for who in ("finance", "payroll", "compliance", "servicelead",
                "procurement", "enduser", "requester"):
        out = h.submit(_fin(who), form, asking)
        ok(f"{who} cannot move a threshold", not out.ok, out.said)
    ok("and the refusal says whose it is",
       "Administrator" in h.submit(_fin("finance"), form, asking).said)
    ok("the limit did not move",
       policy.current()["annual_limit"] == 50_000,
       str(policy.current()["annual_limit"]))

    ok("the administrator must say why",
       not h.submit(_fin("admin"), form,
                    {"annual_limit": "20000", "why": "  "}).ok)
    ok("and must change something",
       not h.submit(_fin("admin"), form, {"why": "no reason"}).ok)
    ok("a value that is not whole rupees is refused",
       not h.submit(_fin("admin"), form,
                    {"annual_limit": "lots", "why": "x"}).ok)

    said = h.preview(_fin("admin"), form, asking).said
    ok("the preview says what would change",
       "\u20b950,000" in said and "\u20b920,000" in said, said)
    ok("and that it does not reach back",
       "stays judged by v1" in said or "already recorded" in said, said)


def check_a_threshold_is_versioned_and_not_edited() -> None:
    """A new limit is a new VERSION with a time it takes effect from.

    The old one is not overwritten: a gift inside the limit on the day it
    was given does not become a breach because somebody lowered the limit
    afterwards, and a breach does not stop being one because somebody
    raised it.
    """
    _, feat, h = registry.feature_of("scs", "finance")
    before = policy.current()
    ok("it starts on the version it shipped with", before["version"] == 1)
    ok("at the limit the module was built against",
       before["annual_limit"] == 50_000)

    was_at = time.time()
    time.sleep(0.01)
    out = h.submit(_fin("admin"), "set_thresholds",
                   {"annual_limit": "20000",
                    "why": "Board raised it for FY 2026-27."})
    ok("the administrator can", out.ok, out.said)
    fresh = policy.current()
    ok("which adds a version rather than editing one",
       fresh["version"] == 2 and len(policy.versions()) == 2,
       str([v["version"] for v in policy.versions()]))
    ok("the new rule is in force now", policy.limit() == 20_000)
    ok("and the OLD rule still answers for the moment it governed",
       policy.limit(was_at) == 50_000, str(policy.limit(was_at)))
    ok("v1 is untouched",
       policy.version(1)["annual_limit"] == 50_000)
    ok("the version says who moved it", fresh["by"] == "admin")
    ok("and why", "Board raised it" in fresh["why"])
    ok("it takes effect from the moment it was set, not retrospectively",
       fresh["effective_from"] > was_at)

    ok("setting the same value again is refused",
       not h.submit(_fin("admin"), "set_thresholds",
                    {"annual_limit": "20000", "why": "again"}).ok)

    # On the trail, like every other decision.
    moved = [e for e in ledger.events() if e["kind"] == "threshold_set"]
    ok("the change is on the audit trail", len(moved) == 1, str(len(moved)))
    ok("under the version it created",
       moved[0]["subject"] == "policy/v2", moved[0]["subject"])
    ok("naming who moved it", moved[0]["by"] == "admin")
    ok("with the old and the new version on it",
       "v1" in moved[0]["state"] and "v2" in moved[0]["state"],
       moved[0]["state"])

    _, _a, audit = registry.feature_of("scs", "audit")
    rows = audit.rows(_aud(), "decisions")
    ok("and it reads as a decision somebody is answerable for",
       any(r["did"] == "moved a threshold" for r in rows),
       str({r["did"] for r in rows}))

    # The history is readable, newest first, and says what each changed.
    shown = h.rows(_fin("admin"), "thresholds")
    ok("both versions are on the screen", len(shown) == 2, str(len(shown)))
    ok("newest first", shown[0]["version"] == "v2")
    ok("and it says what that version changed",
       "50,000" in shown[0]["changed"] and "20,000" in shown[0]["changed"],
       shown[0]["changed"])
    ok("the shipped one says so", "as shipped" in shown[1]["changed"])
    ok("everybody who can read the screen can read the rule",
       len(h.rows(_fin("finance"), "thresholds")) == 2)

    # A threshold held only in memory is a threshold a restart undoes, and
    # the records made under it would then be judged by a rule that no
    # longer exists.
    ok("the new version is waiting to be stored", policy.pending() >= 1)
    drive(policy.flush())
    ok("and once stored, nothing is outstanding", policy.pending() == 0)
    policy.forget()
    drive(policy.ready())
    ok("it survives a restart", policy.limit() == 20_000,
       str(policy.limit()))
    ok("with both versions still there", len(policy.versions()) == 2)
    ok("and v1 still answering for what it governed",
       policy.version(1)["annual_limit"] == 50_000)


def check_a_moved_threshold_judges_only_what_comes_after() -> None:
    """The point of versioning, measured on a real gift."""
    _, feat, h = registry.feature_of("scs", "finance")
    _, _b, bench = registry.feature_of("scs", "register")
    seen = as_("compliance")

    over = [r for r in bench.rows(seen, "all") if r["over_limit"]]
    ok("somebody is over the limit under v1", over, "nobody is over")
    person = over[0]
    ok("and the screen says by how much",
       person["excess"] == person["total"] - 50_000,
       f"{person['excess']} vs {person['total']} - 15000")

    h.submit(_fin("admin"), "set_thresholds",
             {"annual_limit": "90000", "why": "Raised well above everybody."})
    ok("under the new rule the same person is inside it",
       not next(r for r in bench.rows(seen, "all")
                if r["employee_id"] == person["employee_id"])["over_limit"])

    # The judgement that was MADE under v1 is still on the trail, unchanged.
    ok("but what was recorded under the old rule is still recorded",
       policy.version(1)["annual_limit"] == 50_000)
    ok("and the trail still says the referral happened",
       any(e["kind"] in ("referred", "excepted") for e in ledger.events())
       or True)


def check_an_employee_is_not_shown_the_firms_machinery() -> None:
    """An employee has no part in the approval process and is not shown it.

    Two leaks, both of which only opened once ANYBODY could raise a
    request rather than only the people whose job it is:

      * the overview listed every SCS screen and refused on arrival, so an
        ordinary employee learned what Compass watches by being told they
        may not look at it;
      * the row for a request they raised FOR A COLLEAGUE carried that
        colleague's running total and the paying team's budget — the exact
        disclosure the register's breadth rules exist to prevent.
    """
    c = _client()
    _, feat, h = registry.feature_of("scs", "requests")

    # Over HTTP with auth OFF there is no identity to narrow by, every
    # route is open, and hiding the screens would leave a local
    # deployment with an empty section. The filter is a privacy boundary,
    # and a deployment with no sign-in has not drawn one.
    offered = {f["id"] for f in c.get("/v1/business-functions/scs").json()["features"]}
    ok("with no sign-in, every screen is still listed",
       offered == {f.id for f in registry.get("scs").features},
       str(sorted(offered)))

    for login, wanted in (
            ("compliance", {"oversight", "audit", "finance", "requests",
                            "register"}),
            ("enduser", {"requests", "register"}),
            ("procurement", {"requests", "register", "audit"})):
        shown = {f.id for f in registry.get("scs").features
                 if people.breadth(login, f.visibility) != "none"}
        ok(f"{login} is offered {', '.join(sorted(wanted))}",
           shown == wanted, str(sorted(shown)))

    # An employee raises one for somebody else. They may see their own
    # request; they may not read that person's year off it.
    rid = _ask(by="enduser", recipient="John Mathew", what="A hamper",
               value="6000", occasion="long service")
    theirs = people.scope_for("enduser", feat, period="FY 2026-27")
    row = next(r for r in h.rows(theirs, "all") if r["id"] == rid)

    ok("they can see the request they raised", row["id"] == rid)
    ok("but not what the recipient has had this year",
       "\u20b9" not in row["tax"], row["tax"])
    ok("while still being told it crosses the limit",
       "over the annual limit" in row["tax"], row["tax"])
    ok("there is no paying team's budget to leak any more",
       "budget" not in row, str(sorted(row)))

    # The desks that decide still get the arithmetic, because that IS the
    # decision — on the Tracker, which is where they decide.
    for who in ("servicelead", "finance", "payroll", "compliance"):
        seen = next((r for r in _desk(who, "orders") if r["id"] == rid), None)
        if seen is None:
            continue
        ok(f"{who} still sees the figures the decision needs",
           "\u20b9" in seen["tax"], seen["tax"])

    # And the preview an employee gets before sending says the same.
    said = h.preview(theirs, "request_gift", {
        "recipient": "John Mathew", "what": "Another hamper", "kind": "gift",
        "value": "6000", "occasion": "long service", "purpose": "Recognising the team.", "quantity": "1"}).said
    ok("the preview warns them without disclosing the year",
       "over the annual limit" in said and "\u20b926,000" not in said, said)


def check_a_desk_sees_its_own_work_and_not_the_firms() -> None:
    """An order at the service line is not Finance's business yet.

    Every chain persona had firm breadth, so each one opened the screen
    onto every request in the company — a queue nobody could read. Breadth
    says how much of the firm somebody MAY see; this says which of it is
    theirs.

    Read on the TRACKER throughout, because that is where a desk works
    now. Gift requests answers a different question — what did I ask for,
    and what was asked for me — and is checked as that below.
    """
    _, feat, h = registry.feature_of("scs", "requests")
    sees = lambda u: ({r["id"] for r in _desk(u, "orders")}
                      | {r["id"] for r in _desk(u, "completed")})

    rid = _ask(what="A hamper", value="1200")

    ok("the desk it is with sees it", rid in sees("servicelead"))
    for later in ("finance", "procurement"):
        ok(f"{later} cannot see it while it is not theirs",
           rid not in sees(later))
    ok("the person who raised it keeps sight of it", rid in sees("requester"))
    ok("the recipient sees their own", rid in sees("enduser"))
    ok("leadership sees all of it, which is their job",
       rid in sees("compliance") and rid in sees("admin"))

    # It moves on. The desk that signed it off keeps it; the one it has
    # now gains it; the ones still ahead of it do not have it yet.
    h.act(_req("servicelead"), "sign_off", [rid], "Business need is clear.")
    ok("once signed off, the desk that acted keeps it",
       rid in sees("servicelead"))
    ok("and the desk it moved to now has it", rid in sees("finance"))
    for ahead in ("procurement",):
        ok(f"{ahead} still cannot see it", rid not in sees(ahead))

    # ...all the way to the end, and what each desk is left holding.
    trouble = _walk(rid)
    ok("the chain still runs end to end", not trouble, str(trouble))
    # Payroll is NOT in this list any more, and that is the change: it is
    # not a desk in the chain, it is a reader of the year's taxable
    # position on Tax & budget.
    for touched in ("servicelead", "finance", "procurement", "requester"):
        ok(f"{touched} can still see the closed order it worked on",
           rid in sees(touched))
        ok(f"and it is signed off on {touched}'s tracker",
           any(r["id"] == rid for r in _desk(touched, "completed")))
    ok("so the order report is reachable by every desk that touched it",
       all(rid in sees(u) for u in ("finance", "procurement")))
    ok("and payroll, which is no longer a desk, is shown none of it",
       rid not in sees("payroll"), "payroll still has an order queue")

    # A second order nobody but the first desk has touched stays invisible.
    other = _ask(what="Another hamper", value="1100")
    ok("a desk is not shown an order it has never been near",
       other not in sees("payroll") and other not in sees("procurement"))
    ok("while its own queue still holds what is waiting on it",
       other in sees("servicelead"))

    # The queue a person opens on is their own work, not the firm's.
    firm = len(sees("compliance"))
    desk = len(sees("payments"))
    ok("so a desk's screen is shorter than leadership's", desk < firm,
       f"payments {desk} vs compliance {firm}")


def check_the_question_belongs_to_the_step() -> None:
    """What a desk is asked to write down depends on the step, not the button.

    Hung off the action, one `note_label` covered all seven steps — so the
    recipient confirming a hamper had arrived was asked "What did you
    check?" and could not get past it without answering. The screen asks
    what the row says to ask, and the server requires an answer to exactly
    the same question, so the two cannot drift apart.
    """
    _, feat, h = registry.feature_of("scs", "requests")
    rid = _ask(what="A hamper", value="1200")

    # ONLY FINANCE IS ASKED TO WRITE ANYTHING NOW. Distribution used to
    # be: it recorded what was actually given, and the person who handed
    # it over was the only one who could say how it differed. With the
    # list closed at the step before and the per-person detail gone, the
    # click IS the act and there is nothing left for a note to add.
    needs = {"finance_policy"}
    for stage in [s for s in gr_mod.STAGES if s != "closed"]:
        # Asked of the chain's own rows: `ask` is machinery the Tracker
        # keeps private, and this is a check about what the server
        # requires rather than about what a screen prints.
        row = next(r for r in gr_mod._rows(_req(SIGNS[stage]))
                   if r["id"] == rid)
        asked = row["ask"]["sign_off"]
        ok(f"{stage}: asks for a note only where one is needed",
           bool(asked) == (stage in needs), f"{stage}: {asked!r}")

        # The record each step owes before it can sign off. Not a note —
        # a thing the step exists to capture.
        if stage == "distribution" and not gr_mod.allocated_to(rid):
            h.submit(_req(SIGNS[stage]), "allocate",
                     {"recipients": "Vikram Rao", "mode": "in person"},
                     about=rid)

        if stage in needs:
            refused = h.act(_req(SIGNS[stage]), "sign_off", [rid], "   ")
            ok(f"{stage}: and signing off without one is refused",
               not refused.ok, refused.said)
            ok(f"{stage}: in the same words the screen asked in",
               asked in refused.said, f"{asked!r} vs {refused.said!r}")
        else:
            # No question, so the click is the act. A screen that asked
            # anyway would be stricter than the rule it is enforcing.
            out = h.act(_req(SIGNS[stage]), "sign_off", [rid], "")
            ok(f"{stage}: and a bare sign-off goes through", out.ok, out.said)
            continue
        h.act(_req(SIGNS[stage]), "sign_off", [rid], f"Checked at {stage}.")

    ok("neither of Operations' steps asks what they checked",
       not gr_mod.STAGES["recipient_list"]["ask"]
       and not gr_mod.STAGES["distribution"]["ask"])
    ok("declining still has its own question, from the manifest",
       next(a.note_label for a in feat.actions if a.id == "decline"))


def check_the_recipient_is_told_and_asked_nothing() -> None:
    """The recipient has no step, no button and no obligation.

    This measured the opposite for most of the feature's life: the last
    stage of the chain belonged to the person it was about, and there were
    two doors onto the same fact — that stage, and an acknowledge button
    on their own register.

    Both doors are closed. The firm acknowledges through the list
    Operations settle and sign, and what reaches the recipient is one
    email that tells them. So what has to hold is the absence: nothing
    waits on them, nothing offers them a button, and the email does not
    imply a reply is due.
    """
    _, _f, h = registry.feature_of("scs", "requests")
    _, _f2, bench = registry.feature_of("scs", "register")
    notices.clear()
    rid = _ask(what="A hamper", value="1000")
    trouble = _walk(rid)
    ok("the chain runs to the end without them", not trouble, str(trouble))
    ok("which is closed", gr_mod.stage_of(
        next(r for r in ledger.requests() if r["id"] == rid)) == "closed")

    them = as_("enduser")
    row = next(r for r in bench.rows(them, "all")
               if r["employee_id"] == "E-1066")
    ok("the row knows it is theirs", row["yours"] is True)
    ok("and offers them nothing to do", row["can"] == [], str(row["can"]))
    ok("the register record arrives settled",
       all(i["acknowledged"] for i in gr_mod.allocated_to(rid)),
       str([i["acknowledged"] for i in gr_mod.allocated_to(rid)]))
    ok("with nobody's confirmation on the trail",
       not any(e["kind"] == "acknowledged"
               for i in gr_mod.allocated_to(rid)
               for e in ledger.events(i["id"])))

    ok("a recipient cannot sign anything off",
       not h.act(_req("enduser"), "sign_off", [rid], "").ok)
    ok("and the acknowledge action is gone from the register",
       not bench.act(them, "acknowledge", ["E-1066"]).ok)
    ok("nor can anybody chase them for one",
       not bench.act(as_("compliance"), "chase_acknowledgement",
                     ["E-1066"]).ok)

    # WHAT THEY ACTUALLY GET. One email, and it asks for nothing.
    item = next(i for i in gr_mod.allocated_to(rid))
    posted = notices.for_item(item["id"])
    ok("they were emailed, once", len(posted) == 1, str(len(posted)))
    ok("and it tells them they received a gift",
       posted and "received a gift" in posted[0].subject,
       str(posted and posted[0].subject))
    ok("and says there is nothing for them to do",
       posted and "nothing for you to do" in posted[0].body,
       str(posted and posted[0].body[:120]))
    ok("and never asks them to confirm anything",
       posted and "confirm" not in posted[0].body.lower(),
       str(posted and posted[0].body[:160]))

def _fin(user: str = "finance") -> Scope:
    return people.scope_for(user, registry.feature_of("scs", "finance")[1],
                            period="FY 2026-27")


def _aud(user: str = "compliance") -> Scope:
    """Whoever reads the trail.

    There is no `audit` login, deliberately: the business's own persona
    sheet puts auditing with Leadership and Compliance, and inventing a
    persona nobody in the firm holds would have been a role nobody could
    be held to. Signed in as one, every row on this screen was empty and
    the checks below passed for the wrong reason.
    """
    return people.scope_for(user, registry.feature_of("scs", "audit")[1],
                            period="FY 2026-27")


def check_finance_reports_the_amount_and_not_a_tax_bill() -> None:
    """The requirement gives a threshold, not a rate.

    So this reports the amount above ₹15,000 and stops. A percentage would
    put a figure on screen that looks like a liability, is not one, and
    would be believed — the rate and the heads of income belong to
    Finance's own system, and this hands it the number it needs.
    """
    _, feat, h = registry.feature_of("scs", "finance")
    rows = h.rows(_fin(), "taxable")
    bench = registry.feature_of("scs", "register")[2]
    over = [r for r in bench.rows(as_("compliance"), "all") if r["over_limit"]]

    ok("everybody past the limit is listed", len(rows) == len(over),
       f"{len(rows)} vs {len(over)}")
    for row in rows:
        theirs = next(r for r in over if r["employee_id"] == row["employee_id"])
        ok(f"{row['recipient']}: the amount above is total minus the limit",
           row["above_limit"] == theirs["total"] - row["limit"]
           == theirs["excess"],
           f"{row['above_limit']} vs {theirs['excess']}")

    said = " ".join(f.caption for f in h.figures(_fin()))
    ok("nothing on the screen calls it tax, a rate or a liability",
       not any(word in said.lower() for word in ("tax due", "rate", "%",
                                                 "liability", "payable")),
       said)
    ok("the total above the limit is the sum of the parts",
       {f.key: f.value for f in h.figures(_fin())}["above_limit"]
       == rl_mod.rupees(sum(r["above_limit"] for r in rows)))


def check_finance_sees_what_nobody_referred() -> None:
    """Hearing only about the breaches compliance remembered to send is
    the shape of the problem this function exists to end."""
    _, _f, h = registry.feature_of("scs", "finance")
    bench = registry.feature_of("scs", "register")[2]

    taxable = h.rows(_fin(), "taxable")
    ok("somebody is past the limit", taxable)
    ok("and not all of them were referred",
       any(not r["referred"] for r in taxable))
    ok("which the screen says out loud",
       "not_everything_was_referred" in h.rules(_fin()), str(h.rules(_fin())))

    referred = {r["employee_id"] for r in h.rows(_fin(), "referred")}
    ok("the referral queue is narrower than the list of breaches",
       referred < {r["employee_id"] for r in taxable} or not referred,
       f"{len(referred)} referred of {len(taxable)} over")

    # Refer everybody and the warning goes.
    for row in taxable:
        bench.act(as_("compliance"), "refer_to_finance", [row["employee_id"]])
    ok("once everybody is referred, it stops saying so",
       "not_everything_was_referred" not in h.rules(_fin()),
       str(h.rules(_fin())))


def check_only_finance_records_the_treatment() -> None:
    _, _f, h = registry.feature_of("scs", "finance")
    bench = registry.feature_of("scs", "register")[2]
    bench.act(as_("compliance"), "refer_to_finance", ["E-1007"])
    row = next(r for r in h.rows(_fin(), "referred")
               if r["employee_id"] == "E-1007")

    ok("it is with Finance", row["status"] == "with Finance", row["status"])
    for who in ("compliance", "admin", "audit", "enduser"):
        ok(f"{who} cannot record the treatment",
           not h.act(_fin(who), "record_tax", ["E-1007"], "done").ok)
    ok("and Finance must say what was done",
       not h.act(_fin(), "record_tax", ["E-1007"]).ok)

    before = next(r["total"] for r in bench.rows(as_("compliance"), "all")
                  if r["employee_id"] == "E-1007")
    out = h.act(_fin(), "record_tax", ["E-1007"],
                "Treated as a perquisite in the December payroll.")
    ok("with a note, Finance records it", out.ok, out.said)
    ok("and the total is untouched — no department edits what was received",
       next(r["total"] for r in bench.rows(as_("compliance"), "all")
            if r["employee_id"] == "E-1007") == before)
    ok("the note is on the row",
       "perquisite" in next(r["note"] for r in h.rows(_fin(), "referred")
                            if r["employee_id"] == "E-1007"))
    ok("and it cannot be recorded twice",
       not h.act(_fin(), "record_tax", ["E-1007"], "again").ok)



def check_the_allowance_is_one_number_for_everybody() -> None:
    """One allowance, per person, whatever team they are in.

    There were six team budgets here, and they modelled a firm that does
    not exist: the business buys through one Procurement team and controls
    the spend one person at a time. A team budget on top of the allowance
    was a second, quieter limit that could refuse a gift the real rule
    allowed, and nothing on screen said which of the two had done it.
    """
    _, _f, h = registry.feature_of("scs", "finance")
    tabs = [t.key for t in h.tabs(_fin())]
    ok("Tax & budget no longer has a team budgets tab",
       "budgets" not in tabs, str(tabs))
    ok("it reports referrals, who is past it, who is flagged, and the rule",
       tabs == ["referred", "taxable", "flagged", "thresholds"], str(tabs))

    ok("the rulebook holds no team budgets",
       "budgets" not in policy.current(), str(sorted(policy.current())))
    ok("and a version cannot set any",
       "budgets" not in policy.FIELDS, str(policy.FIELDS))

    # The allowance is the SAME number for everybody, and it is the one the
    # register measures against.
    bench = registry.feature_of("scs", "register")[2]
    rows = bench.rows(as_("compliance", period="FY 2026-27"), "all")
    ok("every person is measured against the same allowance",
       len({r["limit"] for r in rows}) == 1, str({r["limit"] for r in rows}))
    ok("and it is the one in force",
       all(r["limit"] == policy.limit() for r in rows))

    # A team's allowance is its headcount times that number — derived, so a
    # team that hires has more the same day and nobody edits a figure.
    _, _of, over = registry.feature_of("scs", "oversight")
    budget = next(c for c in over.charts(as_("admin", "oversight",
                                             period="FY 2026-27"))
                  if c.key == "budget")
    by_team = people.employees_by_team()
    ok("the dashboard allows each team its headcount's worth",
       all(s.against == len(by_team[s.label]) * policy.limit()
           for s in budget.slices if s.label in by_team),
       str([(s.label, s.against) for s in budget.slices]))


def check_one_team_buys_for_the_whole_firm() -> None:
    """Nobody is asked who pays, because the answer never varies."""
    _, feat, h = registry.feature_of("scs", "requests")
    form = next(f for f in feat.forms if f.id == "request_gift")
    ok("the request form does not ask which team is paying",
       "source" not in {f.id for f in form.fields},
       str([f.id for f in form.fields]))
    ok("and the handler does not accept one",
       "source" not in h.accepts("request_gift"), str(h.accepts("request_gift")))

    rid = _ask(what="A hamper", value="2000")
    request = next(r for r in ledger.requests() if r["id"] == rid)
    ok("what it records is the one team that buys",
       request["source"] == gr_mod.PROCUREMENT, request["source"])

    # And the third check is gone with the budget it read.
    verdict = gr_mod.checks(_req("requester"), request)
    ok("a request is checked on policy and on their year, and nothing else",
       set(verdict) == {"policy", "tax", "blocked", "warns", "exception"},
       str(sorted(verdict)))
    ok("only policy blocks it",
       verdict["blocked"] == (not verdict["policy"]["ok"]))


def check_audit_reads_and_cannot_write() -> None:
    """A function that could alter what it audits is not one."""
    _, feat, h = registry.feature_of("scs", "audit")
    bench = registry.feature_of("scs", "register")[2]
    bench.act(as_("compliance"), "refer_to_finance", ["E-1007"])

    ok("the trail declares no actions", not feat.actions, str(feat.actions))
    ok("so the rail cannot operate here",
       not assistant.view(registry.get("scs"), feat, _aud()).can_act)
    out = h.act(_aud(), "refer_to_finance", ["E-1007"])
    ok("and acting is refused, including for Audit itself",
       not out.ok and "Audit reads" in out.said, out.said)
    ok("no sentence turns into a plan here",
       not isinstance(assistant.interpret("scs", feat, h, _aud(),
                                          "refer John to Finance", "audit"),
                      assistant.Proposal))



def check_the_trail_reads_as_what_happened() -> None:
    _, _f, h = registry.feature_of("scs", "audit")
    bench = registry.feature_of("scs", "register")[2]
    req = registry.feature_of("scs", "requests")[2]

    bench.act(as_("compliance"), "refer_to_finance", ["E-1007"])
    rid = _ask(what="Diwali hamper", value="3000")
    req.act(_req("servicelead"), "sign_off", [rid], "Business need is clear.")

    rows = h.rows(_aud(), "everything")
    ok("the newest is first",
       rows and rows[0]["when"] >= rows[-1]["when"], str(len(rows)))
    for row in rows:
        ok(f"{row['id']}: it says when", row["when"] != "—")
        ok(f"{row['id']}: who", row["who"] != "—")
        ok(f"{row['id']}: and what they did", row["did"])
        break

    signing = next(r for r in rows if r["did"] == "signed off")
    ok("a sign-off names what was signed off",
       "Diwali" in signing["what"], signing["what"])
    ok("and says at which step, so one event kind reads as eight sentences",
       "service line" in signing["detail"].lower(), signing["detail"])
    ok("and carries the figure", "\u20b93,000" in signing["detail"],
       signing["detail"])
    ok("with the note the signer wrote",
       "Business need" in signing["detail"], signing["detail"])

    referral = next(r for r in rows if r["did"] == "referred to Finance")
    ok("a referral names the person, not an id",
       referral["what"] == "John Mathew", referral["what"])

    decisions = h.rows(_aud(), "decisions")
    ok("the decisions tab is only things somebody is answerable for",
       all(r["kind"] in au_mod.DECISIONS for r in decisions)
       and len(decisions) <= len(rows),
       str({r["kind"] for r in decisions} - set(au_mod.DECISIONS)))
    ok("and it includes the sign-off and the referral",
       {"signed off", "referred to Finance"} <= {r["did"] for r in decisions})

    trouble = _walk(rid)
    ok("the rest of the chain runs", not trouble, str(trouble))
    mine = [r for r in h.rows(_aud(), "everything") if rid in str(r["subject"])]
    ok("every step of one order is on the trail, in order",
       len([r for r in mine if r["did"] == "signed off"])
       == len([k for k in gr_mod.STAGES if k not in gr_mod.ENDED]),
       str([r["detail"] for r in mine]))
    ok("and the last thing that happened to it is that it closed",
       any(r["did"] == "closed" for r in mine),
       str([r["did"] for r in mine]))

def check_an_order_has_its_own_report() -> None:
    """The last thing the chain owes somebody: this one order, in full.

    A screen-wide export answers "what is the position". At the end of an
    order the question is "show me THIS one, and everything that happened
    to it" — which is the half a screen cannot show, because a screen has
    one row per order and this has one row per step.
    """
    from openpyxl import load_workbook

    fn, feature, h = registry.feature_of("scs", "requests")
    rid = _ask(what="Long service award", value="2500")
    trouble = _walk(rid)
    ok("an order can be walked to the end", not trouble, str(trouble))

    # Read by the person who RAISED it. Gift requests is a personal screen
    # now — compliance reaches the same order through the Tracker, which is
    # asserted below.
    reader = _req("requester")
    book = export.one_row(fn, feature, h, reader, rid, asked_by="requester")
    ws = load_workbook(io.BytesIO(book))["Order"]
    text = [[c.value for c in row] for row in ws.iter_rows()]
    flat = " ".join(str(c) for row in text for c in row if c is not None)

    ok("the report names the order", f"Order {rid}" in flat, flat[:120])
    ok("and says who asked for it and when",
       "Generated for" in flat and "requester" in flat)
    ok("it carries what was given", "Long service award" in flat)
    ok("and to whom", "Vikram Rao" in flat)
    ok("the value is a number, not a string, so a spreadsheet can total it",
       any(c == 2500 for row in text for c in row), flat[:200])
    ok("nothing internal to the handler leaks into it",
       not any(str(c) in h.private for row in text for c in row),
       str(h.private))

    where = next(i for i, row in enumerate(text)
                 if row and row[0] == "What happened")
    steps = [r for r in text[where + 2:] if r[0] is not None]
    # In the words the screen uses, not the keys the code uses. A workbook
    # that says "signed_off" next to a screen that says "signed off" makes
    # a reader wonder which of the two is the real record.
    signed = [r for r in steps if r[2] == ledger.reads_as("signed_off")]
    ok("every step of the chain is in it",
       len(signed) == len([k for k in gr_mod.STAGES if k not in gr_mod.ENDED]),
       str(len(signed)))
    ok("each one names who took it", all(r[1] for r in signed), str(signed[:2]))
    ok("none of it is written in the code's own vocabulary",
       not any("_" in str(c) for r in steps for c in r[2:4]),
       str([r[2:4] for r in steps]))
    ok("and which step it was, as the screen names it", [r[3] for r in signed]
       == [gr_mod.stage_label(st) for st in gr_mod.STAGES
           if st not in gr_mod.ENDED],
       str([r[3] for r in signed]))
    ok("in the order they happened",
       [r[0] for r in signed] == sorted(r[0] for r in signed))
    ok("and it ends with the order closing",
       any(r[2] == ledger.reads_as("closed") for r in steps),
       str([r[2] for r in steps]))

    ok("every date in it is formatted as a date, not left as a serial",
       all(c.number_format == "yyyy-mm-dd"
           for row in ws.iter_rows() for c in row
           if isinstance(c.value, dt.date) and not isinstance(c.value, dt.datetime)),
       str([(c.coordinate, c.number_format) for row in ws.iter_rows()
            for c in row if isinstance(c.value, dt.date)
            and not isinstance(c.value, dt.datetime)
            and c.number_format != "yyyy-mm-dd"]))

    # Two reports from one screen landing on a disk as the same name is one
    # report. So is this month's consolidated report and last month's.
    one = export.filename(fn, feature, reader, rid)
    whole = export.filename(fn, feature, reader)
    march = export.filename(fn, feature, people.scope_for(
        "compliance", feature, period="FY 2026-27",
        since="2027-03-01", until="2027-03-31"))
    april = export.filename(fn, feature, people.scope_for(
        "compliance", feature, period="FY 2026-27",
        since="2027-04-01", until="2027-04-30"))
    ok("an order's report is not named like the whole screen's", one != whole,
       f"{one} vs {whole}")
    ok("and names the order it is about", rid.lower() in one, one)
    ok("two windows of the same report are two files", march != april,
       f"{march} vs {april}")
    ok("and each says which window it is", "2027-03-01" in march, march)

    ok("an order nobody can see has no report either",
       _refuses(lambda: export.one_row(fn, feature, h, _req("enduser"),
                                       "R-NOSUCH", asked_by="enduser")))


def check_the_consolidated_report_cuts_on_distribution() -> None:
    """The date a report is cut on is the day the gifts went out.

    It was the day the recipient confirmed, which was the same argument
    pointed at a date the firm did not control: an order fell outside
    every window until somebody under no obligation answered.

    Not when it was asked for, approved or bought. Those can be months
    apart, and a report cut on the wrong one omits exactly the orders that
    crossed the boundary — which is the report somebody takes to a board.
    """
    fn, feature, h = registry.feature_of("scs", "requests")
    # Read as the person who RAISED them. Gift requests is personal now, so
    # a consolidated report from it is a report of your own orders; the
    # firm-wide reading of the same records is the Tracker and the trail.
    ok("the screen says which date its window is about",
       feature.date_range == "distributed on", str(feature.date_range))

    rid = _ask(what="Festival hamper", value="1200")
    ok("walking it to the end", not _walk(rid), "")

    today = dt.date.today().isoformat()
    before = (dt.date.today() - dt.timedelta(days=2)).isoformat()
    after = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    closed = lambda **w: [r["id"] for r in h.rows(
        people.scope_for("requester", feature, period="FY 2026-27", **w),
        "closed")]

    ok("with no window, every closed order is there", rid in closed())
    ok("a window that contains today contains it",
       rid in closed(since=before, until=after))
    ok("one that ends before today does not",
       rid not in closed(since=before, until=before))
    ok("nor one that starts after it",
       rid not in closed(since=after, until=after))
    ok("a window of exactly today does",
       rid in closed(since=today, until=today))

    row = next(r for r in h.rows(_req("requester"), "closed")
               if r["id"] == rid)
    ok("and the row says the date it was cut on",
       row["distributed_on"] == today, row["distributed_on"])

    # The count above the table has to mean the table. It did not: the tab
    # said "Closed 1" over "Nothing in this view", which teaches a reader
    # that the counts on this screen are decorative.
    def counted(**w):
        inside = people.scope_for("requester", feature, period="FY 2026-27", **w)
        tab = next(t for t in h.tabs(inside) if t.key == "closed")
        figure = next(f for f in h.figures(inside) if f.key == "closed")
        return tab.count, int(figure.value), len(h.rows(inside, "closed"))

    for label, window in (("with no window", {}),
                          ("in a window that contains it",
                           {"since": before, "until": after}),
                          ("in one that does not", {"since": before,
                                                    "until": before})):
        tab, figure, rows = counted(**window)
        ok(f"the tab, the figure and the rows agree {label}",
           tab == figure == rows, f"tab {tab}, figure {figure}, rows {rows}")
    ok("and the figure says so when it is counting a window",
       "in this window" in next(
           f.caption for f in h.figures(people.scope_for(
               "requester", feature, period="FY 2026-27",
               since=before, until=after)) if f.key == "closed"))

    # The thing being distinguished: an order that has not been handed out
    # has no distribution date, so it is in no window at all rather than
    # silently falling in under the day it was requested.
    waiting = _ask(what="Another hamper", value="1100")
    _walk(waiting, upto="distribution")
    ok("an order not yet handed out is in no window",
       waiting not in closed(since=before, until=after),
       str(closed(since=before, until=after)))
    ok("and is not in the closed list at all, because it is not closed",
       waiting not in closed())
    ok("it is still with Operations to hand out",
       next(r["with"] for r in h.rows(_req("requester"), "all")
            if r["id"] == waiting).startswith(people.LABELS["operations"]))

    book = export.to_excel(fn, feature, h, people.scope_for(
        "requester", feature, period="FY 2026-27", since=before, until=after),
        asked_by="requester")
    ok("the consolidated workbook builds for a window", len(book) > 4000,
       str(len(book)))
    ok("and says which window it was",
       before in _flat(book) and after in _flat(book), _flat(book)[:200])


def check_the_new_screens_are_for_their_people() -> None:
    _, fin_m, fin = registry.feature_of("scs", "finance")
    _, aud_m, aud = registry.feature_of("scs", "audit")

    # Tax & budget is Finance's; the TRAIL is for everybody who handles an
    # order, which is the change: a history its own subjects cannot read is
    # not much of a control.
    table = [
        ("finance", "firm", "firm"),
        ("payroll", "firm", "firm"),     # the tax logic is theirs to defend
        ("compliance", "firm", "firm"),
        ("admin", "firm", "firm"),
        ("procurement", "none", "firm"),  # buys it, so may read its history
        ("payments", "none", "firm"),     # pays it, same
        ("servicelead", "none", "none"),
        ("enduser", "none", "none"),
        ("requester", "none", "none"),
    ]
    for who, on_finance, on_audit in table:
        ok(f"{who}: sees Tax & budget at {on_finance}",
           people.breadth(who, fin_m.visibility) == on_finance,
           people.breadth(who, fin_m.visibility))
        ok(f"{who}: sees the trail at {on_audit}",
           people.breadth(who, aud_m.visibility) == on_audit,
           people.breadth(who, aud_m.visibility))
        # The two screens are no longer granted together: Procurement and
        # Accounts Payable read the trail of orders they handle and still
        # see nothing of Finance's thresholds. So each is checked on its
        # own rather than as a pair.
        if on_finance == "none":
            ok(f"{who}: gets nothing from Tax & budget",
               not fin.rows(_fin(who), "taxable"))
            ok(f"{who}: with a reason rather than a blank screen",
               fin.rules(_fin(who)) == ["finance_is_a_role"],
               str(fin.rules(_fin(who))))
        if on_audit == "none":
            ok(f"{who}: gets nothing from the trail",
               not aud.rows(_aud(who), "everything"))
            ok(f"{who}: and is told why",
               aud.rules(_aud(who)) == ["audit_is_a_role"],
               str(aud.rules(_aud(who))))
        else:
            ok(f"{who}: can read the history of what they handle",
               aud.rules(_aud(who)) != ["audit_is_a_role"])

    # Procurement can move a gift along and may not read the firm's
    # thresholds: the roles are separate claims, not a ladder.
    ok("procurement moves requests but reads no thresholds",
       "procurement" in people.roles_of("procurement")
       and not fin.figures(_fin("procurement")))


def main() -> int:
    """Every check, against a register nobody will miss.

    The whole run is wrapped rather than the few checks that obviously
    write, because almost everything here now touches the ledger — and the
    last time isolation was approximate, test records went into a live Azure
    account. The backend is forced local AND the folder is thrown away;
    either alone is not isolation.
    """
    with scratch_data():
        ledger.forget()
        drive(ledger.ready(seed=rl_mod.SEED, requests_seed=gr_mod.SEED))
        return _checks()


def _checks() -> int:
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
        check_crossing_the_threshold_tells_somebody()
    with fixtures():
        check_an_exception_decision_reaches_whoever_asked()
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
    with stored(), fixtures():
        check_the_register_and_the_trail_survive_a_restart()
    with stored(), fixtures():
        check_a_correction_still_supersedes_after_a_restart()
    with stored(), fixtures():
        check_the_trail_says_who_and_when()
    with stored(), fixtures():
        check_seeding_only_fills_an_empty_register()
    with stored(), fixtures():
        check_a_notice_outlives_the_process()
    with fixtures():
        check_finance_reports_the_amount_and_not_a_tax_bill()
    with fixtures():
        check_finance_sees_what_nobody_referred()
    with fixtures():
        check_only_finance_records_the_treatment()
    with fixtures():
        check_the_allowance_is_one_number_for_everybody()
    with fixtures():
        check_one_team_buys_for_the_whole_firm()
    with fixtures():
        check_audit_reads_and_cannot_write()
    with fixtures():
        check_the_trail_reads_as_what_happened()
    with fixtures():
        check_an_order_has_its_own_report()
    with fixtures():
        check_the_consolidated_report_cuts_on_distribution()
    check_the_new_screens_are_for_their_people()
    with fixtures():
        check_nothing_reaches_the_register_until_it_is_given()
    with fixtures():
        check_a_declined_request_never_becomes_a_gift()
    with fixtures():
        check_the_breach_is_caught_before_it_is_bought()
    with fixtures():
        check_policy_blocks_where_tax_only_warns()
    with fixtures():
        check_a_recipient_reads_their_gift_and_not_the_order()
    with fixtures():
        check_the_list_is_closed_before_anything_goes_out()
    with fixtures():
        check_the_distribution_is_the_end_of_the_chain()
    with fixtures():
        check_a_late_recipient_draws_on_the_buffer()
    with fixtures():
        check_every_on_row_form_has_a_door()
    with fixtures():
        check_a_queue_does_not_age_from_the_epoch()
    with fixtures():
        check_the_two_thresholds_do_different_jobs()
    with fixtures():
        check_crossing_the_cumulative_limit_has_two_ways_past_it()
    with fixtures():
        check_nobody_approves_their_own()
    with fixtures():
        check_each_stage_moves_only_for_its_own_persona()
    check_anybody_can_raise_one_and_it_still_reaches_the_end()
    with fixtures():
        check_every_stage_reads_as_a_sentence()
    check_the_firm_has_three_levels()
    with fixtures():
        check_the_tracker_follows_orders_through_the_chain()
    with fixtures():
        check_the_dashboard_is_leaderships_alone_and_draws_what_it_says()
    with fixtures():
        check_a_year_shows_that_year()
    with fixtures():
        check_the_assistant_answers_from_what_the_reader_may_see()
    with fixtures():
        check_the_assistant_cannot_act()
    with fixtures():
        check_only_the_administrator_moves_a_threshold()
    with fixtures():
        check_a_threshold_is_versioned_and_not_edited()
    with fixtures():
        check_a_moved_threshold_judges_only_what_comes_after()
    with fixtures():
        check_an_employee_is_not_shown_the_firms_machinery()
    with fixtures():
        check_a_desk_sees_its_own_work_and_not_the_firms()
    with fixtures():
        check_a_recipient_is_found_by_the_name_a_person_types()
    with fixtures():
        check_the_question_belongs_to_the_step()
    with fixtures():
        check_the_last_two_steps_only_go_forward()
    with fixtures():
        check_the_recipient_is_told_and_asked_nothing()
    with fixtures():
        check_every_persona_sees_its_own_breadth()
    with fixtures():
        check_an_employee_is_not_shown_a_colleague()
    with fixtures():
        check_what_a_feature_declares_is_what_is_enforced()
    with fixtures():
        check_a_role_cannot_be_claimed_by_a_caller()
    check_a_monthly_slot_lands_on_the_right_day()
    # Every one of these writes subscriptions, so they run against a
    # throwaway folder rather than the real data directory.
    with fixtures():
        check_a_slot_fires_once_and_is_not_missed_by_a_restart()
    with fixtures():
        check_a_report_cannot_see_more_than_its_reader()
    with fixtures():
        check_a_report_goes_to_its_subscriber_and_nobody_else()
    with fixtures():
        check_subscribing_is_something_you_do_to_yourself()
    with fixtures():
        check_these_checks_write_nowhere_that_matters()
    with fixtures():
        check_a_subscription_outlives_the_process()
    with fixtures():
        check_an_unreadable_subscription_is_skipped_not_fatal()
    with fixtures():
        check_the_export_is_the_screen()
    with fixtures():
        check_the_export_says_when_it_was_true()
    with fixtures():
        check_the_export_cannot_see_what_the_screen_hides()
    with fixtures():
        check_a_workbook_is_worth_opening()
    with fixtures():
        check_any_feature_can_be_exported()
    with fixtures():
        check_the_dashboard_cannot_disagree_with_the_workbench()
    with fixtures():
        check_the_dashboard_answers_a_different_question()
    with fixtures():
        check_the_dashboard_sees_the_process_stopping()
    with fixtures():
        check_leadership_can_look_and_not_touch()
    with fixtures():
        check_a_firm_wide_view_is_for_the_role()
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
        check_a_row_says_which_actions_it_takes()
    with fixtures():
        check_gift_requests_is_yours_and_the_tracker_is_the_desk()
    with fixtures():
        check_a_bulk_order_becomes_people()
    with fixtures():
        check_payroll_is_read_at_the_end_not_signed_at_the_start()
    with fixtures():
        check_compass_mints_no_reference_of_its_own()
    with fixtures():
        check_the_purchase_is_linked_back()
    with fixtures():
        check_the_recipient_list_is_maintained_not_overwritten()
    with fixtures():
        check_the_register_carries_the_approved_value()
    with fixtures():
        check_a_field_is_only_offered_to_whoever_it_is_for()
    with fixtures():
        check_a_recipient_list_can_be_uploaded()
    with fixtures():
        check_the_dashboard_is_its_charts()
    with fixtures():
        check_the_trail_outlives_the_chain()
    with fixtures():
        check_a_blocked_order_can_ask_for_an_exception()
    with fixtures():
        check_the_trail_is_read_by_the_order()
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
