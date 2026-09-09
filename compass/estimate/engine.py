"""Deterministic estimation engine — the Python twin of estimation.service.ts.

Every dollar shown to leadership is computed here, never by an LLM, so the
figures are reproducible. The functions return plain snake_case dicts that
validate cleanly into the Pydantic `Estimation` model.

Parity note: JavaScript `Math.round` is half-up; Python's built-in `round`
is banker's rounding. We therefore use `jround`/`round2` (floor(n + 0.5))
so the backend reproduces the TypeScript engine's output exactly.
"""

from __future__ import annotations

import math

from .azure_catalog import build_plan_context, calc_url_for
from .azure_pricing import get_unit_price
from .azure_planner import select_services
from .platforms import (
    PLATFORM_PROFILES,
    classify_platform,
    m365_services,
    onprem_services,
    platform_notes,
    provider_display,
)
from .catalog import (
    AGENTIC_INTEGRATION_UPLIFT,
    BENEFIT_RAMP_MONTHS,
    EFFORT_UNIT_HOURS,
    MAX_SUB_FEATURE_UNITS,
    SCALE_DELIVERY,
    SIZE_BAND_UNITS,
    AUTOMATION_RATE_PERCENT,
    DEV_HOURLY_RATE,
    HOURS_PER_DEV_WEEK,
    LOADED_HOURLY_RATE,
    MAINT_HOURLY_RATE,
    COMPLEXITY_HOURS,
    MODEL_CATALOG,
    PRIORITY_WEIGHT,
    SCALE_MAINT_HOURS,
    TASK_PROFILES,
    archetype_blurb,
    archetype_label,
    rating_for_score,
)
from .types import ProjectInput


# ── Parity helpers ──────────────────────────────────────────────────

def jround(n: float) -> int:
    """JS Math.round (half-up). All engine inputs are non-negative."""
    return math.floor(n + 0.5)


def round2(n: float) -> float:
    """Round to 2 decimals, half-up, matching Math.round(n*100)/100."""
    return math.floor(n * 100 + 0.5) / 100


def clamp(n: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, n))


def _locale(n: float) -> str:
    """Approximate JS Number.toLocaleString() (en-US) for the report copy."""
    if isinstance(n, float) and not n.is_integer():
        return f"{n:,.3f}".rstrip("0").rstrip(".")
    return f"{int(n):,}"


# ── Input accessors (mirror TS `?.` / `??` semantics) ──────────────

def _profile(task_type: str | None):
    return TASK_PROFILES.get(task_type or "rag_qa", TASK_PROFILES["rag_qa"])


def _data_gb(inp: ProjectInput) -> float:
    vs = inp.volume_and_scale
    return vs.data_volume_gb if (vs and vs.data_volume_gb is not None) else 10


def _requests_per_day(inp: ProjectInput) -> int:
    vs = inp.volume_and_scale
    return vs.requests_per_day if (vs and vs.requests_per_day is not None) else 1000


def _growth(inp: ProjectInput) -> float:
    vs = inp.volume_and_scale
    return vs.growth_rate_percent if (vs and vs.growth_rate_percent is not None) else 0


def _currency(inp: ProjectInput) -> str:
    tp = inp.technical_preferences
    return (tp.budget_currency if tp else None) or "USD"


def _compliance(inp: ProjectInput) -> list[str]:
    tp = inp.technical_preferences
    return tp.compliance_requirements if tp else []


def _rates(inp: ProjectInput) -> tuple[float, float, float]:
    """Resolve (dev_rate, maint_rate, effective_hours_per_week).

    An org's overrides win; otherwise we fall back to the catalog baselines, so
    an absent `cost_assumptions` block reproduces the platform defaults exactly.
    """
    ca = inp.cost_assumptions
    dev = ca.dev_hourly_rate if ca and ca.dev_hourly_rate is not None else DEV_HOURLY_RATE
    maint = ca.maint_hourly_rate if ca and ca.maint_hourly_rate is not None else MAINT_HOURLY_RATE
    hpw = ca.effective_hours_per_week if ca and ca.effective_hours_per_week is not None else HOURS_PER_DEV_WEEK
    return dev, maint, hpw


def _benefit_dials(inp: ProjectInput) -> tuple[float, float]:
    """Resolve (loaded_hourly_rate, automation_rate_percent) — org overrides win."""
    ca = inp.cost_assumptions
    loaded = ca.loaded_hourly_rate if ca and ca.loaded_hourly_rate is not None else LOADED_HOURLY_RATE
    auto = ca.automation_rate_percent if ca and ca.automation_rate_percent is not None else AUTOMATION_RATE_PERCENT
    return loaded, auto


def _team_size(inp: ProjectInput) -> float:
    ca = inp.cost_assumptions
    value = ca.team_size if ca and ca.team_size is not None else 3
    return max(1.0, float(value))


def _units_for(sub) -> int:
    """How many nine-hour units a sub-feature is.

    An explicit `units` wins over the band, so an architect who knows a job is
    six days can say six rather than round to the nearest band.

    The ceiling is *not* applied here. Clamping a big number down to the
    maximum would quietly shrink an estimate for the crime of being honest
    about a large piece of work — the same silent reinterpretation the delivery
    overhead used to perform in the other direction. A line at or above the
    ceiling is priced as written and reported in `at_ceiling`, which says what
    is actually true: it has not been broken down far enough to trust.
    """
    raw = sub.units if sub.units is not None else SIZE_BAND_UNITS.get(sub.size, 3)
    return max(1, int(raw))


def _band_for(units: int) -> str:
    """The size band `units` corresponds to, or nothing if none does.

    Explicit units win over the band everywhere else in the model, so they win
    over its name too: a brief sized in days — which is how the sheets this
    model came from are written — leaves `size` at its default, and a pill
    reading "S · 7d" contradicts itself. A count that falls between two bands
    gets no letter at all rather than the nearest one, because the days are the
    number that was priced and a band that does not mean them is noise.
    """
    for band, band_units in SIZE_BAND_UNITS.items():
        if band_units == units:
            return band
    return ""


def build_work_breakdown(inp: ProjectInput, dev_rate: float) -> dict:
    """Price the work breakdown: sub-feature, module, phase.

    Every hour comes from a line somebody named. A module's total is the sum of
    its sub-features and a phase's is the sum of its modules — there is no
    level at which a number appears that was not added up from the level below.

    Empty for a brief with no modules. Older records, and any client still
    posting the flat feature list, are priced by the path they were written
    for, at exactly the figures they always produced; they simply have no
    breakdown to show. Converting them into a synthetic one-module-per-feature
    breakdown was the first attempt and it was worse — it quantised every
    legacy figure to the nearest nine hours and clamped anything over the
    ceiling, so a 130-hour feature became 90 and an old estimate changed its
    mind about what it had cost.

    Two durations, and they answer different questions. A module's is elapsed
    weeks for one engineer, because a module is usually one person's job. A
    phase's is the same hours across the team, because that is what the team
    does at once. Quoting one number for both is how a plan ends up promising
    that six modules take as long as the longest of them.
    """
    hours_per_week = _rates(inp)[2]
    team = _team_size(inp)
    modules = inp.modules or []
    at_ceiling: list[str] = []

    by_phase: dict[int, list] = {}
    for i, module in enumerate(modules):
        subs = []
        for j, sub in enumerate(module.sub_features):
            units = _units_for(sub)
            hours = units * EFFORT_UNIT_HOURS
            if units >= MAX_SUB_FEATURE_UNITS:
                at_ceiling.append(f"{module.name} — {sub.name}")
            subs.append({
                "name": sub.name or f"Item {j + 1}",
                # The band that matches what was actually priced. A brief may
                # set `units` directly and leave `size` at its default, and a
                # pill reading "S · 7d" then contradicts itself — the band is
                # the label, and it has to label the number beside it.
                "size": _band_for(units),
                "units": units,
                "hours": hours,
                "cost": jround(hours * dev_rate),
            })
        module_hours = sum(s["hours"] for s in subs)
        by_phase.setdefault(max(1, module.phase), []).append({
            "name": module.name or f"Module {i + 1}",
            "phase": max(1, module.phase),
            "hours": module_hours,
            "cost": jround(module_hours * dev_rate),
            "weeks": round2(module_hours / hours_per_week) if hours_per_week else 0,
            "sub_features": subs,
        })

    phases = []
    for number in sorted(by_phase):
        mods = by_phase[number]
        phase_hours = sum(m["hours"] for m in mods)
        phases.append({
            "phase": number,
            "hours": phase_hours,
            "cost": jround(phase_hours * dev_rate),
            "weeks": round2(phase_hours / (team * hours_per_week)) if hours_per_week else 0,
            "modules": mods,
        })

    total_hours = sum(p["hours"] for p in phases)
    return {
        "phases": phases,
        "total_hours": total_hours,
        "total_cost": jround(total_hours * dev_rate),
        # Phases run one after another, so the plan is their sum.
        "total_weeks": round2(sum(p["weeks"] for p in phases)),
        "unit_hours": EFFORT_UNIT_HOURS,
        "team_size": team,
        "at_ceiling": at_ceiling,
    }


def _ramp_months(inp: ProjectInput) -> float:
    ca = inp.cost_assumptions
    value = ca.benefit_ramp_months if ca and ca.benefit_ramp_months is not None else BENEFIT_RAMP_MONTHS
    return max(0.0, float(value))


def _benefit_by(month: float, monthly_rate: float, ramp: float) -> float:
    """Benefit accumulated by `month`, under a linear adoption ramp.

    The rate climbs from zero at go-live to `monthly_rate` at `ramp`, so the
    total is the area under that line: a triangle while still ramping, then the
    triangle plus a rectangle. A ramp of 0 is the old behaviour — full rate
    from the first day — and is still reachable by setting the dial there.
    """
    if ramp <= 0:
        return monthly_rate * month
    if month <= ramp:
        return monthly_rate * month * month / (2 * ramp)
    return monthly_rate * (ramp / 2 + (month - ramp))


def _benefit_basis(uc, profile: dict, loaded_rate: float, automation_pct: float) -> dict:
    """Decompose one use case's ROI value into defensible, visible inputs.

    value/call = minutes ÷ 60 × loaded_hourly_rate × automation_rate%.
    A flat `value_per_call` override bypasses the derivation (and carries no
    decomposition); otherwise the per-call dollar is built from minutes saved.
    """
    if uc.value_per_call is not None:
        return {
            "value_per_call": uc.value_per_call,
            "minutes_per_call": None,
            "loaded_hourly_rate": None,
            "automation_rate_percent": None,
        }
    minutes = uc.minutes_per_call if uc.minutes_per_call is not None else profile["minutes_per_call"]
    value_per_call = round2(minutes / 60 * loaded_rate * (automation_pct / 100))
    return {
        "value_per_call": value_per_call,
        "minutes_per_call": minutes,
        "loaded_hourly_rate": loaded_rate,
        "automation_rate_percent": automation_pct,
    }


# ── Feasibility ─────────────────────────────────────────────────────

def compute_sub_scores(inp: ProjectInput) -> dict:
    use_cases = inp.ai_use_cases or []
    if len(use_cases) == 0:
        return {"ai_necessity": 18, "agentic_suitability": 12, "traditional_suitability": 86}

    w_sum = ai_n = agentic = trad = 0.0
    has_multi_agent = False
    for uc in use_cases:
        p = _profile(uc.task_type)
        w = PRIORITY_WEIGHT[uc.priority]
        w_sum += w
        ai_n += p["ai_n"] * w
        agentic += p["agentic"] * w
        trad += p["trad"] * w
        if uc.task_type == "multi_agent_orchestration":
            has_multi_agent = True
    ai_n /= w_sum
    agentic /= w_sum
    trad /= w_sum

    features = inp.features or []
    if features:
        ai_candidate_ratio = sum(1 for f in features if f.ai_candidate) / len(features)
    else:
        ai_candidate_ratio = 0.5
    ai_n += (ai_candidate_ratio - 0.5) * 16

    must_haves = sum(1 for u in use_cases if u.priority == "must_have")
    agentic += min(must_haves, 4) * 4
    if has_multi_agent:
        agentic += 8

    if len(use_cases) <= 1:
        trad += 10

    return {
        "ai_necessity": int(clamp(jround(ai_n))),
        "agentic_suitability": int(clamp(jround(agentic))),
        "traditional_suitability": int(clamp(jround(trad))),
    }


def pick_archetype(s: dict, use_cases: list) -> str:
    has_multi_agent = any(u.task_type == "multi_agent_orchestration" for u in use_cases)
    has_retrieval = any(u.task_type in ("rag_qa", "document_analysis") for u in use_cases)

    if s["ai_necessity"] < 35:
        return "traditional"
    if s["ai_necessity"] < 55:
        return "traditional_plus_ai"

    if s["agentic_suitability"] >= 72 and has_multi_agent:
        return "multi_agent"
    if s["agentic_suitability"] >= 58:
        return "single_agent"
    if has_retrieval:
        return "rag_assistant"
    if s["traditional_suitability"] >= 50:
        return "hybrid"
    return "rag_assistant"


def _archetype_rationale(a: str, s: dict) -> str:
    return (
        f"AI-necessity {s['ai_necessity']}, agentic-suitability {s['agentic_suitability']}, "
        f"traditional-suitability {s['traditional_suitability']}. {archetype_blurb(a)}"
    )


def _feasibility_rationale(s: dict, a: str) -> str:
    if a == "traditional":
        return (
            "The workload is well-defined and deterministic. AI would add operating cost "
            "and unpredictability without a clear accuracy or value gain."
        )
    if a == "multi_agent":
        return (
            "Multiple interdependent, multi-step tasks benefit from specialised agents "
            "coordinating — the value of autonomy outweighs the added orchestration cost."
        )
    return (
        f"A measured AI investment is justified here (necessity {s['ai_necessity']}/100), "
        "with the recommended pattern keeping run-cost proportional to the value delivered."
    )


def _build_risks(inp: ProjectInput, a: str) -> list[dict]:
    risks: list[dict] = []
    if a != "traditional":
        risks.append({
            "category": "Cost",
            "description": "Token spend can grow faster than usage if contexts or retries balloon.",
            "severity": "medium",
            "mitigation": "Set per-feature token budgets, cache, and alert on cost-per-request.",
        })
        risks.append({
            "category": "Quality",
            "description": "Model output variability may surface incorrect or inconsistent results.",
            "severity": "high",
            "mitigation": "Add an eval suite, human-in-the-loop on high-stakes paths, and guardrails.",
        })
    if a == "multi_agent":
        risks.append({
            "category": "Complexity",
            "description": "Multi-agent orchestration adds failure modes and debugging surface.",
            "severity": "high",
            "mitigation": "Start with the smallest agent set; add tracing and step-level retries.",
        })
    compliance = _compliance(inp)
    if compliance:
        risks.append({
            "category": "Compliance",
            "description": f"Data handling must satisfy: {', '.join(compliance)}.",
            "severity": "high",
            "mitigation": "Use private/regional model endpoints and data-residency-aware storage.",
        })
    if len(risks) == 0:
        risks.append({
            "category": "Scope",
            "description": "Requirements are deterministic; main risk is over-engineering.",
            "severity": "low",
            "mitigation": "Ship the standard build; revisit AI only with a measured use case.",
        })
    return risks


def _build_opportunities(a: str, use_cases: list) -> list[str]:
    if a == "traditional":
        return ["Bank the savings now; instrument the product to find a future AI use case with real signal."]
    ops = ["Phase the rollout: prove value on one use case before expanding."]
    if any(u.task_type == "rag_qa" for u in use_cases):
        ops.append("Reuse the retrieval layer across future assistant features.")
    if a in ("multi_agent", "single_agent"):
        ops.append("Capture agent traces to build an evaluation dataset over time.")
    ops.append("Negotiate committed-throughput pricing once volume stabilises.")
    return ops


def score_feasibility(inp: ProjectInput) -> dict:
    use_cases = inp.ai_use_cases or []
    sub = compute_sub_scores(inp)
    archetype = pick_archetype(sub, use_cases)
    composite = int(clamp(jround(
        0.55 * sub["ai_necessity"] + 0.3 * sub["agentic_suitability"] + 0.15 * (100 - sub["traditional_suitability"])
    )))

    use_case_analysis = []
    for uc in use_cases:
        p = _profile(uc.task_type)
        cx = "high" if p["agentic"] >= 70 else "medium" if p["agentic"] >= 40 else "low"
        task = uc.task_type or "rag_qa"
        use_case_analysis.append({
            "use_case_name": uc.name,
            "feasibility_score": int(clamp(jround(p["ai_n"] * 0.6 + p["agentic"] * 0.4))),
            "ai_task_type": task,
            "justification": (
                f"{uc.priority.replace('_', ' ', 1)} · best served by {p['model']} "
                f"given the {task.replace('_', ' ')} workload."
            ),
            "recommended_model": p["model"],
            "complexity": cx,
            "recommended_approach": "ai" if p["ai_n"] >= 50 else "standard",
        })

    return {
        "score": composite,
        "rating": rating_for_score(composite),
        "sub_scores": sub,
        "archetype": archetype,
        "archetype_label": archetype_label(archetype),
        "archetype_rationale": _archetype_rationale(archetype, sub),
        "rationale": _feasibility_rationale(sub, archetype),
        "use_case_analysis": use_case_analysis,
        "risks": _build_risks(inp, archetype),
        "opportunities": _build_opportunities(archetype, use_cases),
    }


# ── Tokens ──────────────────────────────────────────────────────────

def model_token_breakdown(inp: ProjectInput) -> list[dict]:
    use_cases = inp.ai_use_cases or []
    if len(use_cases) == 0:
        return []
    total_requests_per_day = max(_requests_per_day(inp), len(use_cases))

    weights = [PRIORITY_WEIGHT[u.priority] for u in use_cases]
    w_total = sum(weights)

    by_model: dict[str, dict] = {}
    for i, uc in enumerate(use_cases):
        p = _profile(uc.task_type)
        daily_req = (total_requests_per_day * weights[i]) / w_total
        monthly_req = daily_req * 30
        entry = by_model.setdefault(p["model"], {"use_cases": [], "in_tok": 0.0, "out_tok": 0.0})
        entry["use_cases"].append(uc.name)
        entry["in_tok"] += monthly_req * p["in_tokens"]
        entry["out_tok"] += monthly_req * p["out_tokens"]

    result = []
    for model, e in by_model.items():
        price = MODEL_CATALOG[model]
        monthly_cost = round2((e["in_tok"] / 1e6) * price["in_per_1m"] + (e["out_tok"] / 1e6) * price["out_per_1m"])
        result.append({
            "model": model,
            "use_cases": e["use_cases"],
            "monthly_input_tokens": jround(e["in_tok"]),
            "monthly_output_tokens": jround(e["out_tok"]),
            "monthly_cost": monthly_cost,
            "input_price_per_1m": price["in_per_1m"],
            "output_price_per_1m": price["out_per_1m"],
        })
    return result


def project_tokens(inp: ProjectInput) -> dict:
    breakdown = model_token_breakdown(inp)
    monthly = sum(m["monthly_input_tokens"] + m["monthly_output_tokens"] for m in breakdown)
    daily = monthly / 30

    def mk(base: float) -> dict:
        return {"optimistic": jround(base * 0.7), "expected": jround(base), "pessimistic": jround(base * 1.6)}

    use_cases = inp.ai_use_cases or []
    model_recommendations = []
    for uc in use_cases:
        p = _profile(uc.task_type)
        cat = MODEL_CATALOG[p["model"]]
        task = uc.task_type or "rag_qa"
        model_recommendations.append({
            "use_case": uc.name,
            "provider": cat["provider"],
            "recommended_model": p["model"],
            "rationale": f"{task.replace('_', ' ')} ≈ {p['in_tokens']}/{p['out_tokens']} in/out tokens per call.",
            "avg_input_tokens": p["in_tokens"],
            "avg_output_tokens": p["out_tokens"],
        })

    return {
        "daily": mk(daily),
        "monthly": mk(monthly),
        "annual": mk(monthly * 12),
        "model_recommendations": model_recommendations,
        "assumptions": [
            f"{_requests_per_day(inp)} requests/day at launch",
            f"{jround(_growth(inp)) if float(_growth(inp)).is_integer() else _growth(inp)}% projected growth",
            "Pessimistic scenario assumes 60% higher volume and longer contexts",
        ],
    }


# ── Infrastructure (Azure, live-priced) ─────────────────────────────

def compute_infrastructure(inp: ProjectInput, model_breakdown: list[dict], *, platform: str) -> dict:
    """Build the infrastructure cost lines for the project's delivery platform.

    Dispatches on the platform's cost *model*: consumption (Azure/AWS/GCP) reuses
    the catalog + planner; licensing (M365/Copilot) and capex (on-prem) build
    their own deterministic line items. The chosen platform is recorded so the
    report can explain why the cost is shaped the way it is.
    """
    monthly_token_cost = round2(sum(m["monthly_cost"] for m in model_breakdown))
    monthly_tokens = sum(m["monthly_input_tokens"] + m["monthly_output_tokens"] for m in model_breakdown)
    uses_azure_openai = any(
        MODEL_CATALOG[m["model"]]["provider"] == "Azure OpenAI" for m in model_breakdown
    )

    profile = PLATFORM_PROFILES.get(platform) or PLATFORM_PROFILES["azure_paas"]
    ctx = build_plan_context(
        inp,
        uses_azure_openai=uses_azure_openai,
        monthly_token_cost=monthly_token_cost,
        monthly_tokens=monthly_tokens,
    )

    if profile.cost_model == "consumption":
        services = _consumption_services(inp, ctx, profile)
    elif profile.key == "m365_copilot":
        services = m365_services(ctx)
    else:  # on_prem
        services = onprem_services(ctx)

    infra_monthly = jround(sum(s["monthly_cost"] for s in services if s["included_in_total"]))
    return {
        "monthly_cost": infra_monthly,
        "annual_cost": infra_monthly * 12,
        "services": services,
        "platform": profile.key,
        "platform_label": profile.label,
        "cost_model": profile.cost_model,
        "meters_tokens": profile.meters_tokens,
        "notes": platform_notes(profile.key, ctx),
    }


def _consumption_services(inp: ProjectInput, ctx, profile) -> list[dict]:
    """Catalog/planner-driven consumption lines (Azure/AWS/GCP).

    Azure keeps live Retail-Prices refinement and its per-service calculator
    links; AWS/GCP overlay the provider's service names and use the deterministic
    baselines (no live retail lookup for those providers yet). Azure OpenAI is
    listed but excluded from the subtotal — its tokens are already in the
    AI-tokens line — to avoid double counting.
    """
    is_azure = profile.provider == "azure"
    services: list[dict] = []
    for spec in select_services(inp, ctx):
        region = spec.region(ctx) if is_azure else profile.default_region
        qty = spec.quantity(ctx)
        baseline = spec.unit_price(ctx)

        if is_azure and spec.pricing == "metered":
            live = get_unit_price(spec, ctx, region)
            unit_price = live if live is not None else baseline
            source = "live" if live is not None else "fallback"
        elif spec.pricing == "estimate":
            unit_price, source = baseline, "estimate"
        else:  # tokens, or any metered service on a non-live provider
            unit_price, source = baseline, "fallback"

        name, url = provider_display(profile.provider, spec.key, spec.name, calc_url_for(spec))
        monthly = round2(unit_price * qty)
        services.append({
            "service_name": name,
            "category": spec.category,
            "tier": spec.tier(ctx),
            "region": region,
            "quantity": round2(qty),
            "unit": spec.unit,
            "unit_price": round2(unit_price),
            "monthly_cost": monthly,
            "price_source": source,
            "included_in_total": spec.included_in_total,
            "details": spec.details(ctx),
            "azure_pricing_url": url,
        })
    return services


# ── Cost ────────────────────────────────────────────────────────────

def compute_cost(inp: ProjectInput, feas: dict) -> dict:
    use_cases = inp.ai_use_cases or []
    currency = _currency(inp)
    dev_rate, maint_rate, _ = _rates(inp)

    # The work breakdown is the development estimate: one line per module in
    # the summary, and the sub-features underneath are what the line is made
    # of. A brief without modules falls back to the flat feature list it was
    # written with, priced exactly as before.
    wbs = build_work_breakdown(inp, dev_rate)
    if wbs["phases"]:
        feature_breakdown = [
            {"category": m["name"], "hours": m["hours"], "cost": m["cost"]}
            for p in wbs["phases"] for m in p["modules"]
        ]
    else:
        feature_breakdown = [
            {"category": f.name,
             "hours": COMPLEXITY_HOURS.get(f.complexity, 64),
             "cost": jround(COMPLEXITY_HOURS.get(f.complexity, 64) * dev_rate)}
            for f in (inp.features or [])
        ]

    # A separate AI-integration bucket only where the breakdown does not name
    # the AI work itself. With a real work breakdown it does — "subtitle
    # generation, 90 hours" is a line like any other — and adding a bucket on
    # top would charge for the same work twice. It stays for the flat-feature
    # path, where a feature called "Search" says nothing about the retrieval
    # pipeline underneath it.
    ai_integration_hours = 0.0
    for uc in (use_cases if not inp.modules else []):
        base = 80 if uc.priority == "must_have" else 48 if uc.priority == "nice_to_have" else 28
        # Graded on the agentic score rather than stepped at 70. The step put
        # retrieval Q&A (55) in the same bracket as text classification (20),
        # and retrieval has a corpus, a chunking strategy and an evaluation set
        # to build before anyone can say whether it works.
        cx = 1 + (_profile(uc.task_type)["agentic"] / 100) * AGENTIC_INTEGRATION_UPLIFT
        ai_integration_hours += base * cx
    if ai_integration_hours > 0:
        # Cost from the *rounded* hours, not from the raw sum. It was priced
        # off 161.6 while showing 162, so the line read "162 h · $18,584" —
        # a rate of $114.72 against a stated $115 — and the breakdown came to
        # $46 less than the development total it was breaking down. Small, and
        # exactly the kind of thing that makes a reader stop trusting a table.
        integration_hours = jround(ai_integration_hours)
        feature_breakdown.append({
            "category": "AI integration & evaluation",
            "hours": integration_hours,
            "cost": jround(integration_hours * dev_rate),
        })

    if inp.project_type == "enhancement":
        framework = (inp.current_architecture.framework if inp.current_architecture else "") or "existing app"
        integration_hours = jround(40 + len(use_cases) * 24)
        feature_breakdown.append({
            "category": f"Integrate with existing {framework}",
            "hours": integration_hours,
            "cost": jround(integration_hours * dev_rate),
        })

    # ── the one multiplier that is left ──────────────────────────────────
    #
    # There were two. The delivery overhead is gone: it doubled every entered
    # figure on the theory that the entered figure was code-and-unit-tests, and
    # nobody typing "45 hours for SSO" means that. An estimate now costs what
    # it says it costs.
    #
    # The scale uplift stays, because it prices work that is not a feature at
    # all: access control against a real directory, audit, disaster recovery,
    # load and failure testing, staged rollout. A pilot for one team does not
    # carry those and an organisation-wide rollout does, and no amount of
    # enumerating features finds them. It is a visible line, so it can be
    # argued with or set to zero by calling the reach smaller.
    scale = inp.scale or "medium"
    build_hours = sum(b["hours"] for b in feature_breakdown)

    scale_factor = SCALE_DELIVERY.get(scale, 1.0)
    scale_hours = jround(build_hours * (scale_factor - 1))
    if scale_hours:
        # Named by direction. A pilot genuinely carries fewer non-functional
        # demands than a department, so the figure is negative there and the
        # arithmetic is right — but a line reading "Non-functional work: −53
        # hours" reads like a defect rather than like a discount.
        label = (f"Non-functional work for {scale} reach" if scale_hours > 0
                 else "Pilot scope — fewer non-functional demands")
        feature_breakdown.append({
            "category": label,
            "hours": scale_hours,
            "cost": jround(scale_hours * dev_rate),
        })

    total_dev_hours = sum(b["hours"] for b in feature_breakdown)
    development_cost = jround(total_dev_hours * dev_rate)

    model_breakdown = model_token_breakdown(inp)
    monthly_token_cost = round2(sum(m["monthly_cost"] for m in model_breakdown))
    monthly_tokens = sum(m["monthly_input_tokens"] + m["monthly_output_tokens"] for m in model_breakdown)

    # The delivery platform selects the infrastructure cost *model*. On a
    # licensing platform (M365/Copilot) AI usage is bundled into the per-seat
    # Copilot add-on, so the metered token line is zeroed to avoid double counting
    # — token *volumes* are still reported for transparency.
    platform = classify_platform(inp)
    profile = PLATFORM_PROFILES.get(platform) or PLATFORM_PROFILES["azure_paas"]
    infrastructure = compute_infrastructure(inp, model_breakdown, platform=platform)
    infra_monthly = infrastructure["monthly_cost"]

    token_factor = 1.0 if profile.meters_tokens else 0.0
    token_cost = round2(monthly_token_cost * token_factor)
    token_breakdown = (
        model_breakdown
        if token_factor == 1.0
        else [{**m, "monthly_cost": 0.0} for m in model_breakdown]
    )

    maint_hours = SCALE_MAINT_HOURS.get(scale, 24) + len(use_cases) * 4
    maint_monthly = jround(maint_hours * maint_rate)

    annual_run = (infra_monthly + token_cost + maint_monthly) * 12
    expected = jround(development_cost + annual_run)

    return {
        "development": {
            "ai_integration_hours": jround(ai_integration_hours),
            "hourly_rate": dev_rate,
            "total_cost": development_cost,
            "breakdown": feature_breakdown,
            "work_breakdown": wbs,
        },
        "infrastructure": infrastructure,
        "ai_tokens": {
            "monthly_tokens": {"optimistic": jround(monthly_tokens * 0.7), "expected": jround(monthly_tokens), "pessimistic": jround(monthly_tokens * 1.6)},
            "monthly_cost": {"optimistic": round2(token_cost * 0.7), "expected": token_cost, "pessimistic": round2(token_cost * 1.6)},
            "annual_cost": {"optimistic": round2(token_cost * 0.7 * 12), "expected": round2(token_cost * 12), "pessimistic": round2(token_cost * 1.6 * 12)},
            "model_breakdown": token_breakdown,
        },
        "maintenance": {
            "monthly_hours": maint_hours,
            "hourly_rate": maint_rate,
            "monthly_cost": maint_monthly,
            "annual_cost": maint_monthly * 12,
            "includes": ["Prompt & model upkeep", "Monitoring & cost guardrails", "Eval regression checks", "Dependency updates"],
        },
        "total": {"min": jround(expected * 0.82), "expected": expected, "max": jround(expected * 1.35)},
        "currency": currency,
    }


# ── AI vs Standard comparison ───────────────────────────────────────

def compare_approaches(inp: ProjectInput, feas: dict, cost: dict) -> dict:
    dev_rate, _, hours_per_week = _rates(inp)
    ai_total = cost["total"]
    ai_monthly_run = cost["infrastructure"]["monthly_cost"] + cost["ai_tokens"]["monthly_cost"]["expected"] + cost["maintenance"]["monthly_cost"]
    ai_dev_hours = sum(b["hours"] for b in cost["development"]["breakdown"])
    ai_team = max(2, math.ceil(ai_dev_hours / (hours_per_week * 8)))
    ai_weeks = max(4, jround(ai_dev_hours / (hours_per_week * ai_team)))

    reliance = feas["sub_scores"]["ai_necessity"] / 100
    std_dev_hours = jround(ai_dev_hours * (0.85 + reliance * 0.8))
    std_dev_cost = jround(std_dev_hours * dev_rate)
    std_monthly_run = jround(cost["infrastructure"]["monthly_cost"] * 0.5 + cost["maintenance"]["monthly_cost"] * 0.8)
    std_team = max(2, math.ceil(std_dev_hours / (hours_per_week * 8)))
    std_weeks = max(4, jround(std_dev_hours / (hours_per_week * std_team)))
    std_expected = jround(std_dev_cost + std_monthly_run * 12)

    recommendation = "ai" if feas["score"] >= 55 else "hybrid" if feas["score"] >= 40 else "standard"
    # A genuinely mixed product — some capabilities AI-led, some standard-led — is
    # "hybrid" by definition. Don't let a borderline composite brand it all-standard
    # while the per-capability split still shows an AI-led feature (the two would
    # otherwise contradict each other in the report).
    approaches = [u["recommended_approach"] for u in feas["use_case_analysis"]]
    is_mixed = "ai" in approaches and "standard" in approaches
    if is_mixed and recommendation == "standard" and feas["score"] >= 30:
        recommendation = "hybrid"

    dimensions = [
        {"dimension": "Time to market", "ai_score": int(clamp(jround(5 + reliance * 4), 0, 10)), "standard_score": int(clamp(jround(8 - reliance * 3), 0, 10)), "notes": "AI accelerates ambiguous tasks; standard is faster for well-specified ones."},
        {"dimension": "Accuracy on fuzzy input", "ai_score": int(clamp(jround(4 + reliance * 5), 0, 10)), "standard_score": int(clamp(jround(8 - reliance * 5), 0, 10)), "notes": "Unstructured input favours AI."},
        {"dimension": "Run cost", "ai_score": int(clamp(jround(9 - reliance * 4), 0, 10)), "standard_score": 9, "notes": "Tokens add ongoing cost the standard build avoids."},
        {"dimension": "Scalability", "ai_score": 8, "standard_score": 7, "notes": "Both scale on Azure; AI adds token-budget management."},
        {"dimension": "Maintainability", "ai_score": int(clamp(jround(7 - reliance * 2), 0, 10)), "standard_score": 7, "notes": "Prompt/model drift needs evals; rules need manual upkeep."},
        {"dimension": "Flexibility", "ai_score": int(clamp(jround(6 + reliance * 4), 0, 10)), "standard_score": int(clamp(jround(6 - reliance * 2), 0, 10)), "notes": "AI adapts to new cases with less re-coding."},
    ]

    if recommendation == "ai":
        summary = "AI delivers materially more value here than a standard build, and the run-cost premium is justified."
    elif recommendation == "hybrid":
        summary = "A hybrid split — AI on the fuzzy parts, standard software elsewhere — gives the best cost/value balance."
    else:
        summary = "A standard build meets the requirement at lower total cost; reserve AI for a later, targeted phase."

    return {
        "ai_approach": {
            "total_cost": ai_total,
            "timeline_weeks": ai_weeks,
            "team_size": ai_team,
            "benefits": ["Handles unstructured & ambiguous input", "Faster to adapt to new cases", "Higher ceiling on automation"],
            "challenges": ["Ongoing token cost", "Needs evaluation & guardrails", "Output variability to manage"],
            "monthly_run_cost": jround(ai_monthly_run),
        },
        "standard_approach": {
            "total_cost": {"min": jround(std_expected * 0.85), "expected": std_expected, "max": jround(std_expected * 1.3)},
            "timeline_weeks": std_weeks,
            "team_size": std_team,
            "benefits": ["Predictable, testable behaviour", "No per-request token cost", "Simpler compliance story"],
            "challenges": ["Brittle on unstructured input", "More manual rules to maintain", "Lower automation ceiling"],
            "monthly_run_cost": std_monthly_run,
        },
        "summary": summary,
        "recommendation": recommendation,
        "recommendation_rationale": feas["archetype_rationale"],
        "dimensions": dimensions,
    }


# ── ROI projection (deterministic payback + savings curve) ──────────

def project_roi(inp: ProjectInput, feas: dict, cost: dict) -> dict:
    use_cases = inp.ai_use_cases or []
    development_cost = cost["development"]["total_cost"]
    ai_monthly_run = (
        cost["infrastructure"]["monthly_cost"]
        + cost["ai_tokens"]["monthly_cost"]["expected"]
        + cost["maintenance"]["monthly_cost"]
    )
    annual_run_cost = round2(ai_monthly_run * 12)

    loaded_rate, automation_pct = _benefit_dials(inp)
    value_drivers = []
    annual_benefit = 0.0
    if use_cases:
        total_requests_per_day = max(_requests_per_day(inp), len(use_cases))
        weights = [PRIORITY_WEIGHT[u.priority] for u in use_cases]
        w_total = sum(weights)
        for i, uc in enumerate(use_cases):
            p = _profile(uc.task_type)
            daily_req = (total_requests_per_day * weights[i]) / w_total
            annual_calls = jround(daily_req * 365)
            basis = _benefit_basis(uc, p, loaded_rate, automation_pct)
            annual_value = round2(annual_calls * basis["value_per_call"])
            annual_benefit += annual_value
            value_drivers.append({
                "use_case": uc.name,
                **basis,
                "annual_calls": annual_calls,
                "annual_value": annual_value,
            })
    annual_benefit = round2(annual_benefit)

    net_annual_benefit = round2(annual_benefit - annual_run_cost)

    # Benefit ramps; cost does not. You pay for the infrastructure and the
    # engineers from the first month, while adoption climbs — which is the
    # whole reason a payback figure is worth computing rather than dividing.
    ramp = _ramp_months(inp)
    monthly_benefit = annual_benefit / 12
    first_year_benefit = round2(_benefit_by(12, monthly_benefit, ramp))

    def cumulative_net(month: float) -> float:
        return (_benefit_by(month, monthly_benefit, ramp)
                - ai_monthly_run * month
                - development_cost)

    # Solved by walking the curve rather than by dividing, because with a ramp
    # there is no closed form worth the trouble and a tenth of a month is finer
    # than any of the inputs deserve. Sixty months: past five years a payback
    # figure is not the number anyone is actually deciding on.
    payback_months: float | None = None
    if monthly_benefit > 0:
        for step in range(1, 601):
            month = step / 10
            if cumulative_net(month) >= 0:
                payback_months = round2(month)
                break

    total_investment_3yr = development_cost + annual_run_cost * 3
    total_benefit_3yr = _benefit_by(36, monthly_benefit, ramp)
    three_year_value = round2(total_benefit_3yr - total_investment_3yr)
    roi_percent = jround(three_year_value / total_investment_3yr * 100) if total_investment_3yr > 0 else 0

    curve = [
        {"month": m, "cumulative_net": round2(cumulative_net(m))}
        for m in range(0, 37, 3)
    ]

    return {
        "annual_benefit": annual_benefit,
        "annual_run_cost": annual_run_cost,
        "development_cost": development_cost,
        "net_annual_benefit": net_annual_benefit,
        "first_year_benefit": first_year_benefit,
        "benefit_ramp_months": ramp,
        "payback_months": payback_months,
        "three_year_value": three_year_value,
        "roi_percent": roi_percent,
        "curve": curve,
        "value_drivers": value_drivers,
        "assumptions": [
            "Benefit/call = minutes saved ÷ 60 × loaded labour rate × automation rate.",
            f"Loaded labour rate ${loaded_rate:,.0f}/hr; automation rate {automation_pct:.0f}% of calls handled end-to-end.",
            (f"Benefit ramps linearly over {ramp:.0f} months to full adoption; "
             "run cost is paid in full from month one."
             if ramp > 0 else
             "No adoption ramp: benefit runs at full rate from go-live."),
            f"{_requests_per_day(inp)} requests/day at launch, distributed across use cases by priority.",
            "Run cost mirrors the first-year operating total (infra + tokens + maintenance).",
            "Three-year view holds volume and pricing flat — no growth or discounting applied.",
            f"Year one delivers ${first_year_benefit:,.0f} of the ${annual_benefit:,.0f} steady-state annual benefit.",
        ],
    }


# ── Verdict (the decisive, willing-to-say-no call) ──────────────────

def derive_verdict(feas: dict, comparison: dict) -> dict:
    """Collapse the analysis into one actionable decision.

    Keyed on the AI-vs-standard recommendation (which already encodes the
    feasibility score) plus the traditional-only archetype guard, so the verdict
    never contradicts the comparison the rest of the report shows.
    """
    label = feas["archetype_label"]
    rec = comparison["recommendation"]  # "ai" | "hybrid" | "standard"

    if rec == "standard" or feas["archetype"] == "traditional":
        return {
            "decision": "do_not_use_ai",
            "headline": "Don't build this with AI",
            "one_liner": (
                "Standard software solves this at lower cost and risk; revisit AI "
                "only with a sharper, measured use case."
            ),
            "disposition": "stop",
            "recommend_ai": False,
        }
    if rec == "ai":
        return {
            "decision": "build_with_ai",
            "headline": "Build this with AI",
            "one_liner": f"A measured AI investment pays off here — build it as a {label}.",
            "disposition": "go",
            "recommend_ai": True,
        }
    return {
        "decision": "hybrid",
        "headline": "Take a hybrid approach",
        "one_liner": f"Use AI only where it clearly pays — a {label} blend beats going all-in or skipping it.",
        "disposition": "caution",
        "recommend_ai": True,
    }


# ── Confidence (deterministic self-assessment) ──────────────────────

# Decision lines the verdict keys on; we measure how decisively a score clears
# them (a score sitting on a boundary is fragile and lowers confidence).
_DECISION_THRESHOLDS = (40, 55)
_ARCHETYPE_THRESHOLDS = (35, 55)
_DECISIVE_MARGIN = 15  # distance from a threshold we treat as fully decisive


def _impact_for(ratio: float) -> str:
    if ratio >= 0.66:
        return "positive"
    if ratio >= 0.4:
        return "neutral"
    return "negative"


def assess_confidence(inp: ProjectInput, feas: dict) -> dict:
    """Score how much weight to place on this estimate (0–100, deterministic)."""
    # Factor A — requirements detail: are the descriptive inputs substantive?
    detail_signals = [
        len((inp.description or "").strip()) >= 30,
        len(inp.features or []) > 0,
        bool((inp.target_users or "").strip()),
        bool((inp.industry_domain or "").strip()),
    ]
    ratio_detail = sum(1 for s in detail_signals if s) / len(detail_signals)

    # Factor B — volume certainty: are the usage drivers given, or defaulted?
    vs = inp.volume_and_scale
    volume_signals = [
        bool(vs and vs.requests_per_day is not None),
        bool(vs and vs.data_volume_gb is not None),
        bool(vs and vs.expected_daily_users is not None),
        bool(vs and vs.growth_rate_percent is not None),
    ]
    ratio_volume = sum(1 for s in volume_signals if s) / len(volume_signals)

    # Factor C — decisiveness: how far the scores sit from the decision lines.
    score = feas["score"]
    ai_n = feas["sub_scores"]["ai_necessity"]
    dist_score = min(abs(score - t) for t in _DECISION_THRESHOLDS)
    dist_nec = min(abs(ai_n - t) for t in _ARCHETYPE_THRESHOLDS)
    ratio_decisive = (
        clamp(dist_score / _DECISIVE_MARGIN, 0, 1) + clamp(dist_nec / _DECISIVE_MARGIN, 0, 1)
    ) / 2

    # Factor D — grounding: an analyzed codebase beats a greenfield guess.
    grounded = inp.project_type == "enhancement" and inp.current_architecture is not None
    ratio_ground = 1.0 if grounded else 0.5

    raw = 100 * (
        0.30 * ratio_detail + 0.30 * ratio_volume + 0.30 * ratio_decisive + 0.10 * ratio_ground
    )
    score_out = int(clamp(jround(raw), 0, 100))
    level = "high" if score_out >= 70 else "medium" if score_out >= 45 else "low"

    factors = [
        {
            "label": "Requirements detail",
            "detail": (
                "Project, features and audience are well described."
                if ratio_detail >= 0.66
                else "Some descriptive inputs are thin — add features and context to sharpen the build estimate."
            ),
            "impact": _impact_for(ratio_detail),
        },
        {
            "label": "Volume certainty",
            "detail": (
                "Usage volumes were specified, anchoring the token and ROI math."
                if ratio_volume >= 0.66
                else "Several usage figures fall back to defaults, so run-cost and ROI are indicative."
            ),
            "impact": _impact_for(ratio_volume),
        },
        {
            "label": "Recommendation margin",
            "detail": (
                "The feasibility score sits clear of the decision thresholds."
                if ratio_decisive >= 0.66
                else "The feasibility score is near a decision threshold; small input changes could shift the call."
            ),
            "impact": _impact_for(ratio_decisive),
        },
        {
            "label": "Grounding",
            "detail": (
                "Grounded in an analyzed existing codebase."
                if grounded
                else "Greenfield estimate with no existing system to measure against."
            ),
            "impact": _impact_for(ratio_ground),
        },
    ]

    rationale = {
        "high": (
            "Inputs are detailed and the recommendation sits clear of the decision "
            "thresholds, so these figures are dependable for planning."
        ),
        "medium": (
            "The core inputs are present but some assumptions rely on defaults; treat "
            "the figures as directional and firm up the weak spots."
        ),
        "low": (
            "Several inputs are missing or the recommendation is near a decision "
            "boundary; gather more detail before committing budget."
        ),
    }[level]

    return {"level": level, "score": score_out, "rationale": rationale, "factors": factors}


# ── Recommendations & report ────────────────────────────────────────

def build_recommendations(inp: ProjectInput, feas: dict) -> list[dict]:
    recs = [{
        "priority": "high",
        "category": "Approach",
        "title": f"Build as: {feas['archetype_label']}",
        "description": feas["archetype_rationale"],
        "estimated_impact": "Sets the cost and complexity envelope for the whole project.",
    }]
    if feas["archetype"] != "traditional":
        recs.append({
            "priority": "high",
            "category": "FinOps",
            "title": "Instrument cost-per-request from day one",
            "description": "Tag every model call with a use case and surface $/request in a dashboard.",
            "estimated_impact": "Keeps token spend predictable and prevents budget surprises.",
        })
        recs.append({
            "priority": "medium",
            "category": "Quality",
            "title": "Stand up an evaluation harness early",
            "description": "A small labelled set + automated scoring catches regressions before users do.",
            "estimated_impact": "Reduces production incidents and rework.",
        })
    recs.append({
        "priority": "medium",
        "category": "Delivery",
        "title": "Ship a thin vertical slice first",
        "description": "One use case, end to end, in front of real users before scaling breadth.",
        "estimated_impact": "De-risks the estimate with real usage data.",
    })
    return recs


def compose_markdown(inp: ProjectInput, feas: dict, cost: dict) -> str:
    currency = cost["currency"]

    def fmt(n: float) -> str:
        return f"{currency} {_locale(n)}"

    return "\n".join([
        f"# {inp.project_name} — AI Feasibility & Cost",
        "",
        f"**Recommendation:** {feas['archetype_label']} (feasibility {feas['score']}/100, {feas['rating']}).",
        "",
        feas["rationale"],
        "",
        "## Cost (first year)",
        f"- Development: {fmt(cost['development']['total_cost'])}",
        f"- Infrastructure: {fmt(cost['infrastructure']['annual_cost'])}/yr",
        f"- AI tokens: {fmt(cost['ai_tokens']['annual_cost']['expected'])}/yr (expected)",
        f"- Maintenance: {fmt(cost['maintenance']['annual_cost'])}/yr",
        f"- **Total expected: {fmt(cost['total']['expected'])}** (range {fmt(cost['total']['min'])}–{fmt(cost['total']['max'])})",
    ])


def _build_repo_context(inp: ProjectInput) -> dict | None:
    if inp.project_type != "enhancement" or not inp.current_architecture:
        return None
    a = inp.current_architecture
    return {
        "full_name": inp.repo_full_name or inp.repo_url or inp.project_name,
        "html_url": inp.repo_url or "",
        "branch": inp.repo_branch or "main",
        "primary_language": a.language,
        "stars": inp.repo_stars or 0,
        "file_count": inp.repo_file_count or 0,
        "architecture": a.model_dump(),
        "manifests_found": inp.repo_manifests or [],
        "topics": inp.repo_topics or [],
    }


# ── Top-level orchestrator ──────────────────────────────────────────

def estimate(inp: ProjectInput, est_id: str, generated_at: str) -> dict:
    """Run the full deterministic pipeline and return an Estimation dict."""
    feas = score_feasibility(inp)
    cost = compute_cost(inp, feas)
    tokens = project_tokens(inp)
    comparison = compare_approaches(inp, feas, cost)
    roi = project_roi(inp, feas, cost)
    verdict = derive_verdict(feas, comparison)
    confidence = assess_confidence(inp, feas)
    recommendations = build_recommendations(inp, feas)
    markdown = compose_markdown(inp, feas, cost)

    return {
        "id": est_id,
        "project_id": inp.project_name,
        "project_name": inp.project_name,
        "project_type": inp.project_type,
        "industry_domain": inp.industry_domain,
        "feasibility": feas,
        "verdict": verdict,
        "confidence": confidence,
        "cost_breakdown": cost,
        "token_projection": tokens,
        "comparison": comparison,
        "roi_projection": roi,
        "recommendations": recommendations,
        "report_markdown": markdown,
        "status": "complete",
        "generated_at": generated_at,
        "repo_context": _build_repo_context(inp),
    }
