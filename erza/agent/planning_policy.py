"""PlanningPolicy — deterministic per-turn planning router.

Single-model design (P4 simplification): the planner always reuses the
execution model; whether a turn gets a plan is decided by deterministic
task-text heuristics instead of a global ``use_planner`` flag, a separate
planner model, or a classifier LLM call.

Rules (``PlanningPolicy.should_plan``):
- ``force_plan`` override wins when set (tests / deterministic embedding).
- empty task -> no plan;
- structured lists (numbered/bulleted lines *and* inline numbered sequences
  like "1. read 2. write 3. commit") -> plan;
- two or more distinct multi-step markers (then/接着/…) -> plan;
- very long tasks (>= ``_LONG_TASK_CHARS`` chars) -> plan;
- everything else -> plain ReAct, zero planning overhead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Task shorter than this is never planned (trivial chat), unless a strong
# structural signal (list / step markers / length) already fired.
_TRIVIAL_TASK_CHARS = 24
# Tasks at least this long are planned outright (rich context = real work).
_LONG_TASK_CHARS = 300
# Multi-step markers: a task mentioning two or more distinct ones plans.
_MULTI_STEP_MARKERS: tuple[str, ...] = (
    "then",
    "after that",
    "afterwards",
    "next,",
    "step 1",
    "step one",
    "first,",
    "finally,",
    "然后",
    "接着",
    "之后",
    "再",
    "随后",
    "分步",
    "步骤",
    "最后",
)
# Ordered/bulleted list detection: line-anchored lists ("1. " / "1、" / "- ")
# plus inline numbered sequences ("1. read 2. write 3. commit").
_LIST_LINE_RE = re.compile(r"(?m)^\s*(?:\d+[.、)]\s*|[•\-*]\s+)")
_INLINE_NUMBERED_RE = re.compile(r"(?<!\d)\d+[.、)]\s+\S")


@dataclass(frozen=True, slots=True)
class PlanningPolicy:
    """Whether a turn gets a plan, plus planner tuning knobs.

    ``force_plan`` is a deterministic override (used by tests and by
    embedders that want to force/skip planning); ``None`` routes by the
    task-text heuristics below.
    """

    planner_max_replans: int = 3
    force_plan: bool | None = None

    def should_plan(self, task_text: str | None) -> bool:
        """Decide — deterministically, with zero LLM calls — whether this
        turn's task is complex enough to warrant a plan."""
        if self.force_plan is not None:
            return self.force_plan
        return _looks_multi_step(task_text)


def _looks_multi_step(task_text: str | None) -> bool:
    if not task_text:
        return False
    text = task_text.strip()
    if not text:
        return False
    # Strong structural signals fire before the trivial-length guard: a
    # numbered/bulleted list, a very long task, or two distinct step markers
    # is real work regardless of raw length.
    if _LIST_LINE_RE.search(text):
        return True
    if len(_INLINE_NUMBERED_RE.findall(text)) >= 2:
        return True
    if len(text) >= _LONG_TASK_CHARS:
        return True
    distinct = {marker for marker in _MULTI_STEP_MARKERS if marker in text}
    if len(distinct) >= 2:
        return True
    if len(text) < _TRIVIAL_TASK_CHARS:
        return False
    return False
