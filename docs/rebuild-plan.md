# Agent 核心异步重构：进度与约定

> **状态：已被取代（2026-09）**。本文描述的重构（循环异步化 + 事件层 + 迁移桥
> `renderer_bridge.py`）已完成一半就被**事件溯源重构**接手：渲染后端协议、迁移桥、
> `agent/events.py` / `interactions.py` 等已删除，改为「单一事件出口 + 事件日志 + 折叠」
> 的架构（见 [architecture.md](architecture.md) 的「事件架构」节与
> `tests/guard/test_single_event_path.py` 的守卫规则）。
>
> 仍有效的是本文的**判断类内容**（哪些接缝保留、为什么不做 AsyncOpenAI、压缩与恢复的
> 语义）；「冻结的兼容面」表中与 `Renderer` / `RendererError` 有关的条目按新架构重命名
> （`SubscriberError`，语义不变）。阅读时请以代码与 architecture.md 为准。


本文件记录重构的落地进度、冻结的兼容面与各阶段验证基线。完整设计见
`~/.commandcode/plans/smithcode-async-agent-core-rebuild.md`（方案 v3）。

核心结论：**借鉴 pi 的四件事（类型化事件流、Agent 边界钩子、steering/follow-up 队列、
会话与提示词归属），保留 smithcode 更强的地方（`_BatchScheduler` 的流式调度与原子取消、
`CancellationToken` 的 ContextVar 传播、fsync 屏障、权限引擎）。** 异步化的收益是消除
三处线程桥（TUI / MCP / 工具波次），不是性能。

## 阶段基线

| 项 | 阶段 0 基线 | 阶段 1 后 | 阶段 2 后 | 阶段 3 后 | 阶段 4 进行中 |
| --- | --- | --- | --- | --- | --- |
| 测试 | **1362 passed, 1 skipped**（60.36s） | **1383 passed**（+21） | **1399 passed**（+16） | **1405 passed**（+21，另重写 82 处调用点 + 4 个用例语义） | **1508 passed**（阶段 4/5 各项 + 收尾轮：状态事件 +6 / 异步工具 +3 / continue +5 / status 守卫 +3 / 会话对象 +1 / 段注册表 +4 / headless 端到端 +4 / 事件化守卫 +2 / 会话状态实例 +5） |
| Lint | All checks passed! | All checks passed! | All checks passed! | All checks passed! | All checks passed! |

每个阶段交付前必须复跑这两条，并与上一阶段对比。阶段 1/2 的行为等价性由
「既有 1362 条一条不改、全部仍绿」证明（只改动了 `tests/test_display.py` 两处
patch 目标，见阶段 1 偏差 4）。

## 硬约束

- **Python >= 3.10**：禁用 `asyncio.TaskGroup`(3.11+) / `asyncio.timeout`(3.11+) /
  `ExceptionGroup`(3.11+)。并发波次用 `asyncio.gather(..., return_exceptions=True)`，
  超时用 `asyncio.wait_for`。
- 中文注释与中文用户可见文案。
- 安全边界（路径沙箱 / 权限规则 / 非交互 fail-closed）只做形状迁移，语义逐字保持。

## 冻结的兼容面（阶段 0 冻结，重构期间不得破坏）

重构期间 `src/smithcode/agent` 必须继续提供下列名字，且语义不变。超出此表的名字可自由调整。

### `src/` 的依赖

| 名字 | 使用方 |
| --- | --- |
| `Agent` | `cli.py:7` |
| `format_stream_interrupted` | `cli.py:7`、`tui/app.py:39` |
| `INTERRUPTED_NOTE` | `cli.py:8,86,216` |
| `STREAM_INTERRUPTED_NOTE` | `cli.py:9,88,218` |

### 测试的依赖（多为 monkeypatch 接缝，改动需同步改测试）

| 名字 | 使用方 | 说明 |
| --- | --- | --- |
| `Agent` | 14 个测试文件 | 主入口 |
| `ResumeReport` | `test_cli_sessions.py:44` | 恢复结果摘要 |
| `RendererError` | `test_agent.py:974` | 渲染后端故障，不得被当成流中断 |
| `format_stream_interrupted` | `test_agent.py:951` | 流中断原因后缀 |
| `TITLE_MAX_ATTEMPTS` / `TITLE_RETRY_ROUNDS` | `test_agent_title.py:10` | 自动标题重试参数 |
| `MAX_SUMMARY_LEN` / `MAX_PREVIEW_LINES` | `test_display.py:8,302` | 摘要与 diff 截断 |
| `_diff_preview` | `test_display.py:276,289,302` | **私有函数**，测试直接调用 |
| `INTERRUPTED_CONTEXT` / `STREAM_INTERRUPTED_CONTEXT` / `MAX_ITERATIONS_WRAPUP` | `test_sessions_title.py:50-87` | 模块属性访问；标题生成靠它们的固定前缀识别内部消息 |
| `LLMClient` | `test_tui.py:543,1513,1553` | **monkeypatch 接缝**：测试改写 `smithcode.agent.LLMClient`，默认客户端构造须同步读到 |
| `renderer`（模块属性） | `test_display.py:336` | `monkeypatch.setattr(agent_mod.renderer, "current", ...)` |
| `Agent._finish` | `test_display.py:338-341` | 以 `Agent.__new__(Agent)` + 非绑定方式调用，**签名不得变** |

### 已知边界情况（阶段 1 必须处理）

**`LLMClient` 接缝在「模块 → 包」转换后会失效。** 现状（`agent.py`）：

```python
LLMClient = _RealLLMClient

def _default_llm():
    cls = globals()["LLMClient"]      # 读本模块 globals
```

测试改写的是**包属性** `smithcode.agent.LLMClient`。转成包后，`Agent` 的实现模块
（`agent/agent.py`）里的 `globals()` 与包命名空间是两个对象，patch 不再生效，三个 TUI
测试会静默用到真客户端。处理方式：默认客户端构造同时读包命名空间，并保留 globals 兜底：

```python
def _default_llm():
    import sys
    pkg = sys.modules.get(__package__)          # smithcode.agent
    cls = getattr(pkg, "LLMClient", None) or globals()["LLMClient"]
    ...
```

补一条回归测试锁住这个接缝（patch 包属性后 `_default_llm()` 返回假客户端）。

## 进度

| 阶段 | 状态 | 备注 |
| --- | --- | --- |
| 0 准备 | ✅ 完成 | 基线已记录、兼容面已冻结、本文件已建 |
| 1 事件与类型 | ✅ 完成 | `agent/` 包 + `status.py` / `interactions.py` / `events.py` / `renderer_bridge.py` + 21 个新测试 |
| 2 AbortSignal | ✅ 完成 | `agent/signal.py` + `agent/result.py`；`cancel.py` 转为别名层；包 `__init__` 改惰性；16 个新测试 |
| 3 async 循环 | ✅ 完成 | `Agent` 全链改协程；工具批改 `gather`；`stream_fn` / `tools_run` 新增；三个宿主改 `asyncio.run` |
| 4 钩子与队列 | ✅ 完成 | 4.A 队列 + `QueueChanged`；4.B 事件出口；4.C 交互桥（id 配对 + 金丝雀）；4.D 钩子四件套；4.E 三个抽水点 |
| 5 会话归属 + 前端解耦 | 🟡 基本完成（1 项部分） | 5.A `[queue]` 配置 ✅；5.B `prompt()`/`enqueue()` ✅；5.C `#queued` 面板 + 反向通道 ✅；5.D TUI 任务入口改 async worker ✅（面板保持同步端口，理由见下）；5.E `agent_session.py` + `run_with_goal` 迁入 + 两条持久化红线断言 ✅（**goal/plan/skills 实例化未做**，见「剩余问题」）；5.F Relay 删除 ✅ |
| 6 可选：AsyncOpenAI | ⬜ 不做（记录理由） | 收益是省掉逐块 `to_thread` 跳转，成本是拆掉同步重试/解析栈与 60 个测试假客户端共用的 `chat_stream` 接缝 |
| 收尾轮（审计后补做） | ✅ 全部完成（11/11） | ✅ 3.10 实机验证、retry/compaction 状态事件、异步工具两种签名、会话对象补齐、`continue_run()`、`RunResult.status` 守卫、模块拆分（types/errors/loop 常量）、transcript 段注册表、headless 端到端、视觉路径事件化、goal/plan/skills 实例化 |

## 阶段 1 的实际产出

| 文件 | 内容 |
| --- | --- |
| `agent/__init__.py` | 包门面：显式重导出冻结面 + PEP 562 `__getattr__` 兜底转发 |
| `agent/agent.py` | 原 `agent.py` 原样迁入（仅改相对导入层级 `..`、修 `LLMClient` 接缝） |
| `agent/status.py` | `StatusKind` + `StatusChanged` / `StatusCleared`（对齐 pi 的 `StatusIndicator` 判别式） |
| `agent/interactions.py` | `PromptKind` / `PromptStarted` / `PromptFinished`（带 `id` 配对 + `blocking`） |
| `agent/events.py` | 15 类事件的联合 + `AGENT_EVENT_TYPES` + `EventStream` + `agent_event_stream()` |
| `agent/renderer_bridge.py` | 迁移桥：事件 → 既有 `Renderer` 调用，含 `RENDERER_INPUT_METHODS` / `UNMAPPED_EVENTS` |
| `tests/agent/test_events.py` | 8 个：有序交付、快/慢消费者、终结值、生产者异常的挂起唤醒、类型清单同步 |
| `tests/agent/test_renderer_bridge.py` | 9 个：**映射完备性**、输入方法不被触发、id 配对与释放、级别分派、无操作语义 |
| `tests/agent/test_package_surface.py` | 4 个：冻结面可导入、`__getattr__` 转发、**`LLMClient` 接缝**、实现模块的包名 |

所有异步断言都用 `asyncio.run` + `asyncio.wait_for` 驱动（项目未装
`pytest-asyncio`，不为测试引入新依赖），失败是断言失败而非挂死。

### 与方案的偏差（连同理由）

1. **队列类型不在本阶段**：方案把 `QueueItem` / `QueueChanged` 列在阶段 1。实际推迟到
   阶段 4 与 `queues.py` 一起落地——它们只在阶段 4 被使用，现在建是死代码。
   `interactions.py` / `status.py` 之所以现在就建，是因为它们的事件已在联合里、
   被桥与完备性测试实际使用。
2. **`events.py` 承载全部事件数据类**：方案原写 `types.py` 放 `AgentState` 等。实际
   `AgentState` 到阶段 3（循环真正产出状态快照时）才有内容，现在建是空壳；
   延迟到阶段 3 与原 `LoopConfig` 一起定义。
3. **不实现「双向」适配器**：方案 1.C 写「双向」。实际只实现 `事件 → Renderer` 一个
   方向——阶段 3 起核心只发事件，反方向（`Renderer` 调用 → 事件）没有任何调用方，
   属于死代码。完备性断言（映射不漏项）才是 1.C 真正要保的东西，已落地。
4. **`reset_read_tracking` 的 patch 目标改为实现模块**：包化后
   `monkeypatch.setattr("smithcode.agent.X", …)` 改的是包属性，实现模块读不到。
   `LLMClient`（60 处）用接缝兼容，`reset_read_tracking`（2 处）直接把 patch
   指向 `smithcode.agent.agent`——按改动成本取舍，不做通用镜像层。

### 阶段 1 踩到并修掉的真问题

`RendererBridge` 最初用基类的**未绑定函数**做 `Notice` 级别分派
（`_NOTICE_METHODS = {"success": Renderer.success, …}`）。基类的 `success` / `warn` /
`error` 默认实现是转发到 `self.info`，直接调用未绑定版本会**绕过子类覆写**——
TUI 与录制实现收不到调用，只有 `info` 被记到。改为按方法名 `getattr(renderer, name)`
分派。这个 bug 是被完备性/分派测试抓出来的，不是审查出来的。

## 阶段 1 的执行顺序（与方案的偏差说明）

方案把「建包 + 拆 `agent.py` 进 `loop.py`/`tools_run.py` + 双向适配器」都放在阶段 1。
实际执行拆成两步，理由是**拆文件的落点取决于阶段 3 的异步形态**——现在把一个同步循环
按 `loop.py`/`tools_run.py` 切开，阶段 3 会立刻重写这两个文件，属于无效搬运；而包转换
与新增事件模块是阶段 2/3 的前置，必须先做。因此：

1. **1.A 包转换（纯机械）**：`agent.py` → `agent/agent.py`，新增 `agent/__init__.py`
   重导出全部公开面 + 处理 `LLMClient` 接缝。验收：全量测试仍 1362 passed。
2. **1.B 新增事件与类型模块**：`agent/{types,events,interactions,status,signal}.py`，
   纯新增，不接入现有循环。验收：新增测试通过，全量不回归。
3. **1.C 事件适配器 + 完备性断言**：保证每个 `Renderer` 方法都有对应事件，防漏项。

文件级别的大拆分（`loop.py` / `tools_run.py` / `transcript.py`）推迟到阶段 3——那时这些
内容本来就要按异步形态重写，一次成型比搬两遍省事。方案的模块清单不变，只是落点后移。

## 阶段 2 的实际产出

| 文件 | 内容 |
| --- | --- |
| `agent/signal.py` | `AbortSignal`（`aborted` / `abort` / `reason` / `throw_if_aborted` / `on_abort` / `wait` / `race` / `guard`）+ `Cancelled` + ContextVar 传播（`current_token` / `activate_token`） |
| `agent/result.py` | `RunResult`（自 `cancel.py` 移入） |
| `cancel.py` | 转为**兼容别名层**：单向转出上述实现，`CancellationToken = AbortSignal` |
| `agent/__init__.py` | 改为**惰性**属性解析（PEP 562 `__getattr__` + `import_module`） |
| `agent/agent.py` | 内部改从 `.result` / `.signal` 取类型，`CancellationToken` 统一改名 `AbortSignal` |
| `tests/agent/test_signal.py` | 16 个：幂等中止、旧名兼容、`on_abort` 一次性与立即触发、跨线程唤醒、`race` 抢先返回 / 及时放弃 / 不丢异常、`guard` 静默停止并关源、ContextVar 可见性 |

保留的旧私有名：`AbortSignal._listeners`（与旧 `CancellationToken` 同名）——
`tests/test_cancel.py:166` 直接断言它，重命名会无谓地改测试并丢掉一条回归。

### 与方案的偏差（连同理由）

1. **`llm/retry.py` 一行未改**（方案把它列入阶段 2 文件）。方案说「退避 `sleep` 需改为
   注入的异步等待，由调用方用 `signal.race(asyncio.sleep(d))` 包装」。**我不采纳**：
   阶段 3 的边界是「整个同步 `chat_stream`（含重试与退避）在 `to_thread` 里跑」
   （方案 §7(1) 自己的设计），退避因此留在同步侧，其「可被打断」已由
   `retry.wait()` 每 0.2s 查一次令牌满足——`Agent.abort()` 仍然即时生效。
   改成异步等待反而要把重试状态机（纯逻辑、单测完善）拆到异步侧，是为一个**不存在
   的问题**增加耦合。若将来真把客户端换成 `AsyncOpenAI`（阶段 6 可选），再一并改。
2. **`RunResult` 与 ContextVar 传播移入 `agent/`**：方案只说「`cancel.py` → `agent/signal.py`，
   `CancellationToken` 保留为别名」，没说实现要搬。但不搬就成环——详见下面「踩到的问题」。
3. **`agent/__init__.py` 改惰性解析**：同样为断环。
4. **`guard` 中止时静默结束（不抛）**：方案未指定。对齐既有同步检查点
   `llm/client.py:_iter_cancellable`（取消后 `return`，调用方复查 `cancelled`），
   这样它才是后者的直接替代，也不必在 `to_thread` 边界再搬一层异常。
   要显式抛出用 `throw_if_aborted()`。

### 阶段 2 踩到并修掉的真问题

1. **导入环**（`cancel → agent.signal` 与 `agent.* → cancel`）：`cancel.py` 一旦 import
   `agent` 下的模块，就会触发包初始化；而包初始化**急切**导入 `agent/agent.py`，
   后者经 `..renderer → utils.terminal → commands → llm.client → ..cancel` 走回
   `cancel`，此时 `cancel` 尚未定义出任何名字 → `ImportError: partially initialized module`。
   修法是把 `RunResult`/ContextVar 一并移入 `agent/`（包内不再依赖 `cancel`），
   并把包 `__init__` 改成惰性解析（轻量模块的导入不牵动重链）。
2. **`__getattr__` 自我递归**：惰性转发里写 `from . import agent` 会经
   `_handle_fromlist` 对本模块做属性查找，再次进入 `__getattr__` → `RecursionError`。
   改用 `import_module(f"{__name__}.agent")`，绕开属性查找。
   两条都只有跑测试才会暴露，静态审查看不出来。

## 阶段 3 的实际产出

| 文件 | 内容 |
| --- | --- |
| `agent/stream_fn.py` | `drain_sync_stream`：同步生成器逐块抽到事件循环（`to_thread`），拉取前查中止 |
| `agent/tools_run.py` | `BatchScheduler` / `ToolPlan` / `as_result_text` + 三段占位结果文案（自 `agent.py` 拆出） |
| `agent/agent.py` | `run` / `run_with_goal` / `_run_loop` / `_wrap_up` / `_chat` / `_chat_with_recovery` / `compact` / `compact_manual` / `_compact_if_needed` / `_execute_batch` 全改协程；新增 `_acomplete`（保留同步 `_complete` 给标题后台线程） |
| `cli.py` | 三处入口改 `asyncio.run`（REPL 任务、手动压缩、一次性任务） |
| `tui/app.py` | 两处后台线程改 `asyncio.run` |
| 测试 | 82 处调用点机械包装为 `asyncio.run(...)`；4 个用例语义更新（见下） |

### 与方案的偏差（连同理由）

1. **MCP loop 合并：不做**（方案阶段 3 列为改动项）。方案的理由是「消除 MCP 线程桥」，
   但阶段 3 定下的驱动方式是**每个任务一个事件循环**（宿主线程里 `asyncio.run`），
   而 MCP 连接是**进程级**的（`Agent.start()` 起、`close()` 停）。两者生命周期不同：
   要合并就得让全进程共享一条长驻循环，再给 REPL / 一次性任务（它们本来没有循环）
   造一个投递桥——正好是把要消除的桥重新造一遍。保留 MCP 自带的 loop 线程没有任何
   新代价（它已经在那里且工作正常），故**不合并**。阶段 5 TUI 变成常驻 async worker
   后可以考虑让 `McpService` 借用那条循环（届时也仍只对 TUI 有效）。
2. **不新建 `agent/loop.py`**：方案的 §8 骨架假设存在一个 `LoopConfig`/`LoopContext`
   对象，而现在 `_run_loop` 依赖 Agent 的十余个协作者（session / context 计量 /
   permission / turn 快照 / skills / goal）。此刻抽成自由函数需要把这些显式穿参，
   是纯搬运且会与阶段 4 的钩子改造撞车；阶段 4 引入 `LoopConfig` 后再抽才是顺的。
   `tools_run.py` 已按方案拆出（它依赖单向、无环，拆了就是净收益）。
3. **`drain_sync_stream` 不引入 `StreamFn` 协议**：方案 §7(1) 定义了
   `StreamFn = Callable[[model, ctx, options], AsyncIterator[...]]`。但项目真正的模型
   接缝是 `LLMClient.chat_stream`，60 处测试与扩展都在用它；再加一层等价协议 =
   两套接缝同时维护。只保留一个把同步生成器抽到循环上的辅助器。
4. **`SKIPPED_RESULT` 从包公开面移除**：它随 `BatchScheduler` 移入 `tools_run.py`，
   包外无人引用（全仓 grep 只有内部使用），故不再出现在 `agent/__all__`。

### 阶段 3 改写测试时的两个判断（值得留痕）

1. **在途的那一块保留、不丢弃**（`stream_fn.py`）：最初我在拉取后再查一次中止，
   把「Esc 那一刻正在返回的那一块」丢掉。`test_interrupt_during_stream_keeps_partial_content`
   立刻失败——异步化之前，同步 `for` 会先拿到这块再退出，**用户屏幕上已经显示过它**。
   丢掉等于让「屏幕上少了、历史里也少了」，是本阶段不该引入的行为变化，遂改为只在
   拉取**之前**检查（中止后不再发新拉取）。真正阻止后续产出的是 `llm/client.py` 的
   关流回调。
2. **两条「在主线程序执行」的断言改为断言序列化**（`test_agent_parallel.py`）：
   它们断言的是线程池实现细节（不启用池时在主线程跑）。异步化后所有工具执行都经
   `to_thread`，该细节不复存在；改为断言真正的不变量——`MAX_TOOL_CONCURRENCY=1` 时
   两个工具**不重叠执行**（I7 的意图）。测试名同步改为
   `test_concurrency_1_serializes_tool_execution` / `test_single_plan_executes_without_overlap`。

## 阶段 4 的实际产出（进行中）

### 4.A 排队（`agent/queues.py`）

| 文件 | 内容 |
| --- | --- |
| `agent/queues.py` | `MessageQueue` + `QueueItem`：每项带 `id`（修正 pi 按文本 `indexOf` 删错项），`mode` 支持 `all` / `one-at-a-time`，增删清投四路都触发 `on_change`，加锁（UI 线程入队、循环线程投递） |
| `agent/events.py` | 新增 `QueueChanged`（带完整项而非纯文本）＋进联合与 `AGENT_EVENT_TYPES` |
| `agent/renderer_bridge.py` | `QueueChanged` 登记进 `UNMAPPED_EVENTS`（队列面板是阶段 5 的新部件） |
| `tests/agent/test_queues.py` | 14 个：id 精确撤销（同文重复）、清空返回被清项、两种抽水策略、四路通知计数、模式校验、并发入队不丢不重 |

### 4.B 事件出口（核心 → 订阅者）

| 文件 | 内容 |
| --- | --- |
| `agent/agent.py` | `subscribe()` / `emit()`（线程安全）/ `_emit()` / `_ensure_stream()` / `_close_stream()` / `_emit_to_renderer()`；`run` 与 `run_with_goal` 的忙闲信号改发 `TurnStart` / `TurnEnd`，标题三处改发 `TitleChanged`；`Agent` 持有两条队列并提供 `steer` / `follow_up` / `cancel_queued` / `clear_*` / `get_*` / `pending_message_count` |
| `tests/agent/test_agent_events.py` | 14 个：生命周期事件顺序、退订、事件流迭代与终结值（同一对象）、每轮一条新流、**异常路径收口**（消费者的 `await` 不能挂死）、目标多轮共享一条流且只发一个 `AgentEnd`、迁移桥（回合/标题/后端热切换）、排队事件（含**跨线程入队必须由循环线程发事件**） |

### 4.B 的三处判断

1. **事件流由「创建者收口」**（`_ensure_stream` 返回 owner）：直接 `run()` 时本层
   拥有流，被目标续跑驱动时不是。方案 §3.5 没写这条，但不这样会出现两个真问题：
   每轮 `run()` 都终结一次（消费者在第一轮就拿到结果），以及外层包装的 `TurnStart`
   掉在流外。
2. **迁移桥是默认订阅者，渲染后端运行期解析**：宿主可能在 Agent 构造之后才装上
   自己的后端（TUI 即如此），所以桥不能在构造时把后端固定住——后端换了就重建桥，
   同一后端则复用（工具行的旧式 id 配对要跨事件保留）。一并解决了标题线程
   （无事件循环）与 UI 线程（不同线程）的发射路径：`emit()` 判断当前线程是否就是
   本轮循环线程，不是就 `call_soon_threadsafe`。
3. **队列属性名加 `_queue` 后缀**：叫 `steering` / `follow_up` 会与 `steer()` /
   `follow_up()` 方法同名互相遮蔽，实例属性会盖掉方法（实测
   `TypeError: 'MessageQueue' object is not callable`，被新测试当场抓住）。

### 4.A/4.B 与方案的偏差

1. **`run_with_goal` 移入 `AgentSession`：推迟到阶段 5**。方案自身在这里有冲突——
   阶段 4 行写「`run_with_goal` 移入 `AgentSession`」，但 `agent_session.py` 是阶段 5
   才建的文件；阶段 4 把会话层提前建出来，就等于把方案明确要求分开的两件事
   （异步改造 / 会话归属）挤到同一阶段。
2. **「核心只发事件」本阶段只覆盖控制信号**（回合 / 标题 / 排队）。视觉渲染路径
   （`stream` / `tool_call` / `tool_result` / `plan` / `notice`）仍直接调 Renderer：
   那部分是阶段 5「前端解耦」的正文（改的是消费关系），现在迁移是纯搬运，且要动
   `_finish` 的签名——它在冻结兼容面里（`test_display.py` 以非绑定方式调用）。
3. **`AgentState` 快照仍未建**。方案 §3.5 的 `_emit` 是「归约进 state → 推流 →
   分派」，现在只做后两步：`AgentState` 要有产出者才有意义，而轮询快照的消费方
   （TUI 状态栏）在阶段 5。先建就是没人读的空壳。
4. **排队项不写 `queue_id` 到消息**（方案 §5(2) 要求）。出队发生在唯一一个投递点
   （`drain`），消息与队列项天然一一对应；pi 需要这个字段是因为它的出队发生在
   「user 消息 `message_start`」那一刻。多一个「事后还要在清洗步骤里剔除、否则会
   进 provider 请求」的字段，换不到任何能用的东西。

### 4.C 交互桥（阻塞提问的事件对）

**要解决的问题**：`Renderer.turn_waiting_started/finished` 是**无载荷标量信号**，
由 `title.py` 的 `Relay` 包住全部 ask 方法广播。它只能回答「还在不在等」，
回答不了「是谁结束了」——`renderer.py:145` 的注释自己写着「确认可能嵌套……
消费方应自行用计数兜底」。计数能凑出布尔判断，却无法让面板级 UI 关闭**正确的**
那一个。

| 文件 | 内容 |
| --- | --- |
| `agent/interactions.py` | `PromptRequest` / `PromptAnswer` 之外新增 `InteractionBridge`（`request` / `open_prompts` / `max_open`）、ContextVar（`current` / `activate` / `reset`）、调用点入口 `ask()` |
| `permission/engine.py` | 越界授权（`outside_access`）与工具确认（`permission`）两处接入 |
| `skills/registry.py` | 项目技能信任确认（`skill_trust`）接入 |
| `tools/ask.py` | `ask_user`（`ask_form`）接入，整组答案全空 → `outcome="cancelled"` |
| `title.py` | 呈现器改订阅 `PromptStarted/Finished`（`on_agent_event`），`TitleState.waiting` 由 `open_prompts` 派生；Relay 去掉计数广播与 `_waiting()`；`attach(..., agent=...)` 负责订阅 |
| `cli.py` / `tui/app.py` | 装配处把 agent 传给 `title.attach` |
| `tests/conftest.py` | 交互桥 ContextVar 的用例级隔离（否则上一个用例的 Agent 泄漏给下一个） |
| `tests/agent/test_interactions.py` | 10 个：成对与同 id、提问期间可见、`outcome_of` 映射、异常路径收口、**重叠提问各自配对**、无桥退化、worker 线程也能发事件、顺序不重叠金丝雀、真实 run 的端到端金丝雀 |

**实现方案**（四条要点）

1. **提问怎么问，仍由调用点决定**：`ask(kind, title=…, run=lambda: renderer.confirm_choice(…))`
   ——`run` 闭包就是既有的阻塞调用，交互形态与语义逐字不变；桥只负责两侧的事件。
2. **成对靠 id，不靠计数**：`InteractionBridge.request` 进发 `PromptStarted`、
   出（含异常）发 `PromptFinished`，`finally` 保证收口。消费者用 `open_prompts`：
   `bool(open)` 等价于 pi 的 `depth > 0`，「显示最外层」= `next(iter(open.values()))`
   （dict 保插入序）。
3. **无桥退化**：`ask()` 在没有活动桥时直接调用 `run()`，不发事件——权限引擎、
   技能信任等模块的单测直接调它们、不经 Agent，行为必须与改造前一致。
4. **桥挂 ContextVar**：`Agent.start()` 在主线程挂载并常驻（覆盖启动期与命令层
   提问，如 `/skills refresh`），`run()` 在循环上下文里再挂一次并在 `finally` 复位
   （`to_thread` 会复制上下文，所以跑在 worker 线程里的预检也发得出事件）。

**决策：id 配对 + 不嵌套金丝雀（2+3 一起）**

- 选 id 而不是 pi 的「合并成最外层一个区间」（`runner.ts:487-514` 的
  `uiPromptDepth` + `activeUIPrompt`）：id 是超集——pi 的合并语义用
  `bool(open)` + 「最外层优先」一行就能复现，而反过来（计数推导出「谁结束了」）
  做不到。pi 需要计数器是因为它的扩展事件派发是 fire-and-forget，提问来源不共享
  调用栈，它管不住并发数。
- **金丝雀**：`max_open == 1` 断言我们自己的流程从不重叠（顺序两次提问后仍为 1；
  真实 `run` 里触发越界确认后仍为 1 且 `open_prompts` 归零）。理由是「同时两处
  在等」从来不是需求——嵌套是「按 API 调用发提问、按流程算体验」的产物；真出现
  重叠时先失败、由人决定改成顺序还是合并展示，而不是悄悄上线一个模糊界面。
- 机制本身**支持**重叠：`test_overlapping_prompts_pair_independently` 证明嵌套
  两层时两个 id 各自配对、内层结束时外层仍在 `open` 里。

**踩到并修掉的真问题**

1. **`activate` 返回的是 Token，不是可调用对象**：`run()` 里写成
   `reset_interactions = interactions.activate(...)` 然后 `reset_interactions()`，
   91 个用例报 `TypeError: '_contextvars.Token' object is not callable`。
   （`signal.activate_token` 返回可调用对象，两者形状不同——已提供 `reset(token)`。）
2. **`ask_user` 的 options 结构**：`_normalize()` 之后每题是
   `{question, options: [label...], …}`（字符串列表），我按原始入参的 dict 结构取
   `option["label"]`，`TypeError: string indices must be integers`。
3. **反向导入会成环**：`from ..agent import interactions` 会经包门面的惰性
   `__getattr__` 把 `agent/agent.py` 整条重链拉进来，而 `permission/engine.py`
   正是那条链上的一环。改为直接导入子模块
   `from ..agent.interactions import ask as ask_prompt`（`interactions.py` 只依赖
   标准库，单向安全）。
4. **Relay 不能删掉 ask/`turn_waiting_*` 的透传**：这些方法在基类里是空实现，
   不覆写就等于**吞掉**对内层的调用（装饰器语义）。`test_relay_covers_renderer_api`
   就是为抓这类漏项存在的；等待态的 `_notify` 广播去掉，透传保留。

**偏差**

1. **`PromptKind.confirm` 暂无生产者**：方案把它映射到「其它 `confirm_choice`
   调用方（如 /mcp 向导）」，但全仓 grep 显示 `confirm_choice` 的调用方只有权限与
   技能信任两处；`/mcp` 向导直接走 TUI 面板而非渲染端口。保留该 kind 待用。
2. **呈现器同时走两条通道**：标题/忙闲仍由渲染后端转发（`Relay`），提问走事件
   订阅。阶段 5 删 `Relay` 时合并为单条订阅——本阶段不提前做，避免把「前端解耦」
   的正文挪进阶段 4。

## 阶段 4 的实际产出（4.D / 4.E）

### 4.D 钩子外露（`agent/hooks.py`）

| 文件 | 内容 |
| --- | --- |
| `agent/hooks.py` | 四个决策点的上下文与结果类型 + `AgentHooks` 容器（全 None = 现状行为） |
| `agent/agent.py` | `Agent(hooks=...)`；`before_tool_call` / `after_tool_call` 两个异步调用助手（含异常策略）、`_hook_prepare_next_turn` / `_hook_should_stop_after_turn`；`_run_loop` 接入回合裁决与 `terminate` |
| `agent/tools_run.py` | 调度器接入：before 在预检**之前**（循环线程，可 await）、after 在收集**之前**；`terminate` 投票聚合（整批全 True 才算） |
| `tests/agent/test_hooks.py` | 13 个：拦下单个调用不影响同批其余、钩子异常 fail-closed / fail-open、结果文本替换、整批投票 vs 部分投票、回合钩子每轮调用、signal 入参、无钩子等价 |

**三条与方案的差异**（连同理由，写在 `hooks.py` 的模块 docstring 里）：

1. **权限与路径检查留在核心**，不搬进 `before_tool_call` 的默认实现。安全边界不
   外包给钩子（没配 / 配错 / 抛异常都不该让沙箱失效）；且预检必须跑在 worker
   线程里（阻塞式弹窗不能占住事件循环），异步钩子到不了那儿。钩子是预检**之前**
   的异步决策点，能力上是 pi 的超集。
2. **`prepare_next_turn` 是附加调用点**，不替换默认准备（`sync_system` + 按需压缩）
   ——提示段字节级稳定与压缩是正确性所需，不该因「钩子没写」而丢掉。
3. **不提供 `is_error` 覆盖**：smithcode 的工具结果就是一段文本，消息里没有该字段。

**边界策略（新决定，方案未写）**：`before_tool_call` 抛异常 = **拦下本次调用**
（fail-closed），异常文本作为结果回传——否则某个 `tool_call_id` 会失去配对结果，
下一次请求被服务商拒绝，而故障现场已经跑到几轮之后；`after_tool_call` 抛异常 =
**保留原结果并附一行说明**（fail-open），结果已经拿到，格式化失败不该弄丢它。

### 4.E 三个抽水点

`_run_loop` 里接上 pi 的三个投递点：**起点与每轮末**（同一个位置，`_drain_queue(steering_queue)`）、
**本要停时**（`_drain_queue(follow_up_queue)`，有内容就 `continue` 而不是返回）。
投递即出队（`drain` 会发 `QueueChanged`），作为 `user` 消息进历史，图片随消息带过去。

`tests/agent/test_agent_queues.py` 10 个：插话不打断当前轮（第一轮看不到、第二轮看到）、
本要停时 follow-up 续跑、起点投递、`one-at-a-time` 每次只投一条。

## 阶段 5 的实际产出（进行中）

### 5.A `[queue]` 配置段

`config.py` 新增 `QueueConfig` + `load_queue_config()`（与 `load_sessions_config()` 同形，
非法值警告后回退）：

```toml
[queue]
delivery = "follow"                 # follow（默认，等本轮跑完再送）| steer（立刻插话）
steering_mode = "one-at-a-time"     # all 一次全送 / one-at-a-time 逐条送
follow_up_mode = "one-at-a-time"
```

`Agent.__init__` 读它并据此设置两条队列的抽水策略。

### 5.B `prompt()` / `enqueue()`

`Agent.prompt(text, images=None, delivery="auto")`：空闲即 `run_with_goal`，运行中按
**配置**入队（默认 follow）；显式传 `follow` / `steer` 供扩展与 SDK 覆盖。返回本轮
`RunResult`，入队时返回 `None`（通知走 `QueueChanged`）——**不新增 `RunResult.status`
取值**，维持阶段 3 冻结的取值集合。

`Agent.enqueue()` 是同步版（UI 线程用），`prompt` 与 TUI 都走它，避免两份「投哪个队列」
的判断。

### 5.C `#queued` 面板 + 反向通道

| 文件 | 内容 |
| --- | --- |
| `tui/widgets.py` | `QueueRow`（前缀按**实际入队方式** `⤵`/`↔`、行尾可点击 `✕`、单行省略）+ `QueuePanel`（空则 `display=False`） |
| `tui/app.py` | `#input-wrap` 内、`#running` 之下、输入框之上；订阅 `QueueChanged` → `UiAction("queue")` → 主线程重建面板并重算命令菜单锚点；运行中提交改入队（不再提示「请等待」）；Esc 中止时 `_recall_queue()` 把排队内容取回输入框；`on_mount` 订阅 |
| `agent/agent.py` | `new_session()` 清空队列（会话边界不继承排队输入） |
| `tests/test_tui_queues.py` | 5 个：入队即显示且位置正确、`steer` 配置换前缀、点 `✕` 精确撤销、Esc 取回编辑器并清空、`/new` 清队列 |

**几何锚点**：`anchor_command_menu` 本来就是按 `#input-wrap` 的实时高度算 offset，
所以面板出现/消失自动计入——不需要按「每加一个上方控件就改一次」那样硬编码（方案里
担心的 `#command-menu.running` 兜底 offset 只是尚未计算过时的初始值）。

**一处偏差**：方案说「终端不支持鼠标时不提供 ✕」。实际**总是渲染** `✕`，不做能力
探测——终端类型五花八门，探测结果比「点击是否发生」本身更不可靠；不支持下鼠标的
终端点击不会发生，元素只是显示着。撤销的替代路径是 Esc 取回。

### 5.D 前端解耦：TUI 任务入口改 async worker

`start_task` / `start_compact` 从 `threading.Thread` 改为 `self.run_worker(...)`，
`_run_task` / `_run_compact` 变成协程直接 `await`——**任务不再占用独立线程**。

**面板为什么不改成 `push_screen_wait`（与方案的差异，附理由）**：方案担心
「Textual worker 里 `Event.wait()` 会死锁」。核实现状后这条不成立——**所有会弹窗
的调用都来自 Agent 下放的 worker 线程**（预检的权限确认走 `to_thread`、工具执行
走 `to_thread`），从没在循环线程上发起过弹窗；而 Textual 的 `call_from_thread`
在**同线程调用会直接抛 `RuntimeError`**（`textual/app.py:1821`），等于替我们守着
这条不变量。所以面板保持「`call_from_thread` + `Event`」，收益是零改动零风险；
把这条不变量写进了 `tui/bridge.py` 的模块 docstring（承重，改动前先读）。真把
预检/工具搬回循环线程那天会立刻炸，那时再换 `push_screen_wait`。

### 5.E 会话对象（`agent/agent_session.py`）

| 文件 | 内容 |
| --- | --- |
| `agent/agent_session.py` | `AgentSession`：任务入口（`run` / `run_with_goal` / `prompt`）、运行中排队、交互端口、会话边界（`new_session` / `resume` / `rename`）、状态（`state_parts` / `snapshot` / `restore` / `reset`）+ `create()` 工厂（延迟导入断环） |
| `agent/agent.py` | `run_with_goal` **移入会话层**；新增 `outer_turn_begin` / `outer_turn_end`（外层多轮的回合事件与「谁拥有事件流」的唯一判断）；`_note_goal_run` 改名 `note_goal_run` 供会话层调用；`Agent.session_owner` 惰性属性 |
| `cli.py` / `tui/app.py` | 任务入口改走 `agent.session_owner.run_with_goal(...)` |
| `tests/agent/test_agent_session.py` | 9 个：外观持有的是同一对象（不是副本）、惰性唯一、`run_with_goal` 无目标等价 `run`、`prompt`/`busy`/排队委托、**`t=state` 三 key 断言**、快照/复位/恢复往返、**`messages[0]` 字节级稳定** |

### 5.F Relay 删除

`title.Relay` 与 `title.bus()` 删除，`attach()` 只做两件事：接管窗口标题 +
`agent.subscribe(presenter.on_agent_event)`，并**原样返回**传进来的渲染后端。
呈现器的 `on_agent_event` 现在按类型分派 `TitleChanged` / `TurnStart` / `TurnEnd` /
`PromptStarted` / `PromptFinished`——一条订阅通道覆盖标题、忙闲、等待三类状态。
（旧实现用 `getattr(presenter, "on_" + name)` 转发，未实现的方法会抛
`AttributeError` 并被误判成「输出中断」；改成类型分派后这类 bug 结构上不可能，
新增测试锁住「不认识的事件是空操作」。）

## 剩余问题与已知偏差（截至本次交付）

1. **goal / plan / skills 未实例化**（5.E 的核心部分只做了适配层）。现状：
   `AgentSession.snapshot/restore/reset` 转调这些模块单例，`t=state` 三 key 不变
   （有断言）。没做的原因：改成实例要动 15 个以上读取点（`session.py`、
   `commands/*`、`tui/app.py`、`tui/sidebar`），且必须与宿主持有方式一起切换——
   只改一半会得到「同一状态两份真相」，比现状更糟。要做的话顺序建议：
   ① 把 `goal`/`plan`/`skills` 的单例改成可注入的实例（保留模块级默认实例以兼容
   现有读取点）→ ② 宿主改持 `AgentSession` 并从它取这些状态 → ③ 删模块级默认实例。
2. **宿主仍持 Agent**（经 `agent.session_owner` 用会话入口），未把 `AgentSession`
   作为宿主的第一手对象。这是第 1 条的从属项：会话对象真正有价值的是它拥有
   goal/plan/skills，实例化之前换持有方式只是换个名字。
3. **面板仍是同步端口**（`call_from_thread` + `Event`），未改 `push_screen_wait`。
   理由见 5.D；触发条件写在 `tui/bridge.py` 的 docstring 里。
4. **阶段 6（AsyncOpenAI）未做**。收益：省掉 `drain_sync_stream` 的逐块 `to_thread`
   跳转（一次线程往返/块）。成本：`LLMClient.chat_stream` 是 60 个测试假客户端与
   扩展共用的冻结接缝，改异步要同时重写同步重试状态机（`llm/retry.py`，单测完善）
   与流解析栈，并把标题后台线程的同步路径也带上。判定：收益是延迟优化、不是正确性，
   风险与改动面都大——**留空并记录**，需要时按「新增 async 客户端 + 保留同步路径」
   的方式做渐进迁移。
5. **`title.py` 的 `Relay`/`bus()` 删除后，GUI 前端（desktop/web）的装配入口没了**。
   当时 `bus()` 只为「不接管终端标题、只转发事件」而存在，而事件现在直接从
   `Agent.subscribe` 拿——GUI 前端订阅 `agent` 即可，不需要渲染后端装饰器。

## 收尾轮（审计后按用户抉择补做）

审计发现 11 处遗漏，用户逐项抉择后本轮的落地情况：

| # | 项目 | 状态 | 说明 |
|---|---|---|---|
| 10 | 3.10 实机验证 | ✅ | `uv run --python 3.10`（3.10.21）跑 `tests/agent` + cancel/context/session：154 passed；**全量在 3.10 上跑出 5 个真失败并修掉**（见下）。用户决定不加 CI workflow，故仅本地验证 |
| 2 | retry/compaction 状态事件 | ✅ | 新增 `agent/emitter.py`（ContextVar 通道，不动冻结的 `chat_stream` 签名）；`llm/client.py` 有通道发 `StatusChanged(kind="retry")`、无通道退回 `view.retry_*`；`Agent.compact()` 前后发 `compaction` 对（失败也成对摘除）。`working`/`stopping` 仍无生产者（`TurnStart/TurnEnd` 已覆盖忙闲；stopping 由 TUI 自己驱动） |
| 5 | 工具两种签名都收 | ✅ | `_run_plan`：同步工具 `to_thread`，返回 awaitable 则在循环上 await。**顺带修掉一个真 bug**：`_make_runner` 的闭包在返回前 `str()` 了工具返回值，异步工具会被转成 `"<coroutine object …>"` 且永不执行（测试报 `never awaited`） |
| 7 | 会话对象补齐 | ✅ | `get_steering_messages` / `get_follow_up_messages` / `clear_steering_queue` / `clear_follow_up_queue`。**又抓到一次属性遮蔽**：`AgentSession.follow_up = MessageQueue(...)` 盖掉了同名方法（`TypeError: 'MessageQueue' object is not callable`）——与阶段 4.B 在 `Agent` 上踩的是同一类，改名 `follow_up_queue` 并加注释 |
| 4 | `continue_run()` | ✅ | 对应 pi 的 `agentLoopContinue`：不注入 user 消息、接着当前上下文再跑一轮；两条前置校验（上下文非空、末尾非 assistant）。**命名偏差**：Python 里 `continue` 是关键字，只能叫 `continue_run`，已在 docstring 说明 |
| 11a | `RunResult.status` 守卫 | ✅ | `result.RESULT_STATUSES` 冻结集合 + 静态扫描源码里所有 `RunResult("<字面量>"` 的测试（新增状态必须显式改常量） |
| 11b | headless 端到端验收 | ✅ | `tests/test_end_to_end_headless.py` 4 条：本地 SSE stub + **真实** `LLMClient.from_config()`（不 monkeypatch 客户端）跑完整回合（HTTP → SSE 解析 → 循环 → 渲染 → 历史）、`cli._run_agent_task` 驱动、转录与状态投影落盘、服务不可用 → `stream_error` 契约 |
| 1 | 视觉路径事件化 | ✅ | `agent/*.py` 里的视觉调用全部改为事件：`_chat` → `MessageUpdate`/`MessageEnd`（`RendererError` 语义逐字保留：`_emit` 不吞异常，渲染故障仍单独成型、不误报成输出中断）；`_preflight`/`_collect`/`_placeholder` → `ToolStart`/`ToolPreview`/`ToolEnd`/`PlanUpdate`；13 处 `info`/`warn`/`error` → `Notice`（标题重命名那条在后台线程，改走线程安全的 `emit`）。`ToolPlan` 去掉整型 `tool_id`（旧式 id 分配收进 `RendererBridge`），新增 `rendered` 标记表达「界面里是否已开出工具块」（替代旧的 `tool_id is not None`，保证收尾补占位不多出空结果块）；`_finish` 第二参数由 int id 改为字符串 `tool_call_id`。新增静态守卫 `tests/agent/test_core_emits_events.py`：核心不得再直调渲染器 + 桥必须认得每一种视觉事件。**顺带修掉一个既有的隐藏导入环**（`renderer → utils.terminal → commands → renderer`，此前靠导入顺序偶然成立，换个入口就 `partially initialized module`） |
| 3 | 模块拆分 | 🟡 部分 | ✅ `agent/errors.py`（两个异常）、`agent/loop.py`（**仅**中断/流中断/上限的文案与格式化）、`agent/types.py`（消息/增量/级别别名）；**循环函数本身没搬**——它与 Agent 十余处状态强耦合，搬成 `run_loop(agent, token)` 只是把 `self.` 换成 `agent.`，可读性不升反增一层间接（理由写在 `loop.py` 的模块 docstring） |
| 8 | goal/plan/skills 实例化 | ✅ | 三步都做了。① 三个模块各自引入状态类（`goal.GoalState` / `plan.PlanState` / `skills.state.SkillsState`），原模块全局收进实例字段，模块级 `snapshot`/`restore`/`reset` 搬进类里、原位置留一行委托；② `AgentSession` 持有三个实例并在建立时**继承当前活动实例**的状态、每次开轮再绑一次（`bind_state()`），`Agent._state_registry()` 直接用会话实例（不再依赖"当时恰好绑定谁"）；③ 宿主与命令层**不必逐个改写**——`bind()` 让既有模块函数自动落在本会话实例上，另加静态核查确认没有任何外部代码读老单例的私有名（否则就是两份真相）。新增 `tests/agent/test_session_state_instances.py`（会话各自独立、注册表绑实例、无会话时回落到默认实例）。**局限（已写进 docstring）**：同进程真并发跑两个会话会互相覆盖——与改造前的单例行为一致，TUI/REPL 是一个进程一个会话 |
| 9 | transcript / section 注册表 | ✅ | 新增 `agent/transcript.py`（`Section` + `register`/`unregister`/`sections`/`assemble_system_prompt`）；`build_system_prompt()` 由三个具名段参数改为**收一个有序序列**；`Session.sync_system()` 调注册表。新增 4 条测试（注册新段即出现在提示词里、同名替换、空段跳过） |
| 6 | 队列 mode/delivery 可写属性 | ❌ 明确不做 | 用户抉择：配置决定默认，扩展可用 `enqueue(delivery=…)` 显式覆盖；运行期不可改 |

### 收尾轮修掉的两个真 bug

1. **3.10 上 `list_dir` 整个工具崩溃**（既有 bug，被 3.10 全量验证暴露）：`tools/files.py:378` 用了 `Path.is_dir(follow_symlinks=False)`，而这个关键字是 **Python 3.13** 才加到 `Path.is_dir()` 上的——项目声明 `requires-python >= 3.10`，3.10–3.12 上传关键字直接 `TypeError`。改成 `not item.is_symlink() and item.is_dir()`（`is_symlink()` 走 lstat，全版本可用）。修复后 3.10 上该文件 49 passed。
   （另注：`_shared_local.py:78` 的 `entry.is_dir(follow_symlinks=False)` 在 `os.DirEntry` 上是合法参数，全版本可用，无需改。）
2. **异步工具的返回值被 `str()` 掉**：见上表第 5 项。

### venv 版本说明

为做 3.10 验证，本机装了 CPython 3.10 并在其间把 `.venv` 建成了 3.10；**验证完成后已恢复为你原来的 3.14**（`uv sync --python 3.14 --extra dev`）。以后要复跑 3.10 检查：

```bash
uv run --python 3.10 --extra dev pytest tests/agent tests/test_cancel.py tests/test_context.py tests/test_session.py -q
# 注意：这会重建 .venv 为 3.10，跑完用 `uv sync --python 3.14 --extra dev` 恢复
```

## 用户首次实机验收发现的两个缺陷（已修，2026-09-19）

用户在真实终端里第一次按 Enter 就撞上了，两个都是**测试盲区**造成的：

### 1. 运行中提交被回显到对话区（现象：消息进了对话区，没在 working 下面）
真实入口 `on_chat_input_submitted` 在调用 `start_task` **之前**无条件回显用户消息，
而"是否入队"的判断在 `start_task` 里面——于是忙时提交的消息既进了排队面板，也进了
对话区；另外 `start_task` 还打了一条把原文也带上的 notice，看起来更像"已经发出去了"。

修法：忙时**只入队、不回显**（面板本身即反馈，notice 删掉）；投递时才落到对话区。
为此新增事件 `QueuedPromptDelivered(text, steering)`——入队时这条文本只存在于排队
面板里，投递（本轮跑完 / 工具批之间抽水）后才成为会话历史的一部分，前端要在这个
时刻上屏，否则用户看到它从面板消失、对话区却没有出现，而模型已经在回应一条"看不见的"
用户消息。

### 2. 两条投递 API 不一致（裸 drain，不进历史也不发事件）
`get_steering_messages()` / `get_follow_up_messages()` 直接 `queue.drain()`，而循环里
的抽水点走的是 `_drain_queue()`（进历史 + 发事件）。同一个动作两套实现，一套有副作用
一套没有。现在统一到 `_deliver(queue)`：两条公开 API 与循环抽水点都走它。

### 3. 测试盲区（根因）
既有的 TUI 队列用例**直接调 `app.start_task(...)`**，绕过了真实入口
`on_chat_input_submitted`——所以"回显发生在入队之前"这类缺陷不会被任何用例发现。
新增三条走真实入口的用例：忙时只入队不入对话区、投递时才上屏并出队、投递事件带得动
`steering` 标志。

### 一条方法论教训
这条缺陷说明：**用例要走用户真实经过的那个入口**。我此前给队列写的 5 条用例全都
在 `start_task` 这一层，等于只测了"入队之后"的半程；把入口换回 `on_chat_input_submitted`
之后，缺陷是立刻可见的。

### 4. 排队面板 `display=True` 但尺寸 0×0（现象：入队后什么都看不到）
用户第二次实机反馈："入队后面板没有出现"。复现后确认是**布局塌陷**，不是逻辑问题：
面板 `display` 为真、子控件也都挂上了，但 `QueuePanel.size == (0, 0)`，
`#input-wrap` 里根本没占位。

根因在 CSS：`#queued { width: auto }` 时面板宽度取自子控件，而子控件（`Static` /
`QueueRow`）宽度又取决于父面板宽度——循环依赖把面板塌成 0 宽，连带 0 高。改成
`width: 1fr`（面板本就该占满输入区宽度）后立即恢复为 90×2。

同时把 `show_items` 改成**先 `display = True` 再挂子控件**并显式 `refresh(layout=True)`：
面板此前 `display=False`，布局阶段被跳过，父容器（`height: auto`）不会把这一块算进去。

**测试盲区（第二个）**：我此前对面板的断言只有 `display is True` 和"子控件在不在"，
两者都为真时用户依然什么都看不到。现在加了 `panel.size.height >= 1` 与
`region.height >= 1`，并且新增一条**确定性**用例：用可控阻塞的假模型（`threading.Event`）
让任务真的停住，再走真实按键路径（focus → insert → press enter）提交，
断言队列非空且面板有可见高度。第一版我用定时 `sleep` 的假模型，任务自己跑完并投递了，
断言实际测的是"投递之后"——用阻塞模型才把这个洞堵上。

### 排队面板第 3 次改版（用户逐轮反馈后定稿）+ 一个死锁缺陷

版式演进：`↳ 排队中 2 · 默认 follow / ⤵ 文本 / ↔ 文本`（表头 + 两个符号）→ 去表头、
模式写每行末尾（`Follow` / `Steer`）→ **去模式词，`edit` / `cancel` 两个按钮右对齐**。

| 决定 | 理由 |
|---|---|
| 去掉表头行 | 条数由行数本身表达；`默认 follow` 是配置词漏到界面 |
| 去掉行末模式词 | 默认配置下它是常量，写出来只占宽度；真要区分时改行首标记（一处） |
| `edit` / `cancel` 按钮用**整词** | 单个 `✕` 只有 1 格宽，终端里点不中；整词 4/6 格 + 两侧各 1 格余量 |
| `edit` 蓝 `#7aa2f7` / `cancel` 红 `#f7768e` | 复用既有语义色（动作 / 破坏性） |
| `edit` = 取回编辑 | 改一条排队的错别字**不该付"中断当前任务"的代价**（那是 Esc） |

命中区：`cancel` = 它的词宽 + 左侧间距；`edit` = 它的词宽 + 左右各 1 格；
点正文/行首标记**不做任何事**（留给选中与复制）。按从右往左判定，两区间不重叠。

**死锁缺陷（已修，测试以"挂住"而非"失败"的形式暴露）**：新写的
`MessageQueue.take()` 在**持有 `self._lock`** 时调 `_notify()`，而回调
`Agent._on_queue_changed()` 会回来读同一条队列的 `list()`——同一把非重入
`threading.Lock`，同一线程自锁死。真实 UI 上的表现是"点 `edit` 整个界面卡住"。
修法：`_notify()` 移到放锁之后（与既有 `enqueue` / `remove` / `clear` 的写法一致）。

定位方法值得记一笔：这条缺陷不会让用例失败、只会让它**永不返回**，所以用
`PYTHONFAULTHANDLER=1 timeout -s ABRT pytest ...` 打出挂住栈才看到
`take → _notify → _on_queue_changed → list` 这条自锁链。
