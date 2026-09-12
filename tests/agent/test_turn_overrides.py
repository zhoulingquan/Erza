"""Regression tests for per-turn provider/model/light-context overrides (H7).

The heartbeat used to swap ``agent.provider`` / ``agent.model`` /
``agent._light_context`` globally and restore them in a ``finally`` block.
Because asyncio interleaves concurrent turns on one thread, a user turn that
was in flight could be answered by the heartbeat's model (or lose its
bootstrap context), and concurrent restores raced.

These tests pin the ContextVar-scoped behaviour that replaced it.
"""

from __future__ import annotations

import asyncio

import pytest

from erza.agent.loop import AgentLoop
from erza.agent.provider_registry import ProviderRegistry
from erza.agent.runner import AgentRunner
from erza.agent.turn_overrides import (
    context_without_overrides,
    current_light_context,
    current_model_override,
    current_provider_override,
    turn_runtime_overrides,
)


class _FakeProvider:
    """Minimal stand-in; only identity matters for these tests."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.generation = None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_FakeProvider({self.name!r})"


def _bare_loop(main_provider: _FakeProvider) -> AgentLoop:
    """Build an AgentLoop without running the heavy __init__."""
    loop = AgentLoop.__new__(AgentLoop)
    loop.__dict__["_provider_registry"] = ProviderRegistry(
        provider=main_provider,  # type: ignore[arg-type]
        model="main-model",
        context_window_tokens=128000,
    )
    return loop


def test_overrides_are_off_by_default() -> None:
    assert current_provider_override() is None
    assert current_model_override() is None
    assert current_light_context() is False


def test_loop_provider_and_model_honour_override() -> None:
    main = _FakeProvider("main")
    hb = _FakeProvider("heartbeat")
    loop = _bare_loop(main)

    assert loop.provider is main
    assert loop.model == "main-model"

    with turn_runtime_overrides(provider=hb, model="hb-model", light_context=True):
        assert loop.provider is hb
        assert loop.model == "hb-model"
        assert current_light_context() is True

    # Restored afterwards — shared state was never touched.
    assert loop.provider is main
    assert loop.model == "main-model"
    assert current_light_context() is False


def test_runner_provider_honours_override_over_registry() -> None:
    main = _FakeProvider("main")
    hb = _FakeProvider("heartbeat")
    runner = AgentRunner(main, provider_registry=None)  # type: ignore[arg-type]

    assert runner.provider is main
    with turn_runtime_overrides(provider=hb):
        assert runner.provider is hb
    assert runner.provider is main


def test_override_does_not_leak_into_concurrent_turn() -> None:
    """A user turn running concurrently with a heartbeat keeps the main model."""
    main = _FakeProvider("main")
    hb = _FakeProvider("heartbeat")
    loop = _bare_loop(main)

    heartbeat_ready = asyncio.Event()
    seen_by_user: list[object] = []

    async def user_turn() -> None:
        # Wait until the heartbeat has bound its override, then read the loop's
        # provider from *this* task's context.
        await heartbeat_ready.wait()
        seen_by_user.append(loop.provider)
        seen_by_user.append(loop.model)

    async def heartbeat_turn() -> None:
        with turn_runtime_overrides(provider=hb, model="hb-model"):
            heartbeat_ready.set()
            # Yield so the user turn actually reads while the override is live.
            await asyncio.sleep(0)
            assert loop.provider is hb
            assert loop.model == "hb-model"

    async def main_() -> None:
        await asyncio.gather(heartbeat_turn(), user_turn())

    asyncio.run(main_())

    assert seen_by_user == [main, "main-model"]


def test_overrides_reset_when_the_turn_raises() -> None:
    main = _FakeProvider("main")
    hb = _FakeProvider("heartbeat")
    loop = _bare_loop(main)

    with pytest.raises(RuntimeError):
        with turn_runtime_overrides(provider=hb, model="hb-model", light_context=True):
            raise RuntimeError("boom")

    assert loop.provider is main
    assert loop.model == "main-model"
    assert current_provider_override() is None
    assert current_model_override() is None
    assert current_light_context() is False


def test_context_without_overrides_clears_inherited_overrides() -> None:
    hb = _FakeProvider("heartbeat")

    async def child() -> tuple[object, object, bool]:
        await asyncio.sleep(0)
        return (
            current_provider_override(),
            current_model_override(),
            current_light_context(),
        )

    async def main_() -> tuple[tuple[object, object, bool], tuple[object, object, bool]]:
        with turn_runtime_overrides(provider=hb, model="hb-model", light_context=True):
            inherited = await asyncio.create_task(child())
            cleared = await asyncio.create_task(child(), context=context_without_overrides())
        return inherited, cleared

    inherited, cleared = asyncio.run(main_())

    assert inherited == (hb, "hb-model", True)
    assert cleared == (None, None, False)
