"""Per-turn token budget tracking for AgentRunner.

A TurnBudget caps the total tokens (input + output, optionally cost in USD)
consumed by a single AgentRunner.run() invocation. When the cumulative
usage exceeds the configured limits, the runner stops with
stop_reason="budget_exceeded" instead of continuing to max_iterations.

This prevents runaway agent loops that burn tokens without producing useful
output, and gives callers a hard cost ceiling per turn.

All limits are optional: set a limit to None to disable that dimension.

Single-tier defaults: AgentLoop resolves per-turn ceilings from explicit
``maxInputTokensPerTurn`` / ``maxCostPerTurnUsd`` config, falling back to
the built-in defaults below.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_INPUT_TOKENS = 200_000  # ~5-10 turns of dense context
DEFAULT_MAX_OUTPUT_TOKENS = 50_000  # generous cap for multi-step reasoning
DEFAULT_MAX_COST_USD = 5.0  # hard cost ceiling per turn


@dataclass(slots=True)
class TurnBudget:
    """Cumulative token/cost budget for a single agent turn.

    Attributes:
        max_input_tokens:  Hard cap on cumulative prompt_tokens. None = unlimited.
        max_output_tokens: Hard cap on cumulative completion_tokens. None = unlimited.
        max_cost_usd:      Optional cost ceiling. None = no cost tracking.
        max_iterations:   Optional override for spec.max_iterations. None = use spec.
        used_input:        Cumulative input tokens consumed so far.
        used_output:       Cumulative output tokens consumed so far.
        used_cache_hit:    Cumulative cache-hit (cached_tokens) tokens so far.
        used_cache_miss:   Cumulative cache-miss input tokens so far.
        used_cost:         Cumulative cost in USD (only if pricing provided).
        pricing:           Optional dict mapping model name -> (input_per_1k, output_per_1k).
    """

    max_input_tokens: int | None = DEFAULT_MAX_INPUT_TOKENS
    max_output_tokens: int | None = DEFAULT_MAX_OUTPUT_TOKENS
    max_cost_usd: float | None = DEFAULT_MAX_COST_USD
    max_iterations: int | None = None
    used_input: int = 0
    used_output: int = 0
    used_cache_hit: int = 0
    used_cache_miss: int = 0
    used_cost: float = 0.0
    pricing: dict[str, tuple[float, float]] | None = None
    require_cost_tracking: bool = False
    exceeded_reason: str | None = None  # set when check() first fails
    cost_unavailable_model: str | None = None

    def accumulate(self, usage: dict[str, Any], model: str) -> None:
        """Add one LLM call's usage to the running totals.

        Optional keys recognized: prompt_tokens, completion_tokens,
        total_tokens, cost_usd, cached_tokens, prompt_cache_hit_tokens,
        prompt_cache_miss_tokens.
        """
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        completion = int(usage.get("completion_tokens", 0) or 0)
        # Normalized cache-hit key across providers (see provider usage
        # extraction): DeepSeek/SiliconFlow hit tokens land here too.
        cache_hit = int(usage.get("cached_tokens", 0) or 0)
        # Some providers report cache stats separately; count cache misses
        # toward input consumption (cache hits are ~free).
        separate_hit = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
        if separate_hit > 0 and prompt == 0:
            # DeepSeek-style split reporting: hit/miss replace prompt_tokens.
            prompt = int(usage.get("prompt_cache_miss_tokens", 0) or 0)
            cache_hit = separate_hit
            cache_miss = prompt
        else:
            cache_miss = max(prompt - cache_hit, 0)
        self.used_input += prompt
        self.used_output += completion
        self.used_cache_hit += cache_hit
        self.used_cache_miss += cache_miss

        # Cost tracking: prefer explicit cost_usd if provider reports it
        cost = usage.get("cost_usd")
        if isinstance(cost, (int, float)) and cost >= 0:
            self.used_cost += float(cost)
        elif self.pricing is not None and model in self.pricing:
            in_per_1k, out_per_1k = self.pricing[model]
            self.used_cost += (prompt / 1000.0) * in_per_1k
            self.used_cost += (completion / 1000.0) * out_per_1k
        elif self.require_cost_tracking and self.max_cost_usd is not None:
            self.cost_unavailable_model = model

    def check(self) -> str | None:
        """Return a stop reason if budget is exceeded, None to continue.

        Idempotent within a turn: once exceeded, returns the same reason.
        """
        if self.exceeded_reason is not None:
            return self.exceeded_reason
        if self.max_input_tokens is not None and self.used_input > self.max_input_tokens:
            self.exceeded_reason = (
                f"input_tokens_exceeded ({self.used_input} > {self.max_input_tokens})"
            )
            return self.exceeded_reason
        if self.max_output_tokens is not None and self.used_output > self.max_output_tokens:
            self.exceeded_reason = (
                f"output_tokens_exceeded ({self.used_output} > {self.max_output_tokens})"
            )
            return self.exceeded_reason
        if self.cost_unavailable_model is not None:
            self.exceeded_reason = (
                f"cost_tracking_unavailable (model={self.cost_unavailable_model})"
            )
            return self.exceeded_reason
        if self.max_cost_usd is not None and self.used_cost > self.max_cost_usd:
            self.exceeded_reason = (
                f"cost_exceeded (${self.used_cost:.4f} > ${self.max_cost_usd:.4f})"
            )
            return self.exceeded_reason
        return None

    def cache_hit_ratio(self) -> float | None:
        """Return cached/(cached+miss) ratio, or None when no input recorded."""
        denominator = self.used_cache_hit + self.used_cache_miss
        if denominator <= 0:
            return None
        return self.used_cache_hit / denominator

    def summary(self) -> str:
        """Human-readable one-line summary for logs/UI."""
        parts = [f"in={self.used_input}", f"out={self.used_output}"]
        if self.max_cost_usd is not None:
            parts.append(f"cost=${self.used_cost:.4f}")
        ratio = self.cache_hit_ratio()
        if ratio is not None:
            parts.append(f"cache={round(ratio * 100)}%")
        if self.exceeded_reason:
            parts.append(f"BUDGET_EXCEEDED({self.exceeded_reason})")
        return " ".join(parts)
