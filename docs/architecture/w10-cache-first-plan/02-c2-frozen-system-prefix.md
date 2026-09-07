# W10-C2：系统提示词分区冻结（动态内容出 system）

> 前置依赖：W10-C1 已合并。
> 本批是 W10 收益最大的改造：系统提示词在会话内字节不变。
> 行号是编写时参考值，定位以符号为准。

## 一、问题（现状锚点，已逐一核实）

`erza/agent/context.py` 的 `ContextBuilder.build_system_prompt`（202 行起）每轮重建
系统提示词，其中四个动态段使 system 字节逐轮漂移：

| 段 | 构造来源 | 漂移频率 | 优先级常量 |
|---|---|---|---|
| 结构化召回 | `recall_query` → `_recall_section()`（164 行起，含 `_build_recall_query`） | 每轮（随当前用户消息变化） | `_PRIORITY_MEMORY = 2` |
| Scratchpad 笔记 | `read_notes()`（notes.md，276-278 行注入） | agent 写笔记即变 | `_PRIORITY_NOTES = 6` |
| 最近历史 | `read_unprocessed_history()`（295-302 行内联渲染 `# Recent History` 段） | 每轮有新条目追加 | `_PRIORITY_HISTORY = 3` |
| 归档摘要 | `session_summary` 参数（304-307 行注入） | 归档时变 | `_PRIORITY_SUMMARY = 1` |

相关锚点：

1. `build_system_prompt` 签名（202-213 行）：`skill_names / channel / session_summary /
   workspace / agent_override / light_context / recall_query / recall_session_key /
   recall_user_key`；parts 以 `(priority, content)` 组装，末尾
   `_enforce_injection_budget`（320 行）按 65K 预算截断。
2. `build_messages`（539 行起）：`system + history` 组装，当前用户消息与 runtime context
   合并（`_RUNTIME_CONTEXT_TAG`，586-588 行注释明确了"用户内容前缀稳定"的意图），
   末位消息 `current_role` 可为 assistant（子代理场景）。
3. 调用点：`erza/agent/loop.py` `_build_initial_messages`（940-964 行，传
   `session_summary=pending_summary`）；`erza/agent/dispatch.py` 483-498 行（子代理续跑，
   传 `session_summary=pending`、`skip_runtime_lines=is_subagent`）。
4. 会话消息持久化的是**原始**用户消息（`session_turn._persist_user_message_early`，
   70-95 行），带 runtime context 的合并版只存在于当轮 LLM 请求——尾部动态内容的
   既有模式，C2 直接复用。
5. `Session.clear()`（`session/manager.py` 249-254 行）清 messages、置零 cursor、
   pop `_last_summary`——`/new` 走 `writes.reset` → `clear()`。
6. 既有测试基线：`tests/agent/test_context_prompt_cache.py`（时间稳定性、runtime
   context 尾部语义）、`tests/agent/test_context_builder.py`、
   `tests/agent/test_context_structured_memory.py`（召回注入断言）。

## 二、设计

### 2.1 目标消息形状

```
[system]            仅冻结区：identity(channel) + bootstrap(AGENTS.md/SOUL.md) +
                    tool_contract + POLICY.md + Active Skills/skills 索引 + 子代理段
                    ——会话内逐字节不变
[...history...]     会话消息（含 C2 新增的会话记忆消息，见 2.3）
[user 当前]         用户原文 → 召回块 → notes 块 → runtime context（全部在尾部）
```

### 2.2 system 只留冻结区（context.py）

`build_system_prompt` 删除三个动态段的构造与注入：

- 删 recall 段（262-270 行）、notes 段（272-278 行）、Recent History 段
  （295-302 行）及对应 `_PRIORITY_MEMORY / _PRIORITY_NOTES / _PRIORITY_HISTORY`
  常量；SUMMARY 段留给 C3 处理（本批**保留** SUMMARY 段与 `session_summary`
  参数，避免中间态丢功能——C3 再摘）。
- `recall_query / recall_session_key / recall_user_key` 参数从 `build_system_prompt`
  签名中移除；`build_messages` 内部的 recall 派生逻辑（597-604 行：
  `recall_query = None if light_context else current_message` 与
  `recall_user_key` 解析）与 615-617 行的传参一并改为尾部构造（见 2.3）；
  `light_context` 对 bootstrap 的跳过保留。
- `_recall_section` 及其辅助（`_build_recall_query`、`_recall_degraded_diagnostic`）
  **保留函数本体**，调用点改为 2.3 的尾部构造。
- `_enforce_injection_budget` 机制保留（SUMMARY 段仍走预算控制）。
- 冻结区的稳定性验证：`_get_identity`（channel 稳定）、bootstrap mtime 缓存、
  `render_template("agent/tool_contract.md")`、`read_shared_policy()`、skills 列表
  均已是确定性的；本批不改它们的实现。

### 2.3 动态内容进当前用户消息尾部（context.py）

`build_messages` 的合并逻辑扩展（589-592 行现状
`merged = f"{user_content}\n\n{runtime_ctx}"`；保持"用户原文在最前"既有语义，
`test_runtime_context_appended_after_user_content` 锁定的断言不变）：

```
merged = f"{user_content}\n\n{recall_block}\n\n{notes_block}\n\n{runtime_ctx}"
```

（空块省略，分隔符保持 `\n\n`；`_merge_message_content` 的
role 兼并路径（622-626 行，子代理续跑）自动继承新的 merged 结构，无需单独改。）

- `recall_block`：`_recall_section(...)` 输出（渲染函数本体不动，含降级诊断
  分支）；注入时沿用其现有段标题。
- `notes_block`：notes.md 内容，头部沿用现有 `# Scratchpad Notes (notes.md)`
  标题格式；为空时省略整块。
- `skip_runtime_lines=True`（子代理续跑）时三块行为与现状对齐：子代理不注入
  recall（现状 `_memory_user_key` 已如此处理）与 runtime context；notes 同样跳过。
- `current_role == "assistant"` 的子代理收尾轮：动态块仅追加在 role=assistant
  消息前的既有合并路径上不适用（现状该轮 user 消息为空串），保持现状不动。

### 2.4 跨会话记忆 → 会话首条持久化消息

`read_unprocessed_history` 渲染逻辑保留，注入位置改为**会话出生时的快照消息**：

1. `ContextBuilder` 新增公开方法（workspace 域解析方式与 `build_system_prompt`
   一致——内部用 `self.memory_for(root)`，见 context.py 122-131、230 行）：

```python
def build_memory_context(self, workspace: Path | str | None = None) -> str:
    """Session-start snapshot: recent history entries, rendered once."""
```

   内容 = 现 `build_system_prompt` 295-302 行的 Recent History 内联渲染逻辑
   原样迁出（含既有条数上限 `_MAX_RECENT_HISTORY`、`_MAX_HISTORY_CHARS` 截断）。
   notes 不进快照（notes 是逐轮尾部内容，见 2.3）。

2. 快照消息落库（`erza/agent/loop.py` 新增私有方法，`_build_initial_messages`
   调用前执行）：

```python
def _ensure_memory_context_message(self, session: Session, workspace: Path | str) -> None:
    """Stamp the session-memory snapshot as the first persisted message once."""
```

   - 条件：`not session.messages or not session.messages[0].get("_memory_context")`
     且非子代理轮；
   - 动作：`session.messages.insert(0, {"role": "user",
     "content": self.context.build_memory_context(workspace),
     "timestamp": <now>, "_memory_context": True})` 后 `sessions.save(session)`；
   - `/new` 后 `Session.clear()` 清空 messages → 下一轮重新盖章，天然拿到新快照；
   - 空快照（Dream 已消费全部 history）时插入空内容会得到空 user 消息——
     `build_memory_context` 返回空字符串则**跳过插入**，但需记录"已尝试"避免逐轮
     重试：在 `session.metadata["_memory_context_stamped"] = True` 打标，
     `Session.clear()` 时由 2.5 一并清除。
   - 两个调用点：`turn_orchestrator._state_build`（get_history 之前）与
     `dispatch.py` 子代理续跑路径（其 `session.get_history` 之前，子代理轮不盖章，
     仅确保存在即可）。
   - workspace 参数来源：参照 `_build_initial_messages` 的
     `scope = self.workspace_scopes.for_message(...)` → `scope.project_path`，
     两个调用点各自已具备该解析。

3. `get_history` 回放兼容性核查（只核查不改）：
   - `manager.py` 143-149 行的用户消息对齐逻辑：快照消息 role=user 在 index 0，
     对齐安全；
   - `find_legal_message_start`：快照是普通 user 消息，不涉 orphan tool result，安全；
   - `_annotate_message_time`：快照有 timestamp，正常注记，回放字节稳定；
   - Consolidator 归档越过 index 0 时快照被归档（进入摘要），可接受；
     重新盖章条件 `messages[0]._memory_context` 仍为真（物理消息还在），不会重复盖章。

### 2.5 Session.clear 联动（erza/session/manager.py）

`Session.clear()` 的 `metadata.pop` 列表追加 `"_memory_context_stamped"`（一行）。

### 2.6 调用点收尾

- `loop.py` `_build_initial_messages` 与 `dispatch.py` 483 行：调用参数本身无
  recall 字段（recall 在 `build_messages` 内部派生，见 2.2），调用点只确认
  不需要新传参——尾部构造所需的 `memory_user_key` / `session_key` /
  `light_context` 等均已通过既有参数在位。
- `session_summary` 参数本批保留（C3 处理）。

## 三、测试要求

### 3.1 新增（`tests/agent/test_context_prompt_cache.py` 扩展）

1. **system 冻结**：同一 builder，两次 `build_messages`（第二次 history 多一条、
   recall 结果不同——用注入了不同 history/notes 的 memory store 模拟），
   `messages[0]["content"]` 逐字节相等。
2. **召回进尾部**：recall 结果出现在最后一条 user 消息 content 中，且位于用户原文
   之后；`messages[0]`（system）中不含召回内容。
3. **notes 进尾部**：同上。
4. **快照消息**：构造空 session → 调用盖章路径 → `messages[0]["_memory_context"]`
   is True、内容含 `# Recent History`；同一 session 再调一次不重复插入。
5. **/new 重盖**：`session.clear()` 后再盖章 → 新快照（新 history 条目可见）。
6. **空快照跳过**：Dream cursor 消费完全部条目 → 不插入空消息，metadata 打标。

### 3.2 既有测试更新（仅限下列，逐条给出理由）

- `test_context_structured_memory.py`：召回断言从 system 移到最后一条 user 消息
  （内容不变、位置变化——C2 语义）。
- `test_context_builder.py` / `test_context_prompt_cache.py` 中针对
  `# Recent History` 在 system 内的断言（如
  `test_unprocessed_history_injected_into_system_prompt` 等 4 条 Recent History 用例）：
  改为断言快照消息内容（语义搬家）。
- 其余断言一律不动；跑不过的逐一报告，禁止自行弱化。

## 四、禁改清单

- `_RUNTIME_CONTEXT_TAG` 结构与"用户原文在前"的合并顺序。
- Consolidator / AutoCompact（C3 的地盘）。
- `_recall_section` 渲染逻辑本身、`_MAX_RECENT_HISTORY` / `_MAX_HISTORY_CHARS` 语义。
- Session 持久化格式（新增 kwarg 是既有 `**kwargs` 通道的常规用法，如
  `_channel_delivery`，不构成格式破坏）。
- light_context 心跳轮的 bootstrap 跳过语义。

## 五、验收自检

1. system prompt 只含冻结区；SUMMARY 段仍在（等 C3）
2. recall / notes 出现在当前用户消息尾部、用户原文之后
3. 跨会话记忆为会话首条持久化消息，一次写入、`/new` 后重盖
4. 空快照跳过且不逐轮重试
5. 3.1 全部 6 条新测试 + 3.2 列出的更新就位；其余既有测试零修改
6. 验证门通过
