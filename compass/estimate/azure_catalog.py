"""Azure service catalog — the candidate set the infra planner draws from.

Each `ServiceSpec` describes one Azure service CostCompass can propose, with:
  • a deterministic monthly *quantity* (units consumed), and
  • a baseline *unit price* (USD) used offline / as a fallback.

The dollar figure is always `quantity × unit_price`. When live pricing is on,
`azure_pricing` refines the unit price from the Azure Retail Prices API; the
quantity stays deterministic so figures remain reproducible and explainable.

The LLM (in `azure_planner`) only *selects* which keys apply — it never sets a
price. Selection rules here are the deterministic fallback when no LLM is used.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Optional

from .catalog import NO_MODEL_TASK_TYPES, SCALE_APIM, SCALE_INFRA_BASE
from .types import ProjectInput

HOURS_PER_MONTH = 730


def _jround(n: float) -> int:
    return math.floor(n + 0.5)


def _round2(n: float) -> float:
    return math.floor(n * 100 + 0.5) / 100


# ── Per-scale baseline estimates (USD/month) for services without a clean
#    per-unit retail meter. These are deliberate, defensible round numbers. ──
_DEFENDER = {"small": 15, "medium": 45, "large": 120, "enterprise": 300}
_REDIS = {"small": 16, "medium": 55, "large": 180, "enterprise": 410}
_CONTENT_SAFETY = {"small": 8, "medium": 20, "large": 60, "enterprise": 150}
_SERVICE_BUS = {"small": 10, "medium": 25, "large": 70, "enterprise": 160}
_FRONT_DOOR = {"small": 35, "medium": 35, "large": 120, "enterprise": 300}
_DOC_INTEL = {"small": 30, "medium": 75, "large": 200, "enterprise": 480}
_VISION = {"small": 20, "medium": 50, "large": 140, "enterprise": 320}
_TRANSLATOR = {"small": 15, "medium": 40, "large": 110, "enterprise": 260}
_PRIVATE_NET = {"small": 20, "medium": 40, "large": 90, "enterprise": 180}

# ── The stated-stack services ───────────────────────────────────────
#
# Priced only when a brief names them (`azure_services`), never by the rules
# below: they are the answer to "the architect wrote the stack down". Each is
# sized by reach and priced live in the brief's region where Azure publishes a
# meter, so moving a build from East US to North Europe moves these figures —
# which the flat per-scale baselines above, by construction, never did.

#: App Service Premium v3 P1v3 instances: two from a department up, so a
#: restart or a platform update is not an outage.
_APP_SERVICE_INSTANCES = {"small": 1, "medium": 2, "large": 3, "enterprise": 4}
#: Azure SQL Database, General Purpose Gen5 provisioned vCores. Two is the
#: tier's floor and comfortably holds a few hundred thousand rows.
_SQL_VCORES = {"small": 2, "medium": 2, "large": 4, "enterprise": 8}
#: Log Analytics ingestion, GB a month. The 5 GB monthly free allowance is per
#: billing account, shared with everything else on it, so it is not deducted.
_LOG_GB = {"small": 2, "medium": 5, "large": 20, "enterprise": 60}
#: SendGrid is Twilio's, not an Azure meter. Pro 100K is $89.95 a month, from
#: published third-party listings of Twilio's price; Essentials (50K, $19.95)
#: cannot carry one 100,000-recipient distribution in a month.
_SENDGRID = {"small": ("Essentials 50K", 19.95), "medium": ("Pro 100K", 89.95),
             "large": ("Pro 100K", 89.95), "enterprise": ("Pro 300K", 249.0)}
#: Power BI Pro, per user per month (Microsoft list price).
_POWER_BI_PRO = 14.0

# AI Search SKU → (tier label, USD/hour baseline)
_SEARCH_SKU = {
    "small": ("Basic", 0.101),
    "medium": ("Standard S1", 0.336),
    "large": ("Standard S1", 0.336),
    "enterprise": ("Standard S2", 1.344),
}

#: Regions a brief can be priced in, by ARM name. Every one of them is answered
#: by the Retail Prices API; the list is what the form offers, not a limit on
#: what the engine accepts.
AZURE_REGIONS: dict[str, str] = {
    "northeurope": "North Europe (Ireland)",
    "swedencentral": "Sweden Central",
    "westeurope": "West Europe (Netherlands)",
    "uksouth": "UK South",
    "germanywestcentral": "Germany West Central",
    "francecentral": "France Central",
    "centralindia": "Central India",
    "southindia": "South India",
    "eastus": "East US",
    "eastus2": "East US 2",
    "westus2": "West US 2",
    "southeastasia": "Southeast Asia",
    "australiaeast": "Australia East",
}

# Category display order for grouping in the report.
CATEGORY_ORDER = [
    "Compute",
    "AI",
    "Data & Storage",
    "Integration",
    "Security",
    "Networking",
    "Observability",
    "Analytics",
]


@dataclass(frozen=True)
class PlanContext:
    """Everything the selection rules and quantity formulas need, precomputed."""

    scale: str
    data_gb: float
    requests_per_day: int
    daily_users: int
    compliance: tuple[str, ...]
    task_types: frozenset[str]
    uses_azure_openai: bool
    project_type: str
    monthly_token_cost: float
    monthly_tokens: float
    region_primary: str
    region_openai: str
    region_search: str

    @property
    def has_ai(self) -> bool:
        # A rules engine, a lookup and a threshold alert are task types too,
        # and counting them here gave a finance tracker with no model in it
        # Azure AI Content Safety at $20 a month — a filter for model output
        # that would never be produced.
        return bool(self.task_types - NO_MODEL_TASK_TYPES) or self.uses_azure_openai

    @property
    def needs_search(self) -> bool:
        return bool(self.task_types & {"rag_qa", "document_analysis"})

    @property
    def needs_doc_intel(self) -> bool:
        return bool(self.task_types & {"document_analysis", "data_extraction"})

    @property
    def needs_vision(self) -> bool:
        return "image_analysis" in self.task_types

    @property
    def needs_translation(self) -> bool:
        return "translation" in self.task_types

    @property
    def has_multi_agent(self) -> bool:
        return "multi_agent_orchestration" in self.task_types

    @property
    def has_compliance(self) -> bool:
        return bool(self.compliance)


@dataclass(frozen=True)
class ServiceSpec:
    key: str
    name: str
    category: str
    region_kind: str  # 'primary' | 'openai' | 'search'
    pricing: str  # 'estimate' | 'metered' | 'tokens'
    unit: str
    calc: str  # Azure calculator product slug for the deep link
    purpose: str  # one-line description shown to the LLM selector
    select: Callable[[PlanContext], bool]
    quantity: Callable[[PlanContext], float]
    unit_price: Callable[[PlanContext], float]  # baseline / fallback
    details: Callable[[PlanContext], str]
    tier: Callable[[PlanContext], str]
    retail: Optional[dict] = None  # Retail Prices API filter hints
    included_in_total: bool = True
    net_new: bool = False  # counts as an "extra" service in enhancement mode

    def region(self, ctx: PlanContext) -> str:
        return {
            "openai": ctx.region_openai,
            "search": ctx.region_search,
        }.get(self.region_kind, ctx.region_primary)


def _calc_url(slug: str) -> str:
    return f"https://azure.microsoft.com/en-in/pricing/calculator/?service={slug}"


# ── The candidate catalog ───────────────────────────────────────────
# Order here is the display order within each estimate (already grouped by a
# sensible flow: compute → AI → data → integration → security → net → obs).

SERVICE_SPECS: list[ServiceSpec] = [
    ServiceSpec(
        key="app_hosting",
        name="Azure Container Apps",
        category="Compute",
        region_kind="primary",
        pricing="estimate",
        unit="month",
        calc="container-apps",
        purpose="Serverless container hosting for the app + API (always needed for a new build).",
        select=lambda c: True,
        quantity=lambda c: 1,
        unit_price=lambda c: float(SCALE_INFRA_BASE[c.scale]),
        details=lambda c: "App + API hosting, autoscaling",
        tier=lambda c: c.scale,
    ),
    ServiceSpec(
        key="openai",
        name="Azure OpenAI",
        category="AI",
        region_kind="openai",
        pricing="tokens",
        unit="1M tokens",
        calc="cognitive-services",
        purpose="Managed GPT-4o / GPT-4o-mini endpoints for the AI use cases.",
        select=lambda c: c.uses_azure_openai,
        quantity=lambda c: _round2(c.monthly_tokens / 1e6),
        unit_price=lambda c: _round2(c.monthly_token_cost / max(c.monthly_tokens / 1e6, 1e-9))
        if c.monthly_tokens
        else 0.0,
        details=lambda c: "Token spend (already counted in the AI-tokens line)",
        tier=lambda c: "Standard",
        retail={"service_name": "Foundry Models", "price_type": "Consumption"},
        included_in_total=False,
        net_new=True,
    ),
    ServiceSpec(
        key="ai_search",
        name="Azure AI Search",
        category="AI",
        region_kind="search",
        pricing="metered",
        unit="hour",
        calc="search",
        purpose="Vector + keyword retrieval index for RAG / document grounding.",
        select=lambda c: c.needs_search,
        quantity=lambda c: HOURS_PER_MONTH,
        unit_price=lambda c: _SEARCH_SKU[c.scale][1],
        details=lambda c: "Vector + keyword retrieval for RAG",
        tier=lambda c: _SEARCH_SKU[c.scale][0],
        retail={
            "service_name": "Azure Cognitive Search",
            "sku_contains": lambda c: _SEARCH_SKU[c.scale][0],
            "price_type": "Consumption",
            "unit_hint": "Hour",
        },
        net_new=True,
    ),
    ServiceSpec(
        key="doc_intel",
        name="Azure AI Document Intelligence",
        category="AI",
        region_kind="primary",
        pricing="estimate",
        unit="month",
        calc="ai-document-intelligence",
        purpose="OCR + layout/field extraction for document and data-extraction use cases.",
        select=lambda c: c.needs_doc_intel,
        quantity=lambda c: 1,
        unit_price=lambda c: float(_DOC_INTEL[c.scale]),
        details=lambda c: "OCR & structured field extraction",
        tier=lambda c: "Standard",
        net_new=True,
    ),
    ServiceSpec(
        key="vision",
        name="Azure AI Vision",
        category="AI",
        region_kind="primary",
        pricing="estimate",
        unit="month",
        calc="cognitive-services",
        purpose="Image analysis / tagging / OCR for image-analysis use cases.",
        select=lambda c: c.needs_vision,
        quantity=lambda c: 1,
        unit_price=lambda c: float(_VISION[c.scale]),
        details=lambda c: "Image analysis & tagging",
        tier=lambda c: "Standard",
        net_new=True,
    ),
    ServiceSpec(
        key="translator",
        name="Azure AI Translator",
        category="AI",
        region_kind="primary",
        pricing="estimate",
        unit="month",
        calc="cognitive-services",
        purpose="Machine translation / localisation for translation use cases.",
        select=lambda c: c.needs_translation,
        quantity=lambda c: 1,
        unit_price=lambda c: float(_TRANSLATOR[c.scale]),
        details=lambda c: "Text translation & localisation",
        tier=lambda c: "Standard",
        net_new=True,
    ),
    ServiceSpec(
        key="content_safety",
        name="Azure AI Content Safety",
        category="AI",
        region_kind="primary",
        pricing="estimate",
        unit="month",
        calc="cognitive-services",
        purpose="Prompt/response moderation & jailbreak guardrails on AI traffic.",
        select=lambda c: c.has_ai,
        quantity=lambda c: 1,
        unit_price=lambda c: float(_CONTENT_SAFETY[c.scale]),
        details=lambda c: "Moderation & jailbreak guardrails",
        tier=lambda c: "Standard",
        net_new=True,
    ),
    ServiceSpec(
        key="database",
        name="Azure Cosmos DB",
        category="Data & Storage",
        region_kind="primary",
        pricing="estimate",
        unit="month",
        calc="cosmos-db",
        purpose="Primary operational datastore for application + estimation data.",
        select=lambda c: True,
        quantity=lambda c: 1,
        unit_price=lambda c: float(_jround(40 + c.data_gb * 0.25)),
        details=lambda c: f"~{c.data_gb:g} GB operational data",
        tier=lambda c: "Serverless",
    ),
    ServiceSpec(
        key="storage",
        name="Azure Blob Storage",
        category="Data & Storage",
        region_kind="primary",
        pricing="metered",
        unit="GB/mo",
        calc="storage",
        purpose="Object storage for artifacts, exports and raw documents.",
        select=lambda c: True,
        quantity=lambda c: _round2(max(100.0, c.data_gb)),
        unit_price=lambda c: 0.0184,
        details=lambda c: "Artifacts, exports, raw documents",
        tier=lambda c: "Hot LRS",
        retail={
            "service_name": "Storage",
            "sku_contains": lambda c: "Hot LRS",
            "meter_contains": lambda c: "Data Stored",
            "price_type": "Consumption",
            "unit_hint": "GB",
        },
    ),
    ServiceSpec(
        key="redis",
        name="Azure Cache for Redis",
        category="Data & Storage",
        region_kind="primary",
        pricing="estimate",
        unit="month",
        calc="cache",
        purpose="Low-latency cache for sessions, embeddings and response caching.",
        select=lambda c: c.scale in ("medium", "large", "enterprise"),
        quantity=lambda c: 1,
        unit_price=lambda c: float(_REDIS[c.scale]),
        details=lambda c: "Session & response cache",
        tier=lambda c: "Standard",
    ),
    ServiceSpec(
        key="apim",
        name="Azure API Management",
        category="Integration",
        region_kind="primary",
        pricing="estimate",
        unit="month",
        calc="api-management",
        purpose="API gateway: throttling, keys, routing in front of services.",
        select=lambda c: c.scale in ("medium", "large", "enterprise"),
        quantity=lambda c: 1,
        unit_price=lambda c: float(SCALE_APIM[c.scale]),
        details=lambda c: "Gateway, throttling, keys",
        tier=lambda c: c.scale,
    ),
    ServiceSpec(
        key="service_bus",
        name="Azure Service Bus",
        category="Integration",
        region_kind="primary",
        pricing="estimate",
        unit="month",
        calc="service-bus",
        purpose="Durable messaging / queues for agent orchestration and async jobs.",
        select=lambda c: c.has_multi_agent or c.scale in ("large", "enterprise"),
        quantity=lambda c: 1,
        unit_price=lambda c: float(_SERVICE_BUS[c.scale]),
        details=lambda c: "Async messaging & orchestration",
        tier=lambda c: "Standard",
        net_new=True,
    ),
    ServiceSpec(
        key="key_vault",
        name="Azure Key Vault",
        category="Security",
        region_kind="primary",
        pricing="metered",
        unit="10K ops",
        calc="key-vault",
        purpose="Secret / key / certificate storage for service credentials.",
        select=lambda c: True,
        quantity=lambda c: _round2(c.requests_per_day * 30 * 2 / 10000),
        unit_price=lambda c: 0.03,
        details=lambda c: "Secrets, keys & certificates",
        tier=lambda c: "Standard",
        retail={
            "service_name": "Key Vault",
            "meter_contains": lambda c: "Operations",
            "price_type": "Consumption",
        },
        net_new=True,
    ),
    ServiceSpec(
        key="defender",
        name="Microsoft Defender for Cloud",
        category="Security",
        region_kind="primary",
        pricing="estimate",
        unit="month",
        calc="security-center",
        purpose="Cloud workload protection & posture management baseline.",
        select=lambda c: True,
        quantity=lambda c: 1,
        unit_price=lambda c: float(_DEFENDER[c.scale]),
        details=lambda c: "Workload protection & posture",
        tier=lambda c: "Standard",
    ),
    ServiceSpec(
        key="private_net",
        name="Azure Private Networking",
        category="Networking",
        region_kind="primary",
        pricing="estimate",
        unit="month",
        calc="virtual-network",
        purpose="VNet + private endpoints for data-residency / compliance isolation.",
        select=lambda c: c.has_compliance,
        quantity=lambda c: 1,
        unit_price=lambda c: float(_PRIVATE_NET[c.scale]),
        details=lambda c: "VNet & private endpoints",
        tier=lambda c: "Standard",
        net_new=True,
    ),
    ServiceSpec(
        key="front_door",
        name="Azure Front Door / WAF",
        category="Networking",
        region_kind="primary",
        pricing="estimate",
        unit="month",
        calc="frontdoor",
        purpose="Global entry, CDN and web application firewall.",
        select=lambda c: c.scale in ("large", "enterprise") or c.has_compliance,
        quantity=lambda c: 1,
        unit_price=lambda c: float(_FRONT_DOOR[c.scale]),
        details=lambda c: "Global routing, CDN & WAF",
        tier=lambda c: "Standard",
    ),
    ServiceSpec(
        key="monitor",
        name="Azure Monitor",
        category="Observability",
        region_kind="primary",
        pricing="estimate",
        unit="month",
        calc="monitor",
        purpose="Logs, metrics, traces and alerting (App Insights + Log Analytics).",
        select=lambda c: True,
        quantity=lambda c: 1,
        unit_price=lambda c: float(_jround(30 + c.requests_per_day * 0.00002)),
        details=lambda c: "Telemetry, logs & alerting",
        tier=lambda c: "Pay-as-you-go",
    ),
    # ── stated-stack only (see `_APP_SERVICE_INSTANCES` above) ──────────
    ServiceSpec(
        key="app_service",
        name="Azure App Service",
        category="Compute",
        region_kind="primary",
        pricing="metered",
        unit="instance-hour",
        calc="app-service",
        purpose="Managed web hosting for the frontend and the API on one Premium v3 plan.",
        select=lambda c: False,
        quantity=lambda c: float(_APP_SERVICE_INSTANCES[c.scale] * HOURS_PER_MONTH),
        unit_price=lambda c: 0.168,
        details=lambda c: (f"{_APP_SERVICE_INSTANCES[c.scale]} × P1v3 Linux instance"
                           f"{'s' if _APP_SERVICE_INSTANCES[c.scale] > 1 else ''}, frontend and API "
                           "on one plan · a Windows plan is about twice this"),
        tier=lambda c: "Premium v3 P1v3",
        retail={"service_name": "Azure App Service",
                "product_eq": "Azure App Service Premium v3 Plan - Linux",
                "sku_eq": "P1 v3", "meter_eq": "P1 v3 App", "price_type": "Consumption"},
    ),
    ServiceSpec(
        key="azure_sql",
        name="Azure SQL Database",
        category="Data & Storage",
        region_kind="primary",
        pricing="metered",
        unit="vCore-hour",
        calc="azure-sql-database",
        purpose="Relational database for the application's records.",
        select=lambda c: False,
        quantity=lambda c: float(_SQL_VCORES[c.scale] * HOURS_PER_MONTH),
        unit_price=lambda c: 0.155261,
        details=lambda c: (f"General Purpose Gen5, {_SQL_VCORES[c.scale]} vCores provisioned · "
                           "storage (about $4 for 32 GB) not included"),
        tier=lambda c: f"General Purpose · {_SQL_VCORES[c.scale]} vCore",
        # Every vCore size shares the meter name; the cheapest is one vCore,
        # which is the per-vCore rate the quantity multiplies.
        retail={"service_name": "SQL Database",
                "product_eq": "SQL Database Single/Elastic Pool General Purpose - Compute Gen5",
                "meter_eq": "vCore", "price_type": "Consumption"},
    ),
    ServiceSpec(
        key="functions",
        name="Azure Functions",
        category="Compute",
        region_kind="primary",
        pricing="estimate",
        unit="month",
        calc="functions",
        purpose="Background jobs: queue consumers, scheduled reminders, bulk email fan-out.",
        select=lambda c: False,
        quantity=lambda c: 1,
        unit_price=lambda c: 0.0,
        details=lambda c: ("Consumption plan · within the monthly free grant of 1M executions "
                           "and 400,000 GB-s at this load"),
        tier=lambda c: "Consumption",
    ),
    ServiceSpec(
        key="service_bus_std",
        name="Azure Service Bus",
        category="Integration",
        region_kind="primary",
        pricing="metered",
        unit="hour",
        calc="service-bus",
        purpose="Durable queues between the API and the background jobs.",
        select=lambda c: False,
        quantity=lambda c: float(HOURS_PER_MONTH),
        unit_price=lambda c: 0.013441,
        details=lambda c: "Standard tier base unit · first 13M operations a month included",
        tier=lambda c: "Standard",
        retail={"service_name": "Service Bus", "sku_eq": "Standard",
                "meter_eq": "Standard Base Unit", "price_type": "Consumption",
                "unit_hint": "Hour"},
    ),
    ServiceSpec(
        key="app_gateway",
        name="Azure Application Gateway",
        category="Networking",
        region_kind="primary",
        pricing="metered",
        unit="hour",
        calc="application-gateway",
        purpose="Regional layer-7 entry point in front of App Service.",
        select=lambda c: False,
        quantity=lambda c: float(HOURS_PER_MONTH),
        unit_price=lambda c: 0.24,
        details=lambda c: ("Standard v2 fixed cost · capacity units are $0.008/h each on top; "
                           "WAF v2 is about 1.8× the fixed cost"),
        tier=lambda c: "Standard v2",
        retail={"service_name": "Application Gateway",
                "product_eq": "Application Gateway Standard v2",
                "meter_eq": "Standard Fixed Cost", "price_type": "Consumption"},
    ),
    ServiceSpec(
        key="log_analytics",
        name="Azure Log Analytics",
        category="Observability",
        region_kind="primary",
        pricing="metered",
        unit="GB",
        calc="monitor",
        purpose="Workspace for application and platform logs.",
        select=lambda c: False,
        quantity=lambda c: float(_LOG_GB[c.scale]),
        unit_price=lambda c: 2.76,
        details=lambda c: (f"~{_LOG_GB[c.scale]} GB ingested a month · the 5 GB free allowance "
                           "is shared across the billing account, so not deducted"),
        tier=lambda c: "Analytics Logs",
        retail={"service_name": "Log Analytics",
                "meter_eq": "Analytics Logs Data Ingestion", "price_type": "Consumption"},
    ),
    ServiceSpec(
        key="app_registration",
        name="Microsoft Entra app registration",
        category="Security",
        region_kind="primary",
        pricing="estimate",
        unit="month",
        calc="active-directory",
        purpose="Sign-in for the application through the organisation's Entra ID.",
        select=lambda c: False,
        quantity=lambda c: 1,
        unit_price=lambda c: 0.0,
        details=lambda c: "No charge — part of the organisation's existing Entra ID",
        tier=lambda c: "Entra ID",
    ),
    ServiceSpec(
        key="sendgrid",
        name="SendGrid Email API",
        category="Integration",
        region_kind="primary",
        pricing="estimate",
        unit="month",
        calc="",
        purpose="Outbound email: stage notifications, reminders, recipient emails.",
        select=lambda c: False,
        quantity=lambda c: 1,
        unit_price=lambda c: _SENDGRID[c.scale][1],
        details=lambda c: ("Twilio's price, not an Azure meter — check before committing · "
                           "one 100,000-recipient distribution needs the 100K plan"),
        tier=lambda c: _SENDGRID[c.scale][0],
    ),
    ServiceSpec(
        key="power_bi",
        name="Power BI Pro",
        category="Analytics",
        region_kind="primary",
        pricing="estimate",
        unit="user/month",
        calc="",
        purpose="Dashboards and team-wise reporting.",
        select=lambda c: False,
        quantity=lambda c: 1,
        unit_price=lambda c: _POWER_BI_PRO,
        details=lambda c: ("Per viewer, not counted: usually licensed across the organisation "
                           "already. Viewing without Pro needs a capacity SKU (F64 or P1)"),
        tier=lambda c: "Pro",
        included_in_total=False,
    ),
]

SPEC_BY_KEY: dict[str, ServiceSpec] = {s.key: s for s in SERVICE_SPECS}


def calc_url_for(spec: ServiceSpec) -> str:
    # SendGrid and Power BI have no page on Azure's calculator, and a link to
    # its front page beside them would read as a price check that isn't one.
    return _calc_url(spec.calc) if spec.calc else ""


# ── Plan-context builder ────────────────────────────────────────────

def build_plan_context(
    inp: ProjectInput,
    *,
    uses_azure_openai: bool,
    monthly_token_cost: float,
    monthly_tokens: float,
) -> PlanContext:
    tp = inp.technical_preferences
    vs = inp.volume_and_scale
    data_gb = vs.data_volume_gb if (vs and vs.data_volume_gb is not None) else 10.0
    requests = vs.requests_per_day if (vs and vs.requests_per_day is not None) else 1000
    users = vs.expected_daily_users if (vs and vs.expected_daily_users is not None) else 100
    primary = (tp.azure_region if tp else None) or "northeurope"
    openai_region = (tp.azure_openai_region if tp else None) or primary
    search_region = (tp.azure_search_region if tp else None) or primary
    task_types = frozenset(uc.task_type for uc in (inp.ai_use_cases or []) if uc.task_type)

    return PlanContext(
        scale=inp.scale or "medium",
        data_gb=float(data_gb),
        requests_per_day=int(requests),
        daily_users=int(users),
        compliance=tuple(tp.compliance_requirements if tp else []),
        task_types=task_types,
        uses_azure_openai=uses_azure_openai,
        project_type=inp.project_type,
        monthly_token_cost=float(monthly_token_cost),
        monthly_tokens=float(monthly_tokens),
        region_primary=primary,
        region_openai=openai_region,
        region_search=search_region,
    )
