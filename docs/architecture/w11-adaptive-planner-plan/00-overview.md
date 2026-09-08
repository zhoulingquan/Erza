# W11 自适应 Planner 路由 — 计划总览

> 系列目标：把"是否启用 Plan-and-Execute"的判定从**字面启发式单点布尔**升级为
> **三层级联路由**（L1 启发式门 → L2 灰区 LLM 判别 → L3 执行期兜底），
> 让每个 turn 的规划决策既便宜又贴近任务真实量级。
>
> 背景调研结论（2026-09）：GitHub 高星 agent 项目无一做"自动判别是否规划"
> （Cline/Claude Code 用户手动切换、Codex 模型自主工具、LangGraph/Magentic-One
> 常开编排）。可借鉴的结构：LiteLLM Auto Router v2 的"启发式先行 + LLM 只判
> 灰区 + 决策日志"级联、Laxaar 的"3+ 步清单测试"、Magentic-One 的停滞检测
> replan。本系列即这三者的 Erza 化最小实现。

## 0. 前置条件（W11-0，不属于本系列批次，但必须先完成）

当前工作区存在**未提交的 P4 单模型简化迁移**（40 文件，+356/−1631）。W11 全部
锚点基于 P4 后状态。P4 收口步骤：

1. 全量验证门：`.venv\Scripts\python.exe -m pytest tests/ -q` →
   failed 集合**恰好等于** `tests/agent/test_tool_hint.py` 的 3 个既有失败
   （win32 路径缩写已知问题）；记录 passed 基线数 N₀（后续批次门 = passed ≥ N₀）。
2. `.venv\Scripts\python.exe -m ruff check erza/` → 零输出；
   `ruff format --check <P4 改动文件>` → 零输出。
3. 前端 `use_planner`/`planner_model` 残留清理（后端 `model_settings_api.py`
   已删读写，前端仍引用）：
   - `webui/src/lib/api.ts`（627-632 行：删除两个 query 参数分支）
   - `webui/src/lib/types.ts`（241 行 `use_planner`、529-530 行 `usePlanner`/`plannerModel`）
   - `webui/src/components/settings/components/PlannerConfig.tsx`（整个组件）
   - `webui/src/components/settings/sections/OverviewSettings.tsx`（60/66/109-116 行）
   - `webui/src/components/settings/hooks/useRuntimeSection.ts`（25/103 行）
   - `webui/src/tests/*.test.tsx` 中 `use_planner: false` fixture 与相关断言
   - **范围待用户确认**：若用户决定前端清理留给后续批次，则 P4 只提交后端，
     本项从 W11-0 移出，W11 照常开工（前后端不一致不阻塞本系列——后端已忽略
     未知参数，前端开关形同虚设但不报错）。
4. 提交：P4 后端一个 commit；前端清理（若做）一个 commit。
   建议消息：`refactor(p4): single-model planning — drop usePlanner/PlanningMode, route per turn via PlanningPolicy.should_plan`

## 1. 设计总览

### 1.1 三层级联

```
用户任务（turn 开始）
   │
   ▼
L1 启发式门（planning_policy.classify，纯函数，零 LLM）
   ├─ 强多步信号 / 宏目标(动词×成品名词) ──────────► PLAN
   ├─ 寒暄致谢 / 单问句 ──────────────────────────► DIRECT（纯 ReAct）
   └─ 其余（含"产出动词但非宏目标"）──────────────► GRAY
                                                    │
                                                    ▼
                              L2 灰区 LLM 判别（同模型 1 次小调用，
                              清单测试提示词，失败 fail-open DIRECT）
                                        ├─ PLAN ──► Planner.create_plan
                                        └─ DIRECT ► 纯 ReAct
   │
   ▼（DIRECT 路径执行中）
L3 执行期兜底（runner 主循环内，零 LLM 检测）
   ├─ 停滞：连续 ≥2 次迭代无工具响应 ──► 中途补规划（origin=escalated）
   └─ 漂移：无计划 turn 收到 ≥8 次写入类工具调用 ──► 中途补规划
```

### 1.2 判定代价不对称原则（贯穿全系列的调参依据）

- 误判 PLAN：多付 1 次规划调用（有界损失，约几百 token + 1-2s）
- 误判 DIRECT：宏大任务走 ReAct → 长期漂移（无界损失）

因此 **DIRECT 桶必须极度保守**：L1 只对高精度模式（寒暄、单问句）放行
DIRECT；"短 ≠ 小"，一切拿不准的都进 GRAY 交给 L2；L2 之后还有 L3 兜底。

### 1.3 设计决策记录（已定，批次内不再重议）

| # | 决策 | 理由 |
|---|------|------|
| D1 | L2 复用 `CallPurpose.PLANNER` 记账，不新增枚举值 | L2 本质是规划决策调用，并入 planner 开销口径；避免 ledger/budget 连锁改动 |
| D2 | L2 不带 temperature/max_tokens 覆盖 | 与 `Planner.create_plan` 调用形状一致；provider 默认即可，解析端容错 |
| D3 | L2 会话上下文 = 首条 user 消息 + user 消息数，全部从 `spec.initial_messages` 派生 | 零新增运行时状态、零持久化；解决"把前面那三件事都做了"式引用漏判 |
| D4 | L3 停滞升级恢复 HEAD 语义（连续 2 次无工具迭代、每 turn 至多 1 次、计数每 turn 重置），但删除 PlanningMode 依赖，改由 `init_planner(force_plan=True)` 强制规划 | 语义已被 W2-2 验证过；P4 删除它是因为旧架构耦合，不是语义本身有问题 |
| D5 | 漂移升级复用 `origin="escalated"`，不新增 snapshot origin 值 | 避免触碰 plan_snapshot 消费方（webui transcript 等）；cause 在日志中区分 |
| D6 | 决策日志 = routing 时一条 loguru 结构化行（route/cause/signals） | 零新持久化格式；turn_end 元数据字段如后续需要另立批次 |
| D7 | 词库（产出动词、成品名词、寒暄、步骤标记）为**封闭常量**，批次内不扩展 | 词库打不完地鼠；扩展需决策日志数据支撑，另行提案 |
| D8 | `should_plan()` 保留为兼容门面（= `classify().route is PLAN`），W11-1 不改任何调用方 | 保证批次 1 是零风险纯函数改造，调用方切换在批次 2 |
| D9 | L1 决策表含"单一祈使"DIRECT 行（短 + 无产出动词 + 无结构信号 → DIRECT，见 02 任务书勘误 E1.1） | 恢复已批准流程图 STEP 3 的"单步查看/执行"桶（01 任务书转写遗漏）；否则大量短祈使任务（含既有测试占位 fixture）误入灰区白付 L2 调用 |

## 2. 批次划分

| 批次 | 任务书 | 主题 | commit 前缀 | 依赖 |
|------|--------|------|-------------|------|
| W11-1 | 01-w11-1-heuristic-gate.md | L1 启发式门重写（三值 classify + 宏目标闸门 + 规则缺陷修复） | feat(w11-1) | W11-0 |
| W11-2 | 02-w11-2-gray-llm-router.md | L2 灰区 LLM 判别 + init_planner 切换 classify + 决策日志 | feat(w11-2) | W11-1 |
| W11-3 | 03-w11-3-stall-escalation.md | L3a 停滞升级恢复（HEAD 语义适配新路由） | feat(w11-3) | W11-2 |
| W11-4 | 04-w11-4-drift-escalation.md | L3b 漂移升级（写入类工具计数触发） | feat(w11-4) | W11-3 |
| W11-5 | 05-w11-5-docs-sync.md | 文档同步（module-boundaries §2.16、configuration.md 规划章节） | docs(w11-5) | W11-4 |

顺序强制：1 → 2 → 3 → 4 → 5，不跳批不乱序（2 依赖 1 的三值输出；
3 的强制规划入口在 2 中引入；4 复用 3 的升级方法；5 收口）。

## 3. 全局红线（每批次提示词摘要引用，完整版以本节为准）

1. **W0-W10 受保护成果语义不得触碰**：可信证据管道与 receipt 协议
   （`RECEIPT_TOOLS`、`effective_evidence_level`）、三态审批门、
   runtime_checkpoint 链路、LazyToolRegistry 装载语义、activate_plan
   激活器语义（`take_pending_plan`/`_adopt_activated_plan`）、W10 冻结前缀
   与字节稳定性（L2 是**独立小请求**，不得改任何 system prompt 构造或
   主对话消息形状）。
2. **不引入新抽象**：无新配置项（`force_plan`/`planner_max_replans` 为既有
   逃生口）、无新事件、无新 Port/Adapter、无新持久化格式（含 snapshot origin
   值——见 D5）、无新 CallPurpose 枚举值（见 D1）。
3. **不把 Planning 拆成模型自主工具**（trusted-evidence-plan 既有否决）：
   L3 升级由 runner 系统驱动，不是模型可调用的工具。
4. **定位用符号**（类名/函数名/字段名），任务书行号为编写时参考值。
5. **不加计划外功能**：不顺手重构、不修范围外问题（发现的可疑行为记录进
   批次报告，不动手）、不写解释"改了什么"的注释。
6. **词库封闭**（D7）：实施中发现词库缺词只记录，不自行扩词。
7. **测试纪律**：禁止为让测试通过而弱化断言或删改既有测试；任务书明确
   允许更新的除外（逐条列出）。

## 4. 验证门（每批次 commit 前全过，缺一不可）

- `.venv\Scripts\python.exe -m pytest tests/ -q` →
  failed 集合**恰好等于** `tests/agent/test_tool_hint.py` 3 个既有失败；
  passed ≥ N₀（W11-0 记录的 P4 收口基线；本系列每批净增测试数见各任务书）。
- `.venv\Scripts\python.exe -m ruff check erza/` → 零输出。
- `.venv\Scripts\python.exe -m ruff format --check <本批改动的 erza/ 源文件>` →
  零输出（format 门只查本批改动文件；仓库冷文件基线未格式化，禁止顺手格式化）。
- 警告：必须用项目 venv 的 Python 3.12；系统默认 python 3.10 缺
  `typing.Self`，收集阶段 ImportError 是环境问题不是代码问题。
- 前端批次（仅 W11-0 若含前端清理）：`cd webui && npx vitest run` 通过。

## 5. Git 纪律

- 单批单 commit；禁止 `git add .`，只 add 任务书列明的明确路径。
- commit 消息：`feat(w11-N): <一句话主题>`（W11-5 用 `docs(w11-5)`）。
- 禁止混入 `.tmp-*`、临时报告、无关文件。
- 未过验证门不得 commit；半成品一律 `git checkout -- <files>` 回退后报告。

## 6. 中断续接规则

1. 怀疑中断：先 `git status` + `git diff` 判断半成品范围。
2. 未 commit 的半成品：回退后重新起批，不在半成品上续写。
3. 已 commit 但验证门未过：`git reset --soft HEAD~1` 退回暂存区检查，确认后
   回退重做。
4. 批次有序依赖，前批未合并不得起后批。

## 7. 本系列明确不做（记录，防 scope creep）

- 训练式判别器 / 嵌入路由（RouteLLM、ACRouter 路线）——需要训练数据与基建，
  单用户场景过重。
- Devin 式"每个任务先出计划给用户审批"——交互成本高，L2 已覆盖量级判别。
- 计划审批 UI / WebUI 路由可视化——决策日志先行（D6），有数据后再议。
- L3 停滞/漂移阈值的自适应学习——常量起步，凭决策日志调参。
- 把 `update_plan` 式计划维护开放为模型工具（红线 3）。
