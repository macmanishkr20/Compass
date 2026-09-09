"""Deterministic pricing & scoring catalog.

This is the single source of truth for every number the engine multiplies.
It mirrors the Angular `estimation.service.ts` constants verbatim so the
Python backend and the TypeScript reference produce identical dollars. The
LLM never touches anything in here — code computes every figure.
"""

from __future__ import annotations

from typing import TypedDict

# ── Rate card (blended USD) ──
DEV_HOURLY_RATE = 115
MAINT_HOURLY_RATE = 95
HOURS_PER_DEV_WEEK = 32  # effective, not nominal

# ── ROI benefit basis ──
# The benefit side of ROI is built bottom-up, not from an opaque "$/call":
#   value/call = (minutes_per_call ÷ 60) × loaded_hourly_rate × (automation_rate% ÷ 100)
# so every figure a leader sees traces to two dials they can defend — the
# fully-loaded cost of the person whose work is offset, and the share of calls
# AI actually handles end-to-end (deflection) rather than assuming a perfect 100%.
LOADED_HOURLY_RATE = 75        # fully-loaded labour cost of the offset worker
# The share of calls handled end to end, with no human touch. This was 70,
# which is a best case rather than a planning number: 70% end-to-end deflection
# is what a mature, well-tuned assistant reaches after tuning, and quoting it
# on day one turns every estimate into its own best outcome. 35 is the middle
# of the band a competent first build actually lands in, and it is a dial —
# an organisation with measured deflection should put its own number here.
AUTOMATION_RATE_PERCENT = 35   # share of calls AI handles without human redo

# How long before the benefit runs at full rate.
#
# The model had no ramp at all: value accrued at the steady-state rate from the
# morning of go-live, which is what produced paybacks measured in days. Nothing
# behaves like that. There is a pilot, a subset of users, a period where people
# check the answers, and a tail of cases that route back to a human until the
# retrieval is fixed. Six months to full adoption is conservative-to-typical
# for an internal assistant, and — like every other dial here — it is stated in
# the report rather than buried.
BENEFIT_RAMP_MONTHS = 6

# Hours to *deliver* one feature, all in: analysis, implementation, test,
# review and deployment. Not build hours with the rest added on afterwards.
#
# This was the other way round for one revision, with a separate multiplier
# doubling it. The multiplier is gone, and the reason is worth keeping: nobody
# typing "high — 130 hours" into a form means "130 hours of coding, please
# double it". They mean what it costs to have the thing. A model that silently
# reinterprets the number a person typed is not estimating, it is arguing with
# them in private. The reference workbook reads the same way — 45 hours for
# SSO, with no overhead line anywhere in it.
#
# Only used on the legacy flat-feature path now; a brief with a work breakdown
# prices its sub-features directly.
COMPLEXITY_HOURS: dict[str, int] = {
    "low": 24,
    "medium": 64,
    "high": 130,
    "very_high": 240,
}

# What the reach of the thing does to the effort of building it.
#
# Development did not scale at all. Infrastructure did (120 -> 2200/month
# across the tiers) and so did maintenance (12 -> 90 hours/month), but a
# mission-critical rollout to an entire organisation was costed at exactly the
# same engineering effort as a pilot for one team. That is not a rounding
# error: the difference is access control against a real directory, audit
# trails, a disaster-recovery story, load and failure testing, staged rollout,
# and integration with monitoring and identity that already exist and have
# opinions. None of that is a feature, all of it is work, and all of it grows
# with who is going to use the thing.
SCALE_DELIVERY: dict[str, float] = {
    "small": 0.85,       # a pilot: fewer non-functional demands, less to integrate
    "medium": 1.0,       # a department — the baseline the feature hours describe
    "large": 1.35,       # org-wide: real identity, real audit, real load
    "enterprise": 1.8,   # mission-critical: DR, compliance, staged rollout
}

# ── The work breakdown ──────────────────────────────────────────────
#
# An estimate is a list of things somebody can name, each with an effort. That
# is what makes it arguable: "261 hours for video processing" invites a shrug,
# while "FFmpeg transcoding 63, HLS/DASH renditions 54, API and blob 90, player
# integration 54" invites an argument about the third line, which is the
# conversation worth having.
#
# The unit and the bands below are read off a real architect's workbook for an
# internal video platform — 15 modules, 58 sub-features, 2,034 hours for phase
# one. Every figure in it is a multiple of nine and none exceeds ninety, which
# are two rules rather than a coincidence:
#
#   * nine hours is a working day with the day's overheads taken out, so an
#     effort is quoted in whole days of delivered work;
#   * ninety hours is two person-weeks, and anything bigger has not been
#     understood well enough to estimate — it has to be split first.
#
# The second rule is the one that produces accuracy. Sizing a bucket is a
# guess; enumerating its contents is an estimate.
EFFORT_UNIT_HOURS = 9

#: Size bands, in whole units of EFFORT_UNIT_HOURS. The six cover 48 of the 58
#: line items in the reference workbook exactly; a sub-feature that genuinely
#: falls between two of them carries an explicit unit count instead.
SIZE_BAND_UNITS: dict[str, int] = {
    "xs": 2,    # 18h — a validation, a toggle, one small screen
    "s": 3,     # 27h
    "m": 4,     # 36h
    "l": 5,     # 45h
    "xl": 7,    # 63h
    "xxl": 10,  # 90h — the ceiling
}

#: The ceiling, in units. A sub-feature above it is not estimated, it is split.
MAX_SUB_FEATURE_UNITS = 10

# How much harder a use case is to integrate than the plainest one.
#
# This was a cliff: 1.4 above an agentic score of 70 and 1.0 below it, which
# put retrieval Q&A (55) in the same bracket as text classification (20).
# Retrieval has a corpus to prepare, chunking to tune, and an evaluation set
# that has to exist before anyone can say whether it works; classification has
# a prompt. Graded on the same score instead, so the curve has no step in it
# and every task type pays for what it actually involves.
AGENTIC_INTEGRATION_UPLIFT = 0.6   # at agentic 100; scales linearly to 0

PRIORITY_WEIGHT: dict[str, float] = {
    "must_have": 1.0,
    "nice_to_have": 0.6,
    "exploratory": 0.3,
}


# ── Model pricing catalog (USD per 1M tokens) ──
class ModelPrice(TypedDict):
    provider: str
    in_per_1m: float
    out_per_1m: float


MODEL_CATALOG: dict[str, ModelPrice] = {
    "gpt-4o": {"provider": "Azure OpenAI", "in_per_1m": 2.5, "out_per_1m": 10},
    "gpt-4o-mini": {"provider": "Azure OpenAI", "in_per_1m": 0.15, "out_per_1m": 0.6},
    "claude-sonnet-4": {"provider": "Anthropic", "in_per_1m": 3, "out_per_1m": 15},
    "claude-haiku": {"provider": "Anthropic", "in_per_1m": 0.8, "out_per_1m": 4},
    "gemini-2-pro": {"provider": "Google", "in_per_1m": 1.25, "out_per_1m": 5},
    "gemini-2-flash": {"provider": "Google", "in_per_1m": 0.075, "out_per_1m": 0.3},
}


# ── Per-task-type profile: token shape, default model, and scoring weights ──
class TaskProfile(TypedDict):
    in_tokens: int
    out_tokens: int
    model: str
    ai_n: int      # contribution to AI-necessity
    agentic: int   # contribution to agentic suitability
    trad: int      # contribution to traditional suitability
    minutes_per_call: float  # minutes of manual work one automated call replaces


TASK_PROFILES: dict[str, TaskProfile] = {
    "text_classification": {"in_tokens": 800, "out_tokens": 80, "model": "gpt-4o-mini", "ai_n": 55, "agentic": 20, "trad": 70, "minutes_per_call": 1},
    "summarization": {"in_tokens": 4000, "out_tokens": 600, "model": "gpt-4o-mini", "ai_n": 66, "agentic": 25, "trad": 45, "minutes_per_call": 4},
    "code_generation": {"in_tokens": 2500, "out_tokens": 1200, "model": "claude-sonnet-4", "ai_n": 80, "agentic": 55, "trad": 25, "minutes_per_call": 20},
    "conversational_agent": {"in_tokens": 1500, "out_tokens": 500, "model": "gpt-4o", "ai_n": 80, "agentic": 72, "trad": 25, "minutes_per_call": 6},
    "rag_qa": {"in_tokens": 3500, "out_tokens": 700, "model": "gpt-4o-mini", "ai_n": 78, "agentic": 55, "trad": 30, "minutes_per_call": 6},
    "multi_agent_orchestration": {"in_tokens": 6000, "out_tokens": 2500, "model": "gpt-4o", "ai_n": 88, "agentic": 92, "trad": 15, "minutes_per_call": 25},
    "document_analysis": {"in_tokens": 8000, "out_tokens": 1000, "model": "gpt-4o", "ai_n": 75, "agentic": 50, "trad": 35, "minutes_per_call": 15},
    "image_analysis": {"in_tokens": 1200, "out_tokens": 400, "model": "gpt-4o", "ai_n": 82, "agentic": 35, "trad": 30, "minutes_per_call": 4},
    "translation": {"in_tokens": 1000, "out_tokens": 1000, "model": "gemini-2-flash", "ai_n": 60, "agentic": 20, "trad": 55, "minutes_per_call": 8},
    "data_extraction": {"in_tokens": 2000, "out_tokens": 400, "model": "gpt-4o-mini", "ai_n": 62, "agentic": 35, "trad": 60, "minutes_per_call": 6},
    "recommendation": {"in_tokens": 1500, "out_tokens": 300, "model": "gemini-2-pro", "ai_n": 70, "agentic": 40, "trad": 50, "minutes_per_call": 3},
    "anomaly_detection": {"in_tokens": 1000, "out_tokens": 120, "model": "gemini-2-flash", "ai_n": 58, "agentic": 30, "trad": 72, "minutes_per_call": 5},
    # Deterministic capabilities that standard software handles best. Low AI-necessity
    # so a product built mostly of these can honestly land on "Standard" / "Don't use AI"
    # instead of being force-fit into an AI task type (the lowest of which was 55).
    "rules_workflow": {"in_tokens": 600, "out_tokens": 80, "model": "gpt-4o-mini", "ai_n": 22, "agentic": 12, "trad": 88, "minutes_per_call": 2},
    "crud_lookup": {"in_tokens": 400, "out_tokens": 60, "model": "gpt-4o-mini", "ai_n": 15, "agentic": 8, "trad": 92, "minutes_per_call": 1},
    "threshold_alerting": {"in_tokens": 500, "out_tokens": 60, "model": "gpt-4o-mini", "ai_n": 28, "agentic": 15, "trad": 85, "minutes_per_call": 2},
}

# Valid task types (kept here so the classifier can validate against the catalog).
TASK_TYPES: list[str] = list(TASK_PROFILES.keys())

SCALE_INFRA_BASE: dict[str, int] = {"small": 120, "medium": 320, "large": 850, "enterprise": 2200}
SCALE_APIM: dict[str, int] = {"small": 0, "medium": 50, "large": 250, "enterprise": 700}
SCALE_MAINT_HOURS: dict[str, int] = {"small": 12, "medium": 24, "large": 48, "enterprise": 90}


# ── Archetype copy (mirrors shared/utils/feasibility.ts) ──
ARCHETYPE_LABEL: dict[str, str] = {
    "traditional": "Traditional Only",
    "traditional_plus_ai": "Traditional + AI",
    "rag_assistant": "RAG Assistant",
    "single_agent": "Single Agent",
    "multi_agent": "Multi-Agent",
    "hybrid": "Hybrid",
}

ARCHETYPE_BLURB: dict[str, str] = {
    "traditional": "Standard software delivers this best — AI adds cost without proportional value.",
    "traditional_plus_ai": "A conventional build with a few targeted AI features where they clearly pay off.",
    "rag_assistant": "A retrieval-augmented assistant grounded in your own data and documents.",
    "single_agent": "One autonomous agent with tools, handling multi-step tasks end to end.",
    "multi_agent": "Several coordinated agents orchestrated across specialised roles.",
    "hybrid": "A deliberate mix of deterministic services and agents, each where it fits.",
}


def archetype_label(a: str) -> str:
    return ARCHETYPE_LABEL.get(a, a)


def archetype_blurb(a: str) -> str:
    return ARCHETYPE_BLURB.get(a, "")


def rating_for_score(score: float) -> str:
    if score >= 80:
        return "excellent"
    if score >= 60:
        return "high"
    if score >= 40:
        return "medium"
    return "low"
