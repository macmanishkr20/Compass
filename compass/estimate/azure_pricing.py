"""Azure Retail Prices API client.

Public, unauthenticated endpoint (https://prices.azure.com/api/retail/prices)
queried with an OData `$filter`. Returns a per-unit USD price for a service in a
region, or `None` when the price can't be resolved (offline, timeout, no match)
so callers fall back to the catalog baseline.

Prices are cached in-process for an hour: a single estimate touches each
service once, and figures stay stable across the request.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

from .azure_catalog import PlanContext, ServiceSpec
from compass.common.config import get_settings

logger = logging.getLogger("compass.estimate")

RETAIL_URL = "https://prices.azure.com/api/retail/prices"
_CACHE_TTL = 3600.0
_TIMEOUT = 6.0

# filter string -> (timestamp, price | None)
_cache: dict[str, tuple[float, Optional[float]]] = {}


def _odata(value: str) -> str:
    """Single-quote and escape an OData string literal."""
    return "'" + value.replace("'", "''") + "'"


def _build_filter(spec: ServiceSpec, ctx: PlanContext, region: str) -> Optional[str]:
    r = spec.retail
    if not r:
        return None
    clauses = [f"serviceName eq {_odata(r['service_name'])}"]
    if region:
        clauses.append(f"armRegionName eq {_odata(region)}")
    if r.get("price_type"):
        clauses.append(f"priceType eq {_odata(r['price_type'])}")
    sku = r.get("sku_contains")
    if callable(sku):
        sku = sku(ctx)
    if sku:
        clauses.append(f"contains(skuName, {_odata(sku)})")
    meter = r.get("meter_contains")
    if callable(meter):
        meter = meter(ctx)
    if meter:
        clauses.append(f"contains(meterName, {_odata(meter)})")
    return " and ".join(clauses)


def _pick_price(items: list[dict], unit_hint: Optional[str]) -> Optional[float]:
    """Choose the most representative consumption price from the result set."""
    candidates: list[float] = []
    hinted: list[float] = []
    for it in items:
        price = it.get("retailPrice")
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        meter = (it.get("meterName") or "").lower()
        # Drop free tiers and confidential-compute variants — they distort the figure.
        if "free" in meter or "confidential" in meter:
            continue
        candidates.append(float(price))
        uom = (it.get("unitOfMeasure") or "")
        if unit_hint and unit_hint.lower() in uom.lower():
            hinted.append(float(price))
    pool = hinted or candidates
    return min(pool) if pool else None


def get_unit_price(spec: ServiceSpec, ctx: PlanContext, region: str) -> Optional[float]:
    """Live per-unit USD price for `spec` in `region`, or None to fall back."""
    if not get_settings().estimate.live_pricing:
        return None
    flt = _build_filter(spec, ctx, region)
    if not flt:
        return None

    now = time.time()
    cached = _cache.get(flt)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]

    price: Optional[float] = None
    try:
        resp = httpx.get(
            RETAIL_URL,
            params={"currencyCode": "'USD'", "$filter": flt, "$top": 100},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        items = resp.json().get("Items", [])
        price = _pick_price(items, spec.retail.get("unit_hint") if spec.retail else None)
    except Exception as exc:  # noqa: BLE001 - any failure means fall back to baseline
        logger.warning("Retail price lookup failed for %s (%s): %s", spec.key, region, exc)
        price = None

    _cache[flt] = (now, price)
    return price
