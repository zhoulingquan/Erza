# W10-C5：前缀字节稳定性守卫测试（锁死成果）

> 前置依赖：W10-C2、W10-C3、W10-C4 已合并。
> 本批零生产代码，纯测试：把 reasonix 的"指纹校验"思想落成 CI 回归门——
> 任何未来改动只要悄悄破坏前缀稳定性，测试立刻红。

## 一、背景

reasonix 的 `ImmutablePrefix.verifyFingerprint()`（SHA-256 前缀指纹，漂移即告警）
是其 99.8% 命中率能长期保持的护栏。Erza 的对应物是一组字节级回归断言：
不引入运行时指纹机制（那是给常驻进程用的），而是在测试里直接构造"连续两轮"
的完整消息列表，断言前缀逐字节相等。

## 二、测试设计

新建 `tests/architecture/test_prompt_prefix_stability.py`
（`tests/architecture/` 目录已存在，放架构级守卫测试正合适）。

### 2.1 测试装置

- fake provider（跟随 `tests/agent/` 现有 fake 惯例）、真实 `ContextBuilder` +
  临时 workspace、真实 `Session` + `SessionManager`（内存态）。
- 场景构造函数：`_run_turn(session, workspace, user_text)` —— 走
  `_build_initial_messages`（或直接组装等价调用：`context.build_messages` +
  C2 盖章 + C3/C4 轮界步骤），返回本轮发给 LLM 的完整消息列表
  `[system, *history, user]`（把 runtime context 等尾部块一并算入——
  尾部允许变化，断言只针对前缀）。

### 2.2 必测用例

1. **system 跨轮字节不变**：turn 1 与 turn 2 的 `messages[0]` 完全相等
   （含工具结果回放、notes 变化、召回结果不同三个变量同时存在时）。
2. **历史前缀保序追加**：turn 2 消息列表的前 N 条（N = turn 1 列表长度减去
   尾部 user 消息）与 turn 1 的对应消息逐条相等——即轮与轮之间历史只追加、
   不改写。
3. **轮内迭代前缀稳定**：同一轮内两次"迭代"（模拟工具结果追加后重建
   messages_for_model）——`govern_messages` 输出对既有消息零改写
   （GREEN 压力下；断言 `messages_for_model[:k] == messages[:k]`，
   k 为迭代前长度）。
4. **工具定义稳定**：`tools.get_definitions()` 两次调用 JSON 序列化相等
   （锁死注册顺序确定性）。
5. **归档折叠不伤前缀**：构造触发 `maybe_consolidate_by_tokens` 的长会话，
   归档前后：system 不变；摘要消息插入点之前的消息逐条不变；
   插入点之后首条为 `[Archived Context Summary]` 消息。
6. **轮界压缩确定性**：长会话两轮之间，轮界压缩只把跨窗口的旧工具结果
   变为占位且落库——第二轮回放与第一轮压缩后的落库状态逐条相等
   （不再二次改写）。
7. **召回/notes 尾部化**：连续两轮，recall 结果与 notes 内容变化时
   仅最后一条 user 消息受影响（system 与历史前缀不变——与用例 1 合并覆盖）。

### 2.3 失败信息友好

断言失败时输出两轮 system 的首个差异位置（`difflib` 或首异字节索引），
让破坏者一眼看到从哪一字节开始漂移。

## 三、测试要求

- 上述 7 个用例全部落地，单个文件内自洽，不依赖网络/真实 LLM。
- 现有全部测试零修改（本批纯增）。

## 四、禁改清单

- **零生产代码改动**——若发现必须改生产代码才能让测试通过，立即停止并报告
  （这说明 C2/C3/C4 有缺陷，回主会话裁决）。

## 五、验收自检

1. 7 个用例全部就位且全绿
2. 失败时能输出首个差异字节位置
3. 零生产代码改动
4. 验证门通过
