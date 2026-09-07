# W10 缓存优先系列总览：提示词前缀稳定性改造

> 状态：方案定稿，待分批实施（Cline）。
> 基线：`pytest tests/ -q` → **4005 passed / 3 failed / 29 skipped**（3 个失败均为
> `tests/agent/test_tool_hint.py` 的既有问题，与本系列无关，见第五节验证门）；
> ruff 零告警。
> 建议实施前打 tag：`git tag -a baseline-pre-w10 -m "W9 收官基线，W10 缓存优先系列起点"`。

## 一、背景：为什么做这个

研究过 GitHub 上的 reasonix 项目（DeepSeek 原生终端 Agent 框架，"Cache-First Loop" 设计）后确认：
**上下文缓存（prefix cache）的命中与否，由客户端的提示词工程形状决定**。
DeepSeek 前缀缓存从字节 0 起精确匹配；OpenAI/Anthropic 也是自动前缀缓存（Anthropic 只是多一个
断点标记）。命中部分按约 10% 计费。reasonix 实测 τ-bench 46.6% → 94.4% 命中率、真实用户单日
99.82%；其核心就三条不变量：

1. **ImmutablePrefix**：system + 工具定义在会话内字节冻结；
2. **AppendOnlyLog**：消息只追加，不重排不改写；
3. **折叠在轮次边界**：压缩摘要作为 log 首条消息替换旧轮次，prefix 照常命中。

Erza 对照核查后，存在四处破坏前缀缓存的实况（均已在代码中逐一核实）：

| # | 问题 | 位置 | 后果 |
|---|---|---|---|
| 1 | 系统提示词每轮重建且含每轮必变内容（结构化召回结果、notes.md、未消费 history 条目、归档摘要） | `erza/agent/context.py` `build_system_prompt` | 系统提示词位于字节 0，它一动整请求全量 miss——这是最大漏点 |
| 2 | 归档摘要注入 system 而非 log；归档发生时 system 变 + 历史截断双重 miss | `context.py` SUMMARY 优先级段 + `memory/consolidator.py` | 每次归档 = 一次全量缓存重置 |
| 3 | 治理管道改写历史：`apply_tool_result_budget` 的 RED 减半截断是压力相关的非确定性截断；`microcompact` 的"保留最近 10 个可压缩结果"窗口随历史增长逐轮漂移 | `erza/agent/runner_strategies.py` | 每轮在历史中段断一次前缀 |
| 4 | 缓存命中率无度量：`TurnBudget.accumulate` 读 `prompt_cache_hit_tokens`，但 provider 归一化后的键是 `cached_tokens`——**该字段今天恒为 0**（潜伏 bug） | `erza/ledger/turn_budget.py` | 不可观测即不可优化 |

Erza 已做对的（保持不动）：runtime context（时间戳等）追加在当前用户消息**末尾**
（`tests/agent/test_context_prompt_cache.py::test_runtime_context_appended_after_user_content`
已锁定该语义）；GREEN 压力下治理跳过改写；`normalize_tool_result` 创建期确定性截断。

## 二、设计原则（源自 reasonix，适配 Erza 多 provider 抽象）

1. **冻结区/动态区分离**：系统提示词只放会话内字节不变的内容；一切每轮变化的内容
   （召回结果、notes、runtime context）后移到**当前用户消息尾部**——尾部是"增量 miss"，
   不伤前缀。
2. **跨会话记忆快照进 log**：以会话首条持久化消息形式存在（一次写入、永久回放同字节），
   而非每轮重读重注入 system。
3. **归档摘要进 log 不进 system**：折叠摘要作为持久化消息插入 cursor 位置，
   system+tools 前缀照常命中，只冷 log 中段一次。
4. **改写必须持久化且确定性**：对历史的任何压缩只在轮次边界做一次并写回 session，
   之后回放同字节；禁止压力相关的非确定性截断。
5. **命中率一等公民**：turn/会话两级统计 + 前缀稳定性回归测试锁死成果。

## 三、批次索引

| 批次 | 文件 | 内容 | 规模 | 依赖 |
|---|---|---|---|---|
| W10-C1 | 01-c1-cache-hit-metrics.md | 修复 cached_tokens 记账键 + TurnBudget 命中率统计与展示 | 小 | baseline-pre-w10 |
| W10-C2 | 02-c2-frozen-system-prefix.md | 系统提示词分区冻结：召回/notes→用户消息尾部，跨会话记忆→会话首条消息 | **大** | C1（用于验证效果） |
| W10-C3 | 03-c3-summary-into-log.md | 归档摘要从 system 注入改为 log 持久化消息 | 中 | C2 |
| W10-C4 | 04-c4-deterministic-compaction.md | 预算截断确定性化 + microcompact 移至轮次边界并持久化 | 中 | C2、C3 |
| W10-C5 | 05-c5-prefix-stability-guard.md | 前缀字节稳定性守卫测试（锁死 C2/C3/C4 成果） | 小 | C2、C3、C4 |

依赖说明：C2 之后 context.py 的 system 构造已变，C3 在其上继续摘除 SUMMARY 段；
C4 与 C2/C3 改不同文件但共享 turn_orchestrator.py，放最后避免冲突；C5 是纯测试收口。
建议顺序即编号顺序。

## 四、全局约束

### 4.1 受保护清单（语义不得触碰）

1. 可信证据管道：ToolObservation / receipts / effective_evidence_level 判定 / verifier 熔断
2. 三态审批门（allow/deny/approval_callback）与 policy 传播
3. `runtime_checkpoint` 恢复链路与 tool_blocked 审计
4. LazyToolRegistry 装载语义、McpRuntime / SubagentManager 组合根所有权
5. Consolidator 的 70% checkpoint 提前归档语义与 archive() 的 LLM 调用内容
   （C3 只改摘要的**注入位置**，不改归档时机与摘要生成）
6. AutoCompact 的会话过期重载语义（C3 只移除摘要返回值，reload 逻辑保留）
7. runtime context 追加在用户消息末尾的既有语义（`_RUNTIME_CONTEXT_TAG` 结构不动）

### 4.2 W10 特有红线

1. **提示词内容不丢失**：C2/C3 是内容**搬家**，召回结果、notes、跨会话记忆、归档摘要
   在每个 LLM 请求中仍然全部可见（位置变化，可见性不变）。
2. **不引入新抽象**：无新配置项、无新事件系统、无新 Port/Adapter；
   快照消息直接用 session 既有消息结构 + 标记 kwarg。
3. **定位用符号**（类名/函数名/参数名），任务书中的行号只是编写时参考值。
4. **单批单 commit**，禁止 `git add .`；每批以验证门 + 任务书"验收自检"双达标为准。
5. 每批实施前先核对任务书"现状锚点"；锚点不符立即停止报告（模板见 cline-prompt.md）。

## 五、验证门（每批统一）

```
.venv\Scripts\python.exe -m pytest tests/ -q        # 见下方基线口径
.venv\Scripts\python.exe -m ruff check erza/         # 零输出
.venv\Scripts\python.exe -m ruff format --check erza/
```

**pytest 基线口径**（2026-09-07 实跑）：4005 passed / 3 failed / 29 skipped。
3 个失败全部位于 `tests/agent/test_tool_hint.py`（路径缩写与长度断言，工作区路径
相关的既有问题，与 W10 无关）。判定标准：

- 每批完成时：passed ≥ 4005，failed 集合**恰好等于**这 3 个既有失败——
  除此之外的任何失败都算验证门不过；
- 顺手修复这 3 个失败不在本系列范围内（只记录不动手）。

注意：必须用项目 venv 的 Python 3.12（`.venv\Scripts\python.exe`），系统默认 Python 3.10
缺 `typing.Self` 会导致收集错误。

## 六、实施方式

交由 Cline 分批实施，提示词模板见 `cline-prompt.md`（与 w2-slim-plan 模板同构）。
每批完成以该批任务书"验收自检"清单为准。

## 七、效果验证口径（实施完成后人工抽查）

在真实 DeepSeek/OpenAI 会话中连续对话 10+ 轮（含工具调用），观察：
- C1 落地后：`cached_tokens / prompt_tokens` 比值随轮次的走势（期望基线 < 50%）；
- C2 落地后：普通多轮对话比值期望 > 90%，工具循环内期望 > 95%；
- C3 落地后：触发一次归档后，system+tools 前缀仍命中（只冷 log 中段）；
- C4 落地后：长会话（YELLOW 压力）下比值不再逐轮下滑。
