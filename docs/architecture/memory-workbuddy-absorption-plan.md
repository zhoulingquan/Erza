# WorkBuddy 记忆机制吸收实施计划

来源：对 WorkBuddy（Electron 桌面助手）记忆系统的逆向分析。吸收原则：**只学注入策略与写入行为，不学其裸文件直写**——所有治理能力保留在 Erza 现有的 SQLite 治理库 + Dream 管线内。

共 4 个阶段 8 个任务。阶段间有依赖（Phase B 依赖 Phase A 的 schema 位置知识，Phase C/D 独立）。每个任务附验收标准。

---

## Phase A — 零延迟捕获（reflection 默认开启 + 写入契约）

### A1. 开启 reflection 默认值

**文件**：`erza/config/schema.py:262`

```python
enable_reflection: bool = Field(
    default=True,   # 原 False：零延迟捕获，发现当场进 reflections.jsonl 队列
    validation_alias=AliasChoices("enableReflection"),
    serialization_alias="enableReflection",
)  # Enable post-turn reflection for cross-turn learning
```

注意：
- 只改默认值，不动 `reflection_interval`（默认 5 保持）。
- 全局搜索 tests 里对 `enable_reflection`/`enableReflection` 旧默认值 False 的断言并同步更新（已知相关文件：`tests/agent/test_loop_execution_policy_config.py`、`tests/agent/test_runner_reflection.py`、`tests/agent/test_terminal_reflection.py`、`tests/ledger/test_turn_call_ledger.py`——只改断言"默认关闭"的用例，显式传参的用例不动）。
- `erza/agent/loop.py:169`、`erza/agent/runner.py:123` 里的类字段默认值若与 config schema 重复声明，保持与 schema 一致（改为 True）。

**验收**：`pytest tests/agent/test_runner_reflection.py tests/agent/test_terminal_reflection.py tests/agent/test_loop_execution_policy_config.py tests/config/ -x -q` 全绿。

### A2. 扩展 reflection 提示词为完整写入契约

**文件**：`erza/templates/agent/reflection_system.md`（整文件重写）

当前提示词只让记"mistake 教训"。改为 WorkBuddy 式完整契约，同时捕获三类内容：

1. **教训**（现保留）：什么错了、下次怎么做。
2. **用户明说的规则/偏好**：用户在对话中明确要求的做事方式（如"回复用中文""改动前先确认"）。
3. **发现的规则**：本次实质工作中确认的项目约定、约束、经验证的事实结论。

跳过条件（输出 `{"lesson": ""}` 即合法空，应用层不会持久化空 lesson）：
- 纯寒暄、简短问答、无实质工作；
- 一次性的临时信息（下轮即失效）。

输出格式保持不变：`{"lesson":"..."}` 单条原子原则，一条 lesson 一个独立可修正的断言；禁止把"X，并且 Y"塞进一条。应用层（`erza/agent/reflection.py` 的 `_parse_structured_response`）已天然跳过空值，无需改代码。

**验收**：`pytest tests/agent/test_reflection_structured.py tests/agent/test_runner_reflection.py -x -q` 全绿（解析层无变化，仅提示词变更）。

---

## Phase B — 常驻小画像块（补词法路由盲区）

### B1. StructuredMemoryConfig 新增 3 个字段

**文件**：`erza/config/schema.py`，`StructuredMemoryConfig` 类内（L161-167 字段区追加）

```python
resident_profile_enabled: bool = True
resident_profile_token_budget: int = Field(default=500, ge=100, le=2000)
resident_min_importance: int = Field(default=4, ge=1, le=5)
```

说明：类已有 `model_config = ConfigDict(alias_generator=to_camel, ...)`，自动获得 camelCase 别名 `residentProfileEnabled` 等，无需手写 alias。

**验收**：`pytest tests/config/test_structured_memory_config.py -x -q` 全绿。

### B2. context.py 新增常驻画像渲染 + 注入

**文件**：`erza/agent/context.py`

**新增方法**（放在 `_recall_section` 之后，L188 附近）：

```python
def _resident_profile_section(
    self,
    *,
    user_key: str | None = None,
    store: MemoryStore | None = None,
) -> str:
```

实现要点：
1. `store = store or self.memory`；读 `store.structured_config`，`resident_profile_enabled` 为 False 或 health 非 healthy 时返回 `""`（**静默跳过，不注入降级诊断**——常驻块是增强，不是关键路径）。
2. 取候选：`store.structured_repository.recall_candidates(allowed_scopes=(MemoryScope(kind=ScopeKind.USER, key=user_key or "user:default"),), requested_kinds=(), now=datetime.now(timezone.utc))`。
3. 过滤：`record.importance >= cfg.resident_min_importance` 且 `record.source_level in {EXPLICIT_CORRECTION, CONFIRMED_DECISION, VERIFIED}`（排除 INFERRED 和 REPEATED_EXPERIENCE 的低置信来源）。
4. 排序：`(importance desc, updated_at desc, id)`。
5. 渲染：标题 `# User Profile (Always-On)` + 空行 + 每条一行 `- [mem_xxx] statement`（不带 Why 评分理由，保持紧凑）。逐条累加，用已有的 `self._estimate_tokens` 预算控制，超过 `resident_profile_token_budget` 即停止（宁可少不截断半句）。
6. 候选为空时返回 `""`。

**注入点**（L532-545 `dynamic_blocks` 组装处）：

```python
if not skip_runtime_lines:
    resident = self._resident_profile_section(user_key=recall_user_key, store=store)
    if resident:
        dynamic_blocks.append(resident)
    if recall_query and not light_context:
        ...  # 现有 recall 逻辑不动
```

约束：
- 常驻块放在 recall_text **之前**（偏好类记忆优先级最高）。
- `light_context` 与 `skip_runtime_lines` 时同样跳过（与 recall 一致，心跳轻量场景不注入）。
- 不要改动 `build_system_prompt`——常驻块属于动态块，追加在用户消息尾部，维持 W10-C2 字节稳定前缀架构。
- `user_key` 用注入点已算好的 `recall_user_key` 变量（L522-526），不要重复推导。

**验收**：
- `pytest tests/agent/test_context_structured_memory.py tests/agent/test_context_prompt_cache.py -x -q` 全绿。
- 新增测试 `tests/agent/test_context_structured_memory.py::test_resident_profile_block`：
  - 写入一条 `scope=USER, importance=5, statement="回复始终使用简体中文"` 的记录后，`_resident_profile_section` 返回值包含该 statement 且以 `# User Profile (Always-On)` 开头；
  - `importance=2` 的 USER 记录不出现；
  - `resident_profile_enabled=False` 返回 `""`；
  - token 预算截断：设 `resident_profile_token_budget` 为极小值时条目数减少而非截断半行。

---

## Phase C — 超限强制自清理（防膨胀）

### C1. recall 预算饱和时注入维护指令

**文件**：`erza/memory/recall.py`，`render_prompt` 方法（L175-184）

现状：超过 `token_budget` 的命中被静默丢弃（`excluded_by_budget` 计数），模型毫不知情。

改动：`render_prompt` 中当 `result.excluded_by_budget > 0` 时，在渲染完命中条目后追加一段（确定性文案，无模型参与）：

```
# Memory Maintenance Required

The recall token budget is saturated: {N} active records were excluded from
injection. Before continuing the user's task, check /memory-status and use
/memory-correct to merge duplicates or /memory-revoke to retire stale
high-importance records. Never edit memory/structured/ files directly.
```

注意：
- 文案放在命中列表之后、同一段 prompt 内，不要独立新 section 函数（保持 recall.py 自包含）。
- `degraded` 结果不追加（降级诊断已有自己的格式）。
- 这是给模型的治理指令——Erza 的记忆库不允许直写，清理路径必须指向 `/memory-*` 命令。

### C2. Shared Policy 超限改注入 ACTION REQUIRED

**文件**：`erza/agent/context.py` L270-274

现状：POLICY.md 超过 `_MAX_INJECTION_TOKENS` 时只打 warning log，原文全量注入。

改动：超限时在该 part 前加前缀行：

```python
if self._estimate_tokens(policy) > self._MAX_INJECTION_TOKENS:
    logger.warning("shared policy exceeds injection budget; not truncated")
    policy = (
        "ACTION REQUIRED: This shared policy exceeds the injection budget. "
        "Before executing the task, propose trimming POLICY.md to the user "
        "(keep normative rules only, move facts to governed memory via "
        "/memory-correct). Never edit the file without user confirmation.\n\n"
        + policy
    )
```

保持仍然全量注入（不静默截断），但把膨胀显性化并给出维护动作。

**验收（C1+C2）**：
- `pytest tests/agent/test_memory_recall.py tests/agent/test_context_structured_memory.py -x -q` 全绿。
- 新增测试：recall 结果 `excluded_by_budget>0` 时 `render_prompt` 输出包含 "Memory Maintenance Required" 与被排除计数；`=0` 时不包含。
- POLICY 超限用例：mock 一份超长 policy，断言 build_system_prompt 产物含 "ACTION REQUIRED" 且 policy 正文未被截断。

---

## Phase D — 文案契约（检索路由 + 事故锚定）

### D1. SKILL.md 增补检索路由三分法 + 写入管线说明

**文件**：`erza/skills/memory/SKILL.md`

在 "Search Past Events" 小节**之前**插入新小节 "Retrieval Routing"：

```markdown
## Retrieval Routing

Decide where to look before searching:

1. **Structured memory** — already injected when relevant (deterministic recall
   + always-on user profile). If a needed fact might be governed but was not
   injected, do not grep; the recall is deterministic — ask the user or use
   /memory-show.
2. **This project's past events** — search `memory/history.jsonl` with grep
   (see "Search Past Events" below). Use targeted patterns; avoid full-file reads.
3. **No dependency** — if the task has no plausible link to past events
   (fresh work, pure Q&A), skip history search entirely.

Cost rule: history search is the fallback, never the first move.
```

在 "Important" 小节追加一条：

```markdown
- Reflections you see referenced in /memory-status come from the automated
  reflection queue; they are evidence for Dream, not facts. Do not treat
  unconsumed reflections as established rules.
```

### D2. Dream 提示词增加事故锚定规则

**文件**：`erza/templates/agent/dream_phase1.md`，Rules 列表末尾追加：

```markdown
- When a proposal originates from a failure or correction (reflection triggers
  such as tool_error, or a user-reported incident), anchor the rule: include a
  one-line incident note in "detail" (date + concrete event, e.g.
  "anchored: 2026-09-10 fire-and-forget background task lost, stale data
  nearly shipped"). Rules with incident anchors are followed more reliably
  than bare rules. Never fabricate an incident; only anchor to evidence you
  actually saw in the prompt.
```

**验收（D1+D2）**：`pytest tests/agent/test_dream_structured_memory.py tests/agent/test_memory_extraction.py -x -q` 全绿（模板变更不破坏解析；`_assert_atomic` 等校验仍通过）。

---

## 完成后全量回归

```powershell
pytest tests/ -x -q
```

重点观察：`tests/agent/`、`tests/config/`、`tests/memory/`。任何与记忆注入相关的快照/字节稳定断言失败，优先检查是否 W10-C2 动态块位置被破坏。

## 明确不做（与吸收原则一致）

- 不引入 LLM 直写记忆文件路径；
- 不做 MD+RAW_JSON 双格式 / 文件锁 / 轮询租约（SQLite 事务已覆盖）；
- 不动 Dream 的 evidence 契约与治理命令语义。
