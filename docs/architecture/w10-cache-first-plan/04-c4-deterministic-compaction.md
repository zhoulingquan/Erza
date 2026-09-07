# W10-C4：确定性压缩（预算截断确定化 + microcompact 移至轮次边界并持久化）

> 前置依赖：W10-C2、W10-C3 已合并。
> 目标：治理管道对历史的任何改写只发生在轮次边界、一次成型、写回持久化；
> 轮内迭代回放同字节前缀。RED 应急路径保留（正确性优先于缓存）。

## 一、问题（现状锚点，已逐一核实）

### 锚点 1：apply_tool_result_budget 的压力相关截断（非确定性）

`erza/agent/execution/context_governance.py` `apply_tool_result_budget`
（173-206 行）：

```python
max_chars = spec.max_tool_result_chars
if pressure_level is PressureLevel.RED:
    max_chars = max_chars // 2   # 压力相关的减半
```

同一历史消息在 GREEN 轮与 RED 轮会得到不同截断——压力跨越边界那一刻整段历史
字节突变，前缀断裂。且治理是 request-local 的，突变不落库，每轮重复发生。
（`MicrocompactStrategy` / `ApplyToolResultBudgetStrategy` 在
`erza/agent/runner_strategies.py` 196-213 行做薄委托；GREEN 跳过逻辑在
`MicrocompactStrategy` / `SnipHistoryStrategy`，`ApplyToolResultBudgetStrategy`
无 GREEN 跳过——预算截断任何压力都跑。）

### 锚点 2：microcompact 的相对窗口漂移

`erza/agent/runner_strategies.py` 模块级函数 `microcompact()`（117-159 行）：

- 可压缩判定：工具元数据 `compactable=True` 且 `importance < 1.0`，
  或名字在 `_COMPACTABLE_TOOLS` 白名单（28 行起）；
- 保留窗口 `_MICROCOMPACT_KEEP_RECENT = 10`（26 行）：最近 10 个可压缩结果之外
  替换为 `f"[{name} result omitted from context]"`（154 行）；
- 短于 `_MICROCOMPACT_MIN_CHARS = 500`（27 行）的结果跳过；
- 策略包装 `MicrocompactStrategy`（196-203 行）GREEN 跳过——GREEN 会话无漂移，
  YELLOW/RED 会话窗口随历史增长逐轮后移：每轮恰好一个结果新跨出窗口，
  字节在历史中段逐轮断裂；request-local 不落库。

### 锚点 3：创建期截断已确定性（对照，不改）

`execution/tool_execution.py` `normalize_tool_result`（510-535 行）在结果创建时按
`spec.max_tool_result_chars` 固定截断——同样输入同样输出。这是确定性基线，
C4 只是让治理路径与之对齐。

### 锚点 4：管道注册与轮次边界位置

- 默认管道：`erza/agent/context_governor.py` `ContextGovernor.BUILTIN_PIPELINE`
  （101-111 行，序列含 `microcompact`）；工厂注册 `_load_default_strategies`
  （120-158 行，133-140 行 factories dict）。入口点组
  `erza.context_strategies` 为第三方扩展通道。
- 轮次边界：`turn_orchestrator._state_build`（372-419 行）：
  `maybe_consolidate_by_tokens` 之后、`session.get_history` 之前——
  C3 的摘要插入也在此区间，C4 的轮界压缩紧随其后。
- 治理调用点：`runner.py` 477 行 `messages_for_model = await self.govern_messages(...)`
  每次迭代调用。

## 二、设计

### 2.1 apply_tool_result_budget 确定化（execution/context_governance.py）

- 删 RED 减半分支（184-185 行及 196-201 行的二次截断）：截断上限恒为
  `spec.max_tool_result_chars`（`normalize_tool_result` 内部即按此截断，
  确定且幂等）。
- 轮内 RED 溢出的兜底由 `snip_history` 承担（既有行为，不动）。
- 结果：治理对工具结果的截断在任何压力下都是同一确定性函数——重放回放
  同字节；与创建期截断叠加为幂等（不超限则零字节变化）。
- `pressure_level` 形参保留（签名兼容，其他压力相关逻辑未用则清理调用点）。

### 2.2 microcompact 移至轮次边界并持久化

**从默认治理管道移除，改为 `_state_build` 中的会话级轮界压缩：**

1. `context_governor.py`：`BUILTIN_PIPELINE` 删 `"microcompact"` 项、
   `_load_default_strategies` 的 factories dict 删 `MicrocompactStrategy` 条目；
   `runner_strategies.py` 删 `MicrocompactStrategy` 类与模块函数 `microcompact()`
   （其逻辑迁入下述轮界方法；`_MICROCOMPACT_KEEP_RECENT` /
   `_MICROCOMPACT_MIN_CHARS` / `_COMPACTABLE_TOOLS` 常量与可压缩判定逻辑随迁）。
2. 新增轮界压缩（`turn_orchestrator.py`，`_state_build` 内、`get_history` 之前；
   实现 AgentLoop 私有方法，与 C2 的 `_ensure_memory_context_message` 同层）：

```python
def _microcompact_session_history(self, session: Session, workspace: Path | str) -> None:
    """Turn-boundary microcompact: rewrite old tool results in place, once."""
```

   - 可压缩判定与占位文本格式原样复用（`[{name} result omitted from context]`）；
     作用于 `session.messages[last_consolidated:]` 并**直接写回**
     （改 content 后 `sessions.save(session)`）；
   - 作用范围：该切片内全部可压缩结果，保留最近
     `_MICROCOMPACT_KEEP_RECENT` 个完整（语义不变）；
   - 跳过 C3 的 `_archived_summary` 消息与 C2 的 `_memory_context` 消息
     （compactable 判定只匹配 role=tool，此处为防御性断言）；
   - 无压力门槛——GREEN 会话也做（落库一次后幂等，GREEN 会话每轮零变化；
     相比现状多压缩了 GREEN 会话，这是有意的：提前确定性收缩优于
     YELLOW 才开始的漂移式收缩）。
3. 压缩发生的字节语义：每轮开始时，前轮新产生的可压缩结果若跨出保留窗口，
   在此刻一次性改写落库——本轮内所有迭代回放同字节；下一轮回放的也是已
   落库的占位文本，不再二次改写。

### 2.3 snip_history / schema_crop 不动

- `snip_history`：RED 应急丢头，request-local，保持现状（应急优先正确性）。
- `schema_crop`：RED 下裁工具定义，保持现状（tools 列表本身在前缀内，
  裁剪即缓存断裂——RED 应急同样接受；GREEN/YELLOW 零字节变化不变）。

### 2.4 入口点注册表同步

`erza.context_strategies` entry point 是第三方扩展通道（`context_governor.py`
文档串声明）——本批只删内置注册，扩展协议不动。

## 三、测试要求

### 3.1 新增（扩展 `tests/agent/` 下治理相关测试文件；若无对应文件，新建
`tests/agent/test_turn_boundary_compaction.py`）

1. **预算确定化**：构造超限工具结果，同一输入在 GREEN 与 RED 两个压力信号下
   过 `apply_tool_result_budget`，输出字节相同、且等于按
   `spec.max_tool_result_chars` 截断的结果。
2. **轮界压缩落库**：session 内含 12 个可压缩工具结果（>10），轮界压缩后：
   前 2 个变占位、后 10 个完整；`session.messages` 已持久化为占位
   （重新 get 不再变化）。注意可压缩判定：用 `_COMPACTABLE_TOOLS` 白名单内的
   工具名 + 结果长度 ≥ `_MICROCOMPACT_MIN_CHARS` 构造测试数据。
3. **幂等**：同一 session 连续两次轮界压缩，第二次零变化。
4. **轮内稳定**：轮界压缩后 get_history 两次调用字节相同（时间注记除外——
   timestamp 是持久化字段，注记稳定）。
5. **默认管道无 microcompact**：`ContextGovernor` 默认序列不含 microcompact 步骤。
6. **防御**：`_archived_summary` / `_memory_context` 消息不被改写。

### 3.2 既有测试更新（仅限下列）

- 断言默认管道含 microcompact 的测试（管道序列变化）。
- 断言 RED 减半截断的测试（行为变化——若存在，列出并更新为固定上限语义）。

## 四、禁改清单

- `snip_history` / `schema_crop` / `drop_orphan_tool_results` /
  `backfill_missing_tool_results` 的行为。
- `normalize_tool_result` 创建期截断。
- 治理管道的扩展协议（entry point 注册、策略 Protocol 签名）。
- Consolidator / AutoCompact（W10-C3 已收口，勿动）。

## 五、验收自检

1. RED 减半分支删除，预算截断恒定
2. microcompact 从治理管道移除，轮界压缩落库、幂等
3. 占位文本格式与原 microcompact 一致（`[<name> result omitted from context]`）
4. 3.1 全部 6 条新测试 + 3.2 列出的更新就位；其余既有测试零修改
5. 验证门通过
