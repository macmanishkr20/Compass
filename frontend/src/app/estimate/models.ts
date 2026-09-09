// The Estimate module's wire types, mirroring compass/estimate/types.py.
//
// In its own file rather than in app/models.ts, which is where every other
// section's types live. That is a deliberate departure and the reason is the
// module's flag: Estimate can be switched off, and a section that can be
// removed should take its vocabulary with it. Forty types describing rate
// cards and ROI curves sitting in the file Home and Code also import would
// make the module removable in name only.
//
// snake_case throughout, because that is what the server sends — see the note
// at the top of `compass/estimate/types.py` on why the camelCase it arrived
// with did not survive the port.

/** What a run costs at three levels of optimism. Every money figure in the
 *  model that is not a single number is one of these. */
export interface CostRange {
  min: number;
  expected: number;
  max: number;
}

export interface Scenario {
  optimistic: number;
  expected: number;
  pessimistic: number;
}

// -- the brief ---------------------------------------------------------------

export type ProjectType = 'new' | 'enhancement';
export type ProjectScale = 'small' | 'medium' | 'large' | 'enterprise';
export type Complexity = 'low' | 'medium' | 'high' | 'very_high';
export type Priority = 'must_have' | 'nice_to_have' | 'exploratory';

export type SizeBand = 'xs' | 's' | 'm' | 'l' | 'xl' | 'xxl';

/** One thing somebody can name and size — the unit of an estimate. */
export interface SubFeature {
  id: string;
  name: string;
  description: string;
  size: SizeBand;
  /** Whole nine-hour units. Set directly to override the band. */
  units?: number | null;
  ai_candidate: boolean;
}

/** A group of sub-features, delivered together, in a phase. */
export interface Module {
  id: string;
  name: string;
  phase: number;
  sub_features: SubFeature[];
}

export interface FeatureItem {
  id: string;
  name: string;
  description: string;
  complexity: Complexity;
  ai_candidate: boolean;
}

export interface AIUseCase {
  id: string;
  name: string;
  /** Blank is allowed and normal: the classifier fills it in. */
  task_type: string | null;
  description: string;
  priority: Priority;
  linked_feature_ids: string[];
  /** How many minutes of manual work one automated call replaces. Null takes
   *  the task type's default. This is the number the whole ROI model rests on,
   *  which is why it is overridable per use case rather than global. */
  minutes_per_call?: number | null;
}

export interface TechnicalPreferences {
  preferred_llm_provider: string;
  deployment_model: string;
  existing_infra: string;
  compliance_requirements: string[];
  budget_ceiling?: number | null;
  budget_currency: string;
  delivery_platform?: string | null;
  azure_openai_region?: string;
  azure_search_region?: string;
}

export interface VolumeAndScale {
  expected_daily_users: number | null;
  requests_per_day: number | null;
  data_volume_gb: number | null;
  peak_load_pattern: string;
  growth_rate_percent: number | null;
}

/** The rate card, made explicit and overridable.
 *
 * Every figure leadership sees ultimately multiplies these five numbers, and
 * leaving them buried in a server-side catalog is what makes a cost estimate
 * feel like an oracle. Omit the block and the platform baselines apply
 * unchanged; fill it in and the whole estimate is in the org's own money. */
export interface CostAssumptions {
  dev_hourly_rate: number;
  maint_hourly_rate: number;
  /** Effective, not nominal — the hours a person actually delivers in a week. */
  effective_hours_per_week: number;
  /** People on the build at once. Sets how long a phase takes, not what it
   *  costs — the hours are the hours, whoever does them. */
  team_size: number;
  /** value/call = minutes ÷ 60 × loaded_hourly_rate × automation_rate_percent. */
  loaded_hourly_rate: number;
  automation_rate_percent: number;
  /** Months from go-live to full adoption. Benefit accrues linearly to it. */
  benefit_ramp_months: number;
}

export interface ProjectInput {
  project_name: string;
  project_type: ProjectType;
  description: string;
  industry_domain: string;
  target_users: string;
  scale: ProjectScale;
  /** The work breakdown — this is the estimate. `features` is the older flat
   *  list, still accepted so stored briefs keep opening. */
  modules: Module[];
  features: FeatureItem[];
  ai_use_cases: AIUseCase[];
  /** What the estimate takes as given and what it leaves out. */
  assumptions: string[];
  technical_preferences: TechnicalPreferences;
  volume_and_scale: VolumeAndScale;
  /** Null uses the platform baselines. */
  cost_assumptions?: CostAssumptions | null;
  // Enhancement-only. Absent on a new build.
  repo_url?: string | null;
  repo_branch?: string | null;
  current_architecture?: CurrentArchitecture | null;
  enhancement_scope?: string | null;
}

/** What an existing application already is, so an enhancement is costed as the
 *  difference rather than as a rebuild. */
export interface CurrentArchitecture {
  framework: string;
  language: string;
  database: string;
  api_pattern: string;
  hosting_platform: string;
  ci_cd: string;
}

// -- the estimate ------------------------------------------------------------

export interface UseCaseAnalysis {
  use_case_name: string;
  feasibility_score: number;
  ai_task_type: string;
  justification: string;
  recommended_model: string;
  complexity: string;
  /** 'ai' or 'standard'. What lets a Hybrid verdict name which capabilities go
   *  which way, instead of leaving "hybrid" as a shrug. */
  recommended_approach: string;
}

export interface RiskItem {
  category: string;
  description: string;
  severity: string;
  mitigation: string;
}

export interface Feasibility {
  score: number;
  rating: string;
  sub_scores: {
    ai_necessity: number;
    agentic_suitability: number;
    traditional_suitability: number;
  };
  archetype: string;
  archetype_label: string;
  archetype_rationale: string;
  rationale: string;
  use_case_analysis: UseCaseAnalysis[];
  risks: RiskItem[];
  opportunities: string[];
}

export interface Verdict {
  decision: string;
  headline: string;
  one_liner: string;
  disposition: string;
  recommend_ai: boolean;
}

export interface ConfidenceFactor {
  label: string;
  detail: string;
  impact: string;
}

export interface Confidence {
  level: string;
  score: number;
  rationale: string;
  factors: ConfidenceFactor[];
}

export interface ServiceCost {
  service_name: string;
  category: string;
  tier: string;
  region: string;
  quantity: number;
  unit: string;
  unit_price: number;
  monthly_cost: number;
  /** 'live' | 'fallback' | 'estimate' — where this price came from. Shown,
   *  because a figure you can trace is worth more than a figure you can't. */
  price_source: string;
  /** Azure OpenAI is listed but not summed: its cost is the token line. */
  included_in_total: boolean;
  details: string;
  azure_pricing_url: string;
}

export interface ModelTokenBreakdown {
  model: string;
  use_cases: string[];
  monthly_input_tokens: number;
  monthly_output_tokens: number;
  monthly_cost: number;
  input_price_per_1m: number;
  output_price_per_1m: number;
}

export interface SubFeatureEstimate {
  name: string;
  size: string;
  units: number;
  hours: number;
  cost: number;
}

export interface ModuleEstimate {
  name: string;
  phase: number;
  hours: number;
  cost: number;
  /** Elapsed weeks for one engineer. */
  weeks: number;
  sub_features: SubFeatureEstimate[];
}

export interface PhaseEstimate {
  phase: number;
  hours: number;
  cost: number;
  /** Elapsed weeks for the whole team. */
  weeks: number;
  modules: ModuleEstimate[];
}

export interface WorkBreakdown {
  phases: PhaseEstimate[];
  total_hours: number;
  total_cost: number;
  total_weeks: number;
  unit_hours: number;
  team_size: number;
  /** Sub-features at the ceiling — the lines to break down before committing. */
  at_ceiling: string[];
}

export interface CostBreakdown {
  development: {
    ai_integration_hours: number;
    hourly_rate: number;
    total_cost: number;
    breakdown: { category: string; hours: number; cost: number }[];
    work_breakdown: WorkBreakdown;
  };
  infrastructure: {
    monthly_cost: number;
    annual_cost: number;
    services: ServiceCost[];
    platform: string;
    platform_label: string;
    cost_model: string;
    meters_tokens: boolean;
    notes: string[];
  };
  ai_tokens: {
    monthly_tokens: Scenario;
    monthly_cost: Scenario;
    annual_cost: Scenario;
    model_breakdown: ModelTokenBreakdown[];
  };
  maintenance: {
    monthly_hours: number;
    hourly_rate: number;
    monthly_cost: number;
    annual_cost: number;
    includes: string[];
  };
  total: CostRange;
  currency: string;
}

export interface TokenProjection {
  daily: Scenario;
  monthly: Scenario;
  annual: Scenario;
  model_recommendations: {
    use_case: string;
    provider: string;
    recommended_model: string;
    rationale: string;
    avg_input_tokens: number;
    avg_output_tokens: number;
  }[];
  assumptions: string[];
}

export interface ApproachDetail {
  total_cost: CostRange;
  timeline_weeks: number;
  team_size: number;
  benefits: string[];
  challenges: string[];
  monthly_run_cost: number;
}

export interface Comparison {
  ai_approach: ApproachDetail;
  standard_approach: ApproachDetail;
  summary: string;
  recommendation: string;
  recommendation_rationale: string;
  dimensions: {
    dimension: string;
    ai_score: number;
    standard_score: number;
    notes: string;
  }[];
}

export interface ValueDriver {
  use_case: string;
  value_per_call: number;
  annual_calls: number;
  annual_value: number;
  /** The three numbers that make `value_per_call` arguable rather than magic.
   *  Shown wherever the value is. */
  minutes_per_call: number | null;
  loaded_hourly_rate: number | null;
  automation_rate_percent: number | null;
}

export interface RoiProjection {
  annual_benefit: number;
  annual_run_cost: number;
  development_cost: number;
  net_annual_benefit: number;
  /** What year one actually delivers, with the ramp applied. `annual_benefit`
   *  is the steady-state rate at full adoption — a different, larger number. */
  first_year_benefit: number;
  benefit_ramp_months: number;
  /** Null when the modelled benefit never recovers the build cost — a real
   *  answer, and one the UI must say out loud rather than render as a blank. */
  payback_months: number | null;
  three_year_value: number;
  roi_percent: number;
  curve: { month: number; cumulative_net: number }[];
  value_drivers: ValueDriver[];
  assumptions: string[];
}

export interface Recommendation {
  priority: string;
  category: string;
  title: string;
  description: string;
  estimated_impact: string;
}

export interface SolutionProposal {
  recommended_platform: string;
  platform_label: string;
  cost_model: string;
  rationale: string;
  alternatives: { platform: string; why_not: string }[];
  reasoning_steps: string[];
  /** 'explicit' | 'agent' | 'heuristic' — how the platform was decided. */
  source: string;
}

export interface Estimation {
  id: string;
  project_id: string;
  project_name: string;
  project_type: ProjectType;
  industry_domain: string;
  feasibility: Feasibility;
  verdict: Verdict | null;
  confidence: Confidence | null;
  cost_breakdown: CostBreakdown;
  token_projection: TokenProjection;
  comparison: Comparison;
  roi_projection: RoiProjection;
  recommendations: Recommendation[];
  assumptions: string[];
  report_markdown: string;
  status: string;
  generated_at: string;
  repo_context: Record<string, unknown> | null;
  solution_proposal: SolutionProposal | null;
}

// -- what the store keeps ----------------------------------------------------

/** The list row. Everything but the two heavy payloads, so a portfolio of
 *  fifty does not mean loading fifty full reports. Mirrors
 *  `Estimate.summary()`. */
export interface EstimateSummary {
  id: string;
  name: string;
  project_type: ProjectType;
  industry_domain: string;
  score: number;
  total_expected: number;
  currency: string;
  verdict: string;
  /** "high" | "medium" | "low", denormalised so the index need not load
   *  every full report to show one word. */
  confidence: string;
  status: string;
  owner: string;
  created_at: number;
  updated_at: number;
}

/** One stored estimate in full: the row, plus the brief that was asked and the
 *  estimate that came back. */
export interface EstimateRecord extends EstimateSummary {
  brief: ProjectInput;
  result: Estimation;
}

/** What `GET /v1/estimates/catalog` returns: what a brief may contain, as the
 *  engine understands it. The form is built from this rather than from a
 *  second copy of the same lists kept here — a task type added to the
 *  server's catalog becomes selectable with no change on this side. */
export interface EstimateCatalog {
  task_types: string[];
  scales: ProjectScale[];
  complexities: Complexity[];
  /** Hours in one unit of effort — the grain the engine prices in. */
  unit_hours: number;
  size_bands: { key: SizeBand; units: number; hours: number }[];
  priorities: Priority[];
  platforms: { key: string; label: string; cost_model: string }[];
  stages: { key: string; label: string }[];
  classifier: boolean;
  architect: boolean;
  /** Whether a brief can be drafted from prose on this server. */
  draft: boolean;
}

/** One frame of the estimate stream. */
export interface EstimateProgress {
  stage: string;
  label?: string;
  progress: number;
  status?: 'complete' | 'failed';
  platform?: string;
  estimate?: EstimateRecord;
  error?: string;
}
