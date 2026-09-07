"""W10-C2 frozen system prefix: once-per-session memory snapshot stamping."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from erza.agent.loop import AgentLoop
from erza.bus.queue import MessageBus


def _make_loop(tmp_path: Path) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(max_tokens=0)
    provider.estimate_prompt_tokens.return_value = (1_000, "test")
    return AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )


def test_memory_snapshot_stamped_as_first_session_message(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:stamp-1")
    loop.context.memory.append_history("remember the w10 cache plan")

    loop._ensure_memory_context_message(session, tmp_path)

    assert len(session.messages) == 1
    first = session.messages[0]
    assert first["role"] == "user"
    assert "# Recent History" in first["content"]
    assert "remember the w10 cache plan" in first["content"]
    assert first["_memory_context"] is True
    # Non-empty stamp: no attempt marker recorded.
    assert not session.metadata.get("_memory_context_stamped")


def test_memory_snapshot_not_stamped_twice(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:stamp-2")
    loop.context.memory.append_history("entry one")

    loop._ensure_memory_context_message(session, tmp_path)
    loop.context.memory.append_history("entry two")
    loop._ensure_memory_context_message(session, tmp_path)

    assert len(session.messages) == 1
    assert "entry two" not in session.messages[0]["content"]


def test_empty_snapshot_marks_attempt_without_stamping(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:stamp-3")

    loop._ensure_memory_context_message(session, tmp_path)

    assert session.messages == []
    assert session.metadata.get("_memory_context_stamped") is True


def test_session_clear_allows_restamp(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:stamp-4")

    loop._ensure_memory_context_message(session, tmp_path)
    assert session.metadata.get("_memory_context_stamped") is True

    session.clear()
    assert "_memory_context_stamped" not in session.metadata

    loop.context.memory.append_history("post-clear fact")
    loop._ensure_memory_context_message(session, tmp_path)
    assert len(session.messages) == 1
    assert "post-clear fact" in session.messages[0]["content"]
