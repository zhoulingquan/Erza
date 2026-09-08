# 任务书 W11-5：文档同步（纯文档批次）

> 系列总览与红线见 `00-overview.md`。依赖 W11-4 已合并。本批**零生产代码、
> 零测试改动**，只把两份权威文档的规划章节从 P0/P4 陈旧描述同步到 W11 后
> 实况。若发现必须改生产代码才能自洽，立即停止报告（红线）。

## 1. 现状锚点（逐条核对）

| # | 锚点 | 位置（参考值） |
|---|------|----------------|
| A1 | `module-boundaries.md` §2.16 描述的是 **P0 时代 API**（`ExecutionMode`/`select_mode`/`should_upgrade`/`build_upgrade_context`、连续 2 个 turn 升级——连 HEAD 的 P4 状态都不是）；§2.15 SafetyPolicy 边界说明提及 FAST/MANAGED 与 `usePlanner` | `docs/architecture/module-boundaries.md` L253-264 |
| A2 | 同文件 L548-549：`AgentLoopConfig` 说明称 `usePlanner=false` 保持 FAST ReAct 默认路径 | 同文件 L548-549 |
| A3 | `configuration.md` 规划章节仍写 `usePlanner`/`plannerModel`（P0 语义），含 JSON 示例、配置表、turn limits 说明 | `docs/configuration.md` L1254-1312 |
| A4 | 实况（文档要写成的样子）：`PlanningPolicy`（`planner_max_replans`、`force_plan`）+ `Route`/`RouteDecision`/`classify`/`should_plan` 门面（`erza/agent/planning_policy.py`）；L2 `_classify_gray` + `extract_session_context` + `parse_router_verdict`（`erza/agent/execution/planning.py`）；模板 `erza/templates/agent/planner_router.md`；停滞+漂移双触发 `_maybe_escalate_mid_turn`（`erza/agent/runner.py`，阈值常量 `_DRIFT_RECEIPT_TOOL_THRESHOLD=8`，停滞阈值 2）；`init_planner(spec, *, force_plan=False)`；快照 `origin ∈ {"planner","escalated"}` | 各生产文件（W11-1..4 产物） |
| A5 | `erza/config/loader.py` `DEPRECATED_KEYS` 将 `usePlanner`/`plannerModel` 等旧键映射升级（合法兼容层，保留） | `erza/config/loader.py` |
| A6 | config 现存规划相关字段仅 `agents.defaults.plannerMaxReplans`（schema `planner_max_replans`，默认 3） | `erza/config/schema.py` L258 |

## 2. 改动方案

### 2.1 `module-boundaries.md`

- **§2.16 整节重写**（保留节编号与标题层级，标题改为
  `### 2.16 agent/planning(PlanningPolicy 三层路由)`），内容必须覆盖：
  1. 职责：决定 turn 是否启用 Plan-and-Execute；三层级联（L1 纯函数启发式
     三值路由 → L2 灰区同模型小调用清单测试 → L3 执行期停滞/漂移双触发
     升级）。
  2. 公开 API：`PlanningPolicy`(class)、`Route`(enum: PLAN/DIRECT/GRAY)、
     `RouteDecision`(dataclass: route/cause/signals)、
     `classify(task_text) -> RouteDecision`、`should_plan()`（兼容门面）、
     `init_planner(spec, *, force_plan=False)`。
  3. 依赖方向：`planning_policy.py` 为纯函数模块（不 import provider/
     config 之外的运行时）；L2/升级在 `agent/execution/planning.py` 与
     `agent/runner.py`（需要 provider 与 turn state 的位置）；不 import
     `agent/loop`。
  4. 关键决策表（照抄 00-overview §1.3 D1-D8 精简版）+ 判定代价不对称原则。
  5. 边界说明更新：SafetyPolicy 仍独立于规划（删除 FAST/MANAGED 措辞，
     改为"不参与 Plan/DIRECT 路由决策"）。
- **§2.15 L253 边界说明行**：删除 `usePlanner`/FAST/MANAGED 表述，改为
  "不参与 Plan/DIRECT 路由与升级决策"。
- **L548-549 AgentLoopConfig 说明**：删除 `usePlanner` 句，改为
  "`planning_policy` 按层装配：L1 常驻、L2/升级在 runner 侧"。

### 2.2 `configuration.md` 规划章节重写（L1254-1312 替换）

新章节必须覆盖：

1. **行为总述**：每个 turn 由三层路由自动决定是否规划（用户无需配置）；
   L1 启发式零成本、L2 只在灰区付一次小调用（同执行模型）、L3 执行期
   停滞（连续 2 次无工具迭代）或漂移（无计划下 ≥8 次写入类工具调用）时
   中途升级。
2. **配置项表**（删除 usePlanner/plannerModel 两行，只保留）：
   `agents.defaults.plannerMaxReplans`，默认 3，语义 = 每 turn replan
   provider 尝试上限。
3. **旧配置迁移**：`usePlanner`/`plannerModel` 已废弃——升级 config 时由
   `DEPRECATED_KEYS` 自动映射删除（A5），行为差异说明：旧 `usePlanner=true`
   全局规划 → 新版逐 turn 自动路由；显式禁规划请勿用配置（无此项），
   嵌入方用 `PlanningPolicy(force_plan=False)`。
4. **JSON 示例**：只含 `plannerMaxReplans`。
5. **turn limits 说明**更新：planner 预算口径现包含 L2 路由调用与升级
   planner 调用（同 `CallPurpose.PLANNER`，D1）。
6. 与 `docs/architecture/w11-adaptive-planner-plan/00-overview.md` 互链
   （深入设计去总览，配置文档只讲用户视角）。

### 2.3 明确不做

- 不改其他章节（哪怕发现别处也提 usePlanner——记录进报告，超出本批范围）。
  例外：`module-boundaries.md` 与 `configuration.md` 两文件内对已删除符号
  （`usePlanner`/`plannerModel`/`PlanningMode`/`FAST`/`MANAGED`/
  `should_upgrade`）的**全部残留引用**必须清零——实施前
  `rg -n "usePlanner|plannerModel|PlanningMode|should_upgrade|ExecutionMode" docs/architecture/module-boundaries.md docs/configuration.md`
  列出清单，逐条处理；处理不了的（涉及其他章节语义）记录报告。
- 不动 W11 系列任务书与其他 architecture 文档（历史计划文档保留原貌）。

## 3. 验收自检

1. 两文件内已删除符号引用清零（rg 复查零命中，任务书自身与历史计划文档
   除外）。
2. §2.16 新内容覆盖 2.1 的 5 项；configuration.md 覆盖 2.2 的 6 项。
3. 零生产代码、零测试改动（`git status` 只见两份文档）。
4. 验证门：pytest / ruff 不受影响（仍跑一遍确认无意外）。
5. 发现的其他章节陈旧引用已逐条列于报告。
