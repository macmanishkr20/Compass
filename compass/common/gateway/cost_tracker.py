"""Per-session usage accounting — the port of cost-tracker.ts.

Azure OpenAI bills per deployment; rates here are illustrative defaults and
should be overridden per deployment in .compass/settings.json for real
chargeback. Cached prompt tokens are tracked separately because they are the
lever that matters at agent scale.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# USD per 1M tokens: (input, cached input, output). Illustrative defaults.
DEFAULT_RATES: dict[str, tuple[float, float, float]] = {
    "gpt-4o": (2.50, 1.25, 10.00),
    "gpt-4o-mini": (0.15, 0.075, 0.60),
    "gpt-4.1": (2.00, 0.50, 8.00),
    "mock": (0.0, 0.0, 0.0),
}
FALLBACK_RATE = (2.50, 1.25, 10.00)


@dataclass
class ModelUsage:
    prompt_tokens: int = 0
    cached_prompt_tokens: int = 0
    completion_tokens: int = 0
    requests: int = 0


@dataclass
class CostTracker:
    by_model: dict[str, ModelUsage] = field(default_factory=dict)

    def record(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cached_prompt_tokens: int = 0,
    ) -> None:
        usage = self.by_model.setdefault(model, ModelUsage())
        usage.prompt_tokens += prompt_tokens
        usage.cached_prompt_tokens += cached_prompt_tokens
        usage.completion_tokens += completion_tokens
        usage.requests += 1

    def total_cost_usd(self) -> float:
        total = 0.0
        for model, usage in self.by_model.items():
            in_rate, cached_rate, out_rate = DEFAULT_RATES.get(model, FALLBACK_RATE)
            uncached = usage.prompt_tokens - usage.cached_prompt_tokens
            total += (
                uncached * in_rate
                + usage.cached_prompt_tokens * cached_rate
                + usage.completion_tokens * out_rate
            ) / 1_000_000
        return round(total, 6)

    def cache_hit_rate(self) -> float:
        prompt = sum(u.prompt_tokens for u in self.by_model.values())
        cached = sum(u.cached_prompt_tokens for u in self.by_model.values())
        return round(cached / prompt, 4) if prompt else 0.0
