# W10-C3：归档摘要进 log（持久化消息，不再注入 system）

> 前置依赖：W10-C2 已合并（context.py 的 system 构造已只剩冻结区 + SUMMARY 段）。
> 本批摘除 system 中最后一个动态段，并把摘要改为 log 内持久化消息——
> 归档发生时 system+tools 前缀照常命中，只冷 log 中段一次。

## 一、问题（现状锚点，已逐一核实）

1. `erza/agent/context.py` `build_system_prompt` 的 SUMMARY 段（`_PRIORITY_SUMMARY = 1`）
   注入 `session_summary` 参数（`[Archived Context Summary]` 头）。
2. 摘要注入链：`AutoCompact.prepare_session`（`erza/agent/autocompact.py` 134-150 行）
   从内存 `_summaries` 或 `session.metadata["_last_summary"]` 取摘要并 `_format_summary`
   （含 verbatim_recent 拼接）→ `turn_orchestrator._state_compact`（341-346 行）存入
   `ctx.pending_summary` → `_state_build`（403-409 行）传给 `_build_initial_messages`
   → `loop.py` 958 行 `session_summary=pending_summary` → system。
3. `memory/consolidator.py` 三条归档路径（摘要产生点）：
   - `maybe_consolidate_by_tokens`（382 行起）：`_consolidate_replay_overflow` +
     token 循环归档；每轮 `summary = await self.archive(chunk)` 后
     `session.last_consolidated = end_idx`（469-477 行）；
   - `_consolidate_replay_overflow`（176-197 行）：replay 窗口溢出归档，同样推进 cursor；
   - `compact_idle_session`（498-567 行）：硬截断空闲会话，
     `session.messages = kept; session.last_consolidated = 0`（553-554 行）。
4. `estimate_session_prompt_tokens`（251-280 行）用 `_last_summary` metadata 构造
   probe 消息估算 token（`_build_messages(..., session_summary=summary, ...)`）。
5. `Session.get_history`（`session/manager.py` 125 行起）以
   `messages[last_consolidated:]` 切片回放——**cursor 语义是本批插入位置的支点**。
6. `cmd_new`（`command/builtin.py` 213-227 行）：`/new` 归档快照后 reset。
7. 子代理路径：`dispatch.py` 492 行同样传 `session_summary=pending`。

## 二、设计

### 2.1 摘要消息化（memory/consolidator.py）

三条归档路径在归档成功后，把摘要作为**持久化 user 消息**插入 cursor 位置：

```python
_SUMMARY_MARKER = "_archived_summary"

def _insert_summary_message(session: Session, summary: str, at: int, *, verbatim: list[str] | None = None) -> None:
    """Insert the fold summary as a persisted user message at index *at*."""
```

- 消息结构：`{"role": "user",
  "content": f"[Archived Context Summary]\n\n{summary}" + verbatim_suffix,
  "timestamp": <now>, "_archived_summary": True}`；
  `verbatim_suffix` 沿用现 `AutoCompact._format_summary` 的 verbatim_recent 拼接
  （autocompact.py 49-65 行："Recent user messages (verbatim):" 列表，每条截断
  500 字符——"最近用户消息原文"防偏离语义原样保留，只是拼接位置从 system
  移入消息；该拼接逻辑迁至 Consolidator 的插入辅助函数）。
- 三条路径的插入点与 cursor 推进：
  - `maybe_consolidate_by_tokens`：`session.messages.insert(end_idx, summary_msg)` 后
    `session.last_consolidated = end_idx`——切片 `[end_idx:]` 恰好以摘要消息开头；
  - `_consolidate_replay_overflow`：同上；
  - `compact_idle_session`：`session.messages = kept` 之后在 index 0 插入摘要消息
    （`last_consolidated = 0` 使其成为回放首条）。
- `archive()` 本身（301-369 行）不改：LLM 调用、history.jsonl 追加、notes 清空、
  hygiene 节流全部原样。

### 2.2 停止 system 注入（context.py 及调用链）

1. `build_system_prompt` 删 SUMMARY 段与 `session_summary` 参数；
   `_PRIORITY_SUMMARY` 常量删除。
2. `build_messages` 删 `session_summary` 参数。
3. 调用点清理：`loop.py` `_build_initial_messages`（删 `session_summary=` 传参与
   `pending_summary` 形参）；`dispatch.py` 483 行块（删 `session_summary=`）。
4. `turn_orchestrator.py`：`TurnContext.pending_summary` 字段（103 行）删除；
   `_state_compact`（341-346 行）改为只保留会话重载
   （`prepare_session` 返回值见 2.3）；`_state_build` 407 行传参删除。

### 2.3 AutoCompact 瘦身（erza/agent/autocompact.py）

- `prepare_session` 签名改为 `-> Session`（删返回摘要元组）；过期重载与
  `_archiving` 判断逻辑原样保留。
- 内存 `_summaries` 字典及其 `_SUMMARY_RETENTION` 清理逻辑删除（其唯一用途是给
  prepare_session 热路径供摘要；冷路径 metadata 同理只剩标题生成等 UI 消费——
  `_last_summary` metadata 的**写入**保留在 Consolidator，供非提示词消费者）。
- `_archive`（110-132 行）：`compact_idle_session` 已在 2.1 中自行插入摘要消息，
  `_archive` 里从 metadata 回读 `_summaries` 的块删除。

### 2.4 估算器对齐（memory/consolidator.py）

`estimate_session_prompt_tokens`：不再从 `_last_summary` metadata 注入
`session_summary` 构造 probe（摘要已在 `history` 消息里，随
`_full_unconsolidated_history` 自然计入）。`_build_messages` 调用删
`session_summary=` 实参。

### 2.5 消费者核查（只核查不动手）

全库搜 `_last_summary` 的读取方：`session/manager.py:254`（clear 清理，保留）、
`autocompact.py`（本批删除的注入逻辑）、`consolidator.py`（写入方 + 估算器）。
若发现 WebUI 标题生成等消费，保留 metadata 写入不动——本批只断"提示词注入"链。

## 三、测试要求

### 3.1 新增（扩展 `tests/agent/test_consolidator.py` 与 `tests/agent/test_auto_compact.py`）

1. `maybe_consolidate_by_tokens` 归档一轮后：
   `session.messages[last_consolidated]` 是 `_archived_summary` 消息，内容含摘要文本；
   `get_history()` 回放首条为该摘要。
2. 两轮归档：两条摘要消息先后存在，cursor 语义正确（`[cursor:]` 起于最新摘要）。
3. `compact_idle_session` 后：`messages[0]` 为摘要消息，recent suffix 保留。
4. 摘要消息含 verbatim_recent 拼接（有最近用户消息时）。
5. `build_messages` 产出的 system 不再含 `[Archived Context Summary]`；
   摘要出现在消息列表中（history 首条）。
6. AutoCompact `prepare_session` 返回 Session；过期重载行为回归（既有用例）。

### 3.2 既有测试更新（仅限下列）

- `test_auto_compact.py` 中针对 `prepare_session` 返回摘要的断言（签名变化）。
- `test_consolidator.py` 中 `_last_summary` 注入相关的断言（改为断言消息插入）。
- `test_context_builder.py` 中 SUMMARY 段相关断言（如存在）。

## 四、禁改清单

- `archive()` 的 LLM 调用与 notes.md 清空语义。
- 70% checkpoint 提前归档的触发语义（`_checkpoint_threshold`）。
- `maybe_consolidate_by_tokens` 的循环退出条件与预算判定。
- `Session.clear()` 既有 pop 集合（`_last_summary` 保留清理——metadata 仍在写）。
- `cmd_new` 的归档快照行为。

## 五、验收自检

1. 三条归档路径均插入持久化摘要消息，cursor 语义正确
2. system 不再含任何归档摘要；`session_summary` 参数全链路删除
3. verbatim_recent 语义保留（进摘要消息）
4. 估算器从消息计入摘要，不再双计
5. 3.1 全部新测试 + 3.2 列出的更新就位；其余既有测试零修改
6. 验证门通过
