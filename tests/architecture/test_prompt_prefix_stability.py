"""W10-C5: prompt prefix byte-stability guard tests.

Locks the W10 cache-first transformations into CI regression gates —
reasonix's ``ImmutablePrefix.verifyFingerprint()`` idea without a runtime
fingerprint: each test builds two consecutive turns end-to-end through the
real turn-boundary steps (C3 consolidate -> C2 snapshot stamp -> C4
microcompact -> history replay -> ``ContextBuilder.build_messages``) and
asserts byte-level prefix equality. Any future change that quietly breaks
prefix stability turns these tests red.

Zero production code is touched by this module; if a case cannot pass
without production changes, that means C2/C3/C4 has a defect.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import erza.memory.consolidator as memory_module
from erza.agent.loop import AgentLoop
from erza.bus.events import InboundMessage
from erza.bus.queue import MessageBus
from erza.memory.lifecycle import IngestContext
from erza.memory.models import (
    ActorKind,
    EvidenceKind,
    EvidenceRef,
    MemoryScope,
    ScopeKind,
)

UTC = timezone.utc
RECALL_HEADER = "# Recalled Memory (Deterministic)"
NOTES_HEADER = "# Scratchpad Notes (notes.md)"
ARCHIVED_SUMMARY_HEADER = "[Archived Context Summary]"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _make_loop(workspace: Path) -> AgentLoop:
    """Real AgentLoop on a temp workspace; only the LLM provider is faked."""
    from erza.providers.base import GenerationSettings

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    provider.estimate_prompt_tokens.return_value = (10_000, "test-counter")
    provider.chat_with_retry = AsyncMock()
    return AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=workspace,
        model="test-model",
        context_window_tokens=128_000,
    )


async def _run_turn(
    loop: AgentLoop,
    session: Any,
    user_text: str,
    *,
    sender_id: str = "tester",
) -> list[dict[str, Any]]:
    """Mirror ``turn_orchestrator._state_build`` and return the turn's message list.

    Order matches production: C3 consolidate -> C2 snapshot stamp -> C4
    microcompact -> history replay -> build the full ``[system, *history, tail]``
    list sent to the LLM (runtime context and dynamic blocks ride the tail).
    """
    msg = InboundMessage(channel="cli", sender_id=sender_id, chat_id="test", content=user_text)
    scope = loop.workspace_scopes.for_message(msg, session.metadata)
    await loop._resources._consolidator_for(scope.project_path).maybe_consolidate_by_tokens(
        session, replay_max_messages=loop._max_messages
    )
    loop._ensure_memory_context_message(session, scope.project_path)
    loop._microcompact_session_history(session, scope.project_path)
    history = session.get_history(
        max_messages=loop._max_messages,
        max_tokens=loop._replay_token_budget(),
        include_timestamps=True,
    )
    return await loop._build_initial_messages(msg, session, history)


def _seed_history(loop: AgentLoop, workspace: Path, entries: list[str]) -> None:
    """Seed memory history so the C2 snapshot has content to stamp."""
    store = loop.context.memory_for(workspace)
    for entry in entries:
        store.append_history(entry)


def _seed_active_record(
    store: Any, statement: str, slot: str = "memory.retrieval.strategy"
) -> None:
    """Ingest + promote one ACTIVE structured record (structured recall source)."""
    from erza.memory.extraction import parse_extraction_batch

    evidence_catalog = {
        "history:1": EvidenceRef(
            kind=EvidenceKind.HISTORY,
            ref="history:1",
            excerpt=statement,
            observed_at=datetime(2026, 9, 1, 8, 30, tzinfo=UTC),
        )
    }
    proposal = {
        "proposal_index": 0,
        "kind": "decision",
        "scope_hint": "project",
        "subject": "Erza",
        "slot": slot,
        "statement": statement,
        "detail": "",
        "tags": ["architecture.memory"],
        "aliases": [],
        "confidence": 1.0,
        "importance": 5,
        "evidence_refs": ["history:1"],
        "speech_act": "confirmed_decision",
        "expires_at": None,
    }
    extracted = parse_extraction_batch(
        json.dumps({"schema_version": 1, "proposals": [proposal]}),
        evidence_catalog,
        store.structured_repository.tag_catalog,
    )
    context = IngestContext(
        actor=ActorKind.DREAM,
        reason="prefix stability seed",
        source_batch="seed:prefix-stability",
        scope=MemoryScope(kind=ScopeKind.PROJECT, key=store.project_scope_key),
        evidence_catalog=evidence_catalog,
        now=datetime.now(UTC),
    )
    result = store.structured_lifecycle.ingest(extracted.proposals[0], context)
    store.structured_lifecycle.promote(
        result.candidate_id,
        actor=ActorKind.SYSTEM,
        reason="prefix stability seed promote",
    )


def _record_turn_outcome(session: Any, user_text: str, reply: str = "done") -> None:
    """Persist a turn's plain outcome (what the loop saves after the LLM replies)."""
    session.add_message("user", user_text)
    session.add_message("assistant", reply)


def _add_tool_exchange(session: Any, name: str, content: str, idx: int) -> None:
    """Persist one assistant tool_call + tool result pair."""
    session.add_message(
        "assistant",
        "",
        tool_calls=[
            {
                "id": f"c{idx}",
                "type": "function",
                "function": {"name": name, "arguments": "{}"},
            }
        ],
    )
    session.add_message("tool", content, tool_call_id=f"c{idx}", name=name)


# ---------------------------------------------------------------------------
# Friendly failure reporting (taskbook §2.3)
# ---------------------------------------------------------------------------


def _first_byte_diff(a: str, b: str) -> str:
    for i, (ca, cb) in enumerate(zip(a, b)):
        if ca != cb:
            return f"first differing byte at index {i}: {ca!r} vs {cb!r}"
    if len(a) != len(b):
        return f"identical up to index {min(len(a), len(b)) - 1}; lengths {len(a)} vs {len(b)}"
    return "no difference"


def _assert_message_prefix_equal(expected: list, actual: list, *, label: str) -> None:
    """Assert equal message-list prefixes; report the first drift point."""
    assert len(expected) == len(actual), (
        f"{label}: prefix lengths differ: {len(expected)} vs {len(actual)}"
    )
    for i, (ma, mb) in enumerate(zip(expected, actual)):
        if ma == mb:
            continue
        for field in sorted(set(ma) | set(mb)):
            va, vb = ma.get(field), mb.get(field)
            if va == vb:
                continue
            if isinstance(va, str) and isinstance(vb, str):
                pytest.fail(
                    f"{label}: message[{i}] (role={ma.get('role')!r}) field {field!r} differs: "
                    f"{_first_byte_diff(va, vb)}"
                )
            pytest.fail(
                f"{label}: message[{i}] (role={ma.get('role')!r}) field {field!r} differs: "
                f"{va!r} vs {vb!r}"
            )


def _assert_system_equal(turn_a: list, turn_b: list) -> None:
    assert turn_a[0].get("role") == "system" and turn_b[0].get("role") == "system"
    assert turn_a[0] == turn_b[0], "system prompt drifted between turns: " + _first_byte_diff(
        str(turn_a[0].get("content")), str(turn_b[0].get("content"))
    )


# ---------------------------------------------------------------------------
# 1 + 7. System prefix byte-stable across turns; recall/notes stay in the tail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_byte_stable_with_recall_notes_and_tool_replay(tmp_path: Path) -> None:
    """Turn 1 vs turn 2 with all three dynamic variables in play.

    Variables between the turns: tool results enter the replay, notes.md
    changes, and the recall outcome flips (hit -> miss). The system prompt
    must stay byte-identical; the dynamic content may only move the tail
    user message (W10-C2 / taskbook §2.2-1 and §2.2-7).
    """
    loop = _make_loop(tmp_path)
    _seed_history(loop, tmp_path, ["history entry one", "history entry two"])
    _seed_active_record(
        loop.context.memory_for(tmp_path), "Main uses deterministic structured recall."
    )

    session = loop.sessions.get_or_create("cli:test")
    session.add_message("user", "u0")
    session.add_message("assistant", "a0")
    loop.sessions.save(session)

    turn_1 = await _run_turn(loop, session, "how does architecture.memory recall strategy work?")
    tail_1 = str(turn_1[-1]["content"])
    assert RECALL_HEADER in tail_1  # the recall variable is real in turn 1
    assert NOTES_HEADER not in tail_1
    assert RECALL_HEADER not in str(turn_1[0]["content"])

    # Turn 1's outcome: plain user message, a tool exchange, an assistant reply.
    session.add_message("user", "how does architecture.memory recall strategy work?")
    _add_tool_exchange(session, "read_file", "r" * 600, 0)
    session.add_message("assistant", "answer 1")
    loop.sessions.save(session)

    # Variable: scratchpad notes appear between the turns.
    (tmp_path / "notes.md").write_text("- new scratchpad note\n", encoding="utf-8")

    turn_2 = await _run_turn(loop, session, "tell me something entirely unrelated")
    tail_2 = str(turn_2[-1]["content"])
    assert RECALL_HEADER not in tail_2  # recall outcome flipped
    assert NOTES_HEADER in tail_2  # notes variable is real in turn 2
    assert RECALL_HEADER not in str(turn_2[0]["content"])

    _assert_system_equal(turn_1, turn_2)
    # Only the tail user message may differ; both turns carry exactly one.
    assert turn_1[-1]["role"] == "user" and turn_2[-1]["role"] == "user"


# ---------------------------------------------------------------------------
# 2. History replays append-only between turns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_prefix_append_only_between_turns(tmp_path: Path) -> None:
    """Everything except the tail user message is replayed unchanged in turn 2."""
    loop = _make_loop(tmp_path)
    _seed_history(loop, tmp_path, ["history entry one"])

    session = loop.sessions.get_or_create("cli:test")
    session.add_message("user", "u0")
    session.add_message("assistant", "a0")
    loop.sessions.save(session)

    turn_1 = await _run_turn(loop, session, "first question")
    assert turn_1[-1]["role"] == "user"

    _record_turn_outcome(session, "first question", "answer 1")
    _add_tool_exchange(session, "read_file", "file body", 0)
    session.add_message("assistant", "answer 2")
    loop.sessions.save(session)

    turn_2 = await _run_turn(loop, session, "second question")

    n = len(turn_1) - 1  # the stable prefix excludes only the tail user message
    _assert_message_prefix_equal(turn_1[:n], turn_2[:n], label="cross-turn history prefix")
    # The turn-1 outcome was appended (not spliced into) the replay.
    assert any(m.get("role") == "tool" for m in turn_2)
    assert turn_2[-1]["role"] == "user"


# ---------------------------------------------------------------------------
# 3. In-turn iterations never rewrite the prefix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_turn_iteration_prefix_stable(tmp_path: Path) -> None:
    """A governance pass over grown in-turn messages leaves the prefix untouched.

    The trailing orphan tool result proves the pipeline really ran (it gets
    dropped) instead of silently falling back to the raw input.
    """
    from erza.agent.runner import AgentRunner, AgentRunSpec

    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:test")
    session.add_message("user", "u0")
    _add_tool_exchange(session, "read_file", "r" * 600, 0)
    session.add_message("assistant", "a0")
    loop.sessions.save(session)

    messages = await _run_turn(loop, session, "continue")
    k = len(messages)

    iteration_messages = [
        *messages,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c9",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c9", "name": "read_file", "content": "fresh result"},
        # Canary: orphan result (no matching tool_call) — governance must drop it.
        {"role": "tool", "tool_call_id": "c-orphan", "name": "read_file", "content": "orphan"},
    ]

    runner = AgentRunner(loop.provider)
    spec = AgentRunSpec(
        initial_messages=[],
        tools=loop.tools,
        model="test-model",
        max_iterations=2,
        max_tool_result_chars=1000,
        context_window_tokens=128_000,
    )
    governed = await runner.context_governance.govern_messages(
        spec, iteration_messages, iteration=1
    )

    _assert_message_prefix_equal(messages, governed[:k], label="in-turn governance prefix")
    # Canary dropped: the pipeline executed (not the exception fallback).
    assert len(governed) == k + 2
    assert governed[-1].get("tool_call_id") == "c9"
    assert governed[-2].get("role") == "assistant"
    assert all(m.get("tool_call_id") != "c-orphan" for m in governed)


# ---------------------------------------------------------------------------
# 4. Tool definitions serialize deterministically
# ---------------------------------------------------------------------------


def test_tool_definitions_deterministic(tmp_path: Path) -> None:
    """Registry order is locked: repeat calls and fresh registries agree byte-wise."""
    ws_a = tmp_path / "ws-a"
    ws_b = tmp_path / "ws-b"
    ws_a.mkdir()
    ws_b.mkdir()
    loop_a = _make_loop(ws_a)
    loop_b = _make_loop(ws_b)

    defs_a1 = loop_a.tools.get_definitions()
    defs_a2 = loop_a.tools.get_definitions()
    defs_b = loop_b.tools.get_definitions()

    assert defs_a1, "tool registry should register default tools"
    assert json.dumps(defs_a1) == json.dumps(defs_a2), "same registry serialized differently"
    assert json.dumps(defs_a1) == json.dumps(defs_b), "fresh registries disagree on order"


# ---------------------------------------------------------------------------
# 5. Archival folding inserts the summary without touching the prefix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archival_folding_preserves_prefix(tmp_path: Path, monkeypatch) -> None:
    """One consolidation round: prefix before the insertion point is untouched."""
    loop = _make_loop(tmp_path)
    _seed_history(loop, tmp_path, ["history entry one"])

    session = loop.sessions.get_or_create("cli:test")
    for i in range(3):
        session.add_message("user", f"u{i}")
        session.add_message("assistant", f"a{i}")
    loop.sessions.save(session)

    turn_1 = await _run_turn(loop, session, "third question")
    pre_archive = [dict(m) for m in session.messages]

    # Fire exactly one archival round on a tiny budget.
    loop.consolidator.context_window_tokens = 400
    loop.consolidator.max_completion_tokens = 0
    loop.consolidator._SAFETY_BUFFER = 0
    loop.consolidator.archive = AsyncMock(return_value="Summary of the archived discussion.")
    calls = [0]

    def mock_estimate(_session):
        calls[0] += 1
        return (500, "test") if calls[0] == 1 else (80, "test")

    loop.consolidator.estimate_session_prompt_tokens = mock_estimate
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 150)

    await loop.consolidator.maybe_consolidate_by_tokens(session)

    loop.consolidator.archive.assert_awaited_once()
    insertion = session.last_consolidated
    assert 0 < insertion < len(pre_archive)
    # Messages before the insertion point are untouched, one by one.
    assert session.messages[:insertion] == pre_archive[:insertion]
    summary_msg = session.messages[insertion]
    assert summary_msg.get("_archived_summary") is True
    assert summary_msg["content"].startswith(ARCHIVED_SUMMARY_HEADER)
    assert "Summary of the archived discussion." in summary_msg["content"]

    # The next turn replays the folded window: system byte-equal, history
    # now starts at the archived summary message.
    turn_2 = await _run_turn(loop, session, "fourth question")
    _assert_system_equal(turn_1, turn_2)
    assert str(turn_2[1]["content"]).startswith(ARCHIVED_SUMMARY_HEADER)


# ---------------------------------------------------------------------------
# 6. Turn-boundary compaction is deterministic and persisted once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_boundary_compaction_deterministic_replay(tmp_path: Path) -> None:
    """The second turn replays the first turn's persisted, compacted window unchanged."""
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:test")
    session.add_message("user", "u0")
    for i in range(loop._MICROCOMPACT_KEEP_RECENT + 2):
        _add_tool_exchange(session, "read_file", "r" * 600, i)
    session.add_message("assistant", "final answer")
    loop.sessions.save(session)

    turn_1 = await _run_turn(loop, session, "next question")
    after_turn_1 = [dict(m) for m in session.messages]

    tool_contents = [str(m["content"]) for m in turn_1 if m.get("role") == "tool"]
    assert tool_contents.count("[read_file result omitted from context]") == 2
    assert tool_contents.count("r" * 600) == loop._MICROCOMPACT_KEEP_RECENT

    turn_2 = await _run_turn(loop, session, "another question")

    # No second rewrite: the persisted session is byte-identical.
    assert session.messages == after_turn_1
    # The replayed prefix (everything except the tail) is identical too.
    n = len(turn_1) - 1
    _assert_message_prefix_equal(turn_1[:n], turn_2[:n], label="post-compaction replay")
    assert turn_2[-1]["role"] == "user"
