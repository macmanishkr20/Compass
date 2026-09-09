"""The estimate's domain model — what goes in, and what comes back.

Pydantic rather than the dataclasses the other stores use, and deliberately:
this is forty-odd nested models the engine reads by attribute and rebuilds with
`model_copy`, and validating a brief that arrives from a form is exactly the
job Pydantic exists for. The *store* still follows the house pattern — see
`store.py`, where a dataclass record carries the id, the owner and the times
around these payloads.

The wire is snake_case. It arrived here camelCase, bridged by a `to_camel`
alias generator, because the app it was written for spoke camelCase. Every
other endpoint in Compass speaks snake_case, and a section whose JSON is shaped
differently from its four neighbours is a seam a reader trips over for the life
of the module. Dropping the generator costs five explicit aliases and buys one
convention.

`extra="ignore"` is kept: a brief posted by an older client that still carries
a field this version dropped should estimate, not 422.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class EstimateModel(BaseModel):
    """Base: snake_case in Python and on the wire alike."""

    model_config = ConfigDict(extra="ignore")


# ── Project input ───────────────────────────────────────────────────

ProjectType = Literal["new", "enhancement"]
ProjectScale = Literal["small", "medium", "large", "enterprise"]
Complexity = Literal["low", "medium", "high", "very_high"]
Priority = Literal["must_have", "nice_to_have", "exploratory"]
AITaskType = Literal[
    "text_classification", "summarization", "code_generation", "conversational_agent",
    "rag_qa", "multi_agent_orchestration", "document_analysis", "image_analysis",
    "translation", "data_extraction", "recommendation", "anomaly_detection",
    # Deterministic, non-AI capabilities — let a use case honestly say it needs no LLM.
    "rules_workflow", "crud_lookup", "threshold_alerting",
]


class FeatureItem(EstimateModel):
    id: str = ""
    name: str = ""
    description: str = ""
    complexity: Complexity = "medium"
    ai_candidate: bool = False


class AIUseCase(EstimateModel):
    id: str = ""
    name: str = ""
    # Free-text intake can leave this blank; the classify node fills it in.
    task_type: Optional[str] = None
    description: str = ""
    priority: Priority = "must_have"
    linked_feature_ids: list[str] = Field(default_factory=list)
    # ROI benefit basis, overridable per use case: how many minutes of manual
    # work one automated call replaces. None falls back to the task-type default.
    minutes_per_call: Optional[float] = None
    # Optional hard override of the derived $/call — bypasses the minutes×rate
    # math entirely for a power user who already knows the unit value. None keeps
    # the transparent derivation, so existing payloads compute identical figures.
    value_per_call: Optional[float] = None


SizeBand = Literal["xs", "s", "m", "l", "xl", "xxl"]


class SubFeature(EstimateModel):
    """One thing somebody can name and size. The unit of an estimate.

    `size` is a band; `units` is the band in whole nine-hour units and is what
    the engine actually prices. Setting `units` directly is allowed — an
    architect who knows a job is six days should be able to say six rather than
    round to the nearest band — and it is what keeps the bands a convenience
    rather than a cage.
    """

    id: str = ""
    name: str = ""
    description: str = ""
    size: SizeBand = "s"
    units: Optional[int] = None
    #: Whether a model does the work here. Marked per sub-feature rather than
    #: per module, because "Search & Discovery" is three jobs and only one of
    #: them wants a language model.
    ai_candidate: bool = False


class Module(EstimateModel):
    """A group of sub-features, delivered together, in a phase."""

    id: str = ""
    name: str = ""
    phase: int = 1
    sub_features: list[SubFeature] = Field(default_factory=list)


class CostAssumptions(EstimateModel):
    """Org-specific rate card and resourcing dials.

    Every figure leadership sees ultimately multiplies these. They default to
    the platform's blended baseline so an omitted block reproduces the
    out-of-the-box numbers exactly; an org overrides them with its own real
    rates and effective working hours to localise the entire estimate.
    """

    dev_hourly_rate: float = 115
    maint_hourly_rate: float = 95
    # Effective (not nominal) engineering hours delivered per person-week —
    # the resourcing dial behind timeline and team-size math.
    effective_hours_per_week: float = 32
    # People working the build at once. Sets how long a phase takes, not what
    # it costs — the hours are the hours, whoever does them.
    team_size: float = 3
    # ROI benefit dials. value/call = minutes ÷ 60 × loaded_rate × automation%.
    # The fully-loaded cost of the person whose work AI offsets, and the share of
    # calls AI handles end-to-end (deflection) rather than an assumed perfect 100%.
    loaded_hourly_rate: float = 75
    automation_rate_percent: float = 35
    # Months from go-live to full adoption. Benefit accrues linearly to it.
    benefit_ramp_months: float = 6


DeliveryPlatform = Literal["azure_paas", "aws", "gcp", "m365_copilot", "on_prem"]


class TechnicalPreferences(EstimateModel):
    preferred_llm_provider: str = "Azure OpenAI"
    deployment_model: Literal["cloud", "hybrid", "edge"] = "cloud"
    # Which delivery platform the app is (or will be) built on. Selects the cost
    # *model* — consumption (Azure/AWS/GCP), per-seat licensing (M365/Copilot) or
    # capex (on-prem) — not just a price book. None lets the engine infer it from
    # existing_infra / hosting_platform / deployment_target keywords.
    delivery_platform: Optional[DeliveryPlatform] = None
    existing_infra: str = ""
    compliance_requirements: list[str] = Field(default_factory=list)
    budget_ceiling: Optional[float] = None
    budget_currency: str = "USD"
    # Azure regions used to price infrastructure. A single primary region drives
    # every service, with dedicated overrides for the two services whose
    # availability/price varies most by region.
    azure_region: str = "eastus"
    azure_openai_region: str = "eastus"
    azure_search_region: str = "eastus"


class VolumeAndScale(EstimateModel):
    expected_daily_users: Optional[int] = None
    requests_per_day: Optional[int] = None
    data_volume_gb: Optional[float] = None
    peak_load_pattern: str = ""
    growth_rate_percent: Optional[float] = None


class CurrentArchitecture(EstimateModel):
    framework: str = ""
    language: str = ""
    database: str = ""
    api_pattern: str = ""
    hosting_platform: str = ""
    ci_cd: str = ""


class IntegrationConstraints(EstimateModel):
    deployment_target: str = ""
    budget_ceiling: float = 0
    budget_currency: str = "USD"
    timeline_weeks: int = 0


class ProjectInput(EstimateModel):
    project_name: str = "Untitled project"
    project_type: ProjectType = "new"
    description: str = ""
    industry_domain: str = ""
    target_users: str = ""
    scale: ProjectScale = "medium"
    #: The work breakdown: modules, each a list of sub-features. This is the
    #: estimate. `features` below is the older flat list and is still accepted
    #: — a brief that carries one and not the other is converted on the way
    #: into the engine, so stored estimates keep working.
    modules: list[Module] = Field(default_factory=list)
    features: list[FeatureItem] = Field(default_factory=list)
    ai_use_cases: list[AIUseCase] = Field(default_factory=list)
    #: What this estimate takes as given, and what it leaves out. Written by
    #: the architect, proposed by the drafter. The reference workbook keeps
    #: twenty of these on their own tab, and they are the part a disagreement
    #: is actually about — "excludes native mobile", "Spanish, English and
    #: Filipino only", "high dependency on the data lake".
    assumptions: list[str] = Field(default_factory=list)
    technical_preferences: Optional[TechnicalPreferences] = None
    volume_and_scale: Optional[VolumeAndScale] = None
    # Overridable rate card / resourcing dials; None uses platform baselines.
    cost_assumptions: Optional[CostAssumptions] = None
    # Enhancement-specific
    repo_url: Optional[str] = None
    repo_branch: Optional[str] = None
    current_architecture: Optional[CurrentArchitecture] = None
    enhancement_scope: Optional[str] = None
    integration_constraints: Optional[IntegrationConstraints] = None
    # Repo analysis snapshot (carried into the report for enhancement mode)
    repo_full_name: Optional[str] = None
    repo_stars: Optional[int] = None
    repo_file_count: Optional[int] = None
    repo_manifests: Optional[list[str]] = None
    repo_topics: Optional[list[str]] = None


# ── Feasibility ─────────────────────────────────────────────────────

RecommendationArchetype = Literal[
    "traditional", "traditional_plus_ai", "rag_assistant",
    "single_agent", "multi_agent", "hybrid",
]


class FeasibilitySubScores(EstimateModel):
    ai_necessity: int
    agentic_suitability: int
    traditional_suitability: int


class UseCaseAnalysis(EstimateModel):
    use_case_name: str
    feasibility_score: int
    ai_task_type: str
    justification: str
    recommended_model: str
    complexity: Literal["low", "medium", "high"]
    # Per-capability lean so "Hybrid" can name which features go AI vs standard.
    # Optional for backward-compat with estimations persisted before this field existed.
    recommended_approach: Optional[Literal["ai", "standard"]] = None


class RiskItem(EstimateModel):
    category: str
    description: str
    severity: Literal["low", "medium", "high", "critical"]
    mitigation: str


class FeasibilityResult(EstimateModel):
    score: int
    rating: Literal["low", "medium", "high", "excellent"]
    sub_scores: FeasibilitySubScores
    archetype: RecommendationArchetype
    archetype_label: str
    archetype_rationale: str
    rationale: str
    use_case_analysis: list[UseCaseAnalysis]
    risks: list[RiskItem]
    opportunities: list[str]


# ── Cost ────────────────────────────────────────────────────────────

class SubFeatureEstimate(EstimateModel):
    name: str
    size: str
    units: int
    hours: int
    cost: int


class ModuleEstimate(EstimateModel):
    name: str
    phase: int
    hours: int
    cost: int
    #: Elapsed weeks for one engineer. A module is usually one person's job;
    #: the phase below is what the team does at once.
    weeks: float
    sub_features: list[SubFeatureEstimate] = Field(default_factory=list)


class PhaseEstimate(EstimateModel):
    phase: int
    hours: int
    cost: int
    #: Elapsed weeks for the whole team working this phase.
    weeks: float
    modules: list[ModuleEstimate] = Field(default_factory=list)


class WorkBreakdown(EstimateModel):
    """The estimate as a list of things somebody can name.

    Empty when the brief carried no modules — an older record, or a flat
    feature list — in which case the development breakdown is all there is.
    """

    phases: list[PhaseEstimate] = Field(default_factory=list)
    total_hours: int = 0
    total_cost: int = 0
    total_weeks: float = 0
    unit_hours: int = 9
    team_size: float = 3
    #: Sub-features at the ceiling. Not an error — a flag that the estimate has
    #: reached the edge of what it understands, and those lines are the ones to
    #: break down before anyone commits to the number.
    at_ceiling: list[str] = Field(default_factory=list)


class DevBreakdownItem(EstimateModel):
    category: str
    hours: int
    cost: int


class DevelopmentCost(EstimateModel):
    ai_integration_hours: int
    hourly_rate: float
    total_cost: int
    breakdown: list[DevBreakdownItem]
    #: The same effort, one level down: modules, their sub-features, and what
    #: each is going to take. `breakdown` is the summary a total is read from;
    #: this is the list an argument happens over.
    work_breakdown: WorkBreakdown = Field(default_factory=WorkBreakdown)


class AzureServiceCost(EstimateModel):
    service_name: str
    category: str = "General"
    tier: str
    region: str = ""
    quantity: float = 1
    unit: str = ""
    unit_price: float = 0
    monthly_cost: float
    # 'live'  = priced from the Azure Retail Prices API just now
    # 'fallback' = a metered service the API couldn't reach; catalog price used
    # 'estimate' = no clean per-unit meter; deterministic baseline estimate
    price_source: Literal["live", "fallback", "estimate"] = "estimate"
    # Azure OpenAI token cost is already counted in the AI-tokens line; listing
    # it here for completeness without adding it to the infra subtotal avoids
    # double counting. Such rows carry included_in_total=False.
    included_in_total: bool = True
    details: str
    azure_pricing_url: Optional[str] = None


class InfrastructureCost(EstimateModel):
    monthly_cost: float
    annual_cost: float
    services: list[AzureServiceCost]
    # Delivery-platform metadata. Optional for backward-compat with estimations
    # persisted before the platform dimension existed (those are Azure PaaS).
    platform: Optional[DeliveryPlatform] = None
    platform_label: Optional[str] = None
    # 'consumption' (metered compute), 'licensing' (per-seat) or 'capex' (amortized).
    cost_model: Optional[str] = None
    # Whether AI token spend is metered separately (consumption/capex) or bundled
    # into a per-seat licence (M365/Copilot). When False the AI-tokens line is $0.
    meters_tokens: Optional[bool] = None
    notes: list[str] = Field(default_factory=list)


class TokenScenario(EstimateModel):
    optimistic: float
    expected: float
    pessimistic: float


class ModelTokenBreakdown(EstimateModel):
    model: str
    use_cases: list[str]
    monthly_input_tokens: int
    monthly_output_tokens: int
    monthly_cost: float
    input_price_per_1m: float
    output_price_per_1m: float


class TokenCost(EstimateModel):
    monthly_tokens: TokenScenario
    monthly_cost: TokenScenario
    annual_cost: TokenScenario
    model_breakdown: list[ModelTokenBreakdown]


class MaintenanceCost(EstimateModel):
    monthly_hours: int
    hourly_rate: float
    monthly_cost: int
    annual_cost: int
    includes: list[str]


class CostRange(EstimateModel):
    min: int
    expected: int
    max: int


class CostBreakdown(EstimateModel):
    development: DevelopmentCost
    infrastructure: InfrastructureCost
    ai_tokens: TokenCost
    maintenance: MaintenanceCost
    total: CostRange
    currency: str


# ── Token projection ────────────────────────────────────────────────

class ModelRecommendation(EstimateModel):
    use_case: str
    provider: str
    recommended_model: str
    rationale: str
    avg_input_tokens: int
    avg_output_tokens: int


class TokenProjection(EstimateModel):
    daily: TokenScenario
    monthly: TokenScenario
    annual: TokenScenario
    model_recommendations: list[ModelRecommendation]
    assumptions: list[str]


# ── AI vs Standard comparison ───────────────────────────────────────

class ApproachDetail(EstimateModel):
    total_cost: CostRange
    timeline_weeks: int
    team_size: int
    benefits: list[str]
    challenges: list[str]
    monthly_run_cost: int


class ComparisonDimension(EstimateModel):
    dimension: str
    ai_score: int
    standard_score: int
    notes: str


class AIvsStandardComparison(EstimateModel):
    ai_approach: ApproachDetail
    standard_approach: ApproachDetail
    summary: str
    recommendation: Literal["ai", "standard", "hybrid"]
    recommendation_rationale: str
    dimensions: list[ComparisonDimension]


# ── ROI projection (deterministic) ──────────────────────────────────

class ValueDriver(EstimateModel):
    use_case: str
    value_per_call: float
    annual_calls: int
    annual_value: float
    # Transparent benefit basis (None when a flat value_per_call override was
    # used, or for estimates persisted before the decomposition existed).
    minutes_per_call: Optional[float] = None
    loaded_hourly_rate: Optional[float] = None
    automation_rate_percent: Optional[float] = None


class ROICurvePoint(EstimateModel):
    month: int
    cumulative_net: float


class ROIProjection(EstimateModel):
    annual_benefit: float
    annual_run_cost: float
    development_cost: float
    net_annual_benefit: float
    #: What year one actually delivers, with the adoption ramp applied.
    #: `annual_benefit` above is the steady-state rate at full adoption, which
    #: is a different — and larger — number. Both are shown, because quoting
    #: only the first understates the case and only the second oversells it.
    first_year_benefit: float = 0
    benefit_ramp_months: float = 6
    payback_months: Optional[float] = None
    three_year_value: float
    roi_percent: int
    curve: list[ROICurvePoint]
    value_drivers: list[ValueDriver]
    assumptions: list[str]


# ── Confidence (deterministic self-assessment) ──────────────────────

class ConfidenceFactor(EstimateModel):
    """One scored dimension behind the headline confidence level."""

    label: str
    detail: str
    # How this factor moves confidence: a strong signal lifts it, a weak/
    # defaulted one drags it down, a neutral one neither helps nor hurts.
    impact: Literal["positive", "neutral", "negative"]


class ConfidenceAssessment(EstimateModel):
    """How much weight to place on this estimate, computed — not guessed.

    The score is a deterministic function of input completeness, how decisively
    the feasibility score clears the decision thresholds, and how grounded the
    estimate is. It tells leadership whether to treat the numbers as bankable or
    directional, and the factors say exactly what to firm up to raise it.
    """

    level: Literal["low", "medium", "high"]
    score: int  # 0–100
    rationale: str
    factors: list[ConfidenceFactor]


# ── Verdict (the canonical, decisive call) ──────────────────────────

class Verdict(EstimateModel):
    """The platform's single, unambiguous recommendation — willing to say no.

    Derived from the feasibility archetype and the AI-vs-standard comparison, it
    collapses the analysis into one decision leadership can act on, including the
    honest "don't use AI here" when standard software is the better call.
    """

    decision: Literal["build_with_ai", "hybrid", "do_not_use_ai"]
    # Boardroom-ready phrasing of the call and the one-line why.
    headline: str
    one_liner: str
    # UI/copy tone: a green go, an amber qualified go, or a red stop.
    disposition: Literal["go", "caution", "stop"]
    recommend_ai: bool


# ── Recommendations ─────────────────────────────────────────────────

class Recommendation(EstimateModel):
    priority: Literal["critical", "high", "medium", "low"]
    category: str
    title: str
    description: str
    estimated_impact: str


# ── Solution architecture proposal (ReAct agent) ────────────────────

class SolutionAlternative(EstimateModel):
    """A platform the architect considered but did not recommend, and why."""
    platform: DeliveryPlatform
    why_not: str


class SolutionProposal(EstimateModel):
    """The recommended delivery platform + the architect's reasoning.

    Produced by the ReAct solution-architect node (LLM when configured, else a
    deterministic heuristic). The LLM only *chooses a platform label and explains
    it* — the deterministic engine still computes every dollar from the resolved
    `delivery_platform`. Optional on Estimation for backward compatibility with
    records persisted before this node existed.
    """
    recommended_platform: DeliveryPlatform
    platform_label: str
    cost_model: str  # 'consumption' | 'licensing' | 'capex'
    rationale: str
    alternatives: list[SolutionAlternative] = Field(default_factory=list)
    # The Thought/Action/Observation trace, surfaced for transparency. Empty for
    # the heuristic path (which records a short deterministic note instead).
    reasoning_steps: list[str] = Field(default_factory=list)
    # 'agent' = LLM ReAct loop; 'heuristic' = deterministic fallback;
    # 'explicit' = the user picked the platform, no inference needed.
    source: Literal["agent", "heuristic", "explicit"] = "heuristic"


# ── Agentic run (multi-agent ReAct orchestration) ───────────────────

class AgentStep(EstimateModel):
    """One entry in the multi-agent reasoning transcript.

    `kind` is the ReAct/orchestration phase: 'thought' | 'action' |
    'observation' | 'decision' | 'route' | 'critique' | 'fallback'. Surfaced for
    transparency; never carries a number the engine didn't compute.
    """
    agent: str
    kind: str
    content: str


class AgentCritique(EstimateModel):
    """A consistency issue the risk-critic raised about the draft estimate."""
    issue: str
    severity: Literal["low", "medium", "high"]
    target: str  # which specialist/artifact the critique concerns
    resolved: bool = False


class ToolCall(EstimateModel):
    """An audited deterministic computation an agent invoked.

    `output_hash` is a SHA-256 of the tool's canonical-JSON result, so any number
    in the report is traceable to the exact deterministic call that produced it.
    """
    agent: str
    tool: str
    output_hash: str


class AgentRun(EstimateModel):
    """How a multi-agent (supervisor + specialist ReAct agents) run unfolded.

    Present only on estimates produced with AGENTIC_MODE on. The strict integrity
    guard re-derives every figure from the agent-resolved inputs and refuses to
    let an agent-fabricated number survive — `integrity_verified` records whether
    the agents' own tool outputs matched that clean recompute. Optional on
    Estimation for backward compatibility with deterministically produced records.
    """
    mode: Literal["agentic", "deterministic"] = "deterministic"
    supervisor_path: list[str] = Field(default_factory=list)
    steps: list[AgentStep] = Field(default_factory=list)
    critiques: list[AgentCritique] = Field(default_factory=list)
    tool_ledger: list[ToolCall] = Field(default_factory=list)
    revisions: int = 0
    integrity_verified: bool = False
    llm_used: bool = False


# ── Repo context ────────────────────────────────────────────────────

class RepoContext(EstimateModel):
    full_name: str
    html_url: str
    branch: str
    primary_language: str
    stars: int
    file_count: int
    architecture: CurrentArchitecture
    manifests_found: list[str]
    topics: list[str]


# ── Top-level estimation ────────────────────────────────────────────

class Estimation(EstimateModel):
    id: str
    project_id: str
    project_name: str
    project_type: ProjectType
    industry_domain: str = ""
    feasibility: FeasibilityResult
    # The decisive call and how much to trust it. Optional for backward
    # compatibility with estimations persisted before these were introduced;
    # every freshly computed estimate populates both.
    verdict: Optional[Verdict] = None
    confidence: Optional[ConfidenceAssessment] = None
    cost_breakdown: CostBreakdown
    token_projection: TokenProjection
    comparison: AIvsStandardComparison
    roi_projection: ROIProjection
    recommendations: list[Recommendation]
    #: What the estimate takes as given and what it leaves out, as supplied on
    #: the brief. Carried onto the result so an exported estimate travels with
    #: its own scope — the sheet this is modelled on keeps them on their own
    #: tab, and they are what a disagreement is actually about.
    assumptions: list[str] = Field(default_factory=list)
    report_markdown: str
    status: Literal["generating", "complete", "error"] = "complete"
    generated_at: str
    repo_context: Optional[RepoContext] = None
    # How the delivery platform was chosen and why. Optional for backward
    # compatibility with estimations persisted before the architect node.
    solution_proposal: Optional[SolutionProposal] = None
    # The multi-agent ReAct orchestration trace (supervisor path, reasoning
    # steps, critiques, tool ledger, integrity result). Present only for runs
    # produced with AGENTIC_MODE on; Optional for every other record.
    agent_run: Optional[AgentRun] = None


class EstimationSSEChunk(EstimateModel):
    node: str
    status: str
    content: Optional[str] = None
    progress: Optional[int] = None
    data: Optional[Any] = None
