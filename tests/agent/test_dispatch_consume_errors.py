"""Regression tests for the inbound-consumer liveness guard in MessageDispatcher.

A permanently failing ``consume_inbound`` used to turn the dispatch loop's
``except Exception: continue`` into an unbounded hot spin (CPU burn + log flood
+ starvation of every other task on the event loop). The loop must instead give
up loudly and return.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_loop(tmp_path):
    from erza.agent.loop import AgentLoop
    from erza.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    with (
        patch("erza.agent.loop.ContextBuilder"),
        patch("erza.agent.loop.SessionManager"),
        patch("erza.agent.loop.SubagentManager") as MockSubMgr,
    ):
        MockSubMgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path)
    return loop


@pytest.mark.asyncio
async def test_run_stops_after_consecutive_consume_errors(tmp_path, monkeypatch) -> None:
    """A permanently failing consumer must stop the loop instead of hot-spinning."""
    from erza.agent.dispatch import _MAX_CONSECUTIVE_CONSUME_ERRORS

    # Keep the test fast; the retry budget itself is what we assert on.
    monkeypatch.setattr("erza.agent.dispatch._CONSUME_ERROR_BACKOFF_S", 0.0)

    loop = _make_loop(tmp_path)
    loop._connect_mcp = AsyncMock()  # type: ignore[method-assign]

    attempts = 0

    async def _always_fail() -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("inbound queue is bound to a different event loop")

    loop.bus.consume_inbound = _always_fail  # type: ignore[method-assign]

    # The key assertion is that this *returns* — previously it never would.
    await asyncio.wait_for(loop.run(), timeout=10.0)

    assert attempts == _MAX_CONSECUTIVE_CONSUME_ERRORS
    assert loop._dispatcher._running is False


@pytest.mark.asyncio
async def test_run_tolerates_transient_consume_errors(tmp_path, monkeypatch) -> None:
    """A handful of transient errors must not trip the give-up guard."""
    monkeypatch.setattr("erza.agent.dispatch._CONSUME_ERROR_BACKOFF_S", 0.0)

    loop = _make_loop(tmp_path)
    loop._connect_mcp = AsyncMock()  # type: ignore[method-assign]

    real_consume = loop.bus.consume_inbound
    calls = 0

    async def _fail_a_few_times_then_succeed():
        nonlocal calls
        calls += 1
        if calls <= 3:
            raise RuntimeError("transient")
        return await real_consume()

    loop.bus.consume_inbound = _fail_a_few_times_then_succeed  # type: ignore[method-assign]

    run_task = asyncio.create_task(loop.run())
    for _ in range(500):
        if calls > 3:
            break
        await asyncio.sleep(0.01)

    assert calls > 3, "the consumer should have been retried after transient errors"
    assert loop._dispatcher._running is True, "a few errors must not stop the loop"

    loop.stop()
    await asyncio.wait_for(run_task, timeout=10.0)
