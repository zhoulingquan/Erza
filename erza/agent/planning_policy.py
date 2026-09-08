"""PlanningPolicy — deterministic L1 heuristic gate for per-turn planning.

Single-model design (P4): the planner always reuses the execution model.
Whether a turn gets a plan is decided per turn by task-text heuristics rather
than a global ``use_planner`` flag, a separate planner model, or a classifier
LLM call.

Three-valued routing (W11): L1 reduces each turn to a ``Route``:
- PLAN   — strong multi-step signal (lists / many step markers / length) or a
           productive-verb x product-noun macro goal -> straight to the planner;
- DIRECT — greeting / acknowledgement or a single question -> plain ReAct with
           zero planning overhead;
- GRAY   — undecidable by heuristics alone (verb present without a product, or
           no signal at all) -> left for L2 adjudication (wired in W11-2).

``should_plan`` stays a compat facade: True only when L1 routes PLAN. GRAY in
this batch is equivalent to DIRECT from the caller's perspective because only
PLAN is consulted. Decision table (evaluation order, see ``classify``):
forced -> empty -> strong_signal -> macro_goal -> verb_gate -> trivial -> gray.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from loguru import logger

# A task at or below this effective length is treated as trivial chat when it
# reads as a greeting / acknowledgement (feeds the trivial bucket in classify).
_TRIVIAL_TASK_CHARS = 24
# Tasks with an effective length >= this plan outright (rich context = work).
# CJK characters weight 1.0, other characters 0.4 (see _effective_length).
_LONG_TASK_CHARS = 300
# Ordered/bulleted list detection: line-anchored lists ("1. " / "1、" / "- ")
# plus inline numbered sequences ("1. read 2. write 3. commit").
_LIST_LINE_RE = re.compile(r"(?m)^\s*(?:\d+[.、)]\s*|[•\-*]\s+)")
_INLINE_NUMBERED_RE = re.compile(r"(?<!\d)\d+[.、)]\s+\S")

# Step markers: two or more *distinct* ones plan outright. Bare single
# characters like "再" are intentionally excluded so casual sentences ("先看下
# 这个，然后再告诉我") keep only one marker and do not over-trigger.
_STEP_MARKERS_ZH: tuple[str, ...] = ("然后", "接着", "之后", "随后", "分步", "步骤", "最后")
_STEP_MARKERS_EN: tuple[str, ...] = (
    "then",
    "after that",
    "afterwards",
    "next",
    "step 1",
    "step one",
    "first",
    "finally",
)
_STEP_MARKERS_EN_RE = re.compile(
    r"\b(?:then|after\s+that|afterwards|next|step\s+1|step\s+one|first|finally)\b"
)

# Productive-macro word banks (closed vocabulary — D7, do not extend here).
_PRODUCTIVE_VERBS_ZH = ("做", "建", "搭", "开发", "实现", "制作", "设计", "写")
_PRODUCTIVE_VERBS_EN = ("create", "build", "make", "write", "develop", "implement", "design")
_PRODUCT_NOUNS_ZH = (
    "游戏",
    "网站",
    "网页",
    "系统",
    "应用",
    "平台",
    "引擎",
    "工具",
    "页面",
    "机器人",
    "客户端",
    "服务端",
)
_PRODUCT_NOUNS_EN = (
    "game",
    "website",
    "web app",
    "system",
    "application",
    "app",
    "platform",
    "engine",
    "tool",
    "bot",
    "client",
    "server",
)
_GREETINGS = (
    "你好",
    "您好",
    "嗨",
    "哈喽",
    "谢谢",
    "感谢",
    "多谢",
    "好的",
    "好嘞",
    "收到",
    "明白了",
    "ok",
    "okay",
    "thanks",
    "thank you",
    "got it",
)

# Separators allowed inside a greeting phrase (e.g. "好的，收到").
_GREETING_SEPARATORS = ("，", ",", "。", ".", "、", " ", "：", ":", "？", "!", "！", "?")


class Route(str, Enum):
    """Per-turn planning route decided by L1 heuristics."""

    PLAN = "plan"  # straight to Planner.create_plan
    DIRECT = "direct"  # plain ReAct, zero planning overhead
    GRAY = "gray"  # undecidable by heuristics alone (L2 adjudicates)


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """L1 outcome: route + machine-readable audit cause/signals."""

    route: Route
    cause: str  # "forced" | "empty" | "strong_signal" | "macro_goal" \
    # | "verb_gate" | "trivial" | "no_signal"
    signals: dict[str, Any]  # measured evidence only (list_lines / effective_len \
    # / markers / verb / product)


def _effective_length(text: str) -> float:
    """Language-aware effective length: CJK weight 1.0, other chars 0.4."""
    total = 0.0
    for ch in text:
        if "\u3400" <= ch <= "\u4dbf" or "\u4e00" <= ch <= "\u9fff":
            total += 1.0
        else:
            total += 0.4
    return total


def _find_zh(wordbank: tuple[str, ...], text: str) -> str | None:
    for word in wordbank:
        if word in text:
            return word
    return None


def _find_en(wordbank: tuple[str, ...], text: str) -> str | None:
    folded = text.casefold()
    for word in wordbank:
        if re.search(rf"\b{re.escape(word)}\b", folded):
            return word
    return None


def _is_greeting(text: str) -> bool:
    """True when the whole message is a greeting / acknowledgement.

    Greetings are matched greedily from the start (word or separator); a task
    request following the greeting ("你好，帮我看看...") leaves a non-greeting
    tail and therefore does not count as trivial.
    """
    remaining = text.casefold()
    while remaining:
        matched = False
        for greeting in _GREETINGS:
            if remaining.startswith(greeting):
                remaining = remaining[len(greeting) :]
                matched = True
                break
        if matched:
            continue
        for separator in _GREETING_SEPARATORS:
            if remaining.startswith(separator):
                remaining = remaining[len(separator) :]
                matched = True
                break
        if not matched:
            return False
    return True


@dataclass(frozen=True, slots=True)
class PlanningPolicy:
    """Whether a turn gets a plan, plus planner tuning knobs.

    ``force_plan`` is a deterministic override (used by tests and by embedders
    that want to force/skip planning); ``None`` routes by the task-text
    heuristics below.
    """

    planner_max_replans: int = 3
    force_plan: bool | None = None

    def classify(self, task_text: str | None) -> RouteDecision:
        """Route a task text deterministically (zero LLM calls) via L1."""
        if self.force_plan is not None:
            route = Route.PLAN if self.force_plan else Route.DIRECT
            return RouteDecision(route=route, cause="forced", signals={})
        if not task_text:
            return RouteDecision(route=Route.DIRECT, cause="empty", signals={})
        text = task_text.strip()
        if not text:
            return RouteDecision(route=Route.DIRECT, cause="empty", signals={})

        list_lines = len(_LIST_LINE_RE.findall(text))
        inline_numbered = len(_INLINE_NUMBERED_RE.findall(text))
        eff_len = _effective_length(text)

        zh_markers = [m for m in _STEP_MARKERS_ZH if m in text]
        en_markers = list(_STEP_MARKERS_EN_RE.findall(text.casefold()))
        all_markers = zh_markers + en_markers
        marker_count = len(set(all_markers))

        if (
            list_lines >= 2
            or inline_numbered >= 2
            or eff_len >= _LONG_TASK_CHARS
            or marker_count >= 2
        ):
            signals: dict[str, Any] = {}
            if list_lines >= 2:
                signals["list_lines"] = list_lines
            if inline_numbered >= 2:
                signals["inline_numbered"] = inline_numbered
            if eff_len >= _LONG_TASK_CHARS:
                signals["effective_len"] = eff_len
            if marker_count >= 2:
                signals["markers"] = all_markers
            return RouteDecision(route=Route.PLAN, cause="strong_signal", signals=signals)

        verb = _find_zh(_PRODUCTIVE_VERBS_ZH, text) or _find_en(_PRODUCTIVE_VERBS_EN, text)
        product = _find_zh(_PRODUCT_NOUNS_ZH, text) or _find_en(_PRODUCT_NOUNS_EN, text)

        if verb is not None and product is not None:
            return RouteDecision(
                route=Route.PLAN,
                cause="macro_goal",
                signals={"verb": verb, "product": product},
            )
        if verb is not None:
            return RouteDecision(
                route=Route.GRAY,
                cause="verb_gate",
                signals={"verb": verb},
            )

        if eff_len < _TRIVIAL_TASK_CHARS and _is_greeting(text):
            return RouteDecision(route=Route.DIRECT, cause="trivial", signals={})

        stripped = text.rstrip()
        if not re.search(r"[。；;\n]", text) and stripped.endswith(("？", "?", "吗", "呢")):
            return RouteDecision(route=Route.DIRECT, cause="trivial", signals={})

        return RouteDecision(route=Route.GRAY, cause="no_signal", signals={})

    def should_plan(self, task_text: str | None) -> bool:
        """Compat facade: True only when L1 routes PLAN (GRAY awaits L2 in W11-2)."""
        decision = self.classify(task_text)
        logger.debug(
            "Planning route L1: {} cause={} signals={}",
            decision.route.value,
            decision.cause,
            decision.signals,
        )
        return decision.route is Route.PLAN
