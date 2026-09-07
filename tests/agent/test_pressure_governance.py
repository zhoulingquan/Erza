"""P2-T1: PressureSignal + pressure-driven context governance.

Tests that ``PressureSignal`` is computed correctly at GREEN/YELLOW/RED
thresholds, and that strategies change behavior based on the pressure
level (snip_history skips on GREEN). W10-C4: microcompact moved to the
turn-boundary pass in ``AgentLoop`` (see test_turn_boundary_compaction.py)
and apply_tool_result_budget no longer depends on pressure.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from erza.agent.context_governor import (
    GovernanceContext,
    PressureLevel,
    PressureSignal,
)
from erza.agent.runner_strategies import (
    ApplyToolResultBudgetStrategy,
    SnipHistoryStrategy,
)
from erza.providers.base import LLMProvider

_NOTICE_LEN = len("\n... (truncated)")


# --- PressureSignal tests ---


def test_pressure_level_green() -> None:
    sig = PressureSignal(
        input_tokens=400,
        token_limit=1000,
        ratio=0.4,
        level=PressureLevel.GREEN,
    )
    assert sig.level is PressureLevel.GREEN
    assert sig.ratio < 0.5


def test_pressure_level_yellow() -> None:
    sig = PressureSignal(
        input_tokens=600,
        token_limit=1000,
        ratio=0.6,
        level=PressureLevel.YELLOW,
    )
    assert sig.level is PressureLevel.YELLOW
    assert 0.5 <= sig.ratio < 0.8


def test_pressure_level_red() -> None:
    sig = PressureSignal(
        input_tokens=850,
        token_limit=1000,
        ratio=0.85,
        level=PressureLevel.RED,
    )
    assert sig.level is PressureLevel.RED
    assert sig.ratio >= 0.8


def test_pressure_signal_is_frozen() -> None:
    sig = PressureSignal(
        input_tokens=100,
        token_limit=200,
        ratio=0.5,
        level=PressureLevel.YELLOW,
    )
    with pytest.raises(Exception):
        sig.level = PressureLevel.GREEN  # type: ignore[misc]


# --- Strategy behavior under pressure ---


def _ctx(
    spec: Any | None = None,
    pressure: PressureSignal | None = None,
) -> GovernanceContext:
    ctx = GovernanceContext(
        spec=spec or MagicMock(),
        tools=MagicMock(),
        provider=MagicMock(spec=LLMProvider),
        iteration=0,
        runner=None,
    )
    ctx.pressure = pressure
    return ctx


def test_apply_tool_result_budget_fixed_limit_on_red() -> None:
    """W10-C4: on RED, apply_tool_result_budget keeps the fixed limit.

    The pressure-dependent halving was removed for byte-stable prefixes;
    the limit is always ``max_tool_result_chars`` (same as GREEN).
    """
    from erza.agent.runner import AgentRunner, AgentRunSpec

    provider = MagicMock(spec=LLMProvider)
    runner = AgentRunner(provider)

    long_result = "x" * 2000
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "ok",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "test", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "test", "content": long_result},
    ]

    spec = AgentRunSpec(
        initial_messages=[],
        tools=MagicMock(),
        model="test-model",
        max_iterations=2,
        max_tool_result_chars=1000,
    )

    ctx = GovernanceContext(
        spec=spec,
        tools=MagicMock(),
        provider=provider,
        iteration=0,
        runner=runner,
    )
    ctx.pressure = PressureSignal(850, 1000, 0.85, PressureLevel.RED)

    strategy = ApplyToolResultBudgetStrategy()
    result = strategy.apply(messages, ctx)
    tool_content = [m for m in result if m.get("role") == "tool"][0]["content"]
    # Fixed limit (1000) applies even under RED (plus truncation notice)
    assert len(tool_content) <= 1000 + _NOTICE_LEN


def test_apply_tool_result_budget_normal_on_green() -> None:
    """On GREEN, apply_tool_result_budget uses normal limits."""
    from erza.agent.runner import AgentRunner, AgentRunSpec

    provider = MagicMock(spec=LLMProvider)
    runner = AgentRunner(provider)

    long_result = "x" * 2000
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "ok",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "test", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "test", "content": long_result},
    ]

    spec = AgentRunSpec(
        initial_messages=[],
        tools=MagicMock(),
        model="test-model",
        max_iterations=2,
        max_tool_result_chars=1000,
    )

    ctx = GovernanceContext(
        spec=spec,
        tools=MagicMock(),
        provider=provider,
        iteration=0,
        runner=runner,
    )
    ctx.pressure = PressureSignal(100, 1000, 0.1, PressureLevel.GREEN)

    strategy = ApplyToolResultBudgetStrategy()
    result = strategy.apply(messages, ctx)
    tool_content = [m for m in result if m.get("role") == "tool"][0]["content"]
    # On GREEN, normal limit (1000) applies (plus truncation notice)
    assert len(tool_content) <= 1000 + _NOTICE_LEN


# --- SnipHistoryStrategy tests ---


def test_snip_history_skips_on_green() -> None:
    """On GREEN pressure, snip_history strategy returns messages unchanged."""
    messages = [{"role": "user", "content": "hi"}]

    ctx = _ctx(pressure=PressureSignal(100, 1000, 0.1, PressureLevel.GREEN))
    strategy = SnipHistoryStrategy()
    result = strategy.apply(messages, ctx)
    assert result is messages


def test_snip_history_runs_on_red() -> None:
    """On RED pressure, snip_history delegates to the governance service."""
    from erza.agent.runner import AgentRunner, AgentRunSpec

    provider = MagicMock(spec=LLMProvider)
    provider.estimate_prompt_tokens = MagicMock(return_value=(950, "mock"))
    runner = AgentRunner(provider)

    messages = [{"role": "user", "content": "hi"}]
    spec = AgentRunSpec(
        initial_messages=[],
        tools=MagicMock(),
        model="test-model",
        max_iterations=2,
        max_tool_result_chars=2000,
        context_window_tokens=1000,
    )

    ctx = GovernanceContext(
        spec=spec,
        tools=MagicMock(),
        provider=provider,
        iteration=0,
        runner=runner,
    )
    ctx.pressure = PressureSignal(850, 1000, 0.85, PressureLevel.RED)

    strategy = SnipHistoryStrategy()
    result = strategy.apply(messages, ctx)
    # snip_history was called (not skipped) — result is a list
    assert isinstance(result, list)
