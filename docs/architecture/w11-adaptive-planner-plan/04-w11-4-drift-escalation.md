# 任务书 W11-4：L3b 漂移升级

> 系列总览与红线见 `00-overview.md`。依赖 W11-3 已合并（`_maybe_escalate_mid_turn`
> 与状态字段已就位）。本批补第二执行期兜底：**无计划 turn 收到 ≥8 次写入类
> 工具调用 → 判定漂移，中途补建计划**。动机：宏大目标被误判 DIRECT 后模型
> 不会停滞（每轮都动手），停滞检测器看不见漂移——写入量是最便宜的机械代理
> 信号（零 LLM 成本，Magentic-One Progress Ledger 的机械简化版）。

## 1. 现状锚点（逐条核对，任何一条不符立即停止报告）

| # | 锚点 | 位置（参考值） |
|---|------|----------------|
| A1 | `_maybe_escalate_mid_turn`（W11-3 产物）：5 项条件门（plan is None / 未升级 / policy 非 None / force_plan 非 False / 连续无工具 ≥2）+ HEAD 流程升级体 | `erza/agent/runner.py` |
| A2 | `_execute_tool_iteration`：`state.tools_used.extend(tc.name for tc in response.tool_calls)`（~608 行）附近为工具批次结果统计区 | 同文件 |
| A3 | `RECEIPT_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})` | `erza/agent/planner.py` L69 |
| A4 | `_TurnState` 已含 `consecutive_nontool_iterations`/`escalated_this_turn`（W11-3）；`tools_used`/`tool_events` 既有 | `erza/agent/runner.py` L163- |
| A5 | 升级快照 `origin="escalated"`；D5：漂移**复用**该值，不新增 origin | `execution/planning.py` L88 |
| A6 | 测试样板：`tests/agent/test_stall_escalation.py`（W11-3）驱动完整 run 循环；工具响应构造参考 `tests/agent/test_loop_progress.py` / `test_planner_results.py` 的 tool_calls 样板 | 两个测试文件 |

## 2. 改动方案

涉及 1 个生产文件 + 1 个新测试文件：`erza/agent/runner.py`、
`tests/agent/test_drift_escalation.py`。

### 2.1 模块常量（`runner.py` 顶部常量区）

```python
_DRIFT_RECEIPT_TOOL_THRESHOLD = 8
```

> 阈值依据：单文件小改动通常 1-3 次写入；8 次写入仍无计划 ≈ 宏大任务跑偏。
> 常量封闭，不做配置（红线 2）；后续凭决策日志数据调整（overview §7）。

### 2.2 `_TurnState` 增加计数（A4 处追加）

```python
    # W11-4: receipt-tool call count for drift detection (unplanned turns).
    receipt_tool_calls: int = 0
```

### 2.3 计数递增（`_execute_tool_iteration`，A2 统计区）

在 `state.tools_used.extend(...)` 同一工具批次处理段内：

```python
from erza.agent.planner import RECEIPT_TOOLS  # 延迟 import，与 Planner 惯例一致

if plan is None:
    state.receipt_tool_calls += sum(
        1 for tc in response.tool_calls if tc.name in RECEIPT_TOOLS
    )
```

- `if plan is None` 限定：已规划 turn 的写入本来就受计划步骤约束，不计数
  （也避免升级后计数继续涨造成歧义；升级门另由 `escalated_this_turn` 双保险）。
- 注意 `plan` 在 `_execute_tool_iteration` 内是形参（本方法可能返回被
  activate_plan 替换的新 plan）；递增判断用**进入方法时**的形参值即可
  （放置位置在方法前段、`_adopt_activated_plan` 之前）。

### 2.4 `_maybe_escalate_mid_turn` 增加漂移触发（A1 方法改造）

条件门从单一停滞触发改为**双触发 OR**：

```python
        policy = spec.planning_policy
        policy_ok = policy is not None and policy.force_plan is not False
        stalled = state.consecutive_nontool_iterations >= 2
        drifting = state.receipt_tool_calls >= _DRIFT_RECEIPT_TOOL_THRESHOLD
        if plan is not None or state.escalated_this_turn or not policy_ok:
            return planner, plan, planner_task_text, planner_tools_summary
        if not (stalled or drifting):
            return planner, plan, planner_task_text, planner_tools_summary
        trigger = "drift" if (drifting and not stalled) else "stall"
```

升级体与 W11-3 相同，仅两处差异：
- 成功后同时归零两个计数器：
  `state.consecutive_nontool_iterations = 0`、`state.receipt_tool_calls = 0`
- 日志带 trigger：
  `logger.info("Escalated ReAct turn to plan mid-turn (turn {}, trigger={})", state.turn_id, trigger)`
- 快照 origin 仍为 `"escalated"`（D5），漂移与停滞的区分只在日志 trigger。

> 同时停滞又漂移（理论不可能——无工具迭代不计写入）时 trigger 取 "stall"，
> 分支写法保证确定性行为即可。

## 3. 测试要求（新文件 `tests/agent/test_drift_escalation.py`，一条不落）

样板同 S 系列（A6）：FakeProvider 按迭代序返回带 tool_calls 的响应驱动完整
run；tool_call 构造复用 `test_loop_progress.py` 惯例。逐条实现：

- D1 **漂移触发**：无计划 turn（GRAY 文本 + router 返回 DIRECT 或直接
  force_plan=False 路由外的自然 DIRECT——注意 force_plan=False 会同时关死
  升级门，**必须用 router 返回 DIRECT 的 GRAY 任务**构造无计划 turn），
  provider 连续返回 write_file 工具调用 → 第 8 次写入后升级：plan 非 None、
  快照 `origin=="escalated"`、`escalated_this_turn is True`、两个计数归零。
- D2 **阈值之下不触发**：整 turn 恰 7 次 write_file 后以纯文本收尾 →
  零升级、零 planner 调用。
- D3 **非写入工具不计数**：若干 read_file/exec 调用 + 7 次 write_file →
  不升级。
- D4 **跨迭代累计**：每迭代 2 次 write_file，4 迭代共 8 次 → 第 4 迭代后
  升级（证明计数不按迭代重置）。
- D5 **已规划 turn 不触发**：`force_plan=True` + 合法计划 + 8+ 次 write_file
  → 无 origin=="escalated" 快照（计划内写入不升级）。
- D6 **升级后不二度升级**：漂移升级后继续 write_file → planner 调用总数
  恰 2（router/planner 各 1 + 升级 1——按 D1 构造方式核算）。
- D7 **停滞与漂移互不干扰**：W11-3 的 S1/S6 回归仍绿（停滞路径行为不变，
  trigger="stall"）。
- D8 **路由零回归**：W11-2 的 R6-R12 全绿（路由层未被本批触碰）。

## 4. 禁改清单

- `erza/agent/planner.py` 的 `RECEIPT_TOOLS`（只 import，不改定义——它是
  证据协议的一部分，红线 1）。
- `erza/agent/planning_policy.py`、`execution/planning.py`（本批零改动；
  若发现需改即锚点不符，停止报告）。
- `plan_snapshot.py`（origin 值域不变）。
- `erza/agent/loop.py`、`loop_builder.py`、config、ledger。
- W11-3 已有测试文件不改（D7 是回归验证，不是修改对象）。

## 5. 验收自检

1. 漂移触发条件与 2.4 一致；与停滞触发共享升级体，`trigger` 日志可区分。
2. 计数仅统计 `plan is None` 时的 RECEIPT_TOOLS 调用，跨迭代累计。
3. 阈值为模块常量 8，未配置化、未硬编码散落。
4. D1-D8 全过；S 系列、R 系列回归全绿。
5. 验证门全过（pytest / ruff check / ruff format --check 本批文件）。
