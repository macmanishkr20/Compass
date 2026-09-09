"""Azure service planner — decides *which* services an estimate needs.

Given the project description, use cases, scale and compliance, a deterministic
rule set (`ServiceSpec.select`) picks a subset of the catalog's service keys.
The planner only ever returns *labels from the fixed catalog*; it never prices
anything.

For enhancement projects the candidate set is narrowed to the "net-new" services
(`ServiceSpec.net_new`), so the estimate counts only the *extra* Azure services
the AI work requires on top of the existing application.

It had a model-led variant, and this port drops it — for a structural reason
rather than a preference. `select_services` is called from inside
`engine.compute_infrastructure`, which is synchronous arithmetic; Compass's
model client is async, and the only ways to call it from there are to thread
async through nine hundred lines of costing or to block the event loop. Both
are worse than waiting. The right shape is the one the architect already uses:
resolve the choice in a stage *before* the engine runs and write it onto the
input, so the engine still prices exactly what was chosen. That is a later
phase; the rules are what runs until then, which is also what ran whenever no
key was configured — so this port's numbers are the numbers the smoke test
already asserts.
"""

from __future__ import annotations

import logging

from .azure_catalog import CATEGORY_ORDER, SERVICE_SPECS, SPEC_BY_KEY, PlanContext, ServiceSpec
from .types import ProjectInput

logger = logging.getLogger("compass.estimate")


def _candidate_specs(ctx: PlanContext) -> list[ServiceSpec]:
    """Services eligible for this project (all for new builds, net-new for enhancements)."""
    if ctx.project_type == "enhancement":
        return [s for s in SERVICE_SPECS if s.net_new]
    return list(SERVICE_SPECS)


def _rule_keys(ctx: PlanContext, candidates: list[ServiceSpec]) -> set[str]:
    """Deterministic selection: every candidate whose rule fires."""
    return {s.key for s in candidates if s.select(ctx)}


def _mandatory_keys(ctx: PlanContext, candidate_keys: set[str]) -> set[str]:
    """The floor no selection may remove — without these the estimate is
    incoherent. It guarded against a model dropping one; it still guards
    against a rule gap."""
    req: set[str] = set()
    if ctx.project_type != "enhancement":
        for k in ("app_hosting", "database", "monitor"):
            if k in candidate_keys:
                req.add(k)
    if ctx.uses_azure_openai and "openai" in candidate_keys:
        req.add("openai")
    if ctx.needs_search and "ai_search" in candidate_keys:
        req.add("ai_search")
    return req


def select_services(inp: ProjectInput, ctx: PlanContext) -> list[ServiceSpec]:
    """Return the ordered list of ServiceSpecs to price for this project."""
    candidates = _candidate_specs(ctx)
    candidate_keys = {s.key for s in candidates}

    chosen = _rule_keys(ctx, candidates)
    chosen |= _mandatory_keys(ctx, candidate_keys)

    # Stable display order: by category flow, then catalog order within a category.
    rank = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    ordered = sorted(
        (SPEC_BY_KEY[k] for k in chosen if k in SPEC_BY_KEY),
        key=lambda s: (rank.get(s.category, 99), SERVICE_SPECS.index(s)),
    )
    return ordered
