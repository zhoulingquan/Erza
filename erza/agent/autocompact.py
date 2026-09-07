"""Auto compact: proactive compression of idle sessions to reduce token cost and latency."""

from __future__ import annotations

import time
from collections.abc import Collection
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Coroutine

from loguru import logger

from erza.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from erza.memory import Consolidator


class AutoCompact:
    _RECENT_SUFFIX_MESSAGES = 8
    # list_sessions() 做全目录 glob + 逐文件扫描, 空闲网关下每秒执行一次代价
    # 过高; 节流为每 30 秒最多扫描一次 (主循环仍每秒轮询消息, 不影响响应)。
    _RESCAN_INTERVAL_S = 30.0

    def __init__(
        self,
        sessions: SessionManager,
        consolidator: Consolidator,
        session_ttl_minutes: int = 0,
        consolidator_for: Callable[[str], Consolidator] | None = None,
    ):
        self.sessions = sessions
        self.consolidator = consolidator
        self.consolidator_for = consolidator_for
        self._ttl = session_ttl_minutes
        self._archiving: set[str] = set()
        self._last_scan_monotonic = 0.0

    def _is_expired(self, ts: datetime | str | None, now: datetime | None = None) -> bool:
        if self._ttl <= 0 or not ts:
            return False
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        return ((now or datetime.now()) - ts).total_seconds() >= self._ttl * 60

    def check_expired(
        self,
        schedule_background: Callable[[Coroutine], None],
        active_session_keys: Collection[str] = (),
    ) -> None:
        """Schedule archival for idle sessions, skipping those with in-flight agent tasks."""
        if self._ttl <= 0:
            return
        now_mono = time.monotonic()
        if now_mono - self._last_scan_monotonic < self._RESCAN_INTERVAL_S:
            return
        self._last_scan_monotonic = now_mono
        now = datetime.now()
        for info in self.sessions.list_sessions():
            key = info.get("key", "")
            if not key or key in self._archiving:
                continue
            if key in active_session_keys:
                continue
            if self._is_expired(info.get("updated_at"), now):
                self._archiving.add(key)
                # 先标记再调度，调度失败需回滚标记，避免任务集泄漏
                try:
                    schedule_background(self._archive(key))
                except Exception:
                    self._archiving.discard(key)
                    raise

    async def _archive(self, key: str) -> None:
        """Archive an idle session via Consolidator.compact_idle_session.

        摘要持久化（metadata["_last_summary"] + 摘要消息插入会话日志）完全由
        Consolidator 负责；这里只负责调度与兜底日志。prepare_session 不再
        回读摘要（W10-C3：摘要随会话消息重放，不注入 system prompt）。
        """
        try:
            consolidator = (
                self.consolidator_for(key)
                if self.consolidator_for is not None
                else self.consolidator
            )
            await consolidator.compact_idle_session(
                key,
                self._RECENT_SUFFIX_MESSAGES,
            )
        except Exception:
            logger.exception("Auto-compact: failed for {}", key)
        finally:
            self._archiving.discard(key)

    def prepare_session(self, session: Session, key: str) -> Session:
        """Reload a session that is being archived or has expired (TTL).

        W10-C3：摘要不再经此方法注入（旧的 _summaries 热路径与
        metadata 冷路径已删除）；摘要以消息形式存在于会话日志中，
        随 get_history 重放。本方法只负责归档/过期时的会话重载。
        """
        if key in self._archiving or self._is_expired(session.updated_at):
            logger.info(
                "Auto-compact: reloading session {} (archiving={})", key, key in self._archiving
            )
            session = self.sessions.get_or_create(key)
        return session
