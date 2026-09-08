# 任务书 W11-3：L3a 停滞升级恢复

> 系列总览与红线见 `00-overview.md`。依赖 W11-2 已合并。本批恢复 P4 迁移中
> 被删除的"FAST 停滞检测 → 中途升级 MANAGED"语义（D4），适配新路由：
> **DIRECT/GRAY→DIRECT 路由的 turn 在执行中连续无工具响应 ≥2 次 → 中途补建
> 计划**，snapshot origin="escalated"。恢复语义以 HEAD（commit 9cce3385）
> 的 `runner.py` 为参照，**但删除 PlanningMode 依赖**。

## 0. 勘误 E2（2026-09-08 二次实施裁定；必读）

首次实施按纪律停止：`test_runner_injections.py::test_pending_queue_preserves_overflow_for_next_injection_cycle`
回归——注入驱动的连续无工具迭代触发升级真调规划器，消耗一次脚本化响应使
断言错位。裁定前已核实 HEAD 真实机制：**HEAD 的第三门是
`spec.planning_policy is not None`**（默认配置 policy=None，升级从未默认生效）；
W11 语义下 policy 恒存在，升级首次面向所有无 plan 的 turn 生效——回归是
新语义的**真实行为暴露**，不是 D4 对 HEAD 的偏离。同时发现 HEAD 停滞计数
定义在新架构下不完备：注入系统会在无工具响应后追加新消息继续循环，这类
"应答新输入"的迭代与"原地停滞"不可区分，需语义补全。

### E2.1 停滞计数语义补全（授权改动）

停滞计数定义修正为：**"无新输入下连续无工具迭代"**。重置点两个：

1. 工具迭代执行（既有 2.5，模型动手了）；
2. **真实注入到达**（本勘误新增）：`try_drain_injections` 返回
   `(should_continue, new_cycles)` 且 `new_cycles > 注入前 cycles`（真实用户
   消息入列，模型是在应答新输入，不构成停滞证据）。

**goal_continue 注入不重置**（它是系统对同一目标的再提示，模型对同一目标
持续不动手仍是停滞；且 goal 重写生成的目标指令通常已带强信号在 turn 开头
规划，plan 非 None 时升级门本就关闭）。

实现约束：`try_drain_injections` 签名与行为**不改**；重置逻辑放调用侧
（`should_continue and new_cycles > old_cycles` 时置
`state.consecutive_nontool_iterations = 0`）。授权文件集在原 3 个路径之上
扩展为"`runner.py` 中 `try_drain_injections` 的调用侧计数重置"。

### E2.2 新增测试 S9

`test_injection_resets_stall_counter`：真实注入到达后停滞计数归零——
注入后连续无工具迭代不触发升级；注入耗尽（达到 `_MAX_INJECTION_CYCLES`
或队列空）后恢复累积、正常触发升级。`test_runner_injections.py` 既有用例
（不改）作为回归守卫。

### E2.3 安全性论证（记录）

注入流持续重置 → 升级不触发，但有 `_MAX_INJECTION_CYCLES` 上限兜底：真实
注入耗尽后计数恢复累积，升级安全网仍然闭环。goal_continue 不重置，/goal
turn 的停滞升级不受影响。

## 1. 现状锚点（逐条核对，任何一条不符立即停止报告）

| # | 锚点 | 位置（参考值） |
|---|------|----------------|
| A1 | `AgentRunner._run_with_ledger`：`for iteration in range(spec.max_iterations)` 主循环；循环体顺序 = deadline 检查（443-450）→ `govern_messages`（452）→ 计划步骤指引（456-459）→ hook/预算预检 → `request_model`（478）→ 工具/非工具分派（514/533） | `erza/agent/runner.py` L441-535 |
| A2 | `init_planner` 唯一调用点：`planner, plan, planner_task_text, planner_tools_summary = await self.init_planner(spec)`（循环外，433 行） | 同文件 L433 |
| A3 | `_TurnState` 字段：`turn_id`、`plan_snapshot`、`tools_used`、`tool_events`、`progress_tracker` 等；**无** `consecutive_nontool_iterations`/`escalated_this_turn`（P4 已删） | 同文件 L163-190 |
| A4 | `_execute_tool_iteration` 末尾：`take_pending_plan()`/`_adopt_activated_plan` 后 `return "continue", plan` | 同文件（方法体） |
| A5 | `_finalize_nontool_iteration` 开头：`if response.has_tool_calls:` 仅告警 | 同文件 L733-738 |
| A6 | `emit_plan_snapshot(spec, plan, turn_id, stop_reason=None, origin="planner")` **已有 origin 形参**；`PlanSnapshot.from_plan(..., origin=...)` | `erza/agent/execution/planning.py` L82-106 |
| A7 | HEAD 参照（`git show HEAD:erza/agent/runner.py`）：`_maybe_escalate_to_managed` L606-677（条件门→`init_planner` 重试→快照→ProgressTracker 重建→状态写回→except 降级）；计数重置 L795（工具迭代末尾，"T5: Reset FAST stall counter"）；计数递增 L833（`if plan is None: state.consecutive_nontool_iterations += 1`） | git 历史 |
| A8 | HEAD 测试参照：`git show HEAD:tests/agent/test_fast_to_managed_escalation.py`（283 行，含 FakeProvider 多轮 no-tool 响应驱动全循环的样板） | git 历史 |
| A9 | `ProgressPolicy`/`ProgressTracker` 延迟 import 样板：`runner.py` L436-438 | `erza/agent/runner.py` |
| A10 | W11-2 后 `init_planner(spec)` 内部为 classify 路由（L1+L2）；`spec.planning_policy` 可能是 `PlanningPolicy()` 默认实例（loop 侧无 policy 时 runner 侧兜底，见 planning.py 现状） | `erza/agent/execution/planning.py` |

## 2. 改动方案

涉及 2 个生产文件 + 1 个新测试文件：

- `erza/agent/execution/planning.py`：`init_planner` 增加 `force_plan` 形参
- `erza/agent/runner.py`：状态字段 + 升级方法 + 循环接线 + 计数维护
- `tests/agent/test_stall_escalation.py`（新增）

### 2.1 `init_planner` 增加强制入口（`execution/planning.py`）

```python
async def init_planner(
    self, spec: AgentRunSpec, *, force_plan: bool = False
) -> tuple[Any, Any, str | None, str | None]:
```

- `force_plan=True`：**跳过 L1 classify 与 L2 灰区裁决**，直进 planner 创建
  路径（延迟 import Planner → create_plan → fallback 处理 → max_replans）。
  供停滞/漂移升级复用（D4）；与 `PlanningPolicy.force_plan` 字段语义一致
  （都是"确定要规划"），不新增配置（红线 2）。
- `force_plan=False`（默认）：行为与 W11-2 完全一致，既有调用方零感知。

### 2.2 `_TurnState` 字段恢复（`runner.py`，A3 处追加）

```python
    # W11-3: mid-turn stall escalation state (restored from HEAD semantics).
    consecutive_nontool_iterations: int = 0
    escalated_this_turn: bool = False
```

### 2.3 新私有方法 `_maybe_escalate_mid_turn`（`runner.py`，置于
`_execute_tool_iteration` 之前，与 HEAD `_maybe_escalate_to_managed` 同区）

```python
async def _maybe_escalate_mid_turn(
    self,
    spec: AgentRunSpec,
    state: _TurnState,
    planner: Any,
    plan: Any,
    planner_task_text: str | None,
    planner_tools_summary: str | None,
) -> tuple[Any, Any, str | None, str | None]:
    """ReAct 停滞检测 → 中途补建计划。返回 (planner, plan, task_text, tools_summary)。

    条件不满足或升级失败时原样返回入参（plan 不变即无升级）。
    """
```

条件门（全部满足才尝试，任一不满足直接原样返回）：

1. `plan is None`（仅未规划 turn；升级过或本就有计划则跳过）
2. `not state.escalated_this_turn`（每 turn 至多一次，HEAD 语义）
3. `spec.planning_policy is not None`（HEAD 语义：显式无 policy 不升级）
4. `spec.planning_policy.force_plan is not False`（embedder 显式禁规划则尊重）
5. `state.consecutive_nontool_iterations >= 2`（停滞阈值，HEAD 语义）

条件通过后（HEAD 流程适配版）：

```python
try:
    planner, new_plan, planner_task_text, planner_tools_summary = (
        await self.init_planner(spec, force_plan=True)
    )
    if new_plan is not None:
        plan = new_plan
        state.plan_snapshot = await self.emit_plan_snapshot(
            spec, plan, state.turn_id, stop_reason=None, origin="escalated"
        )
        from erza.agent.progress_policy import ProgressPolicy, ProgressTracker

        state.progress_tracker = ProgressTracker(ProgressPolicy())
        state.escalated_this_turn = True
        state.consecutive_nontool_iterations = 0
        logger.info("Escalated stalled ReAct turn to plan (turn {})", state.turn_id)
except Exception:
    logger.warning("Mid-turn escalation failed; staying ReAct", exc_info=True)
return planner, plan, planner_task_text, planner_tools_summary
```

与 HEAD 的差异（有意为之，报告需注明）：
- 不再调用 `policy.escalate(PlanningMode.FAST, ...)`（枚举已删，条件门直接表达）。
- `origin="escalated"` 经 `emit_plan_snapshot` 形参传入（A6），不再
  `with_origin` 后处理。
- 注意：`init_planner(force_plan=True)` 在 provider 失败/JSON 无效时返回
  plan=None（fallback 路径），升级自然落空——**这是期望的降级行为**，
  不需要额外分支。

### 2.4 主循环接线（`runner.py`，A1 的 deadline 检查之后、govern_messages 之前）

```python
            # W11-3: stall check before governance (HEAD call-site parity).
            (
                planner,
                plan,
                planner_task_text,
                planner_tools_summary,
            ) = await self._maybe_escalate_mid_turn(
                spec, state, planner, plan, planner_task_text, planner_tools_summary
            )
```

变量作用域核对：`planner`/`plan`/`planner_task_text`/`planner_tools_summary`
为循环外 init（A2）定义的局部变量，循环内重新赋值合法（`_execute_tool_iteration`
已同模式返回新 plan）。

### 2.5 计数维护（HEAD T5 语义恢复）

- **重置**：`_execute_tool_iteration` 末尾 `return "continue", plan` 之前：
  `state.consecutive_nontool_iterations = 0`
- **递增**：`_finalize_nontool_iteration` 开头 `if response.has_tool_calls:`
  告警块之后：
  ```python
  if plan is None:
      state.consecutive_nontool_iterations += 1
  ```
  （`if plan is None` 限制：已规划/已升级 turn 的纯文本迭代不计停滞。）

## 3. 测试要求（新文件 `tests/agent/test_stall_escalation.py`）

以 HEAD 的 `test_fast_to_managed_escalation.py`（A8）为样板：FakeProvider
按迭代序返回 no-tool 纯文本响应驱动主循环。适配点：无 PlanningMode；
policy 用默认 `PlanningPolicy()` 或 `PlanningPolicy(force_plan=None)`。
逐条实现：

- S1 **升级触发**：无计划 turn，provider 依次返回 2 次纯文本响应（无工具）
  → 第 2 次迭代后升级：`plan is not None`（后续迭代有步骤指引）、
  `state.escalated_this_turn is True`、checkpoint 队列出现
  `phase=="plan_snapshot"` 且 `plan_snapshot["origin"]=="escalated"`、
  计数归零。
- S2 **已规划 turn 不升级**：`force_plan=True` 的 spec，planner 返回合法
  计划后 provider 返回 N 次纯文本 → `chat_with_retry` 无第二次 planner
  调用、无 origin=="escalated" 快照。
- S3 **每 turn 至多一次**：升级后继续无工具停滞 → 不再二次升级
  （planner 调用总数恰 2：初始 0 + 升级 1）。
- S4 **显式禁规划不升级**：`PlanningPolicy(force_plan=False)` + 2 次纯文本
  → 零 planner 调用（L1 也被 force 覆盖为 DIRECT）。
- S5 **planning_policy=None 不升级**：spec 不带 policy（runner 侧兜底逻辑
  存在于 init_planner，但升级条件门要求 spec.planning_policy 非 None——
  若构造该场景需绕过兜底，按实际兜底行为断言并在报告注明）。
- S6 **工具执行重置计数**：响应序 = 纯文本、工具调用、纯文本、纯文本 →
  第 4 次才达到连续 2 次；断言升级发生在第 4 次迭代后而非第 2 次。
- S7 **force_plan=True 入口绕过路由**：GRAY 任务文本（如"写个注释说明这段
  代码"）+ `init_planner(spec, force_plan=True)` 直接调用 → plan 非 None，
  router/planner 调用序中**无** router 裁决调用（mock provider 记录
  messages，断言首个 system 不是 planner_router 模板渲染结果——或更简单：
  断言 `chat_with_retry` 恰 1 次且其 user 消息含 `## Task`）。
- S8 **升级失败降级**：provider 对 force planner 调用返回非 JSON 文本 →
  plan 仍为 None、无 escalated 快照、循环继续不崩溃（fail-soft）。

S1/S6 需要驱动完整 `AgentRunner.run`（参考 A8 样板的 spec 构造与 hook 捕获
checkpoint 的方式）。

## 4. 禁改清单

- `erza/agent/planning_policy.py`、`erza/agent/planner.py`、
  `erza/agent/plan_snapshot.py`（origin 值域不加新值，D5；escalated 是既有值）。
- `erza/agent/execution/planning.py` 中 W11-2 的 classify/L2 逻辑（本批只加
  force_plan 形参，路由行为零变化）。
- `erza/agent/loop.py`、`loop_builder.py`、config、ledger。
- `erza/agent/execution/recovery.py`、`tool_execution.py`、
  `context_governance.py`、`model_request.py`。
- 既有测试全部不改（A8 是历史文件，不复活到工作区；只新建
  `test_stall_escalation.py`）。
- `take_pending_plan`/`_adopt_activated_plan`（activate_plan 语义，红线 1）。

## 5. 验收自检

1. 停滞升级条件门 5 项与 2.3 一致；每 turn 至多一次。
2. 升级快照 `origin=="escalated"` 经 `emit_plan_snapshot` 形参传入。
3. `init_planner(force_plan=False)` 路径与 W11-2 行为逐字节等价（S 系列外的
   既有测试全绿即证）。
4. 计数维护两处（重置/递增）与 HEAD T5 语义一致。
5. 验证门全过（pytest / ruff check / ruff format --check 本批文件）。
6. 与 HEAD 的三处有意差异（无 PlanningMode、origin 形参、fallback 落空）
   已在报告注明。
