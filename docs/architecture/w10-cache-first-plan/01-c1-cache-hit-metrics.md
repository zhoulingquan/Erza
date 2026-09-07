# W10-C1：缓存命中率指标（修复记账键 + 统计与展示）

> 前置依赖：tag `baseline-pre-w10`。
> 本批是纯观测批：不加任何缓存优化，只把"命中了多少"这件事修准、算出来、亮出来。

## 一、问题（现状锚点，已逐一核实）

### 锚点 1：记账键不一致（潜伏 bug）

- `erza/providers/openai_compat_provider.py` 的 `_extract_usage`（约 946-993 行）把各厂商
  缓存字段归一化到 **`cached_tokens`** 单一键。优先链：
  `prompt_tokens_details.cached_tokens`（OpenAI/智谱/MiniMax/Qwen/Mistral/xAI）
  → `cached_tokens`（StepFun/Moonshot 顶层）→ `prompt_cache_hit_tokens`（DeepSeek/SiliconFlow）。
- `erza/agent/execution/model_request.py` 的 `usage_dict`（232-241 行）原样透传 usage 键。
- `erza/ledger/turn_budget.py` 的 `TurnBudget.accumulate`（62-89 行）读的是
  **`prompt_cache_hit_tokens`**——归一化后的 usage dict 里根本不存在这个键，
  所以 `cache_hit` 分支永远不生效，命中率记账恒为 0。
- CallLedger（`erza/ledger/call_ledger.py`）的 `total_usage` / `purpose_usage` / `records`
  走 `record()`，键是归一化后的 `cached_tokens`——这条链是对的，但没人消费它算比率。

### 锚点 2：已存在的展示点

- `erza/agent/progress_hook.py` `after_iteration`（155-178 行）：debug 日志
  `"LLM usage: prompt={} completion={} cached={}"`，已读 `cached_tokens`。
- `erza/session/webui_turns.py` `handle_turn_end`（305-336 行）：websocket 渠道 turn_end
  metadata 的 `context_usage` 里已含 `cached_tokens`。

## 二、改动

### 2.1 TurnBudget 修复与扩展（erza/ledger/turn_budget.py）

1. `accumulate()` 的缓存命中读取键改为 `cached_tokens`（原 `prompt_cache_hit_tokens`
   分支删除——归一化链已保证单一键；docstring 的 recognized keys 同步更新）。
   注意保留既有"cache stats 单独上报时 miss 计入 input"的语义：DeepSeek 风格
   `prompt_tokens` 为 0 且 `prompt_cache_hit_tokens`/`prompt_cache_miss_tokens` 分列时，
   `prompt = cache_miss`——这段逻辑不动，只把命中累计的键改对。
2. 新增字段 `used_cache_hit: int = 0`（dataclass 字段，`accumulate` 里从
   `cached_tokens` 累加；上述单独上报分支里从 `prompt_cache_hit_tokens` 累加）。
3. 新增方法：

```python
def cache_hit_ratio(self) -> float | None:
    """Return cached/(cached+miss) ratio, or None when no input recorded."""
    if self.used_input <= 0:
        return None
    return self.used_cache_hit / self.used_input
```

4. `summary()` 输出追加 `cache=NN%` 段（`ratio is None` 时不追加）。

### 2.2 每回合命中汇总（erza/agent/runner.py）

`AgentRunState` 有 `last_call_usage` 与累计 `usage`（163、187 行附近）。在回合收尾处
（`_run_with_ledger` 结束、`result_last_usage` 组装附近，约 587-600 行）把回合累计
`cached_tokens` 与 `prompt_tokens` 一并放入既有 usage 通道（`accumulate_usage` 已透传
任意键，无需额外机制——若 `state.usage` 已含 `cached_tokens` 则确认透传到
`result.usage` 即可，不重复造字段）。

### 2.3 turn_end 展示（erza/session/webui_turns.py）

`handle_turn_end`（305-336 行）的 `context_usage` blob 追加一个键（现状
`context_usage` 来自调用点 dispatch.py 328-334 行的 `last_call_usage`——本批
不改调用链，比率即"末次调用"口径，够用且纯增量）：

```python
"cache_hit_ratio": round(
    context_usage.get("cached_tokens", 0)
    / max(1, context_usage.get("prompt_tokens", 0)),
    4,
),
```

（provider 未上报 cached_tokens 时该值为 0.0，前端不特殊处理。）

### 2.4 progress_hook 日志升级（erza/agent/progress_hook.py）

`after_iteration` 的 debug 日志追加 `ratio=`，单调用级：
`cached / max(1, prompt)` 保留 2 位小数。仅改日志格式，无新事件。

### 2.5 不改的部分

- `_extract_usage` 的归一化优先链——原样。
- CallLedger 的记账结构——原样（其 `total_usage["cached_tokens"]` 天然可用）。
- webui 前端（`erza/web/dist`）——本批不碰前端。
- CLI `/status` 命令——本批不扩展（命中比率已进 turn_end metadata 与日志）。

## 三、测试要求

扩展 `tests/ledger/test_turn_call_ledger.py`（TurnBudget/CallLedger 的既有测试文件；
同目录 `test_call_ledger.py` 为另一部分，确认不回归）：

1. `accumulate({"prompt_tokens": 1000, "completion_tokens": 10, "cached_tokens": 800}, "m")`
   → `used_cache_hit == 800`，`used_input == 1000`，`cache_hit_ratio() == 0.8`。
2. `accumulate` 两笔（800 命中 + 200 全 miss）→ 比率正确加权（1000/2000=0.5）。
3. 无输入时 `cache_hit_ratio() is None`；`summary()` 无 cache 段。
4. 有命中时 `summary()` 含 `cache=` 与百分比字样。
5. DeepSeek 单独上报分支：`{"prompt_tokens": 0, "prompt_cache_hit_tokens": 300,
   "prompt_cache_miss_tokens": 100, "completion_tokens": 5}` → `used_input == 100`、
   `used_cache_hit == 300`、比率 0.75。
6. 回归：既有预算超限测试全部不动、全绿。

另加一条集成级断言（`tests/providers/` 下既有 provider 测试文件中扩展）：
`_extract_usage` 产出的 dict 传给 `TurnBudget.accumulate` 后 `used_cache_hit` 非零
（锁死"记账键 = 归一化键"这一契约，防止未来再漂移）。

## 四、禁改清单

- `_extract_usage` 归一化优先链及其三个数据源注释。
- CallLedger 的绑定/子任务权限机制。
- TurnBudget 的预算判定逻辑（`check()`）与超限语义。
- 任何提示词构造、消息构造代码（那是 C2/C3 的事）。

## 五、验收自检

1. `TurnBudget.accumulate` 读 `cached_tokens`，旧键分支删除
2. `used_cache_hit` / `cache_hit_ratio()` / `summary()` cache 段就位
3. webui turn_end `context_usage` 含 `cache_hit_ratio`
4. progress_hook debug 日志含 ratio
5. 新测试 6 条全绿；既有测试零修改
6. 验证门通过（pytest 全绿 + ruff 零告警）
