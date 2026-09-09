"""Planning and reflection service split out of ``AgentRunner`` (PR-5c).

``PlanningReflectionService`` owns the plan-and-execute and reflection
paths that used to live on ``AgentRunner``: planner initialization,
per-step plan guidance, plan-step completion, reflection initialization,
and periodic reflection firing.  It also owns the ``_reflection_tasks``
task-tracking set that used to live on the runner.

The provider is read through ``runner.provider`` at call time (never cached
at construction) so ``ProviderRegistry`` hot-switching keeps applying to
in-flight turns.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from loguru import logger

from erza.ledger import CallPurpose, allow_call_ledger_child_tasks, call_purpose
from erza.utils.prompt_templates import render_template

if TYPE_CHECKING:
    from erza.agent.hook import AgentHook, AgentHookContext
    from erza.agent.runner import AgentRunner, AgentRunSpec
    from erza.agent.step_acceptance import ToolObservation


def extract_task_from_messages(messages: list[dict[str, Any]]) -> str:
    """Extract the user's task from the initial messages (last user msg)."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            text = _message_text(msg)
            if text is not None:
                return text
    return "(task)"


def _message_text(msg: dict[str, Any]) -> str | None:
    """Return a message's text content, or None when it has no text block."""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in reversed(content):
            if isinstance(block, dict) and block.get("type") == "text":
                return str(block.get("text", ""))
    return None


def extract_session_context(messages: list[dict[str, Any]]) -> tuple[str | None, int]:
    """First user message text (truncated to 200 chars) and user message count.

    Gives the gray-zone router the session's original goal so referential
    tasks ("do the three things above") can be judged, without any new
    runtime state or persistence (D3).
    """
    user_msgs = [m for m in messages if m.get("role") == "user"]
    if not user_msgs:
        return None, 0
    first = _message_text(user_msgs[0])
    if first is None:
        return None, len(user_msgs)
    return first[:200], len(user_msgs)


def parse_router_verdict(content: str) -> bool:
    """True iff the first meaningful line says PLAN. Anything else -> False."""
    head = content.strip()[:200].casefold()
    if re.search(r"\b(?:no|not|don'?t|skip|without)\s+(?:the\s+)?plan\b", head):
        return False
    if re.search(r"\bplan\b", head):
        return True
    return False


def inject_step_guidance(
    messages: list[dict[str, Any]],
    guidance: str,
) -> list[dict[str, Any]]:
    """Append step guidance to the last user message (non-destructive copy).

    Returns a new list; the input list and its dicts are not mutated. The
    guidance is appended to the last user message's content so the model
    sees it as additional context without polluting the persisted history
    (the caller passes the returned list only to the LLM, not to messages).
    """
    if not messages:
        return messages
    updated = [dict(m) for m in messages]
    for i in range(len(updated) - 1, -1, -1):
        if updated[i].get("role") == "user":
            content = updated[i].get("content")
            if isinstance(content, str):
                updated[i] = {**updated[i], "content": content + guidance}
            elif isinstance(content, list):
                new_content = list(content) + [{"type": "text", "text": guidance}]
                updated[i] = {**updated[i], "content": new_content}
            break
    return updated


class PlanningReflectionService:
    """Plan-and-Execute and reflection for a single agent turn.

    Constructed with the host ``AgentRunner``; helper collaborators (task
    extraction, step guidance injection) are homed in this module as module
    functions; ``build_tools_summary`` remains on the runner.
    """

    def __init__(self, runner: AgentRunner) -> None:
        self._runner = runner
        # 跟踪 reflection 后台任务，避免被 GC 回收
        self._reflection_tasks: set[asyncio.Task] = set()

    async def emit_plan_snapshot(
        self,
        spec: AgentRunSpec,
        plan: Any,
        turn_id: str,
        stop_reason: str | None = None,
        origin: str = "planner",
    ) -> Any:
        """Serialize *plan* into a PlanSnapshot and emit it as a checkpoint.

        Returns the created snapshot so callers can keep the latest one on
        the turn state. The ``plan_snapshot`` checkpoint payload is additive
        and never replaces existing checkpoint emissions.
        """
        from erza.agent.plan_snapshot import PlanSnapshot

        snapshot = PlanSnapshot.from_plan(plan, turn_id, stop_reason, origin=origin)
        await self._runner.emit_checkpoint(
            spec,
            {
                "phase": "plan_snapshot",
                "plan_snapshot": snapshot.to_dict(),
            },
        )
        return snapshot

    async def init_planner(
        self, spec: AgentRunSpec, *, force_plan: bool = False
    ) -> tuple[Any, Any, str | None, str | None]:
        """Plan-and-Execute 初始化, 返回 (planner, plan, task_text, tools_summary)。

        创建计划失败时回退 ReAct-only (planner/plan 均为 None)。Typed as Any
        to avoid importing planner at module load time (keeps runner.py
        import-light).

        ``force_plan=True`` skips L1 classify and L2 gray-zone adjudication,
        going straight to planner creation. Used by mid-turn stall/drift
        escalation (D4). ``force_plan=False`` (default) is the normal routing
        path — existing callers are unaware of the parameter.
        """
        from erza.agent.planner import Planner as _Planner
        from erza.agent.planner import PlannerStatus as _PlannerStatus
        from erza.agent.planning_policy import PlanningPolicy

        policy = getattr(spec, "planning_policy", None) or PlanningPolicy()
        task_text = extract_task_from_messages(spec.initial_messages)

        if not force_plan:
            from erza.agent.planning_policy import Route

            decision = policy.classify(task_text)
            if decision.route is Route.GRAY:
                wants_plan = await self._classify_gray(spec, task_text)
                logger.info(
                    "Planning route: gray (cause={}, llm={}) signals={}",
                    decision.cause,
                    "plan" if wants_plan else "direct",
                    decision.signals,
                )
                if not wants_plan:
                    return None, None, None, None
            else:
                logger.info(
                    "Planning route: {} cause={} signals={}",
                    decision.route.value,
                    decision.cause,
                    decision.signals,
                )
                if decision.route is Route.DIRECT:
                    return None, None, None, None

        planner = _Planner(self._runner.provider, spec.model)
        tools_summary = self._runner.build_tools_summary(spec.tools)
        try:
            result = await planner.create_plan(
                task=task_text,
                tools_summary=tools_summary,
            )
            if result.status is not _PlannerStatus.VALID:
                logger.warning(
                    "Planner returned fallback {}; using ReAct-only",
                    result.error_code,
                )
                return None, None, task_text, tools_summary
            plan = result.plan
            plan.max_replans = policy.planner_max_replans
            logger.info(
                "Planner produced {} steps for: {}",
                len(plan.steps),
                plan.goal,
            )
        except Exception:
            logger.exception("Planner.create_plan failed; falling back to ReAct-only")
            return None, None, None, None
        return planner, plan, task_text, tools_summary

    async def _classify_gray(self, spec: AgentRunSpec, task_text: str) -> bool:
        """One cheap same-model call deciding a gray task: plan or direct.

        Fail-open to False (DIRECT): a router failure must not block the turn.
        Accounted under CallPurpose.PLANNER (D1); no temperature/max_tokens
        overrides (D2) — call shape mirrors Planner.create_plan (A3).
        """
        first_task, user_count = extract_session_context(spec.initial_messages)
        user_content = f"## Task\n{task_text}\n\n## Session Context\n"
        if first_task is not None:
            user_content += f"First user message: {first_task}\n"
        user_content += f"User messages so far: {user_count}"
        try:
            async with call_purpose(CallPurpose.PLANNER):
                response = await self._runner.provider.chat_with_retry(
                    model=spec.model,
                    messages=[
                        {
                            "role": "system",
                            "content": render_template("agent/planner_router.md", strip=True),
                        },
                        {"role": "user", "content": user_content},
                    ],
                    tools=None,
                    tool_choice=None,
                )
            if response.finish_reason == "error":
                return False
            return parse_router_verdict(response.content or "")
        except Exception:
            logger.warning("Gray-zone router failed; defaulting to DIRECT", exc_info=True)
            return False

    def init_reflection(self, spec: AgentRunSpec) -> Any | None:
        """Optional reflection: produces "lesson learned" entries on failure or
        every reflection_interval iterations. Default False keeps the legacy
        behavior with zero reflection overhead."""
        if not getattr(spec, "enable_reflection", False):
            return None
        from erza.agent.reflection import Reflection

        return Reflection(
            self._runner.provider,
            spec.model,
            spec.workspace,
        )

    async def apply_plan_step_guidance(
        self,
        messages_for_model: list[dict[str, Any]],
        plan: Any,
        *,
        spec: AgentRunSpec | None = None,
        turn_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Mark the current plan step IN_PROGRESS and append step guidance.

        When *spec* and *turn_id* are provided, a plan snapshot is emitted
        after the status transition.
        """
        from erza.agent.planner import StepStatus as _StepStatus

        step = plan.current_step
        step.status = _StepStatus.IN_PROGRESS
        step.iterations_used += 1
        if spec is not None and turn_id is not None:
            await self.emit_plan_snapshot(spec, plan, turn_id)
        guidance = (
            f"\n\n[Current Plan Step {step.id}/{len(plan.steps)}: {step.action}]\n"
            f"Done when: {step.done_criteria or 'step goal achieved'}\n"
            f"Focus on this step. Use tool_hint={step.tool_hint} if applicable."
        )
        return inject_step_guidance(messages_for_model, guidance)

    def fire_periodic_reflection(
        self,
        reflection: Any | None,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        iteration: int,
    ) -> None:
        """Periodic reflection (every reflection_interval iterations).

        Non-blocking: fire-and-forget so the main loop isn't slowed.
        """
        if reflection is None or (iteration + 1) % getattr(spec, "reflection_interval", 5) != 0:
            return
        # 跟踪 reflection 任务避免被 GC 回收，完成后从集合移除
        with allow_call_ledger_child_tasks():
            task = asyncio.create_task(
                reflection.reflect(
                    trigger="periodic",
                    iteration=iteration,
                    context_summary=f"Periodic reflection at iteration {iteration}",
                    messages=messages,
                    session_key=spec.session_key,
                    user_key=spec.user_key,
                )
            )
        self._reflection_tasks.add(task)
        task.add_done_callback(self._reflection_tasks.discard)

    # Stop reasons that already have inline reflection in the loop or
    # recovery path — terminal reflection skips them to avoid duplication.
    _INLINE_REFLECTED_REASONS = frozenset(
        {"tool_error", "plan_failed", "no_progress", "error", "max_iterations"}
    )

    _TERMINAL_TRIGGER_MAP = {
        "completed": "turn_completed",
        "budget_exceeded": "budget_exceeded",
        "turn_timeout": "turn_timeout",
    }

    async def fire_terminal_reflection(
        self,
        reflection: Any | None,
        spec: AgentRunSpec,
        state: Any,
        messages: list[dict[str, Any]],
        iteration: int,
    ) -> None:
        """Fire reflection once after the runner loop exits (P1-T6).

        Only fires for stop reasons that lack inline reflection (completed,
        budget_exceeded, turn_timeout). Wrapped in try/except so a
        reflection failure never blocks ``AgentRunResult`` return.
        """
        if reflection is None:
            return
        reason = state.stop_reason
        if reason in self._INLINE_REFLECTED_REASONS:
            return
        trigger = self._TERMINAL_TRIGGER_MAP.get(reason, reason)
        try:
            await reflection.reflect(
                trigger=trigger,
                iteration=iteration,
                context_summary=state.final_content or f"Turn ended: {reason}",
                messages=messages,
                session_key=spec.session_key,
                user_key=spec.user_key,
            )
        except Exception:
            logger.warning(
                "Terminal reflection failed for {}; result still returned",
                reason,
            )

    async def complete_plan_step(
        self,
        plan: Any,
        context: AgentHookContext,
        hook: AgentHook,
        clean: str | None,
        stop_reason: str,
        *,
        spec: AgentRunSpec | None = None,
        turn_id: str | None = None,
        tool_observations: list[ToolObservation] | None = None,
    ) -> bool:
        """Evaluate step evidence and mark COMPLETED if accepted.

        Returns True when pending steps remain (continue the loop); False
        when all steps are done (caller finalizes the turn). When *spec*
        and *turn_id* are provided, a plan snapshot is emitted after the
        transition — terminal with ``stop_reason="plan_completed"`` when
        the plan is fully done.

        When evidence is rejected, the step remains IN_PROGRESS and True
        is returned (more steps remain — the current step still needs work).
        """
        from erza.agent.planner import StepStatus as _StepStatus
        from erza.agent.step_acceptance import StepAcceptancePolicy

        completed_step = plan.current_step
        if completed_step is None:
            return False

        policy = StepAcceptancePolicy()

        # Initialize verifier cache on plan if not present
        if not hasattr(plan, "_verifier_cache"):
            plan._verifier_cache = {}

        # Use verifier-enabled evaluation when spec provides the config
        if spec is not None and getattr(spec, "enable_step_verifier", False):
            evidence = await policy.evaluate_with_verifier(
                step=completed_step,
                observations=tool_observations,
                final_content=clean,
                iterations_used=completed_step.iterations_used,
                provider=self._runner.provider,
                model=spec.model,
                enable_verifier=True,
                step_evidence_cache=plan._verifier_cache,
            )
        else:
            evidence = policy.evaluate(
                step=completed_step,
                observations=tool_observations,
                final_content=clean,
                iterations_used=completed_step.iterations_used,
            )
        plan.step_evidence.append(evidence)

        if not evidence.accepted:
            logger.info(
                "Step {} evidence rejected ({}); keeping IN_PROGRESS",
                completed_step.id,
                evidence.rejection_reason,
            )
            return True

        completed_step.status = _StepStatus.COMPLETED
        if spec is not None and turn_id is not None:
            await self.emit_plan_snapshot(
                spec,
                plan,
                turn_id,
                stop_reason="plan_completed" if plan.all_done else None,
            )
        if plan.current_step is not None:
            logger.info(
                "Step {} completed ({}); {} steps remaining",
                completed_step.id,
                completed_step.action,
                len(plan.pending_steps),
            )
            context.final_content = clean
            context.stop_reason = stop_reason
            await hook.after_iteration(context)
            return True
        logger.info(
            "All plan steps completed (last: {})",
            completed_step.action,
        )
        return False

    def evaluate_step_progress(self, plan: Any, tracker: Any | None) -> Any | None:
        """Check managed-plan progress after a step evaluation (P1-T4).

        Feeds the latest ``Plan.step_evidence`` entry (if any) plus the
        current step into *tracker*. Returns the tracker's
        ``ProgressVerdict``, or ``None`` when no tracker is attached
        (FAST mode / legacy ReAct-only turns).
        """
        if tracker is None or plan is None:
            return None
        evidence = plan.step_evidence[-1] if plan.step_evidence else None
        # Feed the verifier circuit breaker before the verdict is computed: a
        # verdict that was reached without the verifier says nothing about it.
        verdict_dict = evidence.verifier_verdict if evidence is not None else None
        if verdict_dict is not None:
            if verdict_dict.get("error") == "verifier_failed":
                tracker.record_verifier_failure()
            else:
                tracker.record_verifier_success()
        return tracker.check_step_progress(plan.current_step, evidence)
