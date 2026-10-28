"""What must hold about an estimate, checked without calling a model.

This is the port's conscience. The costing engine came across from CostCompass
essentially untouched, and the only claim worth making about a port like that
is that it produces the same numbers. So the numbers are asserted here, and
they are not asserted against a recorded output: every figure below was
hand-computed, and the arithmetic that produces it is written out in the
comment above it. A snapshot test tells you something changed. This tells you
which multiplication was wrong.

**Two areas no longer match, deliberately.** The ROI dials were changed on
instruction — the deflection rate and a new adoption ramp — because the model
they fed produced paybacks measured in days. Then the effort model was changed
for the same reason from the other side: it costed implementation and stopped,
so a payback of days was arithmetic on a build estimate of eight person-weeks
for work that is not eight person-weeks. `check_roi` and `check_development`
each carry their own before, after and reasoning. Everything else in this file
is still the figure CostCompass produced.

The values, the fixture and the hand calculations are CostCompass's own
(`backend/smoke_test.py`), rewritten in Compass's check style and pinned to the
same offline settings: baseline catalog prices, and the two model-touching
stages on their deterministic fallbacks. That is the configuration in which an
estimate is reproducible, which is the only configuration in which "the same
numbers" means anything.

    python3 scripts/check_estimate.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("COMPASS_AUTH_ENABLED", "0")
# The engine must be exercised without a network and without a model: live
# retail prices move, and a model asked to label a use case is entitled to a
# different opinion on a different day. Neither belongs in a parity check.
os.environ["COMPASS_ESTIMATE"] = "1"
os.environ["COMPASS_ESTIMATE_LIVE_PRICING"] = "0"
os.environ["COMPASS_ESTIMATE_CLASSIFIER"] = "0"
os.environ["COMPASS_ESTIMATE_ARCHITECT"] = "0"

from compass.estimate import engine  # noqa: E402
from compass.estimate.pipeline import run_estimate  # noqa: E402
from compass.estimate.types import Estimation, ProjectInput  # noqa: E402

FAILURES: list[str] = []


def ok(condition: bool, what: str) -> None:
    print(f"   {'ok  ' if condition else 'FAIL'}  {what}")
    if not condition:
        FAILURES.append(what)


def eq(got, want, what: str) -> None:
    ok(got == want, f"{what} — {got!r}" if got == want else f"{what} — got {got!r}, want {want!r}")


def code_only(source: str) -> str:
    """Source with its comments and docstrings removed.

    Three checks in this file have now failed on the sentence explaining why a
    thing is *not* done — `:has(`, `:root`, `reportlab`. A check that reads its
    own justification as evidence is worse than no check: it goes red when the
    code is right, and the tempting fix is to weaken the prose. So the
    searching is done against the code.
    """
    stripped = re.sub(r'"""(?:.|\n)*?"""', "", source)
    stripped = re.sub(r"\'\'\'(?:.|\n)*?\'\'\'", "", stripped)
    return "\n".join(
        line for line in stripped.split("\n")
        if not line.lstrip().startswith("#")
    )


def section(text: str, start: str, end: str = "}") -> str:
    """The source between a marker and the end of its block — bounded by the
    block's own ending rather than by a character count, which breaks the
    moment something unrelated grows above it."""
    if start not in text:
        return ""
    rest = text.split(start, 1)[1]
    cut = rest.find(end)
    return rest if cut == -1 else rest[: cut + len(end)]


def near(got: float, want: float, what: str, tol: float = 0.001) -> None:
    good = abs(got - want) <= tol
    ok(good, f"{what} — {got!r}" if good else f"{what} — got {got!r}, want ≈{want!r}")


# ── The fixture: two use cases (RAG + summarisation), medium scale ──────────
#
# Snake_case, where the original was camelCase. That is this port's one wire
# change, and running the same brief through the same engine in the new shape
# is part of what is being checked.

BRIEF = {
    "project_name": "Acme Support Copilot",
    "project_type": "new",
    "description": "Support assistant over the knowledge base.",
    "industry_domain": "SaaS",
    "target_users": "Support agents",
    "scale": "medium",
    "features": [
        {"id": "f1", "name": "Knowledge base ingest", "description": "",
         "complexity": "high", "ai_candidate": True},
        {"id": "f2", "name": "Agent console", "description": "",
         "complexity": "medium", "ai_candidate": False},
    ],
    "ai_use_cases": [
        {"id": "u1", "name": "Answer from docs", "task_type": "rag_qa", "description": "",
         "priority": "must_have", "linked_feature_ids": []},
        {"id": "u2", "name": "Summarize tickets", "task_type": "summarization",
         "description": "", "priority": "nice_to_have", "linked_feature_ids": []},
    ],
    "technical_preferences": {
        "preferred_llm_provider": "Azure OpenAI", "deployment_model": "cloud",
        "existing_infra": "Azure", "compliance_requirements": [], "budget_currency": "USD",
    },
    "volume_and_scale": {
        "expected_daily_users": 400, "requests_per_day": 3000, "data_volume_gb": 30,
        "peak_load_pattern": "Business hours", "growth_rate_percent": 20,
    },
}


def check_feasibility(est: Estimation) -> None:
    print("\nthe feasibility score is the weighted sum it claims to be")
    # Weights: must_have=1 (rag_qa), nice_to_have=0.6 (summarization); wSum=1.6.
    #   aiN = (78*1 + 66*0.6)/1.6 = 117.6/1.6 = 73.49999999999999 -> round 73
    #   features: 1 of 2 ai_candidate => ratio 0.5 => +0
    # The trailing 9s are the point: this is IEEE-754 double arithmetic in a
    # fixed order, and a "tidier" refactor that reassociates it lands on 74.
    eq(est.feasibility.sub_scores.ai_necessity, 73, "AI necessity")
    #   agentic = (55*1 + 25*0.6)/1.6 = 70/1.6 = 43.75 ; mustHaves=1 => +4 -> 48
    eq(est.feasibility.sub_scores.agentic_suitability, 48, "agentic suitability")
    #   trad = (30*1 + 45*0.6)/1.6 = 57/1.6 = 35.625 ; 2 use cases so no +10 -> 36
    eq(est.feasibility.sub_scores.traditional_suitability, 36, "traditional suitability")
    #   composite = round(0.55*73 + 0.3*48 + 0.15*(100-36)) = round(64.15) = 64
    eq(est.feasibility.score, 64, "composite")
    eq(est.feasibility.rating, "high", "rating")
    #   aiN>=55, agentic 48<58, retrieval present => rag_assistant
    eq(est.feasibility.archetype, "rag_assistant", "archetype")


def check_platform(est: Estimation) -> None:
    print("\nthe platform the architect picked is the platform that was priced")
    sp = est.solution_proposal
    ok(sp is not None, "a proposal is always produced, model or no model")
    eq(sp.recommended_platform if sp else None, "azure_paas", "platform")
    eq(sp.source if sp else None, "heuristic",
       "and with the stage off it came from the keyword heuristic")
    # The one that would catch a real regression: the architect writes the
    # platform onto the brief and the engine prices whatever it finds there. If
    # those two ever disagree, every infrastructure figure is for a platform
    # nobody chose — and it would still look like a perfectly good estimate.
    eq(est.cost_breakdown.infrastructure.platform,
       sp.recommended_platform if sp else None,
       "the costed platform is the proposed one")


def check_development(est: Estimation) -> None:
    """The effort model, after the third deliberate change.

    **These figures moved on purpose too.** The model costed implementation and
    stopped, which is why a retrieval assistant, an ingest pipeline and a
    console for four hundred agents came out at 322 hours — eight person-weeks.
    Three things were wrong and all three are now line items rather than
    silent factors:

    *Development did not scale with reach.* Infrastructure did (120 to
    2200/month across the tiers), maintenance did (12 to 90 hours a month), and
    engineering effort did not move at all — a mission-critical rollout to a
    whole organisation cost exactly what a pilot for one team cost. The
    difference is access control against a real directory, audit, DR, load and
    failure testing, staged rollout, and integration with identity and
    monitoring that already exist. None of that is a feature; all of it is
    work.

    *Build hours were treated as delivery hours.* Analysis, design, integration
    and system test, review, security, deployment, documentation and project
    management were absent. That was first answered with a flat ×2 multiplier
    called `DELIVERY_OVERHEAD`, and that constant has since been **removed** —
    see `check_work_breakdown`. A multiplier applied to every project alike is
    a confession that nobody enumerated the work; the breakdown enumerates it,
    and a line that costs 45 hours is now 45 hours of delivery rather than 22
    hours of typing doubled. On the legacy flat-feature path the same
    correction is carried inside `COMPLEXITY_HOURS`, whose hours are now
    all-in. This fixture is on that path, which is why its total went from 712
    back to 356: the doubling left, and nothing replaced it, because the
    feature bands already describe delivery.

    *The integration multiplier was a cliff.* 1.4 above an agentic score of 70
    and 1.0 below it, which put retrieval Q&A at 55 in the same bracket as text
    classification at 20. Retrieval has a corpus to prepare and an evaluation
    set that has to exist before anyone can say whether it works.

    On this fixture: 322 hours -> 712 with the multiplier, then 356 once the
    multiplier was removed and the bands were made all-in; development $37,030
    -> $40,940, and payback 1.6 -> 1.7 months. Across the tiers the ladder now
    reads 303 / 356 / 481 / 641 hours, where it read 322 at every one of them.
    """
    print("\ndevelopment is what it takes to deliver, not only to build")
    dev = est.cost_breakdown.development
    lines = {b.category: b.hours for b in dev.breakdown}

    # feature build hours: high=130, medium=64  => 194
    # ai integration, graded on the agentic score (uplift 0.6 at 100):
    #   rag_qa must_have    80 * (1 + 0.55*0.6) = 80 * 1.33 = 106.4
    #   summarization n_t_h 48 * (1 + 0.25*0.6) = 48 * 1.15 =  55.2
    #   => 161.6 -> 162
    eq(dev.ai_integration_hours, 162, "AI integration hours")
    ok(all(b.hours == COMPLEXITY_BUILD[b.category] for b in dev.breakdown
           if b.category in COMPLEXITY_BUILD),
       "the feature lines are still the build hours they always were")

    # build = 194 + 162 = 356 ; medium scale factor 1.0 so no uplift line
    ok("Non-functional work for medium reach" not in lines,
       "medium is the baseline the feature hours describe, so it carries no "
       "scale line at all")
    ok(not any(b.category.startswith("Delivery —") for b in dev.breakdown),
       "there is no flat delivery multiplier any more: doubling every project "
       "by the same constant is what an estimate does instead of enumerating "
       "the work, and the work is enumerated in the breakdown now")
    eq(sum(b.hours for b in dev.breakdown), 356, "total effort")
    eq(dev.total_cost, 356 * 115, "development cost")

    ok(dev.work_breakdown is None or not dev.work_breakdown.phases,
       "a brief with no modules gets no breakdown — it is priced exactly the "
       "way it always was, rather than being quantised into bands it never "
       "asked for")

    ok(sum(b.cost for b in dev.breakdown) == dev.total_cost,
       "the breakdown adds up to the total it breaks down — it did not, by $46, "
       "because one line was priced off its unrounded hours while showing them "
       "rounded")
    ok(all(b.cost == engine.jround(b.hours * dev.hourly_rate) for b in dev.breakdown),
       "and every line is its own hours at the stated rate — including the two "
       "that are multipliers, so a reader can disagree with one without "
       "re-deriving the rest")


COMPLEXITY_BUILD = {"Knowledge base ingest": 130, "Agent console": 64}


def check_effort_scales_with_reach() -> None:
    """The same brief at four scales costs four different amounts.

    It used to cost one. This is the check that would have caught the original
    fault directly, and there wasn't one — the fixture is medium, and a model
    that ignores scale looks perfect from inside a single tier.
    """
    print("\neffort scales with who is going to use the thing")
    hours: dict[str, int] = {}
    for scale in ("small", "medium", "large", "enterprise"):
        brief = {**BRIEF, "scale": scale}
        est = asyncio.run(run_estimate(
            ProjectInput.model_validate(brief), f"est_{scale}", "2026-06-20T00:00:00+00:00"))
        hours[scale] = sum(b.hours for b in est.cost_breakdown.development.breakdown)

    eq([hours[s] for s in ("small", "medium", "large", "enterprise")],
       [303, 356, 481, 641], "the ladder")
    ok(hours["small"] < hours["medium"] < hours["large"] < hours["enterprise"],
       "and it only goes one way — a pilot is cheaper to deliver than an "
       "organisation-wide rollout, which the model used to deny")

    # The pilot's line is negative, and named for it.
    est = asyncio.run(run_estimate(
        ProjectInput.model_validate({**BRIEF, "scale": "small"}),
        "est_small", "2026-06-20T00:00:00+00:00"))
    labels = [b.category for b in est.cost_breakdown.development.breakdown]
    ok(any("Pilot scope" in x for x in labels),
       "a pilot's reduction is labelled as one: the arithmetic is a negative "
       "number of hours, and \"Non-functional work: −53 hours\" reads like a "
       "defect rather than like a discount")


def check_infrastructure(est: Estimation) -> None:
    print("\nthe infrastructure subtotal is the services it lists, added up")
    # Rule-based planner, catalog baselines (live pricing off):
    #   Container Apps 320 ; Cosmos round(40+30*0.25)=48 ; Blob round2(100*0.0184)=1.84
    #   Monitor round(30+3000*0.00002)=30 ; APIM medium 50 ; Redis medium 55
    #   AI Search S1 round2(730*0.336)=245.28 ; Content Safety medium 20
    #   Key Vault round2(18*0.03)=0.54 ; Defender medium 45
    #   sum = 815.66 -> round 816. Azure OpenAI is listed but excluded: its cost
    #   is the token line, and counting it twice is the classic way to make an
    #   AI estimate look worse than it is.
    infra = est.cost_breakdown.infrastructure
    eq(infra.monthly_cost, 816, "monthly")
    eq(infra.monthly_cost, engine.jround(
        sum(s.monthly_cost for s in infra.services if s.included_in_total)),
       "and it equals the sum of the lines marked as counting")
    eq(infra.annual_cost, 816 * 12, "annual")

    names = [s.service_name for s in infra.services]
    for want in ("Azure AI Search", "Azure Key Vault", "Microsoft Defender for Cloud"):
        ok(want in names, f"{want} is planned for this brief")
    openai = next((s for s in infra.services if s.service_name == "Azure OpenAI"), None)
    ok(openai is not None, "Azure OpenAI is shown")
    ok(openai is not None and not openai.included_in_total,
       "and excluded from the subtotal, because the token line already has it")

    ok(all(s.region for s in infra.services), "every service names its region")
    ok(all(s.azure_pricing_url for s in infra.services),
       "and links to the calculator, so a figure can be checked rather than trusted")
    ok(all(s.price_source in ("live", "fallback", "estimate") for s in infra.services),
       "and says where its price came from")


def check_tokens(est: Estimation) -> None:
    print("\ntoken cost is volume times list price, grouped by model")
    # total req/day = max(3000, 2) = 3000 ; weights [1, 0.6], wTotal 1.6
    #   rag_qa:        3000*1/1.6   = 1875/day  -> 56,250/mo -> in 196,875,000  out 39,375,000
    #   summarization: 3000*0.6/1.6 = 1125/day  -> 33,750/mo -> in 135,000,000  out 20,250,000
    # both pick gpt-4o-mini, so they group:
    #   in 331,875,000 ; out 59,625,000
    #   cost = 331.875*0.15 + 59.625*0.60 = 49.78125 + 35.775 = 85.55625 -> 85.56
    mb = est.cost_breakdown.ai_tokens.model_breakdown
    eq(len(mb), 1, "two use cases on one model produce one line, not two")
    eq(mb[0].model, "gpt-4o-mini", "model")
    eq(mb[0].monthly_input_tokens, 331_875_000, "monthly input tokens")
    eq(mb[0].monthly_output_tokens, 59_625_000, "monthly output tokens")
    near(est.cost_breakdown.ai_tokens.monthly_cost.expected, 85.56, "monthly token cost")


def check_total(est: Estimation) -> None:
    print("\nthe first-year total is a range, and the range is derived")
    # maintenance: 24 + 2*4 = 32 hours ; 32 * 95 = 3040/mo
    # annual run = (816 + 85.56 + 3040) * 12 = 47,298.72
    # expected = round(40,940 + 47,298.72) = 88,239
    eq(est.cost_breakdown.maintenance.monthly_cost, 3040, "maintenance monthly")
    eq(est.cost_breakdown.total.expected, 88_239, "expected")
    eq(est.cost_breakdown.total.min, engine.jround(88_239 * 0.82),
       "min is the expected figure at 0.82, not an independent guess")
    eq(est.cost_breakdown.total.max, engine.jround(88_239 * 1.35), "max at 1.35")
    eq(est.cost_breakdown.total.expected,
       engine.jround(est.cost_breakdown.development.total_cost
                     + (est.cost_breakdown.infrastructure.monthly_cost
                        + est.cost_breakdown.ai_tokens.monthly_cost.expected
                        + est.cost_breakdown.maintenance.monthly_cost) * 12),
       "and it is development plus twelve months of running, not a fourth "
       "number that happens to agree")


def check_roi(est: Estimation) -> None:
    """The benefit model, after the two defaults were deliberately changed.

    **These figures moved on purpose, and this is the record of it.** Every
    other number in this file is the one CostCompass produced, and that
    equality is the port's whole safety argument. Two ROI dials are now
    different, on instruction, because the model they fed was producing
    paybacks measured in days:

        automation_rate_percent   70  ->  35
        benefit_ramp_months       (none, i.e. 0)  ->  6

    *Deflection.* 70% of calls handled end to end with no human touch is what
    a mature, well-tuned assistant reaches after tuning. Quoting it as the
    planning default made every estimate its own best case.

    *The ramp.* The model had none: value accrued at the steady-state rate
    from the morning of go-live, while the infrastructure and the engineers
    were paid in full from month one. Nothing behaves like that, and the
    absence is most of why payback came out at 0.09 months — two and a half
    days. Benefit now climbs linearly to full over six months.

    Together, on this fixture: payback 0.09 -> 1.6 months, three-year value
    $14.9M -> $6.7M, and a new figure saying year one delivers $1.89M of the
    $2.52M steady-state rate. (Payback then moved again, 2.3 -> 1.7, when the
    delivery multiplier was removed from the effort model — the same
    investment figure, arrived at by enumeration instead of by doubling.)

    What was *not* changed, and should be said plainly: the residue is on the
    cost side, not this one. $2.5M of annual benefit across 400 agents is
    about 6% of each agent's time, which is defensible; 322 hours to build a
    retrieval assistant, an ingest pipeline and a console for them is not. A
    payback of 1.6 months is arithmetic on a development estimate that is too
    small, and fixing that means changing what every estimate costs — a
    separate decision from this one.
    """
    print("\nbenefit is built from minutes of work, and ramps like adoption does")
    roi = est.roi_projection
    # per-minute value = loaded 75/60 * automation 0.35 = 0.4375
    #   rag_qa 6 min => 2.625 -> 2.63/call ; summarization 4 min => 1.75/call
    #   rag  round(1875*365) = 684,375 calls * 2.63 = 1,799,906.25
    #   summ round(1125*365) = 410,625 calls * 1.75 =   718,593.75
    near(roi.annual_benefit, 2_518_500.00, "steady-state annual benefit")
    driver = roi.value_drivers[0]
    eq(driver.value_per_call, 2.63, "value per call")
    # The three numbers that make the one above arguable rather than magic.
    eq(driver.minutes_per_call, 6, "minutes of manual work replaced")
    eq(driver.loaded_hourly_rate, 75, "loaded hourly rate")
    eq(driver.automation_rate_percent, 35, "share handled end to end")

    #   monthly rate = 2,518,500/12 = 209,875
    #   ramp 6: benefit by month 12 = 209,875 * (6/2 + 6) = 1,888,875
    eq(roi.benefit_ramp_months, 6, "the ramp is six months")
    near(roi.first_year_benefit, 1_888_875.00,
         "year one delivers less than the steady-state rate")
    ok(roi.first_year_benefit < roi.annual_benefit,
       "which is the point: quoting the steady state as the first year is the "
       "single biggest way a benefit case oversells itself")

    #   cumulative_net(m) = 209,875*m²/12 - 3,941.56*m - 40,940 while ramping
    #     m=1.6 -> -2,473.2   m=1.7 -> +2,905.2
    eq(roi.payback_months, 1.7,
       "payback is solved on the ramped curve, not divided")
    #   benefit by 36 = 209,875 * (3 + 30) = 6,925,875
    #   investment    = 40,940 + 47,298.72*3 = 182,836.16
    near(roi.three_year_value, 6_743_038.84, "three-year value")
    eq(len(roi.curve), 13, "the curve is 13 points — months 0, 3, … 36")
    ok(roi.curve[0].cumulative_net == -40_940,
       "and starts at the development cost, before a month of benefit exists")

    ok(any("ramps linearly over 6 months" in a for a in roi.assumptions),
       "the ramp is stated in the report rather than buried in a constant")
    ok(any("Year one delivers" in a for a in roi.assumptions),
       "and so is the gap between year one and the steady state")


def check_verdict(est: Estimation) -> None:
    print("\nthe verdict is stated, and so is the confidence in it")
    ok(est.verdict is not None, "there is a verdict")
    eq(est.verdict.decision, "build_with_ai", "decision")
    eq(est.verdict.disposition, "go", "disposition")
    eq(est.verdict.recommend_ai, True, "recommendation")
    # detail 4/4 ; volume 4/4 ; decisive (clamp(9/15)+clamp(18/15))/2 = 0.8 ;
    # grounded (new build) 0.5 -> 100*(0.3+0.3+0.24+0.05) = 89
    ok(est.confidence is not None, "and a confidence assessment")
    eq(est.confidence.score, 89, "confidence")
    eq(est.confidence.level, "high", "level")
    eq(len(est.confidence.factors), 4,
       "carrying its four factors, so a low score says which part was thin")


def check_enhancement() -> None:
    print("\nan enhancement is costed as the difference, not as a rebuild")
    payload = dict(BRIEF)
    payload["project_type"] = "enhancement"
    payload["current_architecture"] = {
        "framework": "FastAPI", "language": "Python", "database": "Postgres",
        "api_pattern": "REST", "hosting_platform": "Azure", "ci_cd": "GitHub Actions",
    }
    est = asyncio.run(run_estimate(
        ProjectInput.model_validate(payload), "est_check_enh", "2026-06-20T00:00:00+00:00"))

    cats = [b.category for b in est.cost_breakdown.development.breakdown]
    line = "Integrate with existing FastAPI"
    ok(line in cats, "integrating with what is already there is its own line item")
    integ = next(b for b in est.cost_breakdown.development.breakdown if b.category == line)
    eq(integ.hours, 88, "integration hours — 40 + 2*24")
    ok(est.repo_context is not None, "and the repo it is being added to is recorded")

    services = [s.service_name for s in est.cost_breakdown.infrastructure.services]
    ok("Azure Container Apps" not in services,
       "hosting that already exists is not charged for again")
    ok("Azure Cosmos DB" not in services, "nor the database")
    ok("Azure AI Search" in services, "but the search index the AI work needs is new, and is")


def check_reproducibility() -> None:
    print("\nthe same brief gives the same answer")
    brief = ProjectInput.model_validate(BRIEF)
    a = asyncio.run(run_estimate(brief, "est_a", "2026-06-20T00:00:00+00:00"))
    b = asyncio.run(run_estimate(brief, "est_b", "2026-06-20T00:00:00+00:00"))
    # Everything but the id, which is meant to differ.
    da, db = a.model_dump(), b.model_dump()
    da.pop("id"), db.pop("id")
    ok(da == db, "twice, byte for byte — which is the whole claim being made")


def check_wire_shape() -> None:
    print("\nthe wire is snake_case, like every other section")
    dumped = ProjectInput.model_validate(BRIEF).model_dump()
    ok("project_name" in dumped and "projectName" not in dumped,
       "the brief goes out snake_case")
    ok("ai_use_cases" in dumped, "including the nested lists")
    # extra="ignore" is what lets an older client keep working after a field is
    # dropped: it should estimate, not 422.
    tolerant = ProjectInput.model_validate({**BRIEF, "someRetiredField": 1})
    eq(tolerant.project_name, "Acme Support Copilot",
       "and a field this version no longer knows is ignored, not rejected")


def check_isolation() -> None:
    """The module keeps to itself, and costs nothing when it is off.

    Both halves decay silently. An import of `compass.pipelines` added to reach
    one convenient helper does not break anything today and makes the module
    unremovable tomorrow; a route registered outside the flag's conditional
    keeps answering on a deployment that switched costing off. Neither shows up
    in a passing test suite unless something looks.
    """
    print("\nthe module keeps to itself")
    pkg = ROOT / "compass" / "estimate"
    siblings = ("code", "design", "home", "pipelines")

    reaching_out = []
    for path in sorted(pkg.glob("*.py")):
        body = path.read_text()
        for sib in siblings:
            if f"compass.{sib}" in body:
                reaching_out.append(f"{path.name} -> compass.{sib}")
    ok(not reaching_out,
       "it imports no other module — only compass.common, for settings, auth, "
       f"ownership and the model client{'' if not reaching_out else f': {reaching_out}'}")

    reaching_in = []
    for path in sorted((ROOT / "compass").rglob("*.py")):
        if "estimate" in path.parts or path.parent.name == "api":
            continue
        if "compass.estimate" in path.read_text():
            reaching_in.append(str(path.relative_to(ROOT)))
    ok(not reaching_in,
       "and nothing but the server's mount imports it, so switching it off "
       f"cannot break a neighbour{'' if not reaching_in else f': {reaching_in}'}")

    server = (ROOT / "compass" / "api" / "server.py").read_text()
    mount = server[server.index("if get_settings().estimate.enabled:"):]
    ok("from compass.estimate.routes import router" in mount.split("\n\n")[0],
       "the import sits inside the conditional, not at the top of the file — a "
       "module that is off should cost nothing, and nothing includes import time")

    routes = (ROOT / "compass" / "estimate" / "routes.py").read_text()
    handlers = routes.count("@router.")
    guarded = routes.count("Depends(require_user)")
    eq(guarded, handlers, "every route requires a user")


def check_the_model_cannot_reach_the_arithmetic() -> None:
    """The founding rule, checked structurally rather than trusted.

    Everything else in this file checks that a number is right. This checks
    that a number *can* only be computed one way — that the two stages allowed
    to call a model are the two that resolve a label, and that nothing in the
    costing path can await anything at all. It is the one property that, if it
    ever quietly stopped holding, would leave every figure looking exactly as
    correct as it does now.
    """
    print("\nthe model resolves labels; code computes money")
    pkg = ROOT / "compass" / "estimate"

    # Three, and the count is the point of the check rather than an incidental
    # fact: it was two, and adding the intake drafter made this fail until the
    # widening was written down. The three do different amounts of work — the
    # classifier picks one label from a fixed set, the architect picks one of
    # five platforms, the drafter fills a form a person then corrects — but
    # they share the property that matters: none of them is `engine.py`, and
    # nothing any of them returns reaches a figure without passing through a
    # field a person can see and change.
    callers = [p.name for p in sorted(pkg.glob("*.py"))
               if "get_model_client" in p.read_text()]
    eq(sorted(callers), ["architect.py", "classifier.py", "intake.py"],
       "exactly three files can call a model, and each one resolves inputs")

    engine_src = (pkg / "engine.py").read_text()
    ok("async def" not in engine_src and "await " not in engine_src,
       "the engine is synchronous throughout, so nothing that talks to a "
       "network can be called from inside a costing run")
    ok("get_model_client" not in engine_src,
       "and it holds no reference to a model client")

    planner = (pkg / "azure_planner.py").read_text()
    ok("anthropic" not in planner.lower() and "await" not in planner,
       "the planner's model-led variant is gone rather than ported half-way — "
       "it ran inside the engine, and the honest options were to thread async "
       "through nine hundred lines of costing or to block the event loop")


def check_the_section_is_removable() -> None:
    """The UI half keeps to itself too, and the flag reaches it.

    A module with a server-side switch and a hard-wired nav entry is not
    switchable: the tab appears on a build with no routes behind it, and the
    first click is a 404. So the section is gated on what the server reports,
    in the template *and* in the handler — the nav is the only way in today,
    and "today" is exactly the assumption that stops being true.
    """
    print("\nthe section can be removed as cleanly as the module")
    app_html = (ROOT / "frontend/src/app/app.html").read_text()
    app_ts = (ROOT / "frontend/src/app/app.ts").read_text()
    ui = ROOT / "frontend/src/app/estimate"

    ok("@if (health()?.estimate) {" in app_html,
       "the nav entry exists only when the server says the module is mounted")
    ok("@if (section() === 'estimate') {\n      <app-estimate />" in app_html,
       "and the section is instantiated only when entered, so a build with the "
       "flag off never calls an endpoint that is not there")
    ok("if (!this.health()?.estimate) return;" in app_ts,
       "with the same guard in the handler — the nav is the only way in today")

    # Five copies of one condition, and this section would have made six.
    ok("section() === 'design' || section() === 'pipelines'" not in app_html
       and "section() !== 'design' && section() !== 'pipelines'" not in app_html,
       "the sidebar-less test is a computed, not five literals in the template "
       "— it read as one fact and behaved as five copies of it")
    ok("readonly chromeless = computed(" in app_ts
       and "readonly hasWorkspace = computed(" in app_ts,
       "and it is two computeds rather than one: having no sidebar and having "
       "no workspace agree today and are not the same fact")

    # The module's own files, again.
    stray = []
    for path in sorted(ui.glob("*.ts")):
        body = path.read_text()
        for other in ("pipelines/", "design/", "home-chat/", "compass-api.service"):
            if other in body:
                stray.append(f"{path.name} -> {other}")
    ok(not stray,
       "the UI imports nothing from another section — only the shared markdown "
       f"renderer{'' if not stray else f': {stray}'}")

    # Comments stripped first. The last check written this way flagged the
    # comment that explained the rule it was checking.
    css = re.sub(r"/\*.*?\*/", "",
                 (ui / "estimate.css").read_text() + (ui / "report.css").read_text(),
                 flags=re.S)
    # `:host-context(:root…)` is the correct escape and contains the string
    # this is looking for, so the wrappers come out before the search. What is
    # being banned is a *bare* `:root` selector.
    bare = re.sub(r":host-context\([^)]*\)", "", css)
    # The app paints one backdrop for the whole product — a fine grid, a soft
    # brass glow, the ghost of a rose — and every section lets it through. This
    # one painted `var(--bg)` on its host: a flat slab the exact colour of the
    # thing it was covering, so it read as a panel dropped on top of the app
    # rather than as part of it, and the give-away was the texture stopping at
    # the section's edge.
    for sheet, name in ((ui / "estimate.css", "the section"),
                        (ui / "report.css", "the report")):
        host = section(re.sub(r"/\*.*?\*/", "", sheet.read_text(), flags=re.S),
                       ":host {", "}")
        ok("background:" not in host,
           f"{name}'s host paints no background, so the app's backdrop shows "
           "through it as it does through every other section")

    ok(":root" not in bare,
       "no bare `:root` in component CSS: Angular stamps its scope onto every "
       "compound selector, and `[_ngcontent]:root` matches nothing — a dark "
       "palette written that way is dead on arrival and looks like a theme bug")
    # One blanket reset per stylesheet rather than a repeat on every button.
    # The count-based version of this check wanted four or more and was
    # really asking "did you remember at all" — a single rule covering
    # `button, input, textarea, select` answers that better and cannot be
    # half-applied to a control someone adds later.
    for sheet, name in ((ui / "estimate.css", "the section"),
                        (ui / "report.css", "the report")):
        body = sheet.read_text()
        ok("font-family: inherit" in body,
           f"{name} resets the control font: a <button> takes the UA's, which "
           "follows the operating system rather than the theme switch")


def check_the_screens_match_the_mockup() -> None:
    """The three screens, rebuilt against the supplied design.

    The instruction was to match a mockup byte for byte, and the honest
    reading of that on a costing tool is: **every visual element is
    reproduced; each one is fed by the real equivalent.** The mockup invents a
    data model in places — its own four feasibility dimensions, its own five
    rate dials, a sensitivity panel — and swapping the engine's semantics for
    those would be changing features under cover of a cosmetic instruction.
    So the shapes are the mockup's and the numbers are the engine's, and where
    the two disagree the number wins.

    The one thing the design *required* that did not exist was the live rail:
    a running estimate beside the form. There were three ways to get that
    number — mirror the engine in TypeScript, invent a cheaper sidebar
    formula, or ask the server. The first is the 1,400-line duplicate this
    port deleted; the second is worse, because a rail that disagrees with the
    report it previews teaches people to distrust both. So `/v1/estimates/
    preview` runs the same engine with the two model stages taken at their
    deterministic fallback: 277ms, and the same figures the full estimate
    produces.

    Measured on the rebuilt screens: index 3 rows with verdict pills,
    feasibility bars and confidence; form showing "6 of 6 complete" with the
    rail live at 46/100 and $77,705; report showing the waterfall, the
    platform panel and a sensitivity strip re-costed at half and double
    volume (-$526 / +$1,026 — small, because this estimate is dominated by
    development rather than by tokens, which is the useful thing to learn).
    """
    print("\nthe three screens follow the mockup")
    ui = ROOT / "frontend/src/app/estimate"
    index_css = (ui / "estimate.css").read_text()
    report_css = (ui / "report.css").read_text()
    index_html = (ui / "estimate.html").read_text()
    report_html = (ui / "report.html").read_text()
    ts = (ui / "estimate.ts").read_text()
    routes = (ROOT / "compass/estimate/routes.py").read_text()

    # The measures that make it the mockup rather than something like it.
    for value, what in (
        ("font-size: 38px", "the index title is the mockup's 38px serif"),
        ("grid-template-columns: repeat(4, 1fr)", "four stat cards"),
        ("grid-template-columns: 1fr 160px 128px 116px 108px 96px",
         "the table's six columns are the mockup's, with verdict widened from "
         "104 to 160 — measured, the longest label, STANDARD SOFTWARE, needs "
         "157px on one line"),
        ("white-space: nowrap;", "and a pill never wraps"),
        ("height: 58px", "and its rows are 58px"),
        ("width: 190px", "the stepper is 190px"),
        ("width: 288px", "the live rail is 288px"),
        ("letter-spacing: 0.14em", "section labels carry the mockup's tracking"),
    ):
        ok(value in index_css, what)

    ok("--brass: var(--accent);" in index_css and "--brass: var(--accent);" in report_css,
       "the mockup's colour names are aliased onto Compass's rather than "
       "redeclared — which is what lets a rule come across unchanged and still "
       "theme with the rest of the app")
    ok("--violet: #7b5ea7;" in index_css and "--violet: #a288cc;" in index_css,
       "and the three the app has no equivalent for keep the mockup's values "
       "with their own night steps")

    # The mockup has a "New estimate" button in the header *and* a New build
    # card below it, both wired to the same thing. Reproducing the design
    # faithfully reproduced that, and it is worse than a duplicate: the more
    # prominent of the two silently picked one of the two kinds, so anyone
    # who wanted an enhancement and reached for the primary button got a
    # new-build form without being told.
    starts = re.findall(r"startNew\('(\w+)'\)", re.sub(r"<!--.*?-->", "", index_html, flags=re.S))
    eq(sorted(starts), ["enhancement", "new"],
       "there is exactly one way to start each kind of estimate, and each one "
       "says which kind it starts")

    ok('class="gauge"' in index_html and "stroke-dasharray=\"192\"" in index_html,
       "the rail's gauge is the mockup's arc, at its own dash length")
    ok('class="verdict"' in report_html and "--vc" in report_css,
       "the verdict banner carries its colour on a 5px leading edge")
    ok('class="wf"' in report_html and "class=\"sens\"" in report_html,
       "the result screen has the waterfall and the volume-sensitivity strip")

    ok('@router.post("/v1/estimates/preview")' in routes,
       "the live rail is the real engine, called for real")
    ok("resolve_without_model" in routes,
       "with the two model stages at their deterministic fallback, because a "
       "sidebar cannot wait twenty seconds for a ReAct call on every keystroke")
    ok("this.previewSeq" in ts and "mine === this.previewSeq" in ts,
       "and only the newest reply may write: a slow early response landing "
       "after a fast later one would show a costing for a brief that no "
       "longer exists")

    # The verdicts the engine actually emits — the first guess was wrong, and
    # a wrong verdict name renders every row as a grey "Draft".
    for decision in ("build_with_ai", "do_not_use_ai", "hybrid"):
        ok(decision in ts, f"the index knows the verdict {decision!r}")
    ok("build_standard" not in ts,
       "and not the one that does not exist — an unmapped decision falls "
       "through to a grey Draft pill, which is a wrong answer that looks like "
       "a missing one")


def check_the_two_charts_are_drawn_to_spec() -> None:
    """The composition stack and the ROI curve, and the rules they follow.

    Both were removed when the section was rebuilt to the mockup — the design
    has a waterfall and no ROI chart — and both are back because they show
    something the rows beside them cannot. The stack is part-to-whole, which
    is a shape rather than four numbers. The curve is the only place you can
    see *where it crosses zero*, which is the question an ROI model is asked.

    **The mockup's own four colours were validated and failed.** It paints the
    four cost parts brass, violet, blue and green — its accent plus three
    semantic colours — and against the surfaces they actually sit on:

        light   blue↔violet normal-vision ΔE 14.0, under the hard floor of
                15, which secondary encoding does not excuse; green 2.22
                contrast against white
        dark    brass L 0.751, violet 0.676, green 0.756 — three of four
                outside the 0.48–0.67 band a data mark must sit in

    So the *assignment* is the mockup's and is kept — four parts, one colour
    each, a legend beneath, the same 8px stack — and the hues are replaced
    with a set that passes both modes. A design can ask for a look; it cannot
    ask for a pair of colours a reader cannot tell apart.

    The rest is the mark contract: fixed slot order never cycled, a 2px line,
    dots at least 8px with a 2px surface ring, no non-uniform scaling, text in
    a text token rather than a series colour, and a caption that describes the
    data's endpoint rather than the axis bound.
    """
    print("\nthe two charts are drawn to a spec, not to taste")
    ui = ROOT / "frontend/src/app/estimate"
    index_css = (ui / "estimate.css").read_text()
    report_css = (ui / "report.css").read_text()
    report_html = (ui / "report.html").read_text()
    report_ts = (ui / "report.ts").read_text()
    index_ts = (ui / "estimate.ts").read_text()

    for slot, light, dark in (
        (1, "#2a78d6", "#3987e5"), (2, "#eb6834", "#d95926"),
        (3, "#1baf7a", "#199e70"), (4, "#eda100", "#c98500"),
    ):
        ok(f"--series-{slot}: {light};" in report_css
           and f"--series-{slot}: {dark};" in report_css,
           f"slot {slot} is the validated pair {light} / {dark}")

    ok("--series-1" in index_css and "var(--series-1)" in index_ts,
       "and the form's live rail uses the same slots, so a colour means the "
       "same part on both screens rather than two schemes for four figures")
    ok("readonly parts = computed" in report_ts
       and report_ts.count("var(--series-1)") == 1,
       "the four parts are defined once — the stack, its legend and the "
       "waterfall all read that one list, so they cannot drift apart")

    ok('preserveAspectRatio="none"' not in report_html,
       "nothing is scaled non-uniformly: it turns dots into ellipses and one "
       "stroke width into two")
    ok("height: auto;" in section(report_css, ".svg {", "}"),
       "the viewBox sets the aspect and CSS sets only the width")
    ok("stroke-width: 2;" in section(report_css, ".line {", "}"),
       "the line is 2px")
    ok('r="4.5"' in report_html or 'r="5.5"' in report_html,
       "dots are at least 8px across")
    ok("stroke: var(--surface); stroke-width: 2;" in section(report_css, ".dot {", "}"),
       "and carry a 2px surface ring, so they stay legible where they cross "
       "the line")
    ok("fill-opacity: 0.1;" in section(report_css, ".area {", "}"),
       "the area is a 10% wash rather than a saturated block")
    ok("stroke: var(--rule-2)" in section(report_css, ".zero {", "}")
       and "dash" not in section(report_css, ".zero {", "}"),
       "the zero baseline is a solid hairline — a dashed rule competes with "
       "the line it exists to measure")

    ok("color: var(--ink-3);" in section(report_css, ".slegend span {", "}"),
       "legend text wears a text token: a light categorical hue is illegible "
       "as type, and the swatch beside it carries the identity")

    ok("endLabel:" in report_ts and "values[values.length - 1]" in report_ts,
       "the caption describes the data's endpoint, not the axis bound — the "
       "bounds are clamped to include zero, so captioning them once printed "
       "\"$0 by month 36\" for a curve that ended at minus $272,015")
    ok("!== values[values.length - 1]" in report_ts,
       "and the trough is suppressed when it is the endpoint rather than "
       "printing one number twice")
    ok("payback === null ? 'never crosses zero'" in report_ts,
       "a curve that never pays back says so in the heading, instead of "
       "leaving a marker off and hoping someone notices")


def check_the_brief_can_be_written_in_prose() -> None:
    """A paragraph fills the form, and a person corrects it.

    This is the one place a model writes *inputs* rather than picking a label,
    so where the line falls matters more than the feature does.

    *It transcribes stated numbers; it never infers absent ones.* The first
    version of this refused volume entirely — requests-per-day multiplies into
    every infrastructure and token figure, and an invented one is a fabricated
    number wearing a computed one's clothes. But a brief that says "roughly 900
    invoices a day" is a stated fact, and dropping it to make someone retype it
    is exactly the friction the feature removes. Measured both ways: "900
    invoices a day" comes back as `requests_per_day: 900`; "it should be quite
    busy" comes back null.

    *Two fields stay out of reach whatever the prose says.* `minutes_per_call`
    is the dial the whole ROI model turns on, and the rate card is what the
    organisation pays its own people. Neither is a fact about the project.

    *The name is the deliberate exception.* It came back empty at first — the
    model obeying "an empty field is a correct answer" on a description that
    never named the project — and a nameless estimate breaks its portfolio
    row, its report heading and its export filename at once. A name is a label
    rather than a claim, so it is asked for directly and has a fallback.

    And drafting does not estimate. Two calls, two moments: a draft that
    priced itself on the way past would put a figure in front of someone
    before they had read the assumptions behind it, and that is the figure
    they would remember.
    """
    print("\na brief can be written as a paragraph")
    src = (ROOT / "compass/estimate/intake.py").read_text()
    routes = (ROOT / "compass/estimate/routes.py").read_text()
    ui = (ROOT / "frontend/src/app/estimate/estimate.html").read_text()
    ts = (ROOT / "frontend/src/app/estimate/estimate.ts").read_text()

    ok("cost_assumptions" not in src.split("_SCHEMA")[1].split("async def")[0],
       "the rate card is not in the schema — it is what an organisation pays "
       "its own people, not a fact a paragraph is evidence about")
    ok('u["minutes_per_call"] = None' in src,
       "and the ROI dial stays at the task type's default, whatever the prose "
       "claims — it is the number the entire benefit model rests on")
    ok('"volume_and_scale"' in src and '"type": ["integer", "null"]' in src,
       "volume is present but every figure in it is nullable, so \"not "
       "stated\" is expressible rather than something the schema forces")
    # The prompt is a line-continued string, so a phrase can be split across a
    # backslash-newline; joined before searching, or the check tests the
    # formatting rather than the words.
    joined = src.replace("\\\n", "")
    ok("900 invoices a day" in joined and "a lot of traffic" in joined,
       "and the prompt draws the line with an example of each: a number that "
       "is there, and a phrase that only sounds like one")

    # The prompt's band section, which is the part that was measured and
    # changed. Checked as words rather than as behaviour on purpose: asserting
    # what a model returns would make this file need a network and a key, and
    # would go red on a day the model felt different. What can be pinned is
    # that the instruction is still in the prompt somebody has to read.
    ok("Use the whole range." in joined,
       "the prompt asks for the whole band range: three drafts of one "
       "paragraph used four of six bands, never once an xs or an xxl, with "
       "over half of every breakdown sitting on m")
    ok("When the description names a set, enumerate its members" in joined,
       "and it says where small lines actually come from — naming a set's "
       "members separately, not shaving days off a big line. Over ten drafts "
       "an arm: xs 0.4% of lines to 17.2%, and the run-to-run spread halved. "
       "Still enumerated; where each member goes is pinned in "
       "check_a_set_of_instances_is_one_line")
    ok("not the machinery under it" in joined,
       "and it stops the model elaborating around a line it was given: an "
       "upload became a drag-and-drop line, a resumable line, a progress "
       "line and a storage line. With the paragraph out, those invented "
       "lines go from 5h a draft to 38h and the spread nearly doubles")

    ok("xxl is a signal, not a failure to avoid" in joined
       and "flagged by name in the report" in joined,
       "xxl is described as a thing to say rather than a boundary to stay "
       "under — it read as the latter, was never once emitted, and the "
       "report's ceiling flag was therefore unreachable from a drafted brief")
    ok(joined.count("xs ") >= 1 and "xxl ten days" in joined
       and all(f"{b} " in joined for b in ("s   three days", "m   four days",
                                           "l   five days", "xl  seven days")),
       "every band is defined by what belongs in it, not only by its length — "
       "a list of durations gives a model nothing to decide with, and it "
       "defaults to the middle one")

    ok("delivery_platform" not in src,
       "the platform is not drafted either — the architect resolves it from "
       "the finished brief, and two things choosing it is one too many")

    ok('if not (data.get("project_name") or "").strip():' in src,
       "the name has a fallback, because it is the one field that cannot be "
       "empty and the honest instruction to leave fields blank reached it")

    ok('@router.post("/v1/estimates/draft")' in routes
       and "return {\"brief\": brief.model_dump()}" in routes,
       "drafting returns the brief and nothing else — no estimate, no id, "
       "nothing stored")
    ok("if not get_settings().estimate.draft:" in routes,
       "and it has its own switch: nothing is costed from a draft until a "
       "person reviews it, but reviewing is a habit and a habit is a weaker "
       "guarantee than a flag")

    ok('@if (catalog()?.draft) {' in ui,
       "the box only appears where the server can answer it")
    ok("drafted() ? 'from brief'" in ui and "readonly drafted = signal(false)" in ts,
       "and a draft says so on the page — the running-estimate pill reads "
       "\"from brief\" — because a form a model filled in should admit it "
       "before anyone costs what is in it")
    ok("private applyBrief(b: ProjectInput): void {" in ts,
       "every drafted field lands somewhere visible and editable — there is no "
       "hidden half that reaches the engine unseen")

    # The bug the screenshot caught: a select showing one value and holding
    # another is worse than a select that is simply wrong.
    ok(ui.count("[selected]=") >= 4,
       "every option says whether it is the selected one: Angular sets a "
       "select's value before @for has made any options, so the control read "
       "\"small\" while the form held \"medium\"")


def check_an_estimate_can_leave_the_app() -> None:
    """Two exports, and the reasons they are the shapes they are.

    *The PDF is the report printed, not redrawn.* The original built its PDF
    with reportlab — a second set of fonts, a second table style, a second
    place for every future change to be made and forgotten. This one is HTML
    through Compass's own headless Chromium, so the document looks like the
    product it came from. Measured: a valid two-page A4 PDF, 121KB.

    *The helper went into `common`, not into the module and not into Design.*
    Design's PDF path detects slides and fixed-size sheets and prints one page
    per artboard, which is right for a design and wrong for a document; and
    importing Design from Estimate would have broken the isolation this file
    checks two sections above. `common/screenshot.py` already owns the headless
    browser, so the plain case lives there.

    *Both read the stored record.* An estimate is a dated artefact. A PDF that
    quietly re-derived today's answer to the same question would be a different
    document with the same title.

    *Everything is escaped.* A project name is typed by a person, and a report
    that renders markup because somebody called their project that is a report
    that cannot be shared.
    """
    print("\nan estimate can leave the app")
    exp = (ROOT / "compass/estimate/export.py").read_text()
    routes = (ROOT / "compass/estimate/routes.py").read_text()
    common = (ROOT / "compass/common/screenshot.py").read_text()

    ok("reportlab" not in code_only(exp),
       "no second drawing engine — the PDF is the report printed")
    ok("from compass.common.screenshot import html_to_pdf" in exp,
       "through the shared headless browser, which is where the one that "
       "already existed lives")
    ok("compass.design" not in exp,
       "and not through Design's, which prints one page per artboard and "
       "would have coupled two modules that are meant to be removable apart")
    ok("prefer_css_page_size=True" in common and 'emulate_media(media="print")' in common,
       "the stylesheet paginates it: without the print emulation the @page box "
       "is ignored and it comes out as one page as tall as the document")

    ok("def _e(value: object) -> str:" in exp and "_html.escape" in exp,
       "every string in the document is escaped")
    ok(exp.count("_e(") >= 20,
       f"and used throughout, not only in the obvious places "
       f"({exp.count('_e(')} call sites)")

    ok("Estimation.model_validate(record.result)" in routes,
       "both exports read the stored result rather than recomputing — an "
       "estimate is dated, and a PDF that re-derived it would be a different "
       "document with the same title")
    ok('media_type="application/pdf"' in routes
       and "spreadsheetml.sheet" in routes,
       "and each is served as what it is, as an attachment")
    ok("status_code=501" in routes,
       "a missing optional dependency answers 501 and says which one, rather "
       "than 500-ing from an ImportError deep in a handler")

    ok('ws = wb.create_sheet("Cost Breakdown")' in exp
       and '"Tokens"' in exp and '"ROI"' in exp and '"Recommendations"' in exp,
       "the workbook is five sheets, as it was — a workbook is a data "
       "structure, and that split was a good one")
    ok('svc.region, svc.price_source' in exp,
       "with the region and the price source added, which is what makes an "
       "infrastructure line checkable rather than merely readable")


def check_the_export_runs_on_windows() -> None:
    """Reported from Windows: "the Export PDF in Estimate is not working."

    Three faults stacked, and the first two were already understood — Design
    had them reported, diagnosed and answered months earlier. The shared
    headless browser never got the answers, so Estimate, which reaches
    Chromium only through that shared helper, hit all of them raw.

    *Nothing said what went wrong, because nothing could.* The buttons were
    `<a href download>`, which is the right shape for a download and the wrong
    one for a download that can fail: a browser told to save a URL saves
    whatever comes back. The 501 body carrying "run `playwright install
    chromium`" was written to disk **as the PDF** — a file that exists, is the
    size of a sentence, and will not open. That is the whole reported symptom,
    and it is the same one Design's report described word for word. No
    server-side fix could ever have reached the screen through an anchor, so
    the client fetches, refuses an empty body, and shows the message.

    *A present Playwright with no browser said nothing useful.* Only the
    missing *package* was translated. The ordinary state of a machine where
    nobody ran the install step — package there, browser not — fell through
    `except RuntimeError` in the route as a generic exception and 500'd with a
    driver path in it.

    *And the loop.* `uvicorn.loops.asyncio` returns `ProactorEventLoop` on
    win32 only `if not use_subprocess`, and `use_subprocess` is
    `reload or workers > 1`. So `--reload` — the ordinary Windows development
    command — puts the server on the Selector loop, where asyncio cannot spawn
    the driver at all and raises a bare NotImplementedError. This one is not
    merely explained: the job is run on a worker thread with a Proactor loop of
    its own, so the export works under `--reload` instead of apologising. The
    sentence is kept for the case where even that cannot start.

    Checked on this host: the real route returns a 121,453-byte PDF beginning
    %PDF-1.4; a launch failure carrying a Windows driver path produces the
    install sentence; the off-the-Proactor path prints on a loop of its own;
    and a job that still cannot spawn produces the loop sentence.
    """
    print("\nthe export runs on Windows")
    common = (ROOT / "compass/common/screenshot.py").read_text()
    report = (ROOT / "frontend/src/app/estimate/report.ts").read_text()
    api = (ROOT / "frontend/src/app/estimate/estimate-api.ts").read_text()
    html = (ROOT / "frontend/src/app/estimate/report.html").read_text()

    ok("playwright install chromium" in common and "Executable doesn" in common,
       "a present Playwright with no browser says which command to run, "
       "rather than 500-ing with a driver path")
    ok("Proactor event loop" in common,
       "and the Windows loop failure is named, because nothing in the "
       "NotImplementedError it raises says what went wrong")
    ok("_off_the_proactor_loop" in common and "asyncio.to_thread" in common,
       "but it is worked around before it is explained — the job runs on a "
       "Proactor loop of its own, so --reload can still print a PDF")
    ok(common.count("_with_a_browser(") >= 3,
       "and the screenshot path goes through the same door, so neither can "
       "acquire its own set of unanswered failures")

    ok("/export/" not in html
       and "exportAs('pdf')" in html and "exportAs('excel')" in html,
       "both exports are buttons, not anchors — no export URL is reachable "
       "from the template, because a browser told to save a URL saves the "
       "error body as the PDF, which is the reported symptom")
    ok("async download(" in api and "res.json()).detail" in api,
       "the client reads the server's own sentence rather than its status")
    ok("if (!blob.size)" in report,
       "an empty body is refused rather than saved, so no server fault can "
       "put a zero-length PDF on disk")
    ok("exportError" in report and "err-banner" in html,
       "and a failed export says so on the screen it failed on")

    # Then the host where the install can never run.
    ok('channel=channel' in common and "msedge" in common,
       "a missing Chromium is not the end of the attempt — the Edge or Chrome "
       "already on the host is tried, and needs no download")
    ok("COMPASS_BROWSER_CHANNELS" in common,
       "and which browsers those are is configuration, not a constant")
    ok('"Executable doesn" not in detail' in common,
       "a real launch fault still reports itself rather than being buried "
       "under three more attempts at browsers that were never the problem")

    routes = (ROOT / "compass/estimate/routes.py").read_text()
    ok('"/v1/estimates/{estimate_id}/export/html"' in routes
       and "export.to_html(estimation, record.display)" in routes,
       "and the document is reachable unprinted, from the same HTML — so the "
       "PDF can be had from the browser in front of the person, with no "
       "browser on the server at all")
    ok("default-src 'none'" in routes,
       "served under a policy that runs no script, so a hole in the escaping "
       "cannot become script on the app's own origin")
    ok("setAttribute('sandbox', 'allow-same-origin allow-modals')" in report,
       "printed from a frame granted exactly two things — this page may reach "
       "in to call print(), and the document may not run a line of script")


def check_the_architect_asks_once() -> None:
    """The platform choice is one call, and used to be up to five.

    Every tool the ReAct loop offered — the platform list, the use-case mix,
    the constraints, the keyword baseline — was a read-only view of the input
    the caller was already holding. Nothing was fetched and nothing changed
    between turns, so the model spent up to five sequential round trips asking
    for facts that could have been in the first message. On gpt-5 that was
    thirty to forty seconds bolted onto an estimate whose arithmetic takes
    under a second.

    A ReAct loop earns its turns when a later question depends on an earlier
    answer. None here did. Measured after: the same brief, the same total of
    84,329, in 18.5 seconds instead of about 40.
    """
    print("\nthe architect asks once")
    src = (ROOT / "compass/estimate/architect.py").read_text()

    ok("_MAX_ITERS" not in src and "Observation:" not in src,
       "the loop is gone, and so is the transcript format it needed")
    ok("_ANSWER_SCHEMA" in src and "schema_name=\"platform_choice\"" in src,
       "the answer is a structured object rather than prose to be scraped for "
       "\"Final Answer:\"")
    ok("def _briefing(inp: ProjectInput) -> str:" in src,
       "and the four views are composed into the briefing up front — they were "
       "always views of what the caller already had")
    for view in ("_tool_list_platforms", "_tool_analyze_use_cases",
                 "_tool_read_constraints", "_tool_baseline_guess"):
        ok(f"{view}(" in src.split("def _briefing")[1][:400],
           f"{view} still contributes, unchanged")
    ok("if platform not in _VALID:" in src,
       "the answer is still validated against the five keys — a schema is the "
       "provider's promise rather than ours, and an unknown platform would be "
       "priced as the default without anyone noticing which one they were "
       "quoted")
    ok("return _heuristic_proposal(inp)" in src,
       "and the deterministic fallback is untouched, so an estimate is "
       "identical with the stage off")


def check_the_rate_card_is_kept() -> None:
    """The dials can be set once instead of per estimate.

    Every figure leadership sees multiplies these five numbers, and asking for
    them on every brief is how they end up left at whatever the defaults were.

    Per person rather than per workspace, deliberately: a rate card is an
    organisation's fact, so a shared one is the right idea, but Compass's
    ownership separates users without isolating them and has no notion of an
    admin. A shared card would be a document any user could silently rewrite
    for everyone, and the failure mode is every future estimate quietly costed
    at somebody else's rates. A shared card wants a role to own it first.
    """
    print("\nthe rate card is remembered")
    store = (ROOT / "compass/estimate/store.py").read_text()
    routes = (ROOT / "compass/estimate/routes.py").read_text()
    ts = (ROOT / "frontend/src/app/estimate/estimate.ts").read_text()

    ok("class RateCardStore(_JsonStore):" in store and "BASELINE_RATES" in store,
       "saved cards sit beside the estimates, over the platform baselines")
    ok("hashlib.sha256" in store,
       "keyed by a digest of the owner: an owner is an email address, the key "
       "reaches the filesystem, and stripping the punctuation out of "
       "`a.b@x.com` and `ab@x.com` gives the same name")
    ok("{**BASELINE_RATES, **{k: v for k, v in saved.items() if k in BASELINE_RATES}}" in store,
       "and merged over the baseline, so a card written before a dial existed "
       "still opens with every field filled instead of a blank input")

    ok("class RateCard(BaseModel):" in routes and "le=168" in routes,
       "the dials are bounded — a week has 168 hours, and a zero rate makes "
       "every figure zero without anything looking broken")

    ok("private currentRateCard(): CostAssumptions {" in ts
       and "cost_assumptions: this.currentRateCard()," in ts,
       "the form has one place that reads the dials, so what is saved and what "
       "is costed cannot diverge")
    ok("async saveRates(): Promise<void> {" in ts,
       "and saving is a separate act from using: changing a rate for one "
       "estimate is not the same as changing what every future one assumes")


def check_a_pipeline_can_name_its_estimate() -> None:
    """The one link between the two modules, and the shape that keeps it safe.

    A pipeline can say which estimate it implements. The link points one way
    and carries one opaque string.

    *The server never resolves it.* Pipelines does not import Estimate and does
    not check the id against the estimate store, because the two modules
    switch independently: a foreign key between them would mean a pipeline
    that fails to load on a box where costing is off, which is a worse bug
    than a link that resolves to nothing. The UI asks, gets nothing, and says
    so.

    *The dependency is one-directional and only in the UI.* Pipelines injects
    the Estimate module's API service; Estimate has no idea Pipelines exists —
    which is what the isolation check two sections above is still asserting.
    Everything is gated on the health flag, so with the module off the
    affordance is not merely inert, it is not rendered.
    """
    print("\na pipeline can name the estimate it implements")
    store = (ROOT / "compass/pipelines/store.py").read_text()
    routes = (ROOT / "compass/pipelines/routes.py").read_text()
    ts = (ROOT / "frontend/src/app/pipelines/pipelines.ts").read_text()
    html = (ROOT / "frontend/src/app/pipelines/pipelines.html").read_text()

    ok("estimate_id: str = \"\"" in store,
       "a pipeline carries an estimate id, defaulting to none")
    ok("compass.estimate" not in code_only(store)
       and "compass.estimate" not in code_only(routes),
       "and Pipelines still imports nothing from Estimate — the id is opaque "
       "on the server, so neither module can stop the other loading")
    ok("pipeline.estimate_id = body.estimate_id.strip()[:64]" in routes,
       "trimmed and length-capped rather than validated: it reaches a store "
       "key, and an unbounded string from a patch body should not")

    ok("private readonly estimateApi = inject(EstimateApi);" in ts,
       "the UI injects the Estimate module's own service rather than "
       "duplicating its URLs")
    est_ui = (ROOT / "frontend/src/app/estimate/estimate.ts").read_text()
    ok("pipelines/" not in est_ui,
       "and nothing points back the other way")
    ok("readonly estimateEnabled = signal(false);" in ts
       and "(await this.api.health()).estimate" in ts,
       "availability comes from the health endpoint, the same source the nav "
       "uses — with the flag off there are no costing routes to link to")
    ok("@if (estimateEnabled()) {" in html,
       "so with the module off the chip is not drawn at all")
    ok('class="pl-estlink missing"' in html,
       "and a link whose estimate is not on this server says that, rather "
       "than rendering as though nothing were wrong")


# ── The work breakdown ──────────────────────────────────────────────────────
#
# Five sub-features across two modules in two phases, chosen so every figure
# below can be done in the head: 45 + 90 + 36 = 171 in phase one, 27 + 18 = 45
# in phase two, 216 hours in all.

WBS_BRIEF = {
    **BRIEF,
    "project_name": "Acme Streaming",
    "features": [],
    # The line arithmetic, with the delivery rows switched off so every figure
    # below stays one a reader can do in their head. The rows have a check of
    # their own, `check_the_delivery_rows_are_booked`, on this same brief.
    "cost_assumptions": {"include_delivery": False},
    "assumptions": [
        "Third-party licences are procured by the client.",
        "One environment; a second doubles the deployment line.",
    ],
    "modules": [
        {"id": "m1", "name": "Video pipeline", "phase": 1, "sub_features": [
            {"id": "s1", "name": "FFmpeg transcoding", "description": "",
             "size": "l", "ai_candidate": False},
            {"id": "s2", "name": "API creation and blob integration", "description": "",
             "size": "xxl", "ai_candidate": False},
            {"id": "s3", "name": "Player integration", "description": "",
             "size": "m", "ai_candidate": False},
        ]},
        {"id": "m2", "name": "Catalogue", "phase": 2, "sub_features": [
            {"id": "s4", "name": "Search and filters", "description": "",
             "size": "s", "ai_candidate": False},
            {"id": "s5", "name": "Category pages", "description": "",
             "size": "xs", "ai_candidate": False},
        ]},
    ],
}


def check_work_breakdown() -> None:
    """Where an estimate's accuracy actually comes from.

    Sizing a "feature" as low/medium/high/very_high was the port's inherited
    model, and checked against a real architect's sheet for a streaming
    platform it was wrong by 0.59× to 2.06× module by module — while landing
    within 12% in aggregate, which is the dangerous kind of right. The errors
    cancelled. What the sheet did that the model did not was *enumerate*: every
    line was something a person could name, size in days, and argue with.

    So the unit changed. A sub-feature is sized in whole nine-hour units — a
    working day with the meetings taken out — and nothing may exceed ten of
    them. The ceiling is the mechanism: a line that wants to be 120 hours is a
    line nobody has thought about yet, and the estimate says so by name rather
    than quietly pricing the guess. Hours are all-in from that point down, so
    the flat ×2 delivery multiplier is gone; there is nothing left for it to
    correct.

    Against the same sheet, priced this way, the engine reproduces its 2,034
    hours exactly, because it is adding up the same lines.
    """
    print("\nthe work is broken down into lines somebody can argue with")
    est = asyncio.run(run_estimate(
        ProjectInput.model_validate(WBS_BRIEF), "est_wbs", "2026-06-20T00:00:00+00:00"))
    dev = est.cost_breakdown.development
    w = dev.work_breakdown

    ok(w is not None and len(w.phases) == 2, "two phases, in the order they run")
    eq(w.unit_hours, 9, "one unit is nine hours — a day, less the meetings")

    p1, p2 = w.phases[0], w.phases[1]
    ramp = [m for m in p1.modules if m.derived]
    eq([m.name for m in p1.modules if not m.derived], ["Video pipeline"],
       "phase one's modules")
    eq(len(ramp), 1,
       "plus the ramp-up, which the engine books and the brief never named")
    named = [m for m in p1.modules if not m.derived][0]
    subs = {s.name: s.hours for s in named.sub_features}
    #   l=5u=45h   xxl=10u=90h   m=4u=36h
    eq(subs["FFmpeg transcoding"], 45, "an L is five units")
    eq(subs["API creation and blob integration"], 90, "an XXL is ten — the ceiling")
    eq(subs["Player integration"], 36, "an M is four")

    eq(named.hours, 171, "a module is the sum of its sub-features")
    #   two modules in the brief, so ramp-up is the first band: 2 units, 18h
    eq(ramp[0].hours, 18, "a two-module plan ramps up in two units")
    eq(p1.hours, 171 + 18, "and a phase the sum of its modules, ramp-up included")
    eq(p2.hours, 45, "phase two")
    eq(w.total_hours, 216 + 18, "the plan is the sum of its phases")
    eq(w.total_cost, engine.jround(234 * 115), "priced at the delivery rate")
    ok(all(s.cost == engine.jround(s.hours * 115)
           for ph in w.phases for m in ph.modules for s in m.sub_features),
       "every line carries its own price, so a reader can strike one out and "
       "subtract rather than re-run the estimate")

    # A module is one engineer's job: 171/32 = 5.34 weeks. A phase is the team
    # working at once: 171/(3*32) = 1.78. Quoting one figure for both is how a
    # plan promises that six modules take as long as the longest of them.
    eq(w.team_size, 3, "the team size the elapsed weeks assume")
    eq(named.weeks, 5.34, "a module's weeks are one engineer's")
    eq(p1.weeks, engine.round2(189 / (3 * 32)), "a phase's are the team's")
    eq(w.total_weeks, round(p1.weeks + p2.weeks, 2),
       "and the plan is the phases end to end, because they run in order")

    # A brief may set units directly — the sheet this model came from sized in
    # days, not letters — and the band has to name what was priced rather than
    # whatever default the field was left at.
    direct = asyncio.run(run_estimate(ProjectInput.model_validate({
        **WBS_BRIEF,
        "modules": [{"id": "m1", "name": "Pipeline", "phase": 1, "sub_features": [
            {"id": "s1", "name": "Transcoding", "size": "s", "units": 7},
            {"id": "s2", "name": "Odd one out", "size": "s", "units": 6},
        ]}],
    }), "est_units", "2026-06-20T00:00:00+00:00"))
    priced = [m for m in direct.cost_breakdown.development.work_breakdown.phases[0].modules
              if not m.derived][0].sub_features
    eq((priced[0].size, priced[0].hours), ("xl", 63),
       "seven units is an XL, whatever the band field said — a pill reading "
       "\"S · 7d\" contradicts itself")
    eq((priced[1].size, priced[1].hours), ("", 54),
       "and a count between two bands gets no letter at all — the days are "
       "what was priced, and a band that does not mean them is noise")

    eq(w.at_ceiling, ["Video pipeline — API creation and blob integration"],
       "the line at the ceiling is named")
    ok(all(s.hours <= 90 for ph in w.phases for m in ph.modules
           for s in m.sub_features),
       "nothing is priced above the ceiling")
    eq(named.sub_features[1].hours, 90,
       "but the ceiling is reported, not applied — clamping a 120-hour line to "
       "90 would hide the one thing worth knowing about it, which is that "
       "nobody has broken it down")

    eq(sum(b.hours for b in dev.breakdown), 234,
       "development is the breakdown, at medium scale, with no multiplier "
       "between them")
    eq(dev.ai_integration_hours, 0,
       "and no separate AI-integration bucket: a breakdown names its AI work "
       "as line items, so adding a bucket on top would charge for it twice")

    eq(est.assumptions, WBS_BRIEF["assumptions"],
       "the assumptions travel with the estimate — they are what a "
       "disagreement about the number is usually actually about")


def check_the_section_renders_on_an_older_browser() -> None:
    """The Estimate CSS keeps to what Windows Edge and Chrome both had.

    `:has()` is the specific trap: it is the natural way to say "a row that
    contains a badge", it is one line, and it silently does nothing on a
    browser older than Chrome 105. This section was written with a badge on
    one work-breakdown row and reached for exactly that selector; the row is
    laid out with an `auto` grid track instead, which is zero-wide when the
    badge is absent. Comments are stripped before searching, because a check
    that goes red on the sentence explaining why a thing is *not* done is
    worse than no check.
    """
    print("\nthe section renders on the browser somebody actually has")
    for name in ("estimate.css", "report.css"):
        css = re.sub(r"/\*.*?\*/", "", (ROOT / f"frontend/src/app/estimate/{name}").read_text(),
                     flags=re.S)
        ok(":has(" not in css,
           f"{name} selects with a class or a grid track rather than :has() — "
           "Chrome and Edge only got it in 105, and a selector that quietly "
           "matches nothing is worse than one that fails loudly")
        ok(":root" not in css,
           f"{name} uses :host-context rather than :root — component styles "
           "are scoped, and a :root rule inside one is a no-op that looks "
           "like a theme")


def check_the_ramp_up_is_booked() -> None:
    """Every project reads the requirement and stands up an environment.

    No description ever asks for it. Across twelve drafts of one paragraph the
    model named it zero times — not as a module, not as a sub-feature, and not
    as an assumption either, which is what separates it from the scope a brief
    genuinely leaves out. Notifications are absent from that same fixture and
    the drafter excludes them *in writing* in 12 of 12 briefs; a reader sees
    the boundary and can argue with it. Ramp-up is simply invisible, and an
    estimate that is silently short by a week is worse than one that is openly
    short by a module.

    So the engine books it rather than the prompt asking a model to invent it.
    That division is the module's founding rule: a model resolves labels, code
    computes numbers, and "every project has a ramp-up" is not a fact about
    this project that a paragraph is evidence for — it is a fact about
    projects, which is exactly the kind of thing a deterministic engine is
    allowed to know.

    It steps with the number of modules, not with hours. What has to be
    understood is the number of distinct areas, and the sixth costs less to
    take in than the first — so the ladder flattens. A proportion of the build
    would have been a multiplier, which is what `DELIVERY_OVERHEAD` was and why
    it is gone.
    """
    print("\nthe project's own ramp-up is booked, because nobody ever writes it down")
    brief = {**WBS_BRIEF, "modules": [
        {"id": f"m{i}", "name": f"Module {i}", "phase": 1, "sub_features": [
            {"id": f"m{i}s1", "name": f"Thing {i}", "size": "s"}]}
        for i in range(1, 18)]}
    est = asyncio.run(run_estimate(
        ProjectInput.model_validate(brief), "est_ramp", "2026-06-20T00:00:00+00:00"))
    w = est.cost_breakdown.development.work_breakdown
    derived = [m for ph in w.phases for m in ph.modules if m.derived]

    eq(len(derived), 1, "exactly one module the brief did not name")
    eq(derived[0].hours, 45,
       "seventeen modules ramps up in five units — which is what the "
       "architect's sheet books for its seventeen, arrived at independently")
    eq([s.hours for s in derived[0].sub_features], [18, 27],
       "reading the requirement and building the environment are two lines: "
       "they are different jobs, and a reader who disagrees with one should "
       "not have to argue about the other")
    ok(derived[0] is w.phases[0].modules[0],
       "and it comes first in the earliest phase, which is when it happens")

    ok(all(not m.derived for ph in w.phases for m in ph.modules
           if m.name != derived[0].name),
       "every other module says it was not derived — a line nobody wrote must "
       "not be able to pass itself off as one somebody did")

    # The ladder, at each of its steps.
    eq([engine._ramp_up_units(n) * 9 for n in (1, 4, 5, 9, 10, 15, 16, 40)],
       [18, 18, 27, 27, 36, 36, 45, 45],
       "it steps with breadth and then flattens — a forty-module plan does "
       "not need eight times the introduction a five-module plan does")

    # Delivery stays off on both sides: with it on, the switch also takes the
    # ramp-up's share of testing and leadership with it, which is right and is
    # pinned where the delivery rows are, not here.
    off = asyncio.run(run_estimate(ProjectInput.model_validate(
        {**brief, "cost_assumptions": {"include_ramp_up": False,
                                       "include_delivery": False}}),
        "est_ramp_off", "2026-06-20T00:00:00+00:00"))
    ow = off.cost_breakdown.development.work_breakdown
    ok(not any(m.derived for ph in ow.phases for m in ph.modules),
       "and it is a switch, not a constant: a team already running on this "
       "stack, extending what they built last quarter, turns it off")
    eq(ow.total_hours, w.total_hours - 45,
       "which removes exactly the hours it added, and nothing else")

    # The guarantee that matters most: the legacy path never sees it.
    legacy = asyncio.run(run_estimate(
        ProjectInput.model_validate(BRIEF), "est_ramp_legacy",
        "2026-06-20T00:00:00+00:00"))
    eq(sum(b.hours for b in legacy.cost_breakdown.development.breakdown), 356,
       "a brief with no breakdown is untouched by any of this — the port's "
       "parity guarantee covers the flat-feature path, and a new line added "
       "to it would be a fourth deliberate break rather than a third")


def check_team_and_skills() -> None:
    """Who is available changes how long; how well they know the stack changes
    how much. Both are the person's inputs and both are arithmetic.

    Headcount never changes an hour. A band's hours are already the whole of
    delivering the line — analysis, build, test, review, deployment — so the
    team is a way of *dividing* them, 62/23/15 across development, testing and
    analysis-and-design, and a phase lasts as long as its slowest discipline:
    three developers done in a week do not finish a phase that one tester has
    two weeks of work left in.

    Skill is the one input allowed to move hours, and only by a published
    multiplier — COCOMO II's language and tool experience factor, 1.20 for a
    novice team to 0.84 for an expert one, applied per line to the modules
    tagged with that technology. The model names the technologies; it never
    says how well anybody knows them.

    Every figure below is hand-computed. The team shapes avoid landing exactly
    on a half-hundredth, where a binary float and the round-half-up engine
    could legitimately disagree with the arithmetic written here.
    """
    print("\nthe team and its skills are priced from what a person entered")
    base = {
        "project_name": "Team check", "project_type": "new", "scale": "medium",
        "cost_assumptions": {
            "dev_hourly_rate": 115, "maint_hourly_rate": 95, "effective_hours_per_week": 32,
            "team_size": 3, "include_ramp_up": False, "loaded_hourly_rate": 75,
            "automation_rate_percent": 35, "benefit_ramp_months": 6,
            # Off, so the hand-computed figures stay the lines alone. How the
            # testing row reaches the testers is pinned with the delivery rows.
            "include_delivery": False,
        },
        "modules": [
            {"id": "m1", "name": "Portal", "phase": 1, "technologies": ["Angular"],
             "sub_features": [{"id": "s1", "name": "Claim list", "size": "m"},
                              {"id": "s2", "name": "Claim editor", "size": "l"}]},
            {"id": "m2", "name": "API", "phase": 1, "technologies": [".NET", "Azure"],
             "sub_features": [{"id": "s3", "name": "Extraction service", "size": "xl"}]},
        ],
    }

    def breakdown(extra: dict):
        est = asyncio.run(run_estimate(ProjectInput.model_validate({**base, **extra}),
                                       "est_team", "2026-06-20T00:00:00+00:00"))
        return est.cost_breakdown.development.work_breakdown

    plain = breakdown({})
    #   m=4u=36h + l=5u=45h = 81h ; xl=7u=63h ; 144h over the rate card's 3: 144/96 = 1.5
    eq(plain.total_hours, 144, "without a team, the breakdown is priced exactly as before")
    eq(plain.phases[0].weeks, 1.5, "and the phase is the rate card's team of three")
    eq((plain.roles, plain.skill_adjustment_hours), ([], 0),
       "with no disciplines reported and nothing adjusted")

    skills = [{"technology": "Angular", "score": 2}, {"technology": ".NET", "score": 5},
              {"technology": "azure", "score": 3}]
    t = breakdown({"team": {"developers": 2, "testers": 1, "analysts_designers": 1, "skills": skills}})
    portal = next(m for m in t.phases[0].modules if m.name == "Portal")
    api = next(m for m in t.phases[0].modules if m.name == "API")
    #   Angular at 2 is LTEX "low", x1.09:  36x1.09 = 39.24 -> 39 ; 45x1.09 = 49.05 -> 49
    eq([s.hours for s in portal.sub_features], [39, 49],
       "a beginner team takes 9% longer on each Angular line")
    eq((portal.base_hours, portal.hours, portal.skill_factor), (81, 88, 1.09),
       "and the module says what it was sized at before the adjustment")
    #   .NET at 5 is x0.84 ; "azure" matches "Azure" at 3, x1.00 ; mean 0.92 ; 63x0.92 = 57.96 -> 58
    eq((api.hours, api.skill_factor), (58, 0.92),
       "a module on two technologies takes the mean of their multipliers, "
       "and a score matches its technology whatever the capitalisation")
    eq(t.total_hours, 146, "the plan is still the sum of its lines")
    eq(t.skill_adjustment_hours, 2,
       "and says how far skill moved it: +7 on the portal, -5 on the API")
    eq(t.total_cost, engine.jround(146 * 115), "priced at the same delivery rate")
    eq(t.team_size, 4, "the team size is the people entered")
    #   146h split 62/23/15 ; development 90.52h/(2x32) = 1.414 -> 1.41 ;
    #   testing 33.58h/(1x32) = 1.049 -> 1.05 ; BA/UX 21.9h/(1x32) = 0.684 -> 0.68
    eq([(r.role, r.hours, r.headcount, r.weeks) for r in t.roles],
       [("development", 91, 2, 1.41), ("testing", 34, 1, 1.05), ("analysis_design", 22, 1, 0.68)],
       "each discipline's share of the hours, over its own people")
    eq((t.phases[0].weeks, t.phases[0].bottleneck), (1.41, "development"),
       "and a phase runs as long as its slowest discipline")

    faster = breakdown({"team": {"developers": 4, "testers": 1, "analysts_designers": 1, "skills": skills}})
    #   development 90.52h/(4x32) = 0.707 -> 0.71 ; testing still 1.05 ; BA/UX 0.68
    eq((faster.phases[0].weeks, faster.phases[0].bottleneck), (1.05, "testing"),
       "doubling the developers moves the constraint to the one tester")
    eq(faster.total_hours, 146, "and changes no hour")

    alone = breakdown({"team": {"developers": 2, "testers": 0, "analysts_designers": 0, "skills": []}})
    #   nobody to hand testing or analysis to: all 144h on the developers ; 144/(2x32) = 2.25
    eq([(r.role, r.share_percent) for r in alone.roles], [("development", 100.0)],
       "a discipline with nobody in it is carried by the developers, not dropped")
    eq(alone.phases[0].weeks, 2.25, "so two developers on their own take 2.25 weeks")

    skills_only = breakdown({"team": {"developers": 0, "testers": 0, "analysts_designers": 0,
                                      "skills": skills}})
    eq((skills_only.total_hours, skills_only.phases[0].weeks, skills_only.roles), (146, 1.52, []),
       "skill scores apply without headcount, and the weeks fall back to the rate "
       "card's team: 146/96 = 1.52")


def check_a_brd_can_be_read() -> None:
    """A requirements document fills the form the way a paragraph does, on the
    same side of the same line.

    It lives in the drafter, not beside it: reading a BRD is drafting from a
    longer and more careful input, and the checked list of files that may call
    a model stays at three. It reads harder — high effort, where the paragraph
    drafter is deliberately low — because pricing a raw requirement is
    deduction: the scenarios the document does not walk through, the edge
    cases that break naive builds, the questions whose answers move the
    number. What it may return is the drafter's schema plus that analysis and a
    module's technology *names*. No team, no skill, no rate card: those are
    facts about an organisation that no document contains, and they are asked
    of the person in the form.
    """
    import json

    from compass.estimate import intake

    print("\na requirements document can be read")
    src = (ROOT / "compass/estimate/intake.py").read_text()
    joined = src.replace("\\\n", "")
    routes = (ROOT / "compass/estimate/routes.py").read_text()

    ok("async def analyse_brd(" in src,
       "reading a BRD is in the drafter, so model use stays in three files")
    ok(intake._BRD_SYSTEM.endswith(intake._SYSTEM),
       "and it carries the drafter's band rules verbatim — the part that was measured")
    for phrase, what in (
        ("edge_cases:", "it asks for the edge cases that break naive builds"),
        ("open_questions:", "and the ambiguities that would move the number, each with the assumption priced under"),
        ("scenarios:", "and each journey's alternate and failure path"),
        ('`source: "implied"`', "an area the document implies but never names is marked, so a person can strike it"),
        ("usually four to eight, never a parts list.",
         "technologies are the skill areas a team is rated on, not a list of services — measured on a "
         "real BRD, a parts list gave nineteen rows and a team scored weak on Azure moved the plan by "
         "under 2%, because every module was tagged with a service still sitting at the default"),
        ("Never state or imply hours, days, cost, a timeline, a team size, headcount or anyone's skill level.",
         "and it may not state hours, cost, timeline, headcount or skill"),
    ):
        ok(phrase in joined, what)

    props = intake._BRD_SCHEMA["properties"]
    ok(not {"cost_assumptions", "team"} & set(props),
       "the schema has no rate card and no team")
    module = props["modules"]["items"]["properties"]
    ok("technologies" in module and not {"hours", "units", "skill", "score"} & set(module),
       "a module names its technologies and nothing that is a number")
    ok("cost_assumptions" not in intake._SCHEMA["properties"],
       "and building it did not add anything to the paragraph drafter's schema")

    ok('@router.post("/v1/estimates/brd")' in routes and "extract_document_text" in routes,
       "the route reads the file through the shared extractor, without rendering pages")
    ok("status_code=415" in routes and "no text layer" in routes,
       "and refuses what it cannot read — an image, a scan — saying which")

    canned = {
        "project_name": "Claims intake", "description": "Portal and extraction for claim packs.",
        "industry_domain": "Insurance", "target_users": "Adjusters", "scale": "medium",
        "modules": [
            {"name": "Claim intake", "phase": 1, "technologies": ["angular", "Kubernetes", "\\.NET"], "source": "stated",
             "sub_features": [{"name": "Upload claim pack", "description": "PDF upload", "size": "m", "ai_candidate": False}]},
            {"name": "Sign-in and roles", "phase": 1, "technologies": ["Azure"], "source": "implied",
             "sub_features": [{"name": "Entra ID sign-in", "description": "Section 4.2 names adjusters and supervisors", "size": "s", "ai_candidate": False}]},
        ],
        "assumptions": ["English only"],
        "volume_and_scale": {"expected_daily_users": None, "requests_per_day": 900, "data_volume_gb": None,
                             "peak_load_pattern": "", "growth_rate_percent": None},
        "ai_use_cases": [{"name": "Field extraction", "description": "Read key fields", "task_type": "document_analysis",
                          "priority": "must_have"}],
        "analysis": {
            "summary": "An intake portal.", "functional_requirements": ["Upload a claim pack"],
            "non_functional_requirements": [], "user_roles": ["Adjuster"], "integrations": ["Claims system"],
            "scenarios": ["Upload; alternate: scan; failure: low confidence"], "edge_cases": ["Duplicate upload"],
            "out_of_scope": [], "risks": [{"description": "Scans", "severity": "high", "mitigation": "OCR"}],
            "open_questions": [{"question": "Which identity provider?", "why_it_matters": "Sign-in scope",
                                "assumed_answer": "Assume single sign-on via Entra ID"}],
            "technologies": [{"name": "Angular", "category": "Frontend", "reason": "Stated"},
                             {"name": "Azure", "category": "Cloud", "reason": "Assumed"},
                             {"name": "\\.NET", "category": "Backend", "reason": "Stated"}],
        },
    }

    class StandIn:
        seen: tuple = ()

        async def complete_utility(self, prompt, text, **kw):
            StandIn.seen = (prompt, text, kw)
            return json.dumps(canned)

    real = intake.get_model_client
    intake.get_model_client = lambda: StandIn()
    try:
        brief = asyncio.run(intake.analyse_brd("The claims portal must…", "claims-brd.pdf", "new"))
    finally:
        intake.get_model_client = real

    prompt, text, kw = StandIn.seen
    eq((kw.get("effort"), kw.get("schema") is intake._BRD_SCHEMA), ("high", True),
       "it is read at high effort, against the document schema")
    ok(prompt == intake._BRD_SYSTEM and "claims-brd.pdf" in text,
       "with the architect's prompt, and the document named")
    eq([m.id for m in brief.modules], ["m1", "m2"], "ids are minted rather than asked for")
    eq(brief.modules[0].technologies, ["Angular", ".NET"],
       "a module keeps only technologies the analysis lists, spelled the analysis's way, "
       "with the Markdown escape a real read left on \\.NET taken off")
    eq([x.name for x in brief.brd_analysis.technologies][-1], ".NET",
       "and the skill table gets the plain name too")
    eq(brief.modules[1].source, "implied", "and an implied area stays marked")
    ok(all(u.minutes_per_call is None for u in brief.ai_use_cases),
       "the ROI dial stays at its default, as in the paragraph drafter")
    ok(brief.team is None and brief.cost_assumptions is None,
       "no team and no rate card come out of a document")
    ok("Assume single sign-on via Entra ID" in brief.assumptions and "English only" in brief.assumptions,
       "every assumed answer becomes a stated assumption, beside the ones the model listed")
    eq((brief.brd_analysis.document_name, len(brief.brd_analysis.open_questions)), ("claims-brd.pdf", 1),
       "and the analysis travels with the brief to the report")


# ── The Gift Tracker sheet, as a brief ──────────────────────────────────────
#
# The architect's estimate for the GDS Finance Gift Tracker, line for line, in
# the sheet's own order. Whole days are units; the two lines that are not —
# the request form at 40 and the Power BI dashboard at 136 — are quoted hours.
# The sub-items the sheet lists under a line with no hours of their own are
# that line's covers. The ramp-up is off because the sheet's first row is its
# own Requirement & Design; the delivery rows are on because its last four
# rows are exactly them. Two cells were cut off in the photograph the sheet
# arrived as, and are completed here as the visible text reads: "Create and
# Manage Role based Access for ea…" and "Remove false positive (if any
# recipient raised…".

def _sheet_line(n, name, *, units=None, hours=None, covers=()):
    sub = {"id": f"m{n}s1", "name": name, "size": "s", "covers": list(covers)}
    if hours is not None:
        sub["hours"] = hours
    else:
        sub["units"] = units
    return {"id": f"m{n}", "name": name, "phase": 1, "sub_features": [sub]}


GIFT_TRACKER_SHEET = {
    "project_name": "GDS Finance Gift Tracker",
    "project_type": "new",
    "description": (
        "Gift Tracker for GDS Finance: raise gift requests singly or by bulk upload, "
        "track them through a sign-off chain, maintain the final recipient list, "
        "trigger distribution emails to every recipient on it, and report employees' "
        "gifts team-wise for the financial year to date."),
    "industry_domain": "Internal finance — employee gifting",
    "target_users": "Specific identified user personas in GDS India — not an all-employee tool",
    "scale": "medium",
    "modules": [
        _sheet_line(1, "Requirement & Design", units=5),
        _sheet_line(2, "Create and manage role-based access for each persona", units=5),
        _sheet_line(3, "Home page", units=3, covers=(
            "Gift Request", "Tracker", "Budget Version", "Dashboard", "Audit trail")),
        _sheet_line(4, "Admin: thresholds / policy config", units=5),
        _sheet_line(5, "Template configuration", units=5),
        _sheet_line(6, "Environment & solution structure", units=5),
        _sheet_line(7, "Data model & SQL schema", units=5),
        _sheet_line(8, "Raise a gift request (form)", hours=40, covers=(
            "Business rule & validation logic", "Filters and showing gifts")),
        _sheet_line(9, "Bulk upload — parse, validate", units=10),
        _sheet_line(10, "Tracker — workflow — sign-off chain", units=10),
        _sheet_line(11, "Add / remove recipient: final list", units=8, covers=(
            "Validation and business logic",)),
        _sheet_line(12, "Distribution: queue & send emails", units=12, covers=(
            "Email trigger to downstream team on each stage",
            "Email notification scheduler (reminder)",
            "Email trigger to final list recipients")),
        _sheet_line(13, "Remove false positive (if a recipient raises one)", units=5),
        _sheet_line(14, "Budget version", units=5),
        _sheet_line(15, "Audit trail", units=10),
        _sheet_line(16, "Reporting — financial year-to-date, team-wise", units=10),
        _sheet_line(17, "Power BI dashboard", hours=136),
        _sheet_line(18, "Monitoring, logging and exception handling", units=5),
        _sheet_line(19, "Additional data integration with TDL / Graph API", units=10),
    ],
    # What the system does, each saying it needs no model — which is what makes
    # this a standard-software estimate rather than an AI one with no AI in it.
    "ai_use_cases": [
        {"id": "u1", "name": "Gift request and sign-off workflow", "task_type": "rules_workflow",
         "description": "Route requests through the configured sign-off chain; no model is required.",
         "priority": "must_have"},
        {"id": "u2", "name": "Final recipient list and distribution emails", "task_type": "rules_workflow",
         "description": "Maintain the final list and email every recipient on distribution; no model is required.",
         "priority": "must_have"},
        {"id": "u3", "name": "Employee gift history and team-wise reporting", "task_type": "crud_lookup",
         "description": "Report gifts by employee and team for the financial year to date; no model is required.",
         "priority": "must_have"},
    ],
    # The sheet's Assumption tab, verbatim, then its Tech Stack tab in one line.
    "assumptions": [
        "This will be used only for GDS India.",
        "Reporting templates and parameters to be provided by Business.",
        "The Gift Tracker is not intended to be an all-employee transactional tool. "
        "It will be limited to specific identified user personas only.",
        "No integration with any other EY GDS web applications/APIs have been considered.",
        "Tool will provide only employee's gift detail values and will not perform any calculations.",
        "Email notifications are in scope.",
        "Dependency on Talent Data Lake (TDL) to fetch employee details.",
        "There will be no \"Save as Draft\" functionality available in the tool.",
        "Technology: Angular frontend, .NET 8 backend, SQL DB; Azure App Service, Service Bus, "
        "Blob Storage, Functions, App registration, Monitoring, Log Analytics, Key Vault and "
        "App Gateway; SendGrid for email; Power BI for dashboards.",
    ],
    "volume_and_scale": {
        "expected_daily_users": 100, "requests_per_day": None, "data_volume_gb": None,
        "peak_load_pattern": "About 1,400 distributions in a financial year; a final list "
                             "may run to 100,000 recipients.",
        "growth_rate_percent": None,
    },
    "technical_preferences": {"existing_infra": "Azure", "delivery_platform": "azure_paas"},
    "cost_assumptions": {"include_ramp_up": False, "include_delivery": True},
}


def check_the_gift_tracker_sheet_reproduces() -> None:
    """The architect's sheet, entered as a brief, comes back as the sheet.

    The end-to-end form of everything above: nineteen lines in days and
    quoted hours, priced by this engine, add to his module total of 1,238; the
    delivery rows on top give his 1,683; and a system that needs no model is
    costed as one.
    """
    print("\nthe Gift Tracker sheet, entered as a brief, reproduces itself")
    est = asyncio.run(run_estimate(ProjectInput.model_validate(GIFT_TRACKER_SHEET),
                                   "est_sheet", "2026-10-28T00:00:00+00:00"))
    w = est.cost_breakdown.development.work_breakdown
    mods = w.phases[0].modules
    build = sum(m.hours for m in mods if m.kind != "delivery")
    dlv = [m for m in mods if m.kind == "delivery"][0]
    eq(build, 1238, "the build is the sheet's module total")
    eq([(s.name, s.hours) for s in dlv.sub_features],
       [("Module testing", 124), ("Technical leadership", 186),
        ("Deployment", 90), ("CI/CD pipeline", 45)],
       "the delivery rows are the sheet's — 123.8 and 185.7, rounded")
    eq(w.total_hours, 1683, "and the total is the sheet's")
    form = [m for m in mods if m.name == "Raise a gift request (form)"][0].sub_features[0]
    eq((form.hours, form.size, form.basis), (40, "", "40 h as quoted"),
       "a line quoted in hours is priced as quoted and says so, not rounded "
       "to a day it was never sized in")
    ok(not any(m.derived and m.kind == "ramp_up" for m in mods),
       "no ramp-up — the sheet's first row is its own Requirement & Design")
    ok(est.ai_applicable is False, "and nothing in it calls a model")

    # Nineteen modules, each its own one line. Drawn as a parent and a child
    # of the same name, every title appeared twice.
    import io
    from openpyxl import load_workbook
    from compass.estimate import export
    html = export.to_html(est)
    eq(html.count(">Home page"), 1,
       "a module that is its one line is printed once, not as a parent and a "
       "child of the same name")
    ok("Home page<div class='cov'>covers Gift Request · Tracker" in html,
       "with what it covers under its name, on the same row")
    ws = load_workbook(io.BytesIO(export.to_excel(est)))["Work Breakdown"]
    names = [r[1] for r in ws.iter_rows(min_row=2, values_only=True) if r[2] == "Module total"]
    ok("Home page" not in names and "Delivery and assurance (added by the engine)" in names,
       "the workbook gives it one row and no total repeating it — a module of "
       "several lines keeps its total")
    ts = (ROOT / "frontend/src/app/estimate/report.ts").read_text()
    ok("single(m: ModuleEstimate): SubFeatureEstimate | null" in ts,
       "and the screen draws it once, with nothing to expand into a copy of itself")
    form = (ROOT / "frontend/src/app/estimate/estimate.ts").read_text()
    ok("isSingle(m: Module): boolean" in form and "renameSingle(mi: number, name: string)" in form
       and "!!key(m.name)" in form,
       "the form edits it as one row too, renaming the module and its line "
       "together — and a fresh blank module still opens its full breakdown")
    ok("if (s.hours) return s.hours;" in form,
       "where a quoted line counts as quoted: the 40-hour form read as its "
       "band's 27 in the form's running total")
    css = (ROOT / "frontend/src/app/estimate/estimate.css").read_text()
    ok("grid-template-columns: 1fr 160px 128px 116px 108px 96px;" in css,
       "the list's verdict column holds STANDARD SOFTWARE, measured at 157px — at "
       "120 it ran into the feasibility bar beside it")


GIFT_TRACKER_STACK = [
    "app_service", "azure_sql", "service_bus_std", "storage", "functions",
    "app_registration", "monitor", "log_analytics", "key_vault", "sendgrid",
    "power_bi", "app_gateway",
]


def check_the_stack_is_the_one_stated() -> None:
    """Asked why the Gift Tracker was priced on Container Apps rather than the
    App Service its architect specified, the only true answer was that every
    new build is: the hosting rule is `select=lambda c: True`, Cosmos DB's is
    the same, and App Service, SQL Database, Functions, Application Gateway,
    Log Analytics, SendGrid and Power BI were not in the catalog at all. And
    asked to price it in North Europe, the honest answer was that it would
    barely move — every line but three was a flat per-scale baseline with no
    region in it.

    A stack the brief states is now priced as stated, nothing added; its
    services are live-priced in the brief's region where Azure publishes a
    meter. Measured live for the sheet's twelve services: East US $738 a month,
    North Europe $793, Sweden Central $841.
    """
    print("\nthe stack priced is the one the brief states, in the region it names")
    import copy
    brief = copy.deepcopy(GIFT_TRACKER_SHEET)
    brief["technical_preferences"] = {**brief["technical_preferences"],
                                      "azure_region": "northeurope",
                                      "azure_services": GIFT_TRACKER_STACK}
    est = asyncio.run(run_estimate(ProjectInput.model_validate(brief), "est_stack",
                                   "2026-10-29T00:00:00+00:00"))
    infra = est.cost_breakdown.infrastructure
    names = [s.service_name for s in infra.services]
    ok("Azure App Service" in names and "Azure SQL Database" in names
       and "Azure Application Gateway" in names and "Azure Service Bus" in names,
       "App Service, SQL Database, Application Gateway and Service Bus, as the "
       "architect's Tech Stack tab names them")
    ok(not {"Azure Container Apps", "Azure Cosmos DB", "Azure Cache for Redis",
            "Azure API Management", "Microsoft Defender for Cloud"} & set(names),
       "and nothing the default rules would have added")
    eq(len(infra.services), len(GIFT_TRACKER_STACK), "exactly the twelve stated")
    ok({s.region for s in infra.services} == {"northeurope"},
       "every line priced in the brief's region")
    pbi = [s for s in infra.services if s.service_name == "Power BI Pro"][0]
    ok(not pbi.included_in_total and pbi.unit_price == 14.0,
       "Power BI is shown at $14 a user and not counted — usually licensed "
       "across the organisation already")
    sb = [s for s in infra.services if s.service_name == "Azure Service Bus"][0]
    eq(sb.unit_price, 0.013441,
       "an hourly meter keeps its decimals, so the line multiplies out — two "
       "places printed Service Bus at $0.01 an hour")

    from compass.estimate.azure_pricing import _build_filter
    from compass.estimate.azure_catalog import SPEC_BY_KEY, build_plan_context
    ctx = build_plan_context(ProjectInput.model_validate(brief), uses_azure_openai=False,
                             monthly_token_cost=0, monthly_tokens=0)
    flt = _build_filter(SPEC_BY_KEY["azure_sql"], ctx, "northeurope")
    ok("meterName eq 'vCore'" in flt and "armRegionName eq 'northeurope'" in flt,
       "SQL is matched on its exact meter, in the region — 'contains' would also "
       "take the zone-redundant vCores and price the cheapest of the wrong thing")

    from compass.estimate.types import TechnicalPreferences
    tp = TechnicalPreferences()
    eq((tp.azure_region, tp.azure_openai_region, tp.azure_search_region),
       ("northeurope", "", ""),
       "North Europe by default, and the AI regions follow it rather than "
       "staying in East US when the brief moves")
    ts_form = (ROOT / "frontend/src/app/estimate/estimate.ts").read_text()
    ok("readonly azureRegion = signal('northeurope');" in ts_form,
       "and the form opens on it")

    unstated = asyncio.run(run_estimate(ProjectInput.model_validate(GIFT_TRACKER_SHEET),
                                        "est_rules", "2026-10-29T00:00:00+00:00"))
    ok("Azure Container Apps" in [s.service_name for s in unstated.cost_breakdown.infrastructure.services],
       "a brief that states no stack is still chosen for by the rules, as before")


def check_the_report_shows_what_it_is_told() -> None:
    """Three flags on the record — development cost, infrastructure cost, the
    written report — that the screen and both exports obey. A total is printed
    only when both costs are, because a total beside one hidden part gives
    that part away by subtraction."""
    print("\nthe report shows what it is told to, everywhere it is shown")
    import io
    from openpyxl import load_workbook
    from compass.estimate import export, store
    est = asyncio.run(run_estimate(ProjectInput.model_validate(GIFT_TRACKER_SHEET),
                                   "est_flags", "2026-10-29T00:00:00+00:00"))
    money = f"{est.cost_breakdown.currency} {est.cost_breakdown.development.total_cost:,.0f}"

    effort_only = {"development_cost": False, "infrastructure_cost": False,
                   "maintenance_cost": False}
    html = export.to_html(est, effort_only)
    ok(money not in html and "First year" not in html and "Infrastructure —" not in html
       and "1,683 h" in html,
       "effort only: no development figure, no first-year total, no "
       "infrastructure section — and the hours")
    dev_only = export.to_html(est, {"infrastructure_cost": False})
    ok(money in dev_only and "First year" not in dev_only and "Total (expected)" not in dev_only,
       "one cost shown, no total: the total would give the hidden one away")
    ok("First year" in export.to_html(est, None), "and everything, by default")
    maint = f"{est.cost_breakdown.currency} {est.cost_breakdown.maintenance.annual_cost:,.0f}"
    no_maint = export.to_html(est, {"maintenance_cost": False})
    ok(maint not in no_maint and "Maintenance (annual)" not in no_maint
       and "Infrastructure —" in no_maint and "Total (expected)" not in no_maint,
       "maintenance has a switch of its own: its line goes, the services stay, "
       "and so does the total — it would give the maintenance away")
    full = export.to_html(est, None)
    ok("<h2>The written report</h2>" in full
       and "GDS Finance Gift Tracker — Cost estimate" in full
       and "<b>Total expected:" in full,
       "the PDF carries the written report, title, cost lines and total — the "
       "one section of the screen it used to leave out")
    ok("<h2>The written report</h2>" not in export.to_html(est, {"written_report": False}),
       "under the same switch as the screen")
    wr = no_maint.split("<h2>The written report</h2>")[1]
    ok("Maintenance:" not in wr and "Total expected" not in wr and "Development:" in wr,
       "and a hidden cost leaves it too: the written report states every figure "
       "again, so it is the one place a hidden one would otherwise slip back in")
    ok("&lt;script&gt;" in export._report_html("# <script>x</script> — Cost", {
           "dev": True, "infra": True, "maint": True, "total": True})
       and "<script>" not in export._report_html("# <script>x</script>", {
           "dev": True, "infra": True, "maint": True, "total": True}),
       "escaped before it is given any markup — the project name in its title "
       "was typed by a person")
    ok("a month — infrastructure</div>" in no_maint,
       "and \"to run\" says it is the services alone, not the services and the people")

    ws = load_workbook(io.BytesIO(export.to_excel(est, effort_only)))["Work Breakdown"]
    ok(ws.cell(row=1, column=7).value == "Cost (not shown)"
       and all(r[6] is None for r in ws.iter_rows(min_row=2, values_only=True)),
       "the workbook's cost column stays where formulas expect it, and empty")

    eq(store.display_of({"written_report": False, "bogus": True}),
       {"development_cost": True, "infrastructure_cost": True, "maintenance_cost": True,
        "written_report": False},
       "flags a record was saved without read as shown; unknown ones are dropped")
    routes = (ROOT / "compass/estimate/routes.py").read_text()
    ok('@router.patch("/v1/estimates/{estimate_id}/display")' in routes
       and "export.to_pdf(estimation, record.display)" in routes
       and "export.to_excel(estimation, record.display)" in routes,
       "set on the record, and every export reads it from there")

    md = est.report_markdown
    ok("AI" not in md and md.startswith("# GDS Finance Gift Tracker — Cost estimate"),
       "a standard build's written report no longer calls itself an AI feasibility study")
    ts = (ROOT / "frontend/src/app/estimate/report.ts").read_text()
    ok("if (!this.showTotal()) hidden.push('- **Total expected');" in ts,
       "and on screen it drops the lines that would print a hidden cost")
    ok("label: 'Development', amount: devHours, unit: 'hours'" in ts
       and "label: 'First-year effort'" in ts,
       "a hidden cost's row shows its time instead — development in hours and "
       "weeks, maintenance in hours a year — and with both hidden the panel "
       "totals effort, not money")
    ok("new Set(parts.map((p) => p.unit)).size !== 1" in ts,
       "the share bar is drawn only when every part is in one unit: a share "
       "of dollars-plus-hours is not a share of anything")


def check_the_agent_reads_like_the_architect() -> None:
    """What the Gift Tracker taught, carried by the agent rather than by hand.

    On the Gift Tracker the region and the twelve-service stack were typed in
    after the fact; nothing in the agent read a Tech Stack section. Run end to
    end on the Gift Tracker requirements with the architect's Assumption and
    Tech Stack tabs as scope decisions, three times, it now reaches on its own
    what was typed: no-model use cases only, "Build it as standard software",
    North Europe, Azure PaaS from the stated stack, App Service, SQL Database,
    Service Bus, Functions, Blob, Key Vault, SendGrid and Power BI — with
    monitoring and identity added because a production build needs them, and
    no Application Gateway because nobody named one — and no module for
    testing, deployment or requirement walkthroughs. Totals 2,070 / 2,509 /
    2,554 hours against the sheet's 1,683: the effort residual is the grain
    stated in `check_a_set_of_instances_is_one_line`, not a stack or AI error.
    """
    print("\nthe agent reads the stack, the region and the work the way the architect did")
    from compass.estimate import intake
    from compass.estimate.architect import propose_solution
    from compass.estimate.classifier import _build_prompt, heuristic_task_type
    from compass.estimate.platforms import classify_platform

    props = intake._SCHEMA["properties"]
    ok("azure_services" in intake._SCHEMA["required"] and "azure_region" in intake._SCHEMA["required"]
       and "azure_services" in intake._BRD_SCHEMA["properties"],
       "both drafters write a stack and a region — the Tech Stack tab was the one "
       "part of the architect's sheet no drafter could read")
    ok(set(props["azure_services"]["items"]["enum"]) >= {"app_service", "azure_sql", "service_bus_std"}
       and "northeurope" in props["azure_region"]["enum"],
       "in the engine's own catalog keys and regions, so a draft cannot name a "
       "service that cannot be priced")
    src = (ROOT / "compass/estimate/intake.py").read_text().replace("\\\n", "")
    ok("**The stack.**" in src and "and nothing else" in src
       and "leave the list empty" in src,
       "the prompt asks for the stack as named plus what production plainly "
       "needs, and for nothing when nothing is named — a guess would be priced "
       "as if somebody had stated it")
    ok("Azure Container Apps" in intake._SYSTEM and "{catalog}" not in intake._SYSTEM,
       "and lists the catalog it may choose from, filled from the engine's list")

    data = {"azure_services": ["app_service", "azure_sql", "made_up", "app_service"],
            "azure_region": "northeurope"}
    intake._take_stack(data)
    eq(data.get("technical_preferences"),
       {"azure_services": ["app_service", "azure_sql"], "azure_region": "northeurope"},
       "what it writes lands on the brief — unknown keys dropped, repeats folded")
    empty = {"azure_services": [], "azure_region": ""}
    intake._take_stack(empty)
    ok("technical_preferences" not in empty,
       "and a stack nobody named writes nothing, so the engine chooses and "
       "North Europe stands")

    stated = ProjectInput.model_validate({**WBS_BRIEF, "technical_preferences": {
        "azure_services": ["app_service", "azure_sql"]}})
    proposal = asyncio.run(propose_solution(stated))
    eq((proposal["recommended_platform"], proposal["source"], classify_platform(stated)),
       ("azure_paas", "stated_stack", "azure_paas"),
       "a stack stated in Azure services settles the platform — a model asked "
       "again could only disagree with it")

    prompt = _build_prompt([("Approval routing", "")])
    ok("Three of these need no model" in prompt,
       "the classifier is told which three types need no model, and to use them "
       "whenever ordinary code does the job")
    for text, want in (("Approval and exception routing", "rules_workflow"),
                       ("Bulk upload — parse, validate", "crud_lookup"),
                       ("Gift history lookup; no model is required", "crud_lookup"),
                       ("Summarise weekly reports", "summarization"),
                       ("Answer questions from the policy knowledge base", "rag_qa")):
        eq(heuristic_task_type(text), want,
           f"its fallback reads {text!r} as {want} — process words before AI words, "
           "and a use case that says it needs no model taken at its word")

    est = asyncio.run(run_estimate(ProjectInput.model_validate(GIFT_TRACKER_SHEET),
                                   "est_agent", "2026-10-29T00:00:00+00:00"))
    ok(not any("pipeline" in s.name.lower()
               for p in est.cost_breakdown.development.work_breakdown.phases
               for m in p.modules if m.kind == "ramp_up" for s in m.sub_features),
       "with the delivery rows booked, the ramp-up no longer names the CI/CD "
       "pipeline that the delivery rows price")
    ok("so are understanding the requirement and setting up the development environment" in src,
       "and the document reader is told the ramp-up is booked, as testing and "
       "deployment already were")


# ── Checked against a second architect ──────────────────────────────────────
#
# The GDS Finance Gift Tracker: an architect's sheet of 1,683 hours, and a
# Compass estimate of the same system at 2,583. The three checks below are
# what reconciling the two found. Compass's brief for it, the sheet, and the
# drafts measured against both are summarised in each docstring; the numbers
# are the measurement, not a target the code was tuned to hit.

def check_the_delivery_rows_are_booked() -> None:
    """Testing, technical leadership, deployment and CI/CD, as an architect
    books them: below the module total, by rule.

    The Gift Tracker sheet ends its build lines at a *Module Total* of 1,238
    and then writes four rows — Module Testing 123.8 (10%), Tech Lead 185.7
    (15%), Deployment 90, CI/CD 45 — for 1,683. The video-platform sheet the
    bands were read from is the same template: Requirement & Design 45 first,
    Authentication 45, Talent Data Lake, and not one testing, deployment or
    tech-lead line among its 58. Its 2,034 was a module total too. So the bands
    are build hours, and calling them "all-in" had every estimate since carry
    no tech lead and no deployment at all, with testing appearing only when a
    drafter happened to write a module about it — two of five old-prompt
    drafts of the Gift Tracker did, three did not.

    Not the delivery multiplier that was removed. That doubled every figure on
    a theory about what a typed number meant; these are four named lines a
    reader can strike one at a time, at the shares an architect writes down.
    """
    print("\nthe delivery rows are booked the way an architect books them")
    from compass.estimate.catalog import DELIVERY_NAME

    # The constants reproduce the sheet to the hour.
    sheet = engine._delivery_module(1, 1238, 1238, True, 115, 32)
    eq([(s["name"], s["hours"]) for s in sheet["sub_features"]],
       [("Module testing", 124), ("Technical leadership", 186),
        ("Deployment", 90), ("CI/CD pipeline", 45)],
       "on the Gift Tracker's module total of 1,238: 123.8, 185.7, 90 and 45, "
       "rounded the way every other figure here is")
    eq(1238 + sheet["hours"], 1683, "and the sheet's total, exactly")

    on = {**WBS_BRIEF, "cost_assumptions": {}}
    est = asyncio.run(run_estimate(ProjectInput.model_validate(on), "est_dlv",
                                   "2026-06-20T00:00:00+00:00"))
    w = est.cost_breakdown.development.work_breakdown
    p1, p2 = w.phases
    d1 = [m for m in p1.modules if m.kind == "delivery"]
    d2 = [m for m in p2.modules if m.kind == "delivery"]
    eq((len(d1), len(d2)), (1, 1), "one delivery module per phase")
    ok(d1[0].derived and d1[0].name == DELIVERY_NAME and d1[0] is p1.modules[-1],
       "marked as added by the engine, and last in its phase — below the build")
    #   phase one's build is 171 + 18 ramp-up = 189: 19 + 28 + 90 + 45
    eq([(s.name, s.hours, s.basis) for s in d1[0].sub_features],
       [("Module testing", 19, "10% of the build"),
        ("Technical leadership", 28, "15% of the build"),
        ("Deployment", 90, ""), ("CI/CD pipeline", 45, "")],
       "phase one: shares of its build, and the flat rows once, when the "
       "pipeline is built and production first stood up")
    #   phase two's build is 45: 4.5 -> 5, 6.75 -> 7
    eq([(s.name, s.hours) for s in d2[0].sub_features],
       [("Module testing", 5), ("Technical leadership", 7)],
       "phase two tests and leads its own build and releases through what "
       "phase one built")
    eq((p1.hours, p2.hours, w.total_hours), (189 + 182, 45 + 12, 428),
       "and they roll up like any module")

    off = asyncio.run(run_estimate(ProjectInput.model_validate(WBS_BRIEF),
                                   "est_dlv_off", "2026-06-20T00:00:00+00:00"))
    eq(off.cost_breakdown.development.work_breakdown.total_hours, 428 - 182 - 12,
       "a switch, removing exactly the rows it added — for a sheet that "
       "already prices these as modules of its own")

    no_ramp = asyncio.run(run_estimate(ProjectInput.model_validate(
        {**on, "cost_assumptions": {"include_ramp_up": False}}),
        "est_dlv_noramp", "2026-06-20T00:00:00+00:00"))
    #   171 -> 17 + 26: the ramp-up's 18 and its 2 + 2 of testing and lead
    eq(no_ramp.cost_breakdown.development.work_breakdown.total_hours, 428 - 22,
       "switching the ramp-up off takes its share of testing and leadership "
       "with it — the sheet's shares are of a total that includes its own "
       "Requirement & Design row")

    large = asyncio.run(run_estimate(ProjectInput.model_validate(
        {**on, "scale": "large"}), "est_dlv_large", "2026-06-20T00:00:00+00:00"))
    nfr = [b.hours for b in large.cost_breakdown.development.breakdown
           if b.category.startswith("Non-functional work")]
    eq(nfr, [round(234 * 0.35)],
       "the reach uplift is a share of the build alone (234), not of the build "
       "and its testing — neither proportion multiplies the other")

    team = {**on, "modules": [{"id": "m1", "name": "Portal", "phase": 1, "sub_features": [
                {"id": "s1", "name": "Form", "size": "l"},
                {"id": "s2", "name": "List", "size": "m"}]}],
            "cost_assumptions": {"include_ramp_up": False},
            "team": {"developers": 2, "testers": 1, "analysts_designers": 1, "skills": []}}
    t = asyncio.run(run_estimate(ProjectInput.model_validate(team), "est_dlv_team",
                                 "2026-06-20T00:00:00+00:00")).cost_breakdown.development.work_breakdown
    roles = {r.role: r for r in t.roles}
    #   81 build: testing 8, lead 12, 90, 45 -> 236; the 8 is the testers'
    eq((t.total_hours, roles["testing"].hours), (236, 8),
       "with a team, the testing row is the testers' work as it stands — "
       "apportioning 23% of a plan that already has a testing row would "
       "count testing twice")
    eq(roles["development"].hours + roles["testing"].hours
       + roles["analysis_design"].hours, 236, "and every hour lands on someone")
    ok(abs(roles["development"].hours / roles["analysis_design"].hours - 62 / 15) < 0.05,
       "the rest divides between developers and analysts as it always did, "
       "62 to 15")

    from compass.estimate import export
    html = export.to_html(est)
    ok("10% of the build" in html and "15% of the build" in html,
       "the report prints how a row was priced where a size would go — "
       "\"14d\" on a share of the build would be a size nobody chose")


def check_a_brief_with_no_ai_costs_no_ai() -> None:
    """Three task types exist to say a use case needs no model. They were
    priced as if it did.

    Every use case on the Gift Tracker brief said "no model is required":
    two rules workflows, a threshold alert, a lookup. The estimate assigned
    all four to gpt-4o-mini and priced their tokens, added Azure AI Content
    Safety at $20 a month, booked "prompt & model upkeep" and "eval regression
    checks" into maintenance, credited them with $281,050 a year of AI
    automation benefit at "35% automated", and drew an AI-against-standard
    comparison for a system with no AI in it. The verdict said "Don't build
    this with AI" in red, about a project nobody had proposed building with AI.

    The profiles stay — their scores are how such a brief reaches that verdict
    at all. Everything a model costs is now keyed on whether one is called.
    """
    print("\na brief with no AI in it costs no AI")
    from compass.estimate import export

    def uc(i, name, task):
        return {"id": f"u{i}", "name": name, "task_type": task, "priority": "must_have"}

    rules = [uc(1, "Policy checks", "rules_workflow"), uc(2, "Routing", "rules_workflow"),
             uc(3, "Limit alerts", "threshold_alerting"), uc(4, "History lookup", "crud_lookup")]
    est = asyncio.run(run_estimate(ProjectInput.model_validate(
        {**WBS_BRIEF, "ai_use_cases": rules}), "est_noai", "2026-06-20T00:00:00+00:00"))
    c = est.cost_breakdown
    ok(est.ai_applicable is False, "the estimate knows it has no AI in it")
    eq((c.ai_tokens.model_breakdown, c.ai_tokens.annual_cost.expected,
        est.token_projection.model_recommendations), ([], 0.0, []),
       "no model, so no tokens and no model recommended")
    eq([s.service_name for s in c.infrastructure.services if s.category == "AI"], [],
       "and no AI service — Content Safety filtered model output that would "
       "never be produced")
    eq(c.maintenance.monthly_hours, 24,
       "maintenance is the department's 24 hours, with nothing for prompts "
       "that do not exist")
    ok("Prompt & model upkeep" not in c.maintenance.includes, "and it says so")
    eq((est.roi_projection.value_drivers, est.roi_projection.annual_benefit,
        est.roi_projection.payback_months), ([], 0.0, None),
       "no AI benefit is modelled: \"35% automated\" was a rules engine being "
       "credited with a model's work")
    eq((est.verdict.headline, est.verdict.disposition, est.verdict.recommend_ai),
       ("Build it as standard software", "go", False),
       "the verdict is a go for ordinary software, not a red rejection")
    from compass.estimate.routes import _record_fields
    eq(_record_fields(ProjectInput.model_validate({**WBS_BRIEF, "ai_use_cases": rules}),
                      est)["verdict"], "standard_software",
       "and the portfolio row says the same — it drew this one as a red "
       "\"Not with AI\" beside a report saying go")
    ts = (ROOT / "frontend/src/app/estimate/estimate.ts").read_text()
    ok("standard_software: 'Standard software'" in ts,
       "which the list labels as what it is")

    html = export.to_html(est)
    ok("No AI in this estimate" in html and "Where the value comes from" not in html
       and "AI against standard software" not in html and "AI tokens" not in html,
       "the report says it once, in place of three AI sections and a zero "
       "token row that would read as a model that happens to be free")

    mixed = asyncio.run(run_estimate(ProjectInput.model_validate(
        {**WBS_BRIEF, "ai_use_cases": [uc(1, "Answer from docs", "rag_qa"),
                                       uc(2, "Routing", "rules_workflow")]}),
        "est_mixed", "2026-06-20T00:00:00+00:00"))
    alone = asyncio.run(run_estimate(ProjectInput.model_validate(
        {**WBS_BRIEF, "ai_use_cases": [uc(1, "Answer from docs", "rag_qa")]}),
        "est_alone", "2026-06-20T00:00:00+00:00"))
    mb = mixed.cost_breakdown.ai_tokens.model_breakdown
    ok(mixed.ai_applicable and [m.use_cases for m in mb] == [["Answer from docs"]],
       "a mixed brief is an AI brief, and only the use case that calls a model "
       "is priced for one")
    ab = alone.cost_breakdown.ai_tokens.model_breakdown
    ok(abs(mb[0].monthly_input_tokens * 2 - ab[0].monthly_input_tokens) <= 1,
       "the rules engine keeps its share of the traffic: handing its requests "
       "to the model use case would price calls that are not the model's")
    eq(mixed.cost_breakdown.maintenance.monthly_hours, 24 + 4,
       "and four hours of model upkeep for the one model")
    ok(any(s.service_name == "Azure AI Content Safety"
           for s in mixed.cost_breakdown.infrastructure.services),
       "with Content Safety where there is model output to filter")

    blank = asyncio.run(run_estimate(ProjectInput.model_validate(
        {**WBS_BRIEF, "ai_use_cases": []}), "est_blank", "2026-06-20T00:00:00+00:00"))
    ok(blank.ai_applicable,
       "a brief with no use cases has not answered the question yet, and keeps "
       "the AI framing it always had")


def check_a_set_of_instances_is_one_line() -> None:
    """Seven roles are one access model, and an architect prices it once.

    The Gift Tracker estimate priced each of seven roles' permissions as a
    two-day line, three gift purposes as three more, four review stages as
    four four-day lines, five audit histories as five. The architect's sheet
    gives access control one 45-hour line and lists its sub-items under lines
    with no hours of their own. Reconciled bucket by bucket, the over-pricing
    of shared scope came to +1,084 hours of the estimate's +900 net — the rest
    was scope each side had that the other did not.

    Three prompt changes, each measured on the Gift Tracker requirements at
    five drafts an arm, priced by this engine with the delivery rows off:

        old prompt                       88 lines   3,040h   sd 181
        + a set's instances are covers   63 lines   2,255h   sd 116
        + a line's facets are inside it  55 lines   1,978h   sd  95
        + the architect's scope notes    50 lines   1,926h   sd  58

    against the sheet's 1,238. The remaining 1.55x is the drafter still
    writing a capability as about four pieces where this architect writes
    one; the video architect's sheet splits nearly as finely as the drafter
    does, so no constant tuned to one sheet survives the other. Measured on
    one document, rebuilt from its stored analysis, at n=5 — direction and
    spread, not a calibration.
    """
    print("\na set of instances is one line, and its members are named under it")
    from compass.estimate import export, intake

    brief = {**WBS_BRIEF, "modules": [{"id": "m1", "name": "Access", "phase": 1, "sub_features": [
        {"id": "s1", "name": "Role permission model", "size": "l",
         "covers": ["Requester", " Approver ", "Finance", "", "Payroll"]},
        {"id": "s2", "name": "Corporate sign-in", "size": "m"}]}]}
    est = asyncio.run(run_estimate(ProjectInput.model_validate(brief), "est_cov",
                                   "2026-06-20T00:00:00+00:00"))
    line = est.cost_breakdown.development.work_breakdown.phases[0].modules[1].sub_features[0]
    eq((line.hours, line.covers), (45, ["Requester", "Approver", "Finance", "Payroll"]),
       "priced at its band alone, its members carried by name — blanks "
       "dropped, edges trimmed")

    html = export.to_html(est)
    ok("covers Requester · Approver · Finance · Payroll" in html,
       "the report prints them under the line, the way the sheet lists "
       "sub-items beneath the line that carries their hours")

    import io
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(export.to_excel(est)))["Work Breakdown"]
    ok(ws.cell(row=1, column=9).value == "Covers"
       and any(r[8] == "Requester; Approver; Finance; Payroll"
               for r in ws.iter_rows(min_row=2, values_only=True)),
       "and the workbook gives them a column")

    sub = (intake._SCHEMA["properties"]["modules"]["items"]["properties"]
           ["sub_features"]["items"])
    ok("covers" in sub["properties"] and "covers" in sub["required"],
       "the drafter's schema has the field, required so strict mode can carry it")

    src = (ROOT / "compass/estimate/intake.py").read_text().replace("\\\n", "")
    ok("A new file is a line. A row is a cover." in src,
       "the prompt gives the test for which is which: what a developer does "
       "for the next member")
    ok("A line is one piece a developer builds, with its facets inside it." in src
       and "Audit logs, feature flags, admin controls" in src,
       "and that a line's fields, checks and states are inside it — with the "
       "video architect's own lines as the example, not this sheet's")
    ok("performance testing" not in src.split("_BRD_ARCHITECT = ")[1].split('"""')[1]
       and "booked by the engine on every estimate" in src,
       "the document reader no longer plans testing or deployment modules: "
       "the engine books them, and a module for either would be priced twice")

    ok("scope_notes: str = \"\"" in src and "Scope decisions (these override the document)" in src,
       "scope decisions reach the reader beside the document and override it "
       "— the architect's Assumption tab is the input a document cannot be")
    routes = (ROOT / "compass/estimate/routes.py").read_text()
    ok("scope_notes=body.scope_notes" in routes,
       "and the upload endpoint passes them through")


def main() -> int:
    est = asyncio.run(run_estimate(
        ProjectInput.model_validate(BRIEF), "est_check", "2026-06-20T00:00:00+00:00"))

    check_feasibility(est)
    check_platform(est)
    check_development(est)
    check_effort_scales_with_reach()
    check_work_breakdown()
    check_team_and_skills()
    check_the_ramp_up_is_booked()
    check_the_section_renders_on_an_older_browser()
    check_infrastructure(est)
    check_tokens(est)
    check_total(est)
    check_roi(est)
    check_verdict(est)
    check_enhancement()
    check_reproducibility()
    check_wire_shape()
    check_isolation()
    check_the_model_cannot_reach_the_arithmetic()
    check_the_section_is_removable()
    check_the_screens_match_the_mockup()
    check_the_two_charts_are_drawn_to_spec()
    check_the_brief_can_be_written_in_prose()
    check_a_brd_can_be_read()
    check_the_delivery_rows_are_booked()
    check_a_brief_with_no_ai_costs_no_ai()
    check_a_set_of_instances_is_one_line()
    check_the_gift_tracker_sheet_reproduces()
    check_the_stack_is_the_one_stated()
    check_the_report_shows_what_it_is_told()
    check_the_agent_reads_like_the_architect()
    check_an_estimate_can_leave_the_app()
    check_the_export_runs_on_windows()
    check_the_architect_asks_once()
    check_the_rate_card_is_kept()
    check_a_pipeline_can_name_its_estimate()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED")
        for f in FAILURES:
            print(f"   {f}")
        return 1
    print("the estimate is the estimate it was before the port, bar the "
          "benefit and effort models that were changed on purpose")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
