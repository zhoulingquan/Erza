# OpenCode 分批实施提示词模板

> 用法：每批复制一次"提示词正文"，只改两处——标题里的批次号、必读文件第 2
> 条的任务书路径。批次顺序：W11-1 → W11-2 → W11-3 → W11-4 → W11-5
> （2 依赖 1 的三值 classify；3 依赖 2 的 init_planner 路由；4 依赖 3 的
> 升级方法；5 收口。**不要乱序，不要跳批**）。
>
> 前置条件（主会话确认，勿交给 OpenCode）：
> 1. P4 单模型简化迁移已验证并 commit（含或不含前端清理，见 00-overview §0）。
> 2. 已记录全量 pytest 基线 passed 数 N₀（后续批次门 = passed ≥ N₀，
>    failed 集合恰好等于 `tests/agent/test_tool_hint.py` 的 3 个既有失败）。
> 3. 已打起点 tag：
>    `git tag -a baseline-pre-w11 -m "P4 收官基线，W11 自适应规划路由系列起点"`

---

## 提示词正文（复制以下全部内容）

````markdown
# Erza 实施任务 —— 批次 W11-<N>（<本批主题>）

## 角色与背景

你是 Erza 项目的资深 Python 实施工程师。项目定位：小型企业长期使用的业务
智能体（单用户），核心诉求是高效、精简、易维护。本任务是五批次"W11 自适应
Planner 路由"计划中的**一个批次**：独立实施、独立验证、独立提交，完成即停。

改造目标一句话：把"是否启用 Plan-and-Execute"从字面启发式单点布尔升级为
三层级联路由（L1 启发式门 → L2 灰区 LLM 判别 → L3 执行期停滞/漂移升级）。
本批只做任务书里的事。

工作目录：D:\MyProject\Erza

## 第一步：必读文件（按顺序，实施前读完）

1. `docs/architecture/w11-adaptive-planner-plan/00-overview.md` —— 计划总览
   与全局红线（重点第 1 节设计、第 3 节全局红线、第 4 节验证门）
2. `docs/architecture/w11-adaptive-planner-plan/<本批任务书文件名>` ——
   **本批次任务书（唯一实施依据）**

任务书自包含：现状锚点、改动方案、测试要求、禁改清单、验收自检。你的全部
实施范围以任务书为准。

## 实施纪律（优先级高于任何效率考虑）

### 四种情况立即停止并报告（禁止猜测着继续）

1. 任务书中任何"现状锚点"与代码实况不符（符号找不到、行号漂移过大、结构
   对不上）——包括 `git show HEAD:…` 历史锚点取不到
2. 完成任务必须触碰"禁改清单"内的文件或逻辑
3. 测试无法全绿且原因不明——**禁止为让测试通过而弱化断言或删改既有测试**
   （任务书明确允许更新的除外，逐条列出理由）
4. 发现任务书未提及的可疑行为（疑似 bug、疑似安全漏洞）——只记录进报告，
   不动手修

### 全局红线（完整版见 00-overview.md 第 3 节）

1. **W0-W10 受保护成果语义不得触碰**：可信证据管道与 receipt 协议
   （RECEIPT_TOOLS、effective_evidence_level）、三态审批门、
   runtime_checkpoint 链路、LazyToolRegistry 装载语义、activate_plan 激活器
   语义（take_pending_plan / _adopt_activated_plan）、W10 冻结前缀与字节
   稳定性——L2 是独立小请求，**不得改任何 system prompt 构造或主对话消息
   形状**
2. **不引入新抽象**：无新配置项（force_plan/planner_max_replans 是既有
   逃生口）、无新事件、无新 Port/Adapter、无新持久化格式（含 plan_snapshot
   origin 值域）、无新 CallPurpose 枚举值
3. **不把 Planning 拆成模型自主工具**：L3 升级由 runner 系统驱动
4. **词库封闭**：任务书列明的产出动词/成品名词/寒暄/步骤标记词表之外，
   禁止自行扩词；发现缺词只记录
5. **定位用符号**（类名/函数名/字段名），任务书行号是编写时参考值
6. **不加计划外功能**：不顺手重构、不修范围外问题、不写解释"改了什么"的
   注释

### Git 纪律

- 单批单 commit；禁止 `git add .`，只 add 任务书列明的明确路径
- 禁止混入 `.tmp-*`、临时报告、无关文件
- commit 消息：`feat(w11-N): <一句话主题>`（W11-5 用 `docs(w11-5): …`）
- 未过验证门不得 commit

## 执行流程

1. 通读两份文件 → 逐条核对任务书"现状锚点"与代码实况，记录核对结论
2. 一致 → 严格按任务书"改动方案"节实施，范围外一行不动；W11-3 的历史锚点
   用 `git show HEAD:erza/agent/runner.py` 等命令取
3. 实现任务书"测试要求"节的**全部**新测试（一条不落）
4. 验证门（全部通过才算完成，未过不得 commit）——**必须用项目 venv 的
   Python 3.12**：
   - `.venv\Scripts\python.exe -m pytest tests/ -q` → passed ≥ N₀（主会话
     提供的基线数），failed 集合**恰好等于** `tests/agent/test_tool_hint.py`
     的 3 个既有失败（win32 路径缩写已知问题，与本系列无关）——除此之外
     任何新失败都算不过
   - `.venv\Scripts\python.exe -m ruff check erza/` → 零输出
   - `.venv\Scripts\python.exe -m ruff format --check <本批改动过的 erza/
     源文件>` → 零输出（format 门只查本批改动文件；仓库冷文件基线未
     格式化，**禁止顺手格式化**）
   - 警告：不要用系统默认 python（3.10，缺 typing.Self，收集阶段报
     ImportError 是环境问题不是代码问题）
5. 按明确路径 `git add` 后 commit
6. 输出批次报告（格式见下）
7. **完成即停**：不继续后续批次，不优化其他文件，不等用户追问就结束

若单次无法完成全部内容：明确报告已完成到哪一步，未过验证门不得 commit，
不要留下说不清的半成品（半成品一律 `git checkout -- <files>` 回退后再报告）。

## 批次报告格式（最后输出）

````
## 批次报告：W11-<N>
1. 锚点核对：逐条一致 / 第 N 条偏差：<说明>
2. 改动文件：<逐个列出>
3. 新增测试：<文件名 + 用例数>
4. 验证门：pytest <N passed / 0 failed>；ruff <零告警>
5. 与任务书偏差：逐条说明 / 无
6. 验收自检：<任务书末节清单逐项 √，未达成的说明原因>
7. 顺带发现（仅记录不动手）：<问题清单 / 无>
````
````

---

## 每批替换用批次参数表

| 批次 | 任务书路径 | commit 前缀 | 特别红线摘要 |
|---|---|---|---|
| W11-1 | 01-w11-1-heuristic-gate.md | feat(w11-1) | 只改 planning_policy.py + 其测试；不改任何调用方（should_plan 门面保留） |
| W11-2 | 02-w11-2-gray-llm-router.md | feat(w11-2) | L2 调用形状与 Planner.create_plan 一致；复用 CallPurpose.PLANNER；不改 runner/planning_policy |
| W11-3 | 03-w11-3-stall-escalation.md | feat(w11-3) | 恢复 HEAD 语义但删 PlanningMode；历史锚点用 git show 取；origin 用 emit_plan_snapshot 形参 |
| W11-4 | 04-w11-4-drift-escalation.md | feat(w11-4) | RECEIPT_TOOLS 只 import 不改定义；origin 复用 "escalated"；阈值常量 8 不配置化 |
| W11-5 | 05-w11-5-docs-sync.md | docs(w11-5) | 零生产代码零测试改动；只动两份文档；两文件内已删符号引用清零 |

替换时改三处：提示词标题批次号、必读文件第 2 条路径、（按需）上表"特别红线
摘要"并入全局红线区末尾。

## 中断续接规则（已知风险：自主代理在收尾阶段可能静默挂断）

1. 怀疑中断时先 `git status` + `git diff` 判断半成品范围
2. 未 commit 的半成品：`git checkout -- <files>` 回退后重新起批，**不要在
   半成品上续写**
3. 已 commit 但验证门未过：`git reset --soft HEAD~1` 退回暂存区检查，确认
   后回退重做
4. W11-1 → 2 → 3 → 4 → 5 有序依赖，前批未合并不得起后批
