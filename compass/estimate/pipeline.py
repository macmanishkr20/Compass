"""From a brief to an estimate — the eight stages, in order.

    classify → architect → feasibility → cost → tokens → compare → roi → report

Two of those may call a model, and both do the same narrow thing: resolve a
*label*. The classifier turns "detect dodgy invoices" into `anomaly_detection`;
the architect picks one of five delivery platforms. Both write their answer onto
the input, and everything after them is arithmetic over the resolved brief. That
is the whole design: a model can change what gets priced, never what a price is.

This arrived as a LangGraph `StateGraph` and does not leave as one. The graph
was a straight line with no branch, no condition and no parallel node — eight
edges declaring the order that eight sequential `await`s already declare, at the
cost of a dependency and a layer of indirection between a stage and the reason
it runs. The agentic variant is a different matter: its supervisor genuinely
routes at runtime, and if that comes over it will bring a graph with it, because
there it earns one.

`run_estimate` never raises for want of a model. Every stage that can degrade
does, so a box with no deployment configured still produces a full, defensible
estimate from the keyword rules and the platform heuristic — the same numbers,
reached without help.
"""

from __future__ import annotations

import logging
from typing import Iterator

from . import engine
from .architect import propose_solution
from .catalog import TASK_TYPES
from .classifier import classify_use_cases, heuristic_task_type
from .platforms import PLATFORM_PROFILES, classify_platform
from .types import Estimation, ProjectInput, TechnicalPreferences

logger = logging.getLogger("compass.estimate")

#: The stages, in the order they run, with the line each one shows while it is
#: the current one. The API streams these; the UI reads them. Kept here rather
#: than beside the endpoint because the sequence and the narration describing
#: it are the same fact, and two copies of one fact drift.
STAGES: list[tuple[str, str]] = [
    ("classify", "Reading the brief and labelling each use case…"),
    ("architect", "Choosing the delivery platform that fits…"),
    ("feasibility", "Scoring AI necessity, agentic and traditional fit…"),
    ("cost", "Costing development, infrastructure and tokens…"),
    ("tokens", "Projecting token volume and model mix…"),
    ("compare", "Comparing this against building it the standard way…"),
    ("roi", "Modelling benefit, payback and three-year value…"),
    ("report", "Composing the report…"),
]


async def resolve(inp: ProjectInput) -> tuple[ProjectInput, dict]:
    """The two label-resolving stages, applied to the brief.

    Returned rather than folded into `run_estimate` so a caller that wants to
    show the platform before paying for the costing can have it, and so the
    boundary between "a model was involved" and "arithmetic" stays a boundary
    you can point at.
    """
    inp = inp.model_copy(update={"ai_use_cases": await classify_use_cases(inp.ai_use_cases)})

    proposal = await propose_solution(inp)
    platform = proposal["recommended_platform"]
    tp = inp.technical_preferences
    resolved_tp = (
        tp.model_copy(update={"delivery_platform": platform})
        if tp is not None
        else TechnicalPreferences(delivery_platform=platform)
    )
    return inp.model_copy(update={"technical_preferences": resolved_tp}), proposal


def resolve_without_model(inp: ProjectInput) -> tuple[ProjectInput, dict]:
    """`resolve`, with both model stages taken at their deterministic fallback.

    Synchronous, and that is the whole point: the live preview beside the form
    cannot wait twenty seconds for a ReAct call on every keystroke. Blank task
    types fall to the keyword rules and the platform to `classify_platform` —
    which is exactly what happens when no model is configured, so a preview is
    never a *different* calculation, only the same one without the two label
    resolutions that might have improved its inputs.
    """
    classified = [
        uc if uc.task_type in TASK_TYPES
        else uc.model_copy(update={"task_type": heuristic_task_type(f"{uc.name} {uc.description}")})
        for uc in (inp.ai_use_cases or [])
    ]
    inp = inp.model_copy(update={"ai_use_cases": classified})

    platform = classify_platform(inp)
    tp = inp.technical_preferences
    resolved_tp = (
        tp.model_copy(update={"delivery_platform": platform})
        if tp is not None
        else TechnicalPreferences(delivery_platform=platform)
    )
    profile = PLATFORM_PROFILES.get(platform)
    proposal = {
        "recommended_platform": platform,
        "platform_label": profile.label if profile else platform,
        "cost_model": profile.cost_model if profile else "",
        "rationale": "Matched from the brief; the architect has not run yet.",
        "alternatives": [],
        "reasoning_steps": [],
        "source": "heuristic",
    }
    return inp.model_copy(update={"technical_preferences": resolved_tp}), proposal


def compute(inp: ProjectInput, est_id: str, generated_at: str, proposal: dict) -> Estimation:
    """The six arithmetic stages. Pure: same brief in, same estimate out."""
    for _stage, done in compute_steps(inp, est_id, generated_at, proposal):
        if done is not None:
            return done
    raise RuntimeError("estimate stages finished without producing a result")


def compute_steps(
    inp: ProjectInput, est_id: str, generated_at: str, proposal: dict,
) -> Iterator[tuple[str, Estimation | None]]:
    """Walk the arithmetic stages, announcing each one *before* it runs.

    A generator rather than a single call because the progress a caller streams
    should be the progress that is happening. The original computed the whole
    estimate up front and then played six canned messages against a timer, and
    a progress bar that is a recording of a progress bar is worse than none: it
    tells you a run is healthy at the exact moment it is not.

    Yields `(stage_key, None)` as each stage begins, and finally
    `("report", estimation)`. `compute` drives it to the end and takes the
    estimate; the streaming endpoint drives it a step at a time and sends a
    frame between steps. One order, one definition of it.

    Synchronous on purpose. There is nothing to await here, and making it async
    would invite something that does await into the middle of a costing run —
    which is exactly the door `azure_planner` is currently keeping shut.
    """
    yield "feasibility", None
    feasibility = engine.score_feasibility(inp)

    yield "cost", None
    cost = engine.compute_cost(inp, feasibility)

    yield "tokens", None
    tokens = engine.project_tokens(inp)

    yield "compare", None
    comparison = engine.compare_approaches(inp, feasibility, cost)

    yield "roi", None
    roi = engine.project_roi(inp, feasibility, cost)

    yield "report", None
    estimation = Estimation.model_validate({
        "id": est_id,
        "project_id": inp.project_name,
        "project_name": inp.project_name,
        "project_type": inp.project_type,
        "industry_domain": inp.industry_domain,
        "feasibility": feasibility,
        "verdict": engine.derive_verdict(feasibility, comparison),
        "confidence": engine.assess_confidence(inp, feasibility),
        "cost_breakdown": cost,
        "token_projection": tokens,
        "comparison": comparison,
        "roi_projection": roi,
        "recommendations": engine.build_recommendations(inp, feasibility),
        "assumptions": list(inp.assumptions or []),
        "report_markdown": engine.compose_markdown(inp, feasibility, cost),
        "status": "complete",
        "generated_at": generated_at,
        "repo_context": engine._build_repo_context(inp),
        "solution_proposal": proposal,
    })
    yield "report", estimation


async def run_estimate(inp: ProjectInput, est_id: str, generated_at: str) -> Estimation:
    """Resolve the brief, then price it."""
    resolved, proposal = await resolve(inp)
    return compute(resolved, est_id, generated_at, proposal)
