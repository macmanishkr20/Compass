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
        ("grid-template-columns: 1fr 120px 128px 116px 108px 96px",
         "the table's six columns are the mockup's, with verdict widened from "
         "104 to 120 — measured, the longest label needs 111px on one line"),
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
    ok("When the description names a set, enumerate its members." in joined,
       "and it says where small lines actually come from — naming a set's "
       "members separately, not shaving days off a big line. Over ten drafts "
       "an arm: xs 0.4% of lines to 17.2%, and the run-to-run spread halved")
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
    eq([m.name for m in p1.modules], ["Video pipeline"], "phase one's modules")
    subs = {s.name: s.hours for s in p1.modules[0].sub_features}
    #   l=5u=45h   xxl=10u=90h   m=4u=36h
    eq(subs["FFmpeg transcoding"], 45, "an L is five units")
    eq(subs["API creation and blob integration"], 90, "an XXL is ten — the ceiling")
    eq(subs["Player integration"], 36, "an M is four")

    eq(p1.modules[0].hours, 171, "a module is the sum of its sub-features")
    eq(p1.hours, 171, "and a phase the sum of its modules")
    eq(p2.hours, 45, "phase two")
    eq(w.total_hours, 216, "the plan is the sum of its phases")
    eq(w.total_cost, engine.jround(216 * 115), "priced at the delivery rate")
    ok(all(s.cost == engine.jround(s.hours * 115)
           for ph in w.phases for m in ph.modules for s in m.sub_features),
       "every line carries its own price, so a reader can strike one out and "
       "subtract rather than re-run the estimate")

    # A module is one engineer's job: 171/32 = 5.34 weeks. A phase is the team
    # working at once: 171/(3*32) = 1.78. Quoting one figure for both is how a
    # plan promises that six modules take as long as the longest of them.
    eq(w.team_size, 3, "the team size the elapsed weeks assume")
    eq(p1.modules[0].weeks, 5.34, "a module's weeks are one engineer's")
    eq(p1.weeks, 1.78, "a phase's are the team's")
    eq(w.total_weeks, round(1.78 + 0.47, 2),
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
    priced = direct.cost_breakdown.development.work_breakdown.phases[0].modules[0].sub_features
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
    eq(p1.modules[0].sub_features[1].hours, 90,
       "but the ceiling is reported, not applied — clamping a 120-hour line to "
       "90 would hide the one thing worth knowing about it, which is that "
       "nobody has broken it down")

    eq(sum(b.hours for b in dev.breakdown), 216,
       "development is the breakdown, at medium scale, with no multiplier "
       "between them")
    eq(dev.ai_integration_hours, 0,
       "and no separate AI-integration bucket: a breakdown names its AI work "
       "as line items, so adding a bucket on top would charge for it twice")

    eq(est.assumptions, WBS_BRIEF["assumptions"],
       "the assumptions travel with the estimate — they are what a "
       "disagreement about the number is usually actually about")


def main() -> int:
    est = asyncio.run(run_estimate(
        ProjectInput.model_validate(BRIEF), "est_check", "2026-06-20T00:00:00+00:00"))

    check_feasibility(est)
    check_platform(est)
    check_development(est)
    check_effort_scales_with_reach()
    check_work_breakdown()
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
    check_an_estimate_can_leave_the_app()
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
