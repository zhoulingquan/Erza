"""W10-C4: deterministic compaction.

Covers (taskbook §3.1):
1. apply_tool_result_budget is pressure-independent (GREEN == RED == None).
2. Turn-boundary microcompact rewrites stale tool results and persists them.
3. The pass is idempotent — a second run changes nothing.
4. After the pass, repeated get_history calls replay identical bytes.
5. The default governance pipeline no longer contains microcompact.
6. ``_archived_summary`` / ``_memory_context`` messages are never rewritten.

Plus coverage carried over from the deleted request-local ``microcompact()``
function tests: short results are preserved and non-compactable tools are
skipped.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from erza.agent.loop import AgentLoop
from erza.bus.queue import MessageBus


def _make_loop(tmp_path: Path) -> AgentLoop:
    """Minimal AgentLoop with the real tool registry (compactable metadata)."""
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.estimate_prompt_tokens.return_value = (10_000, "test")
    provider.chat_with_retry = AsyncMock()
    provider.generation.max_tokens = 4096
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=128_000,
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    return loop


def _add_tool_results(session, count: int, *, name: str = "read_file") -> None:
    """Append *count* long tool results (compactable via metadata/whitelist)."""
    for i in range(count):
        session.add_message("tool", "r" * 600, tool_call_id=f"c{i}", name=name)


def _tool_messages(session) -> list[dict[str, Any]]:
    return [m for m in session.messages if m.get("role") == "tool"]


# ---------------------------------------------------------------------------
# 1. Budget determinization
# ---------------------------------------------------------------------------


def test_tool_result_budget_pressure_independent() -> None:
    """GREEN / RED / None pressure produce identical bytes (taskbook §3.1-1)."""
    from erza.agent.context_governor import PressureLevel
    from erza.agent.runner import AgentRunner, AgentRunSpec
    from erza.providers.base import LLMProvider

    provider = MagicMock(spec=LLMProvider)
    runner = AgentRunner(provider)

    messages = [
        {
            "role": "tool",
            "tool_call_id": "c1",
            "name": "test",
            "content": "x" * 2000,
        }
    ]
    spec = AgentRunSpec(
        initial_messages=[],
        tools=MagicMock(),
        model="test-model",
        max_iterations=2,
        max_tool_result_chars=1000,
    )

    out_green = runner.context_governance.apply_tool_result_budget(
        spec, messages, pressure_level=PressureLevel.GREEN
    )
    out_red = runner.context_governance.apply_tool_result_budget(
        spec, messages, pressure_level=PressureLevel.RED
    )
    out_none = runner.context_governance.apply_tool_result_budget(spec, messages)

    assert out_green == out_red == out_none
    content = out_red[0]["content"]
    # Equal to truncation at the fixed creation-time limit (plus notice).
    notice = len("\n... (truncated)")
    assert len(content) <= 1000 + notice


# ---------------------------------------------------------------------------
# 2. Turn-boundary compaction persists
# ---------------------------------------------------------------------------


def test_turn_boundary_compaction_persists(tmp_path: Path) -> None:
    """12 compactable results -> first 2 placeholders, last 10 intact, persisted."""
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:test")
    _add_tool_results(session, loop._MICROCOMPACT_KEEP_RECENT + 2)
    loop.sessions.save(session)

    loop._microcompact_session_history(session, tmp_path)

    reloaded = loop.sessions.get_or_create("cli:test")
    tools = _tool_messages(reloaded)
    placeholders = [
        m for m in tools if m.get("content") == "[read_file result omitted from context]"
    ]
    intact = [m for m in tools if m.get("content") == "r" * 600]
    assert len(placeholders) == 2
    assert len(intact) == loop._MICROCOMPACT_KEEP_RECENT
    # Placeholders occupy the front of the tool sequence (oldest results).
    assert tools[0]["content"] == "[read_file result omitted from context]"
    assert tools[1]["content"] == "[read_file result omitted from context]"


# ---------------------------------------------------------------------------
# 3. Idempotency
# ---------------------------------------------------------------------------


def test_turn_boundary_compaction_idempotent(tmp_path: Path) -> None:
    """A second pass over the same session changes nothing (taskbook §3.1-3)."""
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:test")
    _add_tool_results(session, loop._MICROCOMPACT_KEEP_RECENT + 2)
    loop.sessions.save(session)

    loop._microcompact_session_history(session, tmp_path)
    snapshot = copy.deepcopy(session.messages)

    loop._microcompact_session_history(session, tmp_path)
    assert session.messages == snapshot


# ---------------------------------------------------------------------------
# 4. In-turn replay stability
# ---------------------------------------------------------------------------


def test_history_replay_stable_after_compaction(tmp_path: Path) -> None:
    """Two get_history calls after the pass replay identical bytes (§3.1-4)."""
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:test")
    session.add_message("user", "question")
    _add_tool_results(session, loop._MICROCOMPACT_KEEP_RECENT + 2)
    session.add_message("assistant", "answer")
    loop.sessions.save(session)

    loop._microcompact_session_history(session, tmp_path)

    history_1 = session.get_history(max_messages=120, include_timestamps=True)
    history_2 = session.get_history(max_messages=120, include_timestamps=True)
    assert history_1 == history_2


# ---------------------------------------------------------------------------
# 5. Default pipeline has no microcompact
# ---------------------------------------------------------------------------


def test_default_pipeline_has_no_microcompact() -> None:
    """The governance pipeline no longer registers microcompact (§3.1-5)."""
    from erza.agent.context_governor import ContextGovernor

    gov = ContextGovernor()
    names = [s.name for s in gov._strategies]
    assert "microcompact" not in names
    assert "microcompact" not in ContextGovernor.BUILTIN_PIPELINE
    assert gov.get("microcompact") is None
    # The remaining legacy steps are all still in place.
    for expected in (
        "drop_orphan_tool_results",
        "backfill_missing_tool_results",
        "apply_tool_result_budget",
        "snip_history",
        "schema_crop",
    ):
        assert expected in names


# ---------------------------------------------------------------------------
# 6. Defensive: marked messages are never rewritten
# ---------------------------------------------------------------------------


def test_marked_messages_never_rewritten(tmp_path: Path) -> None:
    """_archived_summary / _memory_context messages survive untouched (§3.1-6)."""
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:test")
    summary_content = "[Archived Context Summary]\n\n" + "s" * 600
    snapshot_content = "# Recent History\n\n" + "h" * 600
    session.messages.insert(
        0, {"role": "user", "content": snapshot_content, "_memory_context": True}
    )
    session.messages.insert(
        0, {"role": "user", "content": summary_content, "_archived_summary": True}
    )
    _add_tool_results(session, loop._MICROCOMPACT_KEEP_RECENT + 2)
    loop.sessions.save(session)

    loop._microcompact_session_history(session, tmp_path)

    assert session.messages[0]["content"] == summary_content
    assert session.messages[0].get("_archived_summary") is True
    assert session.messages[1]["content"] == snapshot_content
    assert session.messages[1].get("_memory_context") is True


# ---------------------------------------------------------------------------
# Coverage carried over from the deleted request-local microcompact() tests
# ---------------------------------------------------------------------------


def test_short_results_preserved(tmp_path: Path) -> None:
    """Tool results below _MICROCOMPACT_MIN_CHARS are never replaced."""
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:test")
    for i in range(loop._MICROCOMPACT_KEEP_RECENT + 5):
        session.add_message("tool", "short", tool_call_id=f"c{i}", name="exec")
    loop.sessions.save(session)

    loop._microcompact_session_history(session, tmp_path)

    tools = _tool_messages(session)
    assert all(m.get("content") == "short" for m in tools)


def test_non_compactable_tools_skipped(tmp_path: Path) -> None:
    """Non-compactable tools (e.g. 'message') are never replaced."""
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:test")
    for i in range(loop._MICROCOMPACT_KEEP_RECENT + 5):
        session.add_message("tool", "y" * 1000, tool_call_id=f"c{i}", name="message")
    loop.sessions.save(session)

    loop._microcompact_session_history(session, tmp_path)

    tools = _tool_messages(session)
    assert all(m.get("content") == "y" * 1000 for m in tools)
