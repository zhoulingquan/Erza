"""Turn-budget resolution (single-tier, P4 simplification).

Explicit ``maxInputTokensPerTurn`` / ``maxCostPerTurnUsd`` config wins;
otherwise the built-in defaults (200k input / $5) apply. No planning-mode
tiers and no fast/managed fields.
"""

from __future__ import annotations

from erza.agent.loop import AgentLoop
from erza.config.schema import Config


class FakeProvider:
    def get_default_model(self) -> str:
        return "test-model"

    class Generation:
        max_tokens = 8192

    generation = Generation()


def _config(defaults: dict) -> Config:
    return Config.model_validate(
        {
            "agents": {"defaults": defaults},
            "providers": {"custom": {"api_key": "sk-test", "api_base": "http://test"}},
            "tools": {},
        }
    )


def _loop(defaults: dict) -> AgentLoop:
    return AgentLoop.from_config(_config(defaults), provider=FakeProvider())


def test_default_budget_200k_and_5usd() -> None:
    loop = _loop({})
    budget = loop._build_turn_budget()
    assert budget is not None
    assert budget.max_input_tokens == 200_000
    assert budget.max_cost_usd == 5.0
    # Default cost cap stays advisory: no hard failure when cost is untracked.
    assert budget.require_cost_tracking is False


def test_explicit_override_takes_priority() -> None:
    loop = _loop(
        {
            "maxInputTokensPerTurn": 1234,
            "maxCostPerTurnUsd": 0.25,
        }
    )
    budget = loop._build_turn_budget()
    assert budget is not None
    assert budget.max_input_tokens == 1234
    assert budget.max_cost_usd == 0.25
    assert budget.require_cost_tracking is True


def test_explicit_input_with_default_cost() -> None:
    """Only maxInputTokensPerTurn set: it wins; cost falls back to the default."""
    loop = _loop({"maxInputTokensPerTurn": 5000})
    budget = loop._build_turn_budget()
    assert budget is not None
    assert budget.max_input_tokens == 5000
    assert budget.max_cost_usd == 5.0
    assert budget.require_cost_tracking is False


def test_zero_cost_cap_respected() -> None:
    """An explicit 0.0 cost cap must not fall back to the default."""
    loop = _loop({"maxCostPerTurnUsd": 0.0})
    budget = loop._build_turn_budget()
    assert budget is not None
    assert budget.max_cost_usd == 0.0
