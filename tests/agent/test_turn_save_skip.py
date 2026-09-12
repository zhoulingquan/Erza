"""回归测试：``_state_save`` 的 ``save_skip`` 必须与实际消息前缀长度一致。

历史缺陷：``save_skip`` 曾固定写成 ``1 + len(history) + (1 if 已提前持久化 else 0)``。
但 ``ContextBuilder.build_messages`` 在 history 末条与当前用户消息**同角色**时会把
两者合并进同一元素（不新增元素），此时固定加 1 会让 ``messages[skip:]`` 多跳过一条，
把本轮的助手回复整段丢掉。触发场景很常见：``_ensure_memory_context_message`` 会把
一条 ``role="user"`` 的记忆快照插到会话首位，于是"记忆非空的工作区 + 新建会话"的首轮
必然命中，并在此后每轮复合恶化。
"""

import dataclasses
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from erza.agent.context import ContextBuilder
from erza.agent.session_turn import SessionTurnService
from erza.agent.turn_orchestrator import TurnContext, TurnDeps, TurnOrchestrator, TurnState
from erza.bus.events import InboundMessage
from erza.session.manager import Session, SessionManager


def _mk_orchestrator(tmp_path: Path, sessions: SessionManager) -> TurnOrchestrator:
    deps = TurnDeps(**{f.name: MagicMock() for f in dataclasses.fields(TurnDeps)})
    deps.session_turn = SessionTurnService(sessions, workspace=tmp_path)
    deps.sessions = sessions
    scope = MagicMock()
    scope.project_path = tmp_path
    deps.resources.workspace_scopes.for_turn.return_value = scope
    deps.resources.memory_for.return_value.raw_archive = MagicMock()
    deps.resources._consolidator_for.return_value.maybe_consolidate_by_tokens = MagicMock()
    return TurnOrchestrator(deps)


def _mk_ctx(session: Session, history: list[dict], initial: list[dict]) -> TurnContext:
    msg = InboundMessage(
        channel="websocket",
        sender_id="u1",
        chat_id="c1",
        content="第二轮提问",
    )
    return TurnContext(
        msg=msg,
        session=session,
        session_key=session.key,
        state=TurnState.SAVE,
        turn_id="t1",
        history=history,
        initial_messages=initial,
        all_messages=list(initial) + [{"role": "assistant", "content": "回答"}],
        final_content="回答",
    )


@pytest.mark.asyncio
async def test_save_keeps_assistant_reply_when_history_ends_with_user(tmp_path: Path) -> None:
    """history 末条是 user → build_messages 会合并 → 助手回复仍必须落盘。"""
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("websocket:c1")
    # _ensure_memory_context_message 插入的 role="user" 记忆快照
    session.messages.append(
        {"role": "user", "content": "【记忆快照】", "_memory_context": True}
    )
    history = session.get_history(max_messages=120, max_tokens=0, include_timestamps=True)

    builder = ContextBuilder(tmp_path, timezone="UTC")
    initial = builder.build_messages(
        history=history,
        current_message="第一轮提问",
        channel="websocket",
        chat_id="c1",
        session_key=session.key,
        workspace=tmp_path,
    )
    # 合并分支：未新增元素
    assert len(initial) == 1 + len(history)

    ctx = _mk_ctx(session, history, initial)
    ctx.user_persisted_early = True  # _persist_user_message_early 已把用户消息写入会话

    await _mk_orchestrator(tmp_path, sessions)._state_save(ctx)

    roles = [m.get("role") for m in session.messages]
    assert "assistant" in roles, f"助手回复被丢弃，实际历史={roles}"
    assert session.messages[-1]["content"] == "回答"


@pytest.mark.asyncio
async def test_save_persists_user_and_assistant_when_appended(tmp_path: Path) -> None:
    """history 末条是 assistant → 追加分支 → 用户与助手消息都要落盘。"""
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("websocket:c2")
    session.messages.append({"role": "user", "content": "旧问题"})
    session.messages.append({"role": "assistant", "content": "旧回答"})
    history = session.get_history(max_messages=120, max_tokens=0, include_timestamps=True)

    builder = ContextBuilder(tmp_path, timezone="UTC")
    initial = builder.build_messages(
        history=history,
        current_message="第二轮提问",
        channel="websocket",
        chat_id="c2",
        session_key=session.key,
        workspace=tmp_path,
    )
    assert len(initial) == 1 + len(history) + 1  # 追加分支

    ctx = _mk_ctx(session, history, initial)
    ctx.user_persisted_early = False  # 用户消息尚未写入会话

    await _mk_orchestrator(tmp_path, sessions)._state_save(ctx)

    roles = [m.get("role") for m in session.messages]
    assert roles[-2:] == ["user", "assistant"], f"实际历史={roles}"


@pytest.mark.asyncio
async def test_save_skips_duplicate_user_when_persisted_early(tmp_path: Path) -> None:
    """追加分支 + 已提前持久化 → 用户消息不得写两次。"""
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("websocket:c3")
    session.messages.append({"role": "user", "content": "旧问题"})
    session.messages.append({"role": "assistant", "content": "旧回答"})
    history = session.get_history(max_messages=120, max_tokens=0, include_timestamps=True)

    builder = ContextBuilder(tmp_path, timezone="UTC")
    initial = builder.build_messages(
        history=history,
        current_message="第二轮提问",
        channel="websocket",
        chat_id="c3",
        session_key=session.key,
        workspace=tmp_path,
    )
    ctx = _mk_ctx(session, history, initial)
    ctx.user_persisted_early = True
    before = len(session.messages)

    await _mk_orchestrator(tmp_path, sessions)._state_save(ctx)

    saved = session.messages[before:]
    assert [m.get("role") for m in saved] == ["assistant"], f"实际新增={saved}"
