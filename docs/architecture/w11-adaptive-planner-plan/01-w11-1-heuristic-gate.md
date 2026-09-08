# 任务书 W11-1：L1 启发式门重写（三值 classify）

> 系列：W11 自适应 Planner 路由。总览与全局红线见
> `docs/architecture/w11-adaptive-planner-plan/00-overview.md`（先读第 1、3 节）。
> 本批只做 L1：重写 `planning_policy.py` 的判定逻辑，输出从布尔升级为
> 三值 `RouteDecision`。**不改任何调用方**（D8：`should_plan()` 保留为门面），
> L2 接线在 W11-2。

## 1. 现状锚点（实施前逐条核对，任何一条不符立即停止报告）

| # | 锚点 | 位置（参考值） |
|---|------|----------------|
| A1 | `PlanningPolicy` dataclass，字段 `planner_max_replans: int = 3`、`force_plan: bool \| None = None`，方法 `should_plan(task_text) -> bool` | `erza/agent/planning_policy.py` L53-70 |
| A2 | `_looks_multi_step(task_text)` 私有函数，末尾两个分支都 `return False`（`_TRIVIAL_TASK_CHARS = 24` 守卫是死代码） | 同文件 L73-93 |
| A3 | 常量：`_TRIVIAL_TASK_CHARS=24`、`_LONG_TASK_CHARS=300`、`_MULTI_STEP_MARKERS`（16 项，含裸 `"再"`）、`_LIST_LINE_RE`、`_INLINE_NUMBERED_RE = re.compile(r"(?<!\d)\d+[.、)]\s+\S")` | 同文件 L25-50 |
| A4 | `should_plan` 的调用方仅一处生产代码：`PlanningReflectionService.init_planner`（`erza/agent/execution/planning.py` L121-124，`policy = getattr(spec, "planning_policy", None) or PlanningPolicy()` 后 `if not policy.should_plan(task_text): return None, None, None, None`） | `erza/agent/execution/planning.py` |
| A5 | 测试 `tests/agent/test_planning_policy.py`：FakeProvider/_spec 惯例，`should_plan` 系列用例（文件头 docstring 标注 P4） | `tests/agent/test_planning_policy.py` |
| A6 | 其余引用 `PlanningPolicy` 的测试：`test_planner_results.py`、`test_loop_progress.py`（L395 `force_plan=False`）、`test_progress_policy.py`、`tests/ledger/test_turn_call_ledger.py`（`force_plan=True`）、`test_lazy_tool_registry.py` | 各文件 |
| A7 | 全仓无 `Route`/`RouteDecision` 命名冲突 | `rg "class Route\b|RouteDecision" erza/` 零命中 |

## 2. 已知缺陷（本批修复的目标，测试要求与之对应）

| # | 缺陷 | 现状行为 | 期望行为 |
|---|------|----------|----------|
| F1 | 单行列表即触发 | 任意一行 `- `/`1.` 开头 → PLAN（贴 diff、行首年份误触发） | 行首列表需 **≥2 行**命中才 PLAN |
| F2 | 裸 `再` 计入步骤标记 | "先看下这个，然后再告诉我" → 然后+再 = 2 标记 → PLAN（误触发） | 词表删除裸 `再`；该例只剩 1 标记 → 非强信号 |
| F3 | 英文标记大小写敏感 | 句首 `Then` 不匹配 `then` | 英文标记用 word-boundary 正则 + casefold 匹配 |
| F4 | 长度阈值语言盲 | 300 字符 ≈ 英文 50-60 词（一段普通叙述即触发） | 语言感知有效长度（见 3.2），中文阈值不变 |
| F5 | trivial 守卫死代码 | `_TRIVIAL_TASK_CHARS` 无行为效果 | 删除或赋予真实语义（本批选择：寒暄/单问句桶，见 3.3） |
| F6 | 一句话宏目标漏判 | "给我做一个射击游戏" → 无信号 → 不规划 | 宏目标闸门（产出动词×成品名词）→ PLAN |

## 3. 改动方案

**唯一改动文件：`erza/agent/planning_policy.py`**（全量重写判定部分）+
**`tests/agent/test_planning_policy.py`**（重写用例）。不碰其他任何文件。

### 3.1 新增公共类型（文件顶部，PlanningPolicy 之前）

```python
class Route(str, Enum):
    """Per-turn planning route decided by L1 heuristics."""
    PLAN = "plan"      # straight to Planner.create_plan
    DIRECT = "direct"  # plain ReAct, zero planning overhead
    GRAY = "gray"      # undecidable by heuristics alone (L2 adjudicates)


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """L1 outcome: route + machine-readable audit cause/signals."""
    route: Route
    cause: str   # "forced" | "empty" | "strong_signal" | "macro_goal"
                 # | "verb_gate" | "trivial" | "no_signal"
    signals: dict[str, Any]  # 只放实测证据，如 {"list_lines": 3,
                             #  "effective_len": 412, "markers": ["然后", "最后"],
                             #  "verb": "做", "product": "游戏"}
```

### 3.2 常量改造

- `_LONG_TASK_CHARS = 300` 保留数值，新增语义：**有效长度**阈值。
  新增 `_effective_length(text) -> float`：CJK 字符（Unicode 区块
  `\u4e00-\u9fff`、`\u3400-\u4dbf`）权重 1.0，其余字符权重 0.4。
  中文 300 字不变；英文需 ~750 字符（≈125 词）才触发。
- `_LIST_LINE_RE` 保留；新增判定方式：`len(_LIST_LINE_RE.findall(text)) >= 2`
  （F1）。
- `_MULTI_STEP_MARKERS` 元组删除，替换为两个常量：
  - `_STEP_MARKERS_ZH: tuple[str, ...] = ("然后", "接着", "之后", "随后", "分步", "步骤", "最后")`
    （**删除裸 `再`**，F2）
  - `_STEP_MARKERS_EN: tuple[str, ...] = ("then", "after that", "afterwards", "next", "step 1", "step one", "first", "finally")`
  - 匹配：中文子串 `in text`；英文编译为单条 word-boundary 正则
    `\b(?:then|after\s+that|afterwards|next|step\s+1|step\s+one|first|finally)\b`
    对 `text.casefold()` 匹配（F3）。
  - 触发条件不变：**不同标记 ≥2 个**。
- `_INLINE_NUMBERED_RE` 不变（≥2 命中触发）。
- 新增词库常量（D7 封闭，**禁止实施时扩词**）：
  ```python
  _PRODUCTIVE_VERBS_ZH = ("做", "建", "搭", "开发", "实现", "制作", "设计", "写")
  _PRODUCTIVE_VERBS_EN = ("create", "build", "make", "write", "develop", "implement", "design")
  _PRODUCT_NOUNS_ZH = ("游戏", "网站", "网页", "系统", "应用", "平台", "引擎", "工具", "页面", "机器人", "客户端", "服务端")
  _PRODUCT_NOUNS_EN = ("game", "website", "web app", "system", "application", "app", "platform", "engine", "tool", "bot", "client", "server")
  _GREETINGS = ("你好", "您好", "嗨", "哈喽", "谢谢", "感谢", "多谢", "好的", "好嘞", "收到", "明白了", "ok", "okay", "thanks", "thank you", "got it")
  ```
  英文词匹配一律 casefold + word-boundary；中文子串匹配。
- `_TRIVIAL_TASK_CHARS = 24` 语义变更：寒暄桶的长度上限（3.3）。

### 3.3 `classify` 决策表（求值顺序严格如下）

`PlanningPolicy.classify(task_text: str | None) -> RouteDecision`：

| 序 | 条件 | 结果 route / cause |
|----|------|--------------------|
| 0 | `self.force_plan is True` → PLAN / `forced`；`is False` → DIRECT / `forced` | 覆盖优先 |
| 1 | 空或纯空白 → DIRECT / `empty` | |
| 2 | **强多步信号**（任一）：列表行 ≥2；行内编号 ≥2；`_effective_length ≥ 300`；不同步骤标记 ≥2 | PLAN / `strong_signal`（signals 记录命中的具体项与值） |
| 3 | **宏目标**：产出动词与成品名词同时出现（简单共现，不做句法分析） | PLAN / `macro_goal`（signals 记 verb 与 product） |
| 4 | **产出动词**单独出现（无成品名词） | GRAY / `verb_gate`（"写个注释"类小活交给 L2 裁决） |
| 5 | **寒暄/致谢**：`_effective_length < 24` 且命中 `_GREETINGS` 任一（startswith 或全文等值，不匹配句子中间） | DIRECT / `trivial` |
| 6 | **单问句**：不含句分隔符（`。；;` 与换行）且以 `？/?/吗/呢` 结尾（去尾部空白后） | DIRECT / `trivial` |
| 7 | 其余一切 | GRAY / `no_signal` |

要点：
- 序 2 在序 3/4 之前（带列表的动词语任务不必进灰区，直接 PLAN 省一次 L2）。
- 序 4 在序 5/6 之前（**"短 ≠ 小"**：`写个游戏`若被序 5/6 先截走会漏判，
  必须让动词闸门先兜住）。
- 序 5 寒暄匹配限定 startswith/等值：`"你好，请帮我..."` 不算寒暄（有实际
  任务跟随），落入序 7 GRAY。
- `signals` 只放本条命中的证据（列表行数、有效长度、标记列表、动词/名词），
  不放全量扫描结果。

### 3.4 `should_plan` 门面与 `_looks_multi_step` 处置

```python
def should_plan(self, task_text: str | None) -> bool:
    """Compat facade: True only when L1 routes PLAN (GRAY awaits L2 in W11-2)."""
    return self.classify(task_text).route is Route.PLAN
```

- `_looks_multi_step` **删除**（其逻辑被序 2 吸收）。
- 模块 docstring 重写：三值路由 + 决策表摘要（保持英文风格与现文件一致）。
- **不改** `PlanningPolicy` 字段、不改 `execution/planning.py`、不改任何
  其他调用方（A4/A6 的调用在 L1-only 阶段语义变化仅为：原误触发/漏判样本
  按新表路由；GRAY 在本批等价于 DIRECT，因为 `should_plan` 只认 PLAN）。

### 3.5 决策日志（本批起生效，D6）

`classify` 内不加日志（纯函数）；在 `should_plan` 门面内加一条：

```python
decision = self.classify(task_text)
logger.debug("Planning route L1: {} cause={} signals={}",
             decision.route.value, decision.cause, decision.signals)
```

loguru `logger` 需在文件头引入（当前文件没有）。debug 级别：正常运营零噪音，
排查时开 DEBUG 即得全量审计流。

## 4. 测试要求（全部落在 `tests/agent/test_planning_policy.py`，重写该文件）

沿用现有 FakeProvider/_spec 惯例（A5）；`should_plan` 兼容用例与 `classify`
新用例并存。逐条实现，一条不落：

**4.1 兼容性（should_plan 门面）**
- T1 `force_plan=True/False` 覆盖 `classify` 一切规则。
- T2 空字符串 / None / 纯空白 → DIRECT。
- T3 带编号列表 ≥2 行（中英文各一）→ PLAN（cause=strong_signal）。
- T4 行内编号 `1. read 2. write 3. commit` → PLAN。
- T5 中文 ≥300 字任务 → PLAN；英文 ~800 字符叙述 → PLAN（有效长度 320>300）。
- T6 英文 60 词普通段落（~350 字符，有效长度 ~140）→ 非 PLAN（GRAY）。

**4.2 缺陷修复回归（对应 F1-F4）**
- T7 单行 `- 好的没问题` → 非 PLAN（F1）。
- T8 `git diff` 片段（含多行 `- ` 删除行但无其他信号、有效长度 <300）→
  非 PLAN（F1）。注意：若 diff 长度超 300 有效字符仍会 PLAN——这是长度规则
  的正确行为，测试样本控制在阈值下。
- T9 "先看下这个，然后再告诉我" → 非 PLAN（F2：裸 `再` 已删，只剩 `然后` 1 标记）。
- T10 "查一下A，然后对比B，最后给结论" → PLAN（3 个中文标记 ≥2）。
- T11 "First, read the file. Then fix it. Finally, run tests." → PLAN
  （F3：句首 First/Then/Finally casefold+word-boundary 命中 3 标记）。
- T12 "Then we left." → 非 PLAN（单标记）。

**4.3 宏目标闸门（F6）**
- T13 "给我做一个射击游戏" → PLAN（macro_goal，verb=做 product=游戏）。
- T14 "build me a snake game" → PLAN（build+game）。
- T15 "做个小工具" → PLAN（做×工具）。

**4.4 灰区与琐碎**
- T16 "写个注释说明这段代码" → GRAY（verb_gate：有 `写` 无成品名词）。
- T17 "你好" / "thanks" / "好的，收到" → DIRECT（trivial）。
- T18 "这个配置项是干嘛的？" / "what does this flag do?" → DIRECT（单问句）。
- T19 "你好，帮我看看今天日程" → GRAY（寒暄非 startswith 独占，落 no_signal）。
- T20 "查一下这个订单为什么没发货，对比库存和物流记录再下结论" → GRAY
  （无 ≥2 标记、无产出动词、非问句——本批等价 DIRECT，W11-2 起 L2 接管）。
- T21 "跑一下测试" → GRAY（`跑` 非产出动词）。

**4.5 signals 审计**
- T22 强信号用例断言 `signals` 含正确键值（如 `list_lines==3`、
  `markers==["然后","最后"]`、`verb`/`product`）。
- T23 所有 `RouteDecision` cause 值 ∈ 第 3.3 节枚举集合。

**4.6 既有用例处置**
- 原 `should_plan` 系列用例中与新决策表**冲突**的（若有：原文写着预期
  True/False 而新表给出不同路由）：允许更新预期值，但**每处在批次报告中
  逐条列出**（旧预期 → 新预期 → 依据的决策表序号）。
- A6 列出的其他测试文件**预期零改动**（它们全部显式 `force_plan`，被序 0
  覆盖）。若跑出失败，属锚点不符，停止报告。

## 5. 禁改清单

- `erza/agent/execution/planning.py`（init_planner 接线属 W11-2）。
- `erza/agent/planner.py`、`erza/agent/runner.py`、`erza/agent/loop.py`、
  `erza/agent/loop_builder.py`。
- `erza/config/schema.py`、`erza/config/loader.py`（无新配置项，红线 2）。
- `erza/ledger/`（无新 CallPurpose，D1）。
- `erza/templates/`（L2 模板属 W11-2）。
- A6 全部测试文件（除 `test_planning_policy.py`）。
- `plan_snapshot.py` 的 origin 值域。

## 6. 验收自检（批次报告逐项 √）

1. `classify` 决策表 8 行全部实现且求值顺序与 3.3 一致。
2. F1-F6 全部有对应回归测试（T7-T15）且通过。
3. `should_plan` 门面签名与返回语义不变，A4/A6 调用方零改动零失败。
4. 词库与常量严格等于 3.2 列表（未扩词）。
5. 验证门全过（pytest / ruff check / ruff format --check 本批文件）。
6. 既有用例更新逐条列于报告；无未列出的测试改动。
