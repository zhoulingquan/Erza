"""Direct unit tests for AutoCompact class methods in isolation."""

from types import SimpleNamespace
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from erza.agent.autocompact import AutoCompact
from erza.memory.consolidator import Consolidator
from erza.session.manager import Session, SessionManager


def _make_session(
    key: str = "cli:test",
    messages: list | None = None,
    last_consolidated: int = 0,
    updated_at: datetime | None = None,
    metadata: dict | None = None,
) -> Session:
    """Create a Session with sensible defaults for testing."""
    session = Session(
        key=key,
        messages=messages or [],
        metadata=metadata or {},
        last_consolidated=last_consolidated,
    )
    if updated_at is not None:
        session.updated_at = updated_at
    return session


def _make_autocompact(
    ttl: int = 15,
    sessions: SessionManager | None = None,
    consolidator: MagicMock | None = None,
) -> AutoCompact:
    """Create an AutoCompact with mock dependencies."""
    if sessions is None:
        sessions = MagicMock(spec=SessionManager)
    if consolidator is None:
        consolidator = MagicMock()
        consolidator.compact_idle_session = AsyncMock(return_value="Summary.")
    return AutoCompact(
        sessions=sessions,
        consolidator=consolidator,
        session_ttl_minutes=ttl,
    )


def _add_turns(session: Session, turns: int, *, prefix: str = "msg") -> None:
    """Append simple user/assistant turns to a session."""
    for i in range(turns):
        session.add_message("user", f"{prefix} user {i}")
        session.add_message("assistant", f"{prefix} assistant {i}")


# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------


class TestInit:
    """Test AutoCompact.__init__ stores constructor arguments correctly."""

    def test_stores_ttl(self):
        """_ttl should match session_ttl_minutes argument."""
        ac = _make_autocompact(ttl=30)
        assert ac._ttl == 30

    def test_default_ttl_is_zero(self):
        """Default TTL should be 0."""
        ac = _make_autocompact(ttl=0)
        assert ac._ttl == 0

    def test_archiving_set_is_empty(self):
        """_archiving should start as an empty set."""
        ac = _make_autocompact()
        assert ac._archiving == set()

    def test_no_summary_cache_attribute(self):
        """W10-C3: the in-memory summary cache is gone; summaries live in the session log."""
        ac = _make_autocompact()
        assert not hasattr(ac, "_summaries")

    def test_stores_sessions_reference(self):
        """sessions attribute should reference the passed SessionManager."""
        mock_sm = MagicMock(spec=SessionManager)
        ac = _make_autocompact(sessions=mock_sm)
        assert ac.sessions is mock_sm

    def test_stores_consolidator_reference(self):
        """consolidator attribute should reference the passed Consolidator."""
        mock_c = MagicMock()
        ac = _make_autocompact(consolidator=mock_c)
        assert ac.consolidator is mock_c


# ---------------------------------------------------------------------------
# _is_expired
# ---------------------------------------------------------------------------


class TestIsExpired:
    """Test AutoCompact._is_expired edge cases."""

    def test_ttl_zero_always_false(self):
        """TTL=0 means auto-compact is disabled; always returns False."""
        ac = _make_autocompact(ttl=0)
        old = datetime.now() - timedelta(days=365)
        assert ac._is_expired(old) is False

    def test_none_timestamp_returns_false(self):
        """None timestamp should return False."""
        ac = _make_autocompact(ttl=15)
        assert ac._is_expired(None) is False

    def test_empty_string_timestamp_returns_false(self):
        """Empty string timestamp should return False (falsy)."""
        ac = _make_autocompact(ttl=15)
        assert ac._is_expired("") is False

    def test_exactly_at_boundary_is_expired(self):
        """Timestamp exactly at TTL boundary should be expired (>=)."""
        ac = _make_autocompact(ttl=15)
        now = datetime(2026, 1, 1, 12, 0, 0)
        ts = now - timedelta(minutes=15)
        assert ac._is_expired(ts, now=now) is True

    def test_just_under_boundary_not_expired(self):
        """Timestamp just under TTL boundary should NOT be expired."""
        ac = _make_autocompact(ttl=15)
        now = datetime(2026, 1, 1, 12, 0, 0)
        ts = now - timedelta(minutes=14, seconds=59)
        assert ac._is_expired(ts, now=now) is False

    def test_iso_string_parses_correctly(self):
        """ISO format string timestamp should be parsed and evaluated."""
        ac = _make_autocompact(ttl=15)
        now = datetime(2026, 1, 1, 12, 0, 0)
        ts = (now - timedelta(minutes=20)).isoformat()
        assert ac._is_expired(ts, now=now) is True

    def test_custom_now_parameter(self):
        """Custom 'now' parameter should override datetime.now()."""
        ac = _make_autocompact(ttl=10)
        ts = datetime(2026, 1, 1, 10, 0, 0)
        # 9 minutes later → not expired
        now_under = datetime(2026, 1, 1, 10, 9, 0)
        assert ac._is_expired(ts, now=now_under) is False
        # 10 minutes later → expired
        now_over = datetime(2026, 1, 1, 10, 10, 0)
        assert ac._is_expired(ts, now=now_over) is True


# ---------------------------------------------------------------------------
# Consolidator._insert_summary_message
# ---------------------------------------------------------------------------


class TestInsertSummaryMessage:
    """Test Consolidator._insert_summary_message formatting (W10-C3)."""

    @staticmethod
    def _session() -> SimpleNamespace:
        return SimpleNamespace(messages=[])

    def test_inserts_user_message_with_header(self):
        """摘要应作为带 _archived_summary 标记的 user 消息插入指定位置。"""
        session = self._session()
        Consolidator._insert_summary_message(None, session, 0, "User discussed Python.")
        assert len(session.messages) == 1
        msg = session.messages[0]
        assert msg["role"] == "user"
        assert msg["_archived_summary"] is True
        assert msg["content"].startswith("[Archived Context Summary]")
        assert "User discussed Python." in msg["content"]

    def test_inserts_at_requested_index(self):
        session = self._session()
        session.messages = [{"role": "user", "content": "kept"}]
        Consolidator._insert_summary_message(None, session, 0, "summary text")
        assert session.messages[0]["_archived_summary"] is True
        assert session.messages[1]["content"] == "kept"

    def test_verbatim_appended_when_provided(self):
        """verbatim_recent 用户消息原文应拼接到 summary 之后（防改写偏离）。"""
        session = self._session()
        Consolidator._insert_summary_message(
            None,
            session,
            0,
            "summary text",
            verbatim_recent=["把订单号改成 12345", "再帮我加一条备注"],
        )
        content = session.messages[0]["content"]
        assert "Recent user messages (verbatim):" in content
        assert "把订单号改成 12345" in content
        assert "再帮我加一条备注" in content

    def test_verbatim_long_message_truncated(self):
        """过长的 verbatim 消息应被截断到 500 字符以内。"""
        long_msg = "q" * 1000  # 'q' 不出现在头部/标签中，便于精确计数
        session = self._session()
        Consolidator._insert_summary_message(
            None, session, 0, "s", verbatim_recent=[long_msg]
        )
        content = session.messages[0]["content"]
        assert "..." in content
        # 截断后应保留 500 字符 + "..."
        assert content.count("q") == 500

    def test_verbatim_omitted_when_empty(self):
        """空的 verbatim 列表不应触发拼接块。"""
        session = self._session()
        Consolidator._insert_summary_message(None, session, 0, "s", verbatim_recent=[])
        assert "Recent user messages (verbatim):" not in session.messages[0]["content"]


# ---------------------------------------------------------------------------
# check_expired
# ---------------------------------------------------------------------------


class TestCheckExpired:
    """Test AutoCompact.check_expired scheduling logic."""

    def test_empty_sessions_list(self):
        """No sessions → schedule_background should never be called."""
        ac = _make_autocompact(ttl=15)
        mock_sm = MagicMock(spec=SessionManager)
        mock_sm.list_sessions.return_value = []
        ac.sessions = mock_sm
        scheduler = MagicMock()
        ac.check_expired(scheduler)
        scheduler.assert_not_called()

    def test_expired_session_schedules_background(self):
        """Expired session should trigger schedule_background."""
        ac = _make_autocompact(ttl=15)
        mock_sm = MagicMock(spec=SessionManager)
        old_ts = (datetime.now() - timedelta(minutes=20)).isoformat()
        mock_sm.list_sessions.return_value = [{"key": "cli:old", "updated_at": old_ts}]
        ac.sessions = mock_sm
        scheduler = MagicMock()
        ac.check_expired(scheduler)
        scheduler.assert_called_once()
        assert "cli:old" in ac._archiving

    def test_active_session_key_skips(self):
        """Session in active_session_keys should be skipped."""
        ac = _make_autocompact(ttl=15)
        mock_sm = MagicMock(spec=SessionManager)
        old_ts = (datetime.now() - timedelta(minutes=20)).isoformat()
        mock_sm.list_sessions.return_value = [{"key": "cli:busy", "updated_at": old_ts}]
        ac.sessions = mock_sm
        scheduler = MagicMock()
        ac.check_expired(scheduler, active_session_keys={"cli:busy"})
        scheduler.assert_not_called()

    def test_session_already_in_archiving_skips(self):
        """Session already in _archiving set should be skipped."""
        ac = _make_autocompact(ttl=15)
        mock_sm = MagicMock(spec=SessionManager)
        old_ts = (datetime.now() - timedelta(minutes=20)).isoformat()
        mock_sm.list_sessions.return_value = [{"key": "cli:dup", "updated_at": old_ts}]
        ac.sessions = mock_sm
        ac._archiving.add("cli:dup")
        scheduler = MagicMock()
        ac.check_expired(scheduler)
        scheduler.assert_not_called()

    def test_session_with_no_key_skips(self):
        """Session info with empty/missing key should be skipped."""
        ac = _make_autocompact(ttl=15)
        mock_sm = MagicMock(spec=SessionManager)
        mock_sm.list_sessions.return_value = [{"key": "", "updated_at": "old"}]
        ac.sessions = mock_sm
        scheduler = MagicMock()
        ac.check_expired(scheduler)
        scheduler.assert_not_called()

    def test_session_with_missing_key_field_skips(self):
        """Session info dict without 'key' field should be skipped."""
        ac = _make_autocompact(ttl=15)
        mock_sm = MagicMock(spec=SessionManager)
        mock_sm.list_sessions.return_value = [{"updated_at": "old"}]
        ac.sessions = mock_sm
        scheduler = MagicMock()
        ac.check_expired(scheduler)
        scheduler.assert_not_called()


# ---------------------------------------------------------------------------
# _archive
# ---------------------------------------------------------------------------


class TestArchiveDelegates:
    """_archive should delegate all session mutation to Consolidator."""

    @pytest.mark.asyncio
    async def test_calls_compact_idle_session(self):
        ac = _make_autocompact()
        mock_sm = MagicMock(spec=SessionManager)
        ac.sessions = mock_sm
        ac.consolidator.compact_idle_session = AsyncMock(return_value="Summary.")

        await ac._archive("cli:test")

        ac.consolidator.compact_idle_session.assert_awaited_once_with(
            "cli:test",
            ac._RECENT_SUFFIX_MESSAGES,
        )

    @pytest.mark.asyncio
    async def test_archive_does_not_touch_metadata_directly(self):
        """W10-C3: _archive delegates everything (incl. metadata persistence) to Consolidator."""
        ac = _make_autocompact()
        mock_sm = MagicMock(spec=SessionManager)
        session = _make_session(
            metadata={"_last_summary": {"text": "Hello.", "last_active": "2026-05-13T10:00:00"}}
        )
        mock_sm.get_or_create.return_value = session
        ac.sessions = mock_sm
        ac.consolidator.compact_idle_session = AsyncMock(return_value="Hello.")

        await ac._archive("cli:test")

        ac.consolidator.compact_idle_session.assert_awaited_once_with(
            "cli:test",
            ac._RECENT_SUFFIX_MESSAGES,
        )

    @pytest.mark.asyncio
    async def test_no_summary_when_compact_returns_empty(self):
        ac = _make_autocompact()
        mock_sm = MagicMock(spec=SessionManager)
        ac.sessions = mock_sm
        ac.consolidator.compact_idle_session = AsyncMock(return_value="")

        await ac._archive("cli:test")

        # W10-C3: nothing to persist; no metadata write happened.
        mock_sm.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_summary_when_compact_returns_nothing(self):
        ac = _make_autocompact()
        mock_sm = MagicMock(spec=SessionManager)
        ac.sessions = mock_sm
        ac.consolidator.compact_idle_session = AsyncMock(return_value="(nothing)")

        await ac._archive("cli:test")

        # W10-C3: nothing to persist; no metadata write happened.
        mock_sm.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_exception_still_removes_from_archiving(self):
        ac = _make_autocompact()
        mock_sm = MagicMock(spec=SessionManager)
        ac.sessions = mock_sm
        ac.consolidator.compact_idle_session = AsyncMock(side_effect=RuntimeError("fail"))

        ac._archiving.add("cli:test")
        await ac._archive("cli:test")

        assert "cli:test" not in ac._archiving


# ---------------------------------------------------------------------------
# prepare_session
# ---------------------------------------------------------------------------


class TestPrepareSession:
    """Test AutoCompact.prepare_session logic."""

    def test_key_in_archiving_reloads_session(self):
        """If key is in _archiving, session should be reloaded via get_or_create."""
        ac = _make_autocompact()
        mock_sm = MagicMock(spec=SessionManager)
        reloaded = _make_session(key="cli:test")
        mock_sm.get_or_create.return_value = reloaded
        ac.sessions = mock_sm
        ac._archiving.add("cli:test")

        original_session = _make_session()
        result_session = ac.prepare_session(original_session, "cli:test")

        mock_sm.get_or_create.assert_called_once_with("cli:test")
        assert result_session is reloaded

    def test_expired_session_reloads(self):
        """If session is expired, it should be reloaded via get_or_create."""
        ac = _make_autocompact(ttl=15)
        mock_sm = MagicMock(spec=SessionManager)
        reloaded = _make_session(key="cli:test", updated_at=datetime.now())
        mock_sm.get_or_create.return_value = reloaded
        ac.sessions = mock_sm

        old_session = _make_session(updated_at=datetime.now() - timedelta(minutes=20))
        result_session = ac.prepare_session(old_session, "cli:test")

        mock_sm.get_or_create.assert_called_once_with("cli:test")
        assert result_session is reloaded

    def test_unchanged_session_returned_as_is(self):
        """W10-C3: prepare_session is reload-only; it never injects a summary."""
        ac = _make_autocompact()
        last_active = datetime(2026, 5, 13, 14, 0, 0)
        session = _make_session(
            metadata={
                "_last_summary": {
                    "text": "Old summary.",
                    "last_active": last_active.isoformat(),
                },
            }
        )

        result = ac.prepare_session(session, "cli:test")

        assert result is session
        # Summary replay comes from session history (messages), not from this method.
        assert "_last_summary" in session.metadata
        assert result.messages == session.messages
