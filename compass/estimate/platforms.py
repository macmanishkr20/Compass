"""Delivery-platform cost-model strategies.

Azure PaaS is one *delivery archetype*, not the only one. Different platforms
have different cost *shapes*, not just different price books:

  • consumption  (Azure / AWS / GCP) — metered compute × data × AI transactions
  • licensing    (Microsoft 365 / Copilot) — per-seat productivity + Copilot
                  add-on + Power Platform capacity; AI is bundled in the seat
  • capex        (on-prem / private cloud) — hardware amortized + ops staffing

`classify_platform` picks the platform from an explicit choice or, failing that,
from intake keywords (existing infra / hosting platform / deployment target).
The consumption path reuses the Azure catalog + planner; AWS/GCP overlay the
provider's service names. The licensing and capex paths are their own
deterministic line-item builders here. As everywhere in CostCompass, code sets
every dollar — nothing here is LLM-computed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .azure_catalog import PlanContext
from .types import ProjectInput


def _jround(n: float) -> int:
    return math.floor(n + 0.5)


def _round2(n: float) -> float:
    return math.floor(n * 100 + 0.5) / 100


# ── Platform profiles ───────────────────────────────────────────────

@dataclass(frozen=True)
class PlatformProfile:
    key: str
    label: str
    cost_model: str            # 'consumption' | 'licensing' | 'capex'
    meters_tokens: bool        # False = AI usage is bundled into a per-seat licence
    provider: Optional[str]    # 'azure' | 'aws' | 'gcp' for consumption, else None
    default_region: str


PLATFORM_PROFILES: dict[str, PlatformProfile] = {
    "azure_paas": PlatformProfile("azure_paas", "Azure PaaS", "consumption", True, "azure", "eastus"),
    "aws": PlatformProfile("aws", "Amazon Web Services", "consumption", True, "aws", "us-east-1"),
    "gcp": PlatformProfile("gcp", "Google Cloud", "consumption", True, "gcp", "us-central1"),
    "m365_copilot": PlatformProfile("m365_copilot", "Microsoft 365 + Copilot", "licensing", False, None, ""),
    "on_prem": PlatformProfile("on_prem", "On-premises / private cloud", "capex", True, None, ""),
}

DEFAULT_PLATFORM = "azure_paas"


# ── Classification (explicit choice, else keyword inference) ─────────
# Order matters: the first family whose keyword appears wins.

_PLATFORM_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("m365_copilot", (
        "sharepoint", "microsoft 365", "m365", "office 365", "o365",
        "power platform", "power apps", "powerapps", "power automate",
        "dataverse", "copilot studio", "copilot", "power bi",
    )),
    ("aws", (
        "aws", "amazon web services", "lambda", "bedrock", "dynamodb",
        "ec2", "fargate", "amazon s3", "s3 bucket", "eks", "sagemaker",
    )),
    ("gcp", (
        "gcp", "google cloud", "vertex ai", "bigquery", "gke",
        "cloud run", "firebase", "firestore",
    )),
    ("on_prem", (
        "on-prem", "on prem", "on-premise", "on premise", "on-premises",
        "bare metal", "bare-metal", "vmware", "data center", "datacenter",
        "data centre", "self-hosted", "self hosted", "private cloud", "openshift",
    )),
]


def classify_platform(inp: ProjectInput) -> str:
    """Pick the delivery platform: explicit choice first, else keyword inference."""
    tp = inp.technical_preferences
    if tp and getattr(tp, "delivery_platform", None):
        return tp.delivery_platform  # type: ignore[return-value]

    haystack = " ".join(
        filter(None, [
            tp.existing_infra if tp else "",
            tp.preferred_llm_provider if tp else "",
            inp.current_architecture.hosting_platform if inp.current_architecture else "",
            inp.integration_constraints.deployment_target if inp.integration_constraints else "",
            inp.description or "",
        ])
    ).lower()

    for key, keywords in _PLATFORM_KEYWORDS:
        if any(kw in haystack for kw in keywords):
            return key
    return DEFAULT_PLATFORM


# ── Consumption provider overlay (AWS / GCP names for the Azure catalog) ──

_AWS_CALC = "https://calculator.aws/#/"
_GCP_CALC = "https://cloud.google.com/products/calculator"

_PROVIDER_NAMES: dict[str, dict[str, str]] = {
    "aws": {
        "app_hosting": "AWS Fargate (ECS)",
        "openai": "Amazon Bedrock",
        "ai_search": "Amazon OpenSearch (vector)",
        "doc_intel": "Amazon Textract",
        "vision": "Amazon Rekognition",
        "translator": "Amazon Translate",
        "content_safety": "Amazon Bedrock Guardrails",
        "database": "Amazon DynamoDB",
        "storage": "Amazon S3",
        "redis": "Amazon ElastiCache (Redis)",
        "apim": "Amazon API Gateway",
        "service_bus": "Amazon SQS",
        "key_vault": "AWS Secrets Manager",
        "defender": "Amazon GuardDuty",
        "private_net": "Amazon VPC + PrivateLink",
        "front_door": "Amazon CloudFront + WAF",
        "monitor": "Amazon CloudWatch",
    },
    "gcp": {
        "app_hosting": "Cloud Run",
        "openai": "Vertex AI",
        "ai_search": "Vertex AI Vector Search",
        "doc_intel": "Document AI",
        "vision": "Cloud Vision API",
        "translator": "Cloud Translation",
        "content_safety": "Vertex AI Safety",
        "database": "Cloud Firestore",
        "storage": "Cloud Storage",
        "redis": "Memorystore (Redis)",
        "apim": "Apigee API Management",
        "service_bus": "Pub/Sub",
        "key_vault": "Secret Manager",
        "defender": "Security Command Center",
        "private_net": "VPC Service Controls",
        "front_door": "Cloud CDN + Cloud Armor",
        "monitor": "Cloud Monitoring",
    },
}


def provider_display(provider: Optional[str], spec_key: str, azure_name: str, azure_url: str) -> tuple[str, Optional[str]]:
    """Return (service_name, pricing_url) for a consumption service under `provider`."""
    if provider == "aws":
        return _PROVIDER_NAMES["aws"].get(spec_key, azure_name), _AWS_CALC
    if provider == "gcp":
        return _PROVIDER_NAMES["gcp"].get(spec_key, azure_name), _GCP_CALC
    return azure_name, azure_url  # azure: catalog name + per-service calculator deep link


# ── Shared line-item helper ─────────────────────────────────────────

def _line(
    *,
    name: str,
    category: str,
    tier: str,
    qty: float,
    unit: str,
    unit_price: float,
    details: str,
    url: Optional[str] = None,
    included: bool = True,
    region: str = "",
) -> dict:
    return {
        "service_name": name,
        "category": category,
        "tier": tier,
        "region": region,
        "quantity": _round2(qty),
        "unit": unit,
        "unit_price": _round2(unit_price),
        "monthly_cost": _round2(unit_price * qty),
        "price_source": "estimate",
        "included_in_total": included,
        "details": details,
        "azure_pricing_url": url,
    }


# ── Licensing strategy: Microsoft 365 + Copilot ─────────────────────
# Per-seat, not per-request. Cost scales with headcount (daily users).

_M365_E3 = 36.0
_M365_E5 = 57.0
_COPILOT = 30.0
_POWER_APPS = 20.0
_AI_BUILDER = 500.0
_POWER_AUTOMATE = 100.0
_M365_PRICING = "https://www.microsoft.com/microsoft-365/enterprise/microsoft365-plans-and-pricing"
_POWER_PRICING = "https://www.microsoft.com/power-platform/pricing"


def m365_services(ctx: PlanContext) -> list[dict]:
    seats = max(1, ctx.daily_users)
    e5 = ctx.has_compliance
    services: list[dict] = [
        _line(
            name=f"Microsoft 365 {'E5' if e5 else 'E3'}",
            category="Licensing",
            tier="E5" if e5 else "E3",
            qty=seats, unit="seat/mo",
            unit_price=_M365_E5 if e5 else _M365_E3,
            details="Per-seat productivity, SharePoint & security licence",
            url=_M365_PRICING,
        ),
    ]
    if ctx.has_ai:
        services.append(_line(
            name="Microsoft 365 Copilot",
            category="AI",
            tier="Add-on",
            qty=seats, unit="seat/mo",
            unit_price=_COPILOT,
            details="Generative-AI assistant — model usage included (replaces metered tokens)",
            url="https://www.microsoft.com/microsoft-365/copilot",
        ))
    services.append(_line(
        name="Power Apps Premium",
        category="Compute",
        tier="Per user",
        qty=seats, unit="user/mo",
        unit_price=_POWER_APPS,
        details="Custom app surface built on Power Platform",
        url=_POWER_PRICING,
    ))
    services.append(_line(
        name="Dataverse capacity",
        category="Data & Storage",
        tier="Database",
        qty=1, unit="month",
        unit_price=float(_jround(40 + ctx.data_gb * 5)),
        details=f"~{ctx.data_gb:g} GB Dataverse database & file storage",
        url=_POWER_PRICING,
    ))
    if ctx.needs_doc_intel or ctx.needs_vision or ctx.needs_translation:
        services.append(_line(
            name="AI Builder credits",
            category="AI",
            tier="Capacity",
            qty=1, unit="pack/mo",
            unit_price=_AI_BUILDER,
            details="Custom forms processing / prediction models",
            url=_POWER_PRICING,
        ))
    if ctx.has_multi_agent or ("rules_workflow" in ctx.task_types):
        services.append(_line(
            name="Power Automate (hosted process)",
            category="Integration",
            tier="Per flow",
            qty=1, unit="month",
            unit_price=_POWER_AUTOMATE,
            details="Automated workflows & RPA flows",
            url=_POWER_PRICING,
        ))
    return services


# ── Capex strategy: on-premises / private cloud ─────────────────────
# Hardware is a capital purchase amortized over 48 months, plus the ops staff a
# self-hosted platform needs (cloud folds this into the service price).

_SERVER_AMORT = 250.0   # ~$12k server / 48 months
_GPU_AMORT = 833.0      # ~$40k GPU node / 48 months
_OPS_FTE = 12000.0      # loaded monthly cost of one infra-ops/SRE engineer
_SERVERS = {"small": 2, "medium": 4, "large": 8, "enterprise": 16}
_GPU = {"small": 1, "medium": 2, "large": 4, "enterprise": 8}
_OPS = {"small": 0.5, "medium": 1.0, "large": 2.0, "enterprise": 4.0}
_FACILITIES = {"small": 400, "medium": 900, "large": 2000, "enterprise": 4500}
_SEC_HW = {"small": 300, "medium": 700, "large": 1500, "enterprise": 3200}


def onprem_services(ctx: PlanContext) -> list[dict]:
    scale = ctx.scale
    services: list[dict] = [
        _line(
            name="Application servers (amortized)",
            category="Compute",
            tier="Capex / 48 mo",
            qty=_SERVERS[scale], unit="server/mo",
            unit_price=_SERVER_AMORT,
            details="Rack servers for app + API tier, 4-year amortization",
        ),
    ]
    if ctx.has_ai:
        services.append(_line(
            name="GPU inference servers (amortized)",
            category="AI",
            tier="Capex / 48 mo",
            qty=_GPU[scale], unit="server/mo",
            unit_price=_GPU_AMORT,
            details="GPU nodes for self-hosted model inference",
        ))
    services.append(_line(
        name="Storage array (amortized)",
        category="Data & Storage",
        tier="SAN/NAS",
        qty=1, unit="month",
        unit_price=float(_jround(120 + ctx.data_gb * 2)),
        details=f"~{ctx.data_gb:g} GB primary operational storage",
    ))
    services.append(_line(
        name="Backup & disaster recovery",
        category="Data & Storage",
        tier="Standard",
        qty=1, unit="month",
        unit_price=float(_jround(150 + ctx.data_gb)),
        details="Offsite backup & DR replication",
    ))
    if ctx.has_compliance:
        services.append(_line(
            name="Security appliances (amortized)",
            category="Security",
            tier="Capex / 48 mo",
            qty=1, unit="month",
            unit_price=float(_SEC_HW[scale]),
            details="Firewalls, HSM & compliance hardware",
        ))
    services.append(_line(
        name="Networking & facilities",
        category="Networking",
        tier=scale,
        qty=1, unit="month",
        unit_price=float(_FACILITIES[scale]),
        details="Power, cooling, colocation & egress",
    ))
    services.append(_line(
        name="Operations & SRE staff",
        category="Observability",
        tier="FTE",
        qty=_OPS[scale], unit="FTE/mo",
        unit_price=_OPS_FTE,
        details="Dedicated infra ops to run the self-hosted platform",
    ))
    return services


# ── Per-platform explanatory notes for the report ───────────────────

def platform_notes(platform: str, ctx: PlanContext) -> list[str]:
    if platform == "m365_copilot":
        return [
            "Cost is licensing-led: it scales with seats (users), not compute hours or requests.",
            "AI usage is bundled into the Microsoft 365 Copilot per-seat add-on, so the metered AI-tokens line is $0 to avoid double-counting.",
            "Microsoft hosts the runtime (SharePoint / Power Platform); there are no app-server or database compute meters.",
        ]
    if platform == "on_prem":
        return [
            "Cost is capex-led: hardware is a capital purchase amortized over 48 months, not a monthly metered bill.",
            "Includes dedicated operations / SRE staffing — self-hosted infrastructure needs people to run it, which cloud folds into the service price.",
            "GPU servers cover self-hosted model inference; if you instead call a hosted LLM API, the AI-tokens line applies on top.",
        ]
    if platform in ("aws", "gcp"):
        prov = PLATFORM_PROFILES[platform].label
        return [
            f"Consumption model on {prov}: metered compute × data × AI transactions, same cost shape as Azure with the equivalent {prov} services.",
            "Unit prices are deterministic baselines (no live retail-price lookup for this provider yet); quantities are computed from your scale and volume.",
        ]
    return []  # azure_paas: the existing live-priced consumption model speaks for itself
