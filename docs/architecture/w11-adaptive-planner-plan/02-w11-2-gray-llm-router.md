# 任务书 W11-2：L2 灰区 LLM 判别

> 系列总览与红线见 `00-overview.md`（先读第 1、3 节）。依赖 W11-1 已合并
> （`PlanningPolicy.classify` 三值输出可用）。本批给 GRAY 路由接上一次廉价的
> LLM 裁决（清单测试），并完成 `init_planner` 从 `should_plan` 到 `classify`
> 的切换。

## 0. 勘误 E1（2026-09-08 裁定；本批首次实施中止的根因修正，必读）

首次 W11-2 实施按纪律中止（未 commit、已回退）：L2 router 调用放入
`init_planner` 后，~19 个既有测试文件失败——它们以 MagicMock/脚本化
FakeProvider + 灰区占位任务文本（"ship"、"drive it" 类）驱动全链路 runner，
新增的 router 调用破坏了调用计数断言与响应序列。

**根因是任务书缺陷而非实施错误**：已批准流程图 STEP 3 的"单步查看/执行 →
DIRECT"桶在 01 任务书决策表转写时遗漏，导致短、无产出动词、无结构信号的
占位 fixture 全部落入 GRAY 触发 router 调用。本节为授权勘误。

### E1.1 决策表补行（授权改动 `erza/agent/planning_policy.py`）

在 01 任务书 3.3 决策表的问句行（序 6）之后、兜底 GRAY（原序 7）之前插入
新行：

| 序 | 条件 | 结果 |
|----|------|------|
| 6.5 | 无产出动词 且 `_effective_length(text) < 24`（到达此行即隐含未命中强信号/宏目标/动词门/寒暄/问句） | DIRECT / `trivial`（signals 记 `effective_len`） |

语义：短且无产出动词且无任何结构信号 = 单一祈使短语（"跑一下测试"、
"看看这个文件"、"ship"）→ 直接执行，不付 L2 调用。带动词的短任务仍走
序 4 verb_gate 进灰区（"写个注释"语义保持不变，"短 ≠ 小"原则对产出型
任务依然成立）。

受影响的 W11-1 测试预期翻转（授权，报告中逐条列出）：
- T19 "你好，帮我看看今天日程"：GRAY → DIRECT（单一祈使，寒暄仅为前缀）
- T21 "跑一下测试"：GRAY → DIRECT（与已批准流程图示例卡一致）
- 其余 GRAY 断言不变（T16 动词门、T20 长调查文本 ≥24 有效字符仍为 GRAY）

### E1.2 既有测试适配授权（A-1）

既有测试若**因 L2 router 调用新增而失败**（调用计数/响应序列断言错位），
允许按以下优先级适配，每处在报告中列出（文件/用例/方式/意图保持说明）：

1. **首选**：spec 构造处显式加 `planning_policy=PlanningPolicy(force_plan=False)`
   —— 零文本改动、零 mock 改动，行为回到"不规划"路径（与
   `test_loop_progress.py` 既有模式一致）；
2. 若测试语义与 force_plan 冲突（测试本身考察规划行为）：改任务文本为
   DIRECT 形态（短、无产出动词、无结构信号）；
3. **禁止**：弱化调用计数断言（如 `assert_not_awaited` → `assert_awaited`）、
   mock 打洞绕过 router、删除既有用例。

适配范围界定：仅限"因 router 调用新增而失败"的用例；其他原因的失败一律
停止报告，不得借 A-1 扩权。

### E1.3 本批授权文件集（替代 §2 开头的文件清单）

- `erza/templates/agent/planner_router.md`（新增，内容仍以 2.1 为准逐字落盘）
- `erza/agent/execution/planning.py`（2.2-2.4 原文不变）
- `erza/agent/planning_policy.py`（**仅** E1.1 补行 + classify docstring 决策
  表摘要同步；其余禁改）
- `tests/agent/test_planning_policy.py`（2.5 确认 + E1.1 预期翻转 + 可新增
  E1 回归用例：短祈使 → DIRECT、带动词短任务 → GRAY 分流）
- `tests/agent/test_gray_router.py`（新增，§3 原文不变）
- A-1 授权的既有测试适配（逐文件逐用例列报告）

### E1.4 验证门执行方式补充

全量 pytest 必须**单进程、默认配置**运行（禁止 `--override-ini timeout`
等改参）；命令超时给足 ≥ 900000ms。若出现真实挂起（非已知 3 失败），
停止报告，不得用超时参数或跳过手段绕过。

## 1. 现状锚点（逐条核对，任何一条不符立即停止报告）

| # | 锚点 | 位置（参考值） |
|---|------|----------------|
| A1 | `PlanningReflectionService.init_planner(spec)` 返回 `(planner, plan, task_text, tools_summary)`；当前用 `policy.should_plan(task_text)` 布尔门（W11-1 后 `should_plan` 为门面，内部走 `classify`） | `erza/agent/execution/planning.py` L108-152 |
| A2 | `extract_task_from_messages(messages)` 取**最后一条** user 消息文本，空则 `"(task)"` | 同文件 L29-40 |
| A3 | `Planner.__init__(provider, model)`；`create_plan(task, tools_summary)` 内部 `async with call_purpose(CallPurpose.PLANNER):` + `provider.chat_with_retry(model=…, messages=[system, user], tools=None, tool_choice=None)` + `render_template("agent/planner_system.md", strip=True)` | `erza/agent/planner.py` L159-195 |
| A4 | 模板根：`erza/templates/agent/`（`planner_system.md`、`planner_replan.md` 在此）；`render_template` 来自 `erza.utils.prompt_templates` | `erza/templates/agent/` |
| A5 | `CallPurpose` 枚举含 `PLANNER`（L20）；本批**不新增枚举值**（D1） | `erza/ledger/call_ledger.py` L16-31 |
| A6 | `init_planner` 唯一生产调用方：`AgentRunner._run_with_ledger`（`runner.py` L433：`planner, plan, planner_task_text, planner_tools_summary = await self.init_planner(spec)`） | `erza/agent/runner.py` |
| A7 | `PlanningReflectionService` 持有 `self._runner`（`self._runner.provider`、`self._runner.build_tools_summary(spec.tools)` 均为既有用法） | `erza/agent/execution/planning.py` |
| A8 | 测试惯例：`tests/agent/test_planning_policy.py` FakeProvider（`chat_with_retry`/`chat_stream_with_retry` 返回 `LLMResponse(content="", tool_calls=[], usage={})`）；`tests/agent/test_planner_results.py` 用 `planning_policy=PlanningPolicy(force_plan=True)` 驱动规划路径 | 两个测试文件 |

## 2. 改动方案

涉及 2 个生产文件 + 1 个新模板 + 1 个新测试文件：

- `erza/templates/agent/planner_router.md`（新增）
- `erza/agent/execution/planning.py`（init_planner 切换 + 两个新函数 + 一个新私有方法）
- `tests/agent/test_gray_router.py`（新增）
- `tests/agent/test_planning_policy.py`（微调：见 2.5）

### 2.1 新模板 `erza/templates/agent/planner_router.md`

```
You are a task router. Decide whether a task deserves an explicit
step-by-step plan before execution.

Answer with exactly one word: PLAN or DIRECT.

- PLAN: a competent assistant could write a checklist of 3 or more concrete,
  ordered steps before starting (multi-part investigation, building or
  modifying something substantial, coordinating several files or systems).
- DIRECT: a single action, a quick lookup, a question, a short conversational
  reply, or a small change whose steps are obvious.

When in doubt, prefer DIRECT: planning costs a call, and a stalled execution
can still be escalated to a plan later.
```

禁止改动其余模板；本模板不引用变量（纯静态）。

### 2.2 新函数（`execution/planning.py`，紧邻 `extract_task_from_messages`）

```python
def extract_session_context(messages: list[dict[str, Any]]) -> tuple[str | None, int]:
    """First user message text (truncated to 200 chars) and user message count.

    Gives the gray-zone router the session's original goal so referential
    tasks ("do the three things above") can be judged, without any new
    runtime state or persistence (D3).
    """
```

实现要点：正序找第一条 role=="user" 的文本（复用现有 content str/list-block
解析逻辑，可抽小助手 `_message_text(msg)` 供 `extract_task_from_messages`
与本法共用）；统计 user 消息总数；首条截断 200 字符。

```python
def parse_router_verdict(content: str) -> bool:
    """True iff the first meaningful line says PLAN. Anything else -> False."""
```

实现要点：取 `content.strip()` 前 200 字符，`casefold()` 后
`re.search(r"\bplan\b", head)` → True；`\bdirect\b` → False；两者皆无或
内容为空 → False（fail-open）。注意顺序：先找 `direct` 再找 `plan` 也可，
但二者词边界互不包含，任一顺序均可；无匹配一律 False。

### 2.3 新私有方法（`PlanningReflectionService`）

```python
async def _classify_gray(self, spec: AgentRunSpec, task_text: str) -> bool:
    """One cheap same-model call deciding a gray task: plan or direct.

    Fail-open to False (DIRECT): a router failure must not block the turn.
    Accounted under CallPurpose.PLANNER (D1); no temperature/max_tokens
    overrides (D2) — call shape mirrors Planner.create_plan (A3).
    """
```

实现要点（照 A3 的调用形状）：

```python
first_task, user_count = extract_session_context(spec.initial_messages)
user_content = f"## Task\n{task_text}\n\n## Session Context\n"
if first_task is not None:
    user_content += f"First user message: {first_task}\n"
user_content += f"User messages so far: {user_count}"
try:
    async with call_purpose(CallPurpose.PLANNER):
        response = await self._runner.provider.chat_with_retry(
            model=spec.model,
            messages=[
                {"role": "system",
                 "content": render_template("agent/planner_router.md", strip=True)},
                {"role": "user", "content": user_content},
            ],
            tools=None,
            tool_choice=None,
        )
    if response.finish_reason == "error":
        return False
    return parse_router_verdict(response.content or "")
except Exception:
    logger.warning("Gray-zone router failed; defaulting to DIRECT", exc_info=True)
    return False
```

import 调整：`call_purpose`/`CallPurpose`（from erza.ledger）、`render_template`
（from erza.utils.prompt_templates）——若 planning.py 已有则复用。**禁止**
在模块顶层 import `erza.agent.planner`（保持 runner import-light 惯例，
Planner 仍是函数内延迟 import）。

### 2.4 `init_planner` 切换（保持签名不变）

```python
policy = getattr(spec, "planning_policy", None) or PlanningPolicy()
task_text = extract_task_from_messages(spec.initial_messages)
decision = policy.classify(task_text)
if decision.route is Route.GRAY:
    wants_plan = await self._classify_gray(spec, task_text)
    logger.info(
        "Planning route: gray (cause={}, llm={}) signals={}",
        decision.cause, "plan" if wants_plan else "direct", decision.signals,
    )
    if not wants_plan:
        return None, None, None, None
else:
    logger.info(
        "Planning route: {} cause={} signals={}",
        decision.route.value, decision.cause, decision.signals,
    )
    if decision.route is Route.DIRECT:
        return None, None, None, None
# PLAN 或 gray->plan：以下与现状完全一致（延迟 import Planner、
# create_plan、fallback 处理、plan.max_replans = policy.planner_max_replans）
```

- import：`from erza.agent.planning_policy import PlanningPolicy, Route`
  （替换现有只导入 PlanningPolicy 的行）。
- `should_plan` 门面保留但生产路径不再调用（W11-1 D8 达成切换）。
- docstring 更新：routing 描述改为 L1 三值 + L2 灰区裁决。

### 2.5 `tests/agent/test_planning_policy.py` 微调

W11-1 的 T20/T21（GRAY 等价 DIRECT 的用例）在本批仍通过（GRAY→L2 fail-open
时返回 DIRECT 行为）——**不改**，但需确认其 FakeProvider 的
`chat_with_retry` 返回 `content=""`：`parse_router_verdict("")` → False →
DIRECT，语义恰好自洽。若该文件用例通过 `init_planner` 间接驱动且行为变化，
在报告中列出并说明。

## 3. 测试要求（新文件 `tests/agent/test_gray_router.py`，一条不落）

沿用 FakeProvider/_spec 惯例（A8）。FakeProvider 需可编程：`chat_with_retry`
按调用序返回预设 `LLMResponse`（router 裁决 → 之后的 planner JSON）。

**3.1 裁决解析（parse_router_verdict 纯函数）**
- R1 `"PLAN"` / `"plan"` / `"Plan."` → True。
- R2 `"DIRECT"` / `"direct"` → False。
- R3 空串 / `"嗯好的"` / `"Let me think..."` → False（fail-open）。
- R4 `"I think we should plan this out"` → True（词边界，非首词也可）。
- R5 `"directly"` → False（`\bdirect\b` 不匹配 directly）。

**3.2 init_planner 路由集成（经 FakeProvider 全链路）**
- R6 GRAY 任务（如"写个注释说明这段代码"），router 返回 `"PLAN"`，第二次
  调用返回合法 plan JSON（复用 test_planner_results.py 的 plan JSON 样例
  形状）→ `init_planner` 返回 plan 非 None，`plan.max_replans == 3`。
- R7 同任务 router 返回 `"DIRECT"` → `(None, None, None, None)`，且
  `chat_with_retry` 只被调用 1 次（没有 planner 调用）。
- R8 强信号任务（编号列表 ≥2 行）→ **不经过 router**：`chat_with_retry`
  恰好 1 次（仅 planner）。
- R9 寒暄/单问句（DIRECT 路由）→ `chat_with_retry` 0 次调用。
- R10 `force_plan=True` → 0 次 router 调用（序 0 覆盖，直进 planner）。
- R11 router 调用抛异常 → fail-open DIRECT，返回全 None，不抛出。
- R12 router `finish_reason=="error"` → fail-open DIRECT。

**3.3 会话上下文（D3）**
- R13 多轮 messages（首条 user="重构认证模块，分三步：..."，末条 user="继续，
  把剩下两步做完"）：捕获 `chat_with_retry` 的 messages 参数，断言 user 消息
  含 `First user message: 重构认证模块` 与 `User messages so far: 2`。
- R14 `extract_session_context`：无 user 消息 → `(None, 0)`；首条 >200 字符
  → 截断为 200。

**3.4 记账与预算（D1）**
- R15 用 `bind_call_ledger`（见 `tests/ledger/test_turn_call_ledger.py` 惯例）
  绑定 ledger 后走 R6 全链路：ledger 记录中 **2 条** `CallPurpose.PLANNER`
  （router + planner），无 UNCLASSIFIED。

## 4. 禁改清单

- `erza/agent/planning_policy.py`（L1 已在 W11-1 定稿；发现其缺陷只记录报告）。
- `erza/agent/planner.py`、`erza/agent/runner.py`（A6 调用点签名未变，零改动；
  主循环接线属 W11-3）。
- `erza/ledger/`（不新增枚举值，D1）。
- `erza/agent/loop.py`、`erza/agent/loop_builder.py`、config 全部。
- 既有模板 `planner_system.md`/`planner_replan.md`。
- `tests/agent/test_planner_results.py` 等既有测试（A8 惯例文件不改；
  若因 init_planner 行为变化而失败，先核对是否 R6-R12 覆盖的预期行为——
  force_plan 用例必须全绿，失败即锚点不符，停止报告）。

## 5. 验收自检

1. GRAY 任务全链路：router PLAN → 规划；router DIRECT/失败 → 纯 ReAct。
2. 强信号/琐碎/force 路径零额外 LLM 调用（R8/R9/R10 断言调用次数）。
3. router 调用形状与 `Planner.create_plan` 一致（同 purpose、无新 kwargs）。
4. 每 turn 恰一条 `Planning route:` info 日志，含 route/cause/signals
   （gray 含 llm 裁决结果）。
5. 验证门全过（pytest / ruff check / ruff format --check 本批文件）。
6. W11-1 测试文件除 2.5 所述确认外零改动。
