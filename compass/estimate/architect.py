"""Solution architect — chooses the delivery platform, and nothing else.

Given the project's use cases and constraints, it picks one of five platforms
and says why. The deterministic engine then prices exactly that platform.

**It was a ReAct loop and is now one call.** The loop offered four tools —
list the platforms, analyse the use-case mix, read the constraints, show the
keyword baseline — and every one of them was a read-only view of the input the
caller was already holding. Nothing was fetched, nothing changed between
turns, so the model spent up to five sequential round trips asking for facts
that could have been handed to it in the first message. Measured on gpt-5 that
was thirty to forty seconds of an estimate that otherwise takes under a
second, and the tools were ceremony rather than capability: a ReAct loop earns
its turns when a later question depends on an earlier answer, and here no
question did.

So the four views are composed into the briefing up front and the answer comes
back as a structured object. Same information, same five-way decision, one
round trip. The functions that build the views are unchanged and still named
`_tool_*`, because that is what they were and the report still shows their
content as the reasoning.

Hard rule, same as the classifier: the LLM **never computes or states a cost**. It
only returns a platform *label* (one of five) plus a rationale. The deterministic
engine then prices that platform — so every dollar is still code-computed. When
the stage is switched off, or the loop fails, a deterministic heuristic that
mirrors `classify_platform` is used, so an estimate is reproducible either way.

Why the platform is resolved here rather than inside the costing: writing the
choice onto the input before the engine runs is what lets a model influence an
estimate without ever being in a position to move a number. `azure_planner`
should eventually work the same way — see the note there.
"""

from __future__ import annotations

import json
import logging
import re

from compass.common.config import get_settings
from compass.common.gateway.azure_client import get_model_client

from .platforms import DEFAULT_PLATFORM, PLATFORM_PROFILES, classify_platform
from .types import ProjectInput

logger = logging.getLogger("compass.estimate")

# Task types that are deterministic software, not AI — a heavy non-AI mix is a
# signal that a licensing/workflow platform may fit better than metered AI compute.
_NON_AI_TASKS = {"rules_workflow", "crud_lookup", "threshold_alerting"}
_AGENTIC_TASKS = {"multi_agent_orchestration"}

_VALID = set(PLATFORM_PROFILES.keys())


# ── The four views the briefing is composed from ────────────────────

def _tool_list_platforms() -> str:
    lines = []
    suits = {
        "azure_paas": "cloud-native, request-driven AI; elastic metered compute",
        "aws": "teams already on AWS; same metered shape, AWS services",
        "gcp": "teams already on Google Cloud; same metered shape, Google services",
        "m365_copilot": "org-wide productivity inside Microsoft 365/SharePoint; AI bundled per seat",
        "on_prem": "self-hosted / data-resident / air-gapped; capex hardware + ops",
    }
    for key, p in PLATFORM_PROFILES.items():
        lines.append(f"- {key}: {p.label} | cost model: {p.cost_model} | best for: {suits.get(key, '')}")
    return "The five delivery platforms:\n" + "\n".join(lines)


def _tool_analyze_use_cases(inp: ProjectInput) -> str:
    ucs = inp.ai_use_cases or []
    types = [u.task_type or "unknown" for u in ucs]
    ai = [t for t in types if t not in _NON_AI_TASKS]
    non_ai = [t for t in types if t in _NON_AI_TASKS]
    agentic = [t for t in types if t in _AGENTIC_TASKS]
    return (
        f"{len(ucs)} use case(s). Task types: {', '.join(types) or 'none'}.\n"
        f"AI-led: {len(ai)} | deterministic/no-AI: {len(non_ai)} | agentic/multi-agent: {len(agentic)}.\n"
        f"Scale: {inp.scale}. Project type: {inp.project_type}."
    )


def _tool_read_constraints(inp: ProjectInput) -> str:
    tp = inp.technical_preferences
    vs = inp.volume_and_scale
    ca = inp.current_architecture
    users = vs.expected_daily_users if vs and vs.expected_daily_users is not None else "unknown"
    compliance = ", ".join(tp.compliance_requirements) if tp and tp.compliance_requirements else "none stated"
    return (
        f"Existing infra: {tp.existing_infra or 'not stated' if tp else 'not stated'}.\n"
        f"Preferred LLM provider: {tp.preferred_llm_provider if tp else 'not stated'}.\n"
        f"Deployment model: {tp.deployment_model if tp else 'not stated'}.\n"
        f"Hosting platform (detected repo): {ca.hosting_platform if ca and ca.hosting_platform else 'n/a'}.\n"
        f"Compliance: {compliance}.\n"
        f"Expected daily users (seat signal): {users}.\n"
        f"Domain: {inp.industry_domain or 'not stated'}."
    )


def _tool_baseline_guess(inp: ProjectInput) -> str:
    guess = classify_platform(inp)
    label = PLATFORM_PROFILES[guess].label
    return (
        f"The deterministic keyword classifier suggests '{guess}' ({label}). "
        "Treat this as a hint — override it if the use-case mix and constraints point elsewhere, "
        "but say why."
    )


# ── Prompt ──────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are a cloud solution architect. Choose the single best DELIVERY PLATFORM "
    "for a software project from this fixed set of keys:\n"
    "  azure_paas, aws, gcp, m365_copilot, on_prem\n\n"
    "You will be given the project, its use-case mix, the stated constraints, what "
    "each platform is for, and the answer a deterministic keyword classifier would "
    "give. Treat that last one as a hint you may overrule — but say why when you do.\n\n"
    "You NEVER compute, estimate or state any cost, price, dollar amount or number "
    "of dollars. A separate deterministic engine does all of the costing, from the "
    "platform you pick. Your rationale is about fit, not about money.\n\n"
    "Return the platform key, a rationale of two or three sentences, and up to three "
    "alternatives you considered with a short reason each was not chosen."
)


def _user_prompt(inp: ProjectInput) -> str:
    return (
        f"Project: {inp.project_name}\n"
        f"Domain: {inp.industry_domain or 'unspecified'}\n"
        f"Description: {inp.description or '(none)'}\n\n"
        "Decide the best delivery platform. Start by gathering facts with the tools."
    )


# ── The model call ─────────────────────────────────────────────────

_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "platform": {"type": "string", "enum": sorted(_VALID)},
        "rationale": {
            "type": "string",
            "description": "Two or three sentences. No costs, prices or numbers of dollars.",
        },
        "alternatives": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "platform": {"type": "string", "enum": sorted(_VALID)},
                    "why_not": {"type": "string"},
                },
                "required": ["platform", "why_not"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["platform", "rationale", "alternatives"],
    "additionalProperties": False,
}


def _briefing(inp: ProjectInput) -> str:
    """Everything the loop's four tools used to return, composed once."""
    return "\n\n".join((
        _user_prompt(inp),
        _tool_analyze_use_cases(inp),
        _tool_read_constraints(inp),
        _tool_list_platforms(),
        _tool_baseline_guess(inp),
    ))


async def _complete_json(system: str, briefing: str) -> str | None:
    try:
        return await get_model_client().complete_utility(
            system, briefing, max_tokens=1_200,
            schema=_ANSWER_SCHEMA, schema_name="platform_choice",
        )
    except Exception as exc:  # noqa: BLE001 - any failure => heuristic fallback
        logger.warning("architect step failed: %s", exc)
        return None


# ── Output parsing ──────────────────────────────────────────────────

def _extract_json_object(text: str) -> dict | None:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except Exception:  # noqa: BLE001
        return None


# ── The choice ──────────────────────────────────────────────────────

async def _choose_platform(inp: ProjectInput) -> dict | None:
    """One call: brief the model on everything, take a structured answer.

    Returns None on any failure, which sends the caller to the heuristic —
    the same fallback the loop had, for the same reason: the platform must be
    decided one way or another, and a deterministic answer is better than no
    estimate.
    """
    raw = await _complete_json(_SYSTEM_PROMPT, _briefing(inp))
    if not raw:
        return None

    data = _extract_json_object(raw)
    if not data:
        return None

    platform = str(data.get("platform", "")).strip()
    if platform not in _VALID:
        # The schema constrains this, but a schema is the provider's promise
        # rather than ours, and an unknown platform would be priced as the
        # default without anyone noticing which one they were quoted.
        logger.warning("Architect proposed invalid platform %r; falling back.", platform)
        return None

    alternatives = []
    for alt in data.get("alternatives") or []:
        key = str(alt.get("platform", "")).strip()
        if key in _VALID and key != platform:
            alternatives.append({
                "platform": key,
                "why_not": str(alt.get("why_not") or "")[:200],
            })

    rationale = str(data.get("rationale", "")).strip()[:600] \
        or _DEFAULT_RATIONALE.get(platform, "")
    # The report shows how the choice was reached. The four views are what the
    # model was given, so they are what it read — the same transparency the
    # loop's transcript provided, without the round trips.
    steps = [
        "Considered: use-case mix, stated constraints, the five platforms, "
        "and the deterministic keyword baseline.",
        f"Decision: {platform} — {rationale}",
    ]
    return _proposal(platform, rationale, alternatives[:3], steps, "agent")


# ── Proposal builders ───────────────────────────────────────────────

_DEFAULT_RATIONALE = {
    "azure_paas": (
        "Azure PaaS uses a consumption model — metered compute, data and AI tokens — which "
        "suits a cloud-native build with elastic, request-driven AI workloads."
    ),
    "aws": (
        "AWS signals were detected; a consumption model on AWS keeps the same metered cost "
        "shape using the equivalent AWS services."
    ),
    "gcp": (
        "Google Cloud signals were detected; a consumption model on GCP keeps the metered "
        "cost shape using the equivalent Google services."
    ),
    "m365_copilot": (
        "Microsoft 365 / Copilot signals were detected; a per-seat licensing model fits an "
        "org-wide productivity rollout where AI is bundled into the seat rather than metered."
    ),
    "on_prem": (
        "On-premises / private-cloud signals were detected; a capex model (amortized hardware "
        "plus ops staffing) fits a self-hosted, data-resident deployment."
    ),
}


def _proposal(platform: str, rationale: str, alternatives: list[dict], steps: list[str], source: str) -> dict:
    profile = PLATFORM_PROFILES.get(platform) or PLATFORM_PROFILES[DEFAULT_PLATFORM]
    return {
        "recommended_platform": profile.key,
        "platform_label": profile.label,
        "cost_model": profile.cost_model,
        "rationale": rationale or _DEFAULT_RATIONALE.get(profile.key, ""),
        "alternatives": alternatives,
        "reasoning_steps": steps,
        "source": source,
    }


def _heuristic_proposal(inp: ProjectInput) -> dict:
    platform = classify_platform(inp)
    steps = [f"Heuristic: matched delivery signals to '{platform}'."]
    return _proposal(platform, _DEFAULT_RATIONALE.get(platform, ""), [], steps, "heuristic")


def _explicit_proposal(platform: str, inp: ProjectInput) -> dict:
    profile = PLATFORM_PROFILES.get(platform) or PLATFORM_PROFILES[DEFAULT_PLATFORM]
    rationale = (
        f"{profile.label} was explicitly selected, so the estimate is built against its "
        f"{profile.cost_model} cost model."
    )
    return _proposal(profile.key, rationale, [], [], "explicit")


async def propose_solution(inp: ProjectInput) -> dict:
    """Return a solution proposal dict (snake_case, ready for SolutionProposal).

    Order of precedence:
      1. An explicit user platform choice wins (no inference).
      2. The model, when the architect stage is enabled.
      3. A deterministic heuristic mirroring `classify_platform`.

    Always returns a dict — never raises — so the pipeline can rely on it.
    """
    tp = inp.technical_preferences
    explicit = tp.delivery_platform if tp else None
    if explicit:
        return _explicit_proposal(explicit, inp)

    if get_settings().estimate.architect:
        try:
            agent = await _choose_platform(inp)
        except Exception as exc:  # noqa: BLE001 - never let the agent break a request
            logger.warning("Architect agent crashed, using heuristic: %s", exc)
            agent = None
        if agent is not None:
            return agent

    return _heuristic_proposal(inp)
