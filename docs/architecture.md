# 架构说明

SmithCode 是一个 mini coding agent，核心是 **Agent 循环（Agentic Loop）**。

## 核心循环

```
用户输入
   │
   ▼
┌─────────────────────────────────────────┐
│  Agent.run()                            │
│                                         │
│  ┌────────┐    消息列表    ┌─────────┐  │
│  │  LLM   │──────────────▶│ Session │  │
│  │(llm.py)│◀──────────────│(session)│  │
│  └────┬───┘               └─────────┘  │
│       │ 返回 tool_calls？               │
│       │ 是                              │
│       ▼                                │
│  ┌─────────────┐   ┌──────────────┐   │
│  │ Permission  │──▶│ tools/*.py   │   │
│  │ (权限确认)   │   │ (执行工具)    │   │
│  └─────────────┘   └──────┬───────┘   │
│                           │ 结果回传    │
│                           ▼            │
│                 下一轮循环（max_iterations > 0 时受限）
└─────────────────────────────────────────┘
   │ 否（纯文本回复）
   ▼
打印给用户，结束
```

1. `cli.py` 接收用户输入，交给 `Agent.run_with_goal()`（无目标时即 `Agent.run()`；/goal 的续跑裁决见后文）。
2. `Agent` 把消息列表（含系统提示词）发给 LLM。
3. 模型要么返回纯文本（任务完成，循环结束），要么返回工具调用。
4. 工具调用先经 `Permission` 确认（读文件/列目录免确认），再由 `tools/` 执行；同一批调用**流式调度**（`_BatchScheduler`）——按接收顺序**边预检边执行**：预检一个就决定去向，可并行的只读/网络调用进波次缓冲、到屏障才提交线程池并发执行（`MAX_TOOL_CONCURRENCY` 上限），声明 `serial` 的有状态工具（shell / 写文件 / 交互确认）在主线程就地执行、作为顺序屏障（执行前先冲刷前面的波次）。因此串行工具在**后续工具的权限确认之前**就已执行完，确认框与执行一一对应；结果一律按提交顺序回传。并行波次在屏障前不产生副作用（并行工具仅只读/网络类），故「首个串行工具执行前」的整段仍可原子取消；一旦某个已确认的串行工具执行完，之后再被拒/中断即不再回滚它（partial apply）。
5. 执行结果以 `role: tool` 消息回传给模型，进入下一轮循环。
6. 迭代轮次默认不限制（`[limits].max_iterations` 默认 `-1`，对齐 opencode 的 `steps` 缺省语义）：只要模型持续请求工具就继续循环，直到模型给出纯文本回复或用户中断。配置为正整数时封顶，达到上限后不再执行工具，改为注入收尾提示、以 `tools=None` 强制模型用纯文本总结已完成工作与剩余任务（对齐 opencode 的 max-steps 收尾），随后以 `max_iterations` 状态结束——总结正文已在流式过程中展示，宿主另可见一条「已达上限」警告。

### 中断（Esc）

任务可随时被用户中断（TUI 按 Esc、REPL 按 Ctrl+C），走同一协作式取消通道：

1. **发起**：宿主层调 `Agent.interrupt()` 触发当前轮次的取消令牌（`cancel.py`），令牌经 ContextVar 沿调用链隐式传播（`run()` 全程同线程，生成器内可见）。
2. **LLM 流截停**：`llm.py` 打开流后把 `stream.close` 登记为令牌监听——取消线程**直接关流**，即使正阻塞在等下一块数据（模型静默期 / 网络慢）也立即解除；流消费每块数据前再查一次令牌。取消后不再产出 message/usage，关流引发的读错误按取消吞掉；已收到的正文由 Agent 拼成**部分消息**保留入库（残缺的工具调用不回传）。打开流之前也先查令牌——中断后**不再发起任何新请求**（含压缩摘要、溢出恢复重试）。
3. **工具批截停**：`_BatchScheduler` 在**每个待预检项**与每个串行屏障前查令牌——预检阶段（含权限确认）中断时剩余确认框不再弹出、当前项即使刚答 y/n 也不执行、已预检未执行的（缓冲区波次）同样跳过（中断 = 不再发起任何新工作，且优先于拒绝语义）；未执行的计划与剩余 `tool_calls` 补占位结果（与权限被拒共用同一会话修复路径，`tool_call_id` 永不悬空），**非 shell** 的正在执行工具让其自然跑完（线程不可强杀）并照常收集结果。
4. **运行中命令强杀**：正在执行的 shell 命令是例外——`run_command`（serial，跑在 run 线程）经 `process.py` 执行命令，`process.run` 自读当前线程令牌并在轮询中判定取消，触发即**终止整个进程树**（Windows `taskkill /F /T`、POSIX 先 SIGTERM 宽限后 SIGKILL），无需等命令自然结束或撞超时；超时路径共用同一终止逻辑。并行 worker 读不到令牌时安全降级为不响应取消。
5. **收尾**：`run()` 返回结构化 `RunResult`（`ok / interrupted / denied / max_iterations / stream_error`，`partial` 标记流中截停），令牌在 finally 中复位，下一任务不受残留状态影响；会话历史始终合法，可直接继续追问。中断时 `run()` 还会把事件作为一条 user 注释（`INTERRUPTED_CONTEXT`：任务未完成、部分输出可能不完整）追加进会话历史——**不触发任何新请求**，只是让模型在下一轮提问时知道上一轮是被主动叫停的，避免把部分输出当成完整结果。响应流**非取消**地断在正文中间（读完超时 `ReadTimeout` / 对端掐断连接 `RemoteProtocolError`）先由客户端按 `llm/retry.py` 的策略重试整请求：正文已输出也照常重试，每次尝试的正文都实时上屏、并按到达顺序累积进同一条 assistant 消息（对齐 opencode 的 text part 累积），因此历史里既有中断的那段、也有补完的那段。重试预算用尽才走收尾路径：`_chat()` 的 `try/finally` 保证 `r.stream_done()` 一定执行（TUI 的正文块必须收口，否则下一轮的增量会被追加进上一轮那个还没闭合的块），流中异常包成 `StreamInterrupted(original, partial)` 上抛；`run()` 捕获后把已上屏的部分正文按 assistant 消息入库并追加 `STREAM_INTERRUPTED_CONTEXT`，返回 `stream_error`（`text` 为部分正文、`reason` 为 `分类: 原始信息` 的失败原因）——屏幕上看到的与历史里的一致，下一轮模型接着写而不是从头重做。**中断原因必须带出来**：TUI 页脚显示「· 输出中断（读取超时: …）」（读超时再附 `llm_timeout` 可调提示），控制台同样打印原因，回写历史的中断说明也带上原因——只报「中断」不报为什么断，用户与排查者都无从下手。

模型输出以流式方式逐字显示；思考内容（如 DeepSeek-R1 类模型的 `reasoning_content`）以暗色实时展示，但不写入会话——多数 OpenAI 兼容服务不接受它被回传。

工具调用在执行前打印一行短摘要（`read src/agent.py`、`command git push`，由各工具注册的 `describe` 生成；生成后先压平空白再截断，多行命令（heredoc 等）的换行不会把工具行撑成多行，TUI 里超列宽的摘要按宽度省略而非折行），粒度由 `~/.smithcode/config.toml` 的 `tool_display` 控制：`summary`（默认）到此为止（附带展示 write/edit 的变更预览 diff），`detail` 再以 `[Result]` 追加结果内容（前 500 字符）。展示粒度只影响终端，回传给模型的内容始终是截断后的完整结果；失败信息（`错误: ...`、用户拒绝）无论粒度都原样展示。TUI 侧另有一层纯展示的**上下文汇总**（对齐 opencode 的「已探索」）：连续的读取 / 搜索工具（`read_file` / `list_dir` 计入读取，`glob` / `grep` 计入搜索）汇总成一个可折叠块（头行按类别计数，展开看逐条明细），遇到非上下文工具、助手正文/思考流或回合结束时封口；分组不改变 `ConsoleRenderer` 行为与回传模型的内容。

### 对话区消息模型（TUI）

TUI 对话区的全部内容经**唯一入口** `ChatView.apply(item)` 挂载/更新，item 为 `tui/chat.py` 的纯数据语义消息（`User` / `Assistant` / `Notice` / `Block` / `Footer` / `Welcome` / `StreamDelta` / `Thinking*` / `Tool*`）。生产者（`TuiRenderer` 事件桥、命令输出、宿主回显、欢迎横幅、历史回放、任务异常兜底）只表达**语义与级别**，缩进 / 着色 / 图标 / 间距统一由渲染层与 tui/app.tcss 决定：所有顶层消息带 `.chat-item`（缩进 3 / 上间距 1 的唯一来源，用户消息左边框占 1 列故 padding-left 为 2，正文左对齐）；通知按 `Level`（info / success / warning / error / retry）着色并带固定 1 格图标，正文左起点不随级别漂移。`Renderer` 提供 `info()` / `warn()` / `error()` 三个语义方法（`ConsoleRenderer` 保持纯文本打印、TUI 映射到通知级别），命令层旧的 `style` 字符串由集中映射兼容。工具块映射与思考块引用收归 `ChatView`，宿主 `SmithTUI` 只做事件路由。

正文块（`widgets.MessageBody`）与工具正文（`widgets._ToolBody`）同为**动态宽度**渲染：正文经 rich 渲染后折行会固化进 `Text`，`Static.update()` 一次定型的内容在窗口缩放时无法重排，故二者都在 `render()` 里按 `self.size.width` 实时生成，宽度变化即整体重排（缓存按宽度失效）。其余块（用户消息、通知、工具头等）直接上屏原始文本，折行交给 Textual，天然随容器自适应。

滚动跟随是**贴底意图 + 布局期贴底**，不是"贴底就 `scroll_end()`"：`scroll_end` 的目标行号取自发出那一刻的 `virtual_size`，而流式正文按帧节流重排（节流掉的 chunk 不触发布局）、输入区高度可变（运行动画 / 命令菜单 / 输入框增高都会让聊天区变矮）——目标行号一旦与真实内容差出 1 行以上，"是否贴底"的判断此后恒为假，跟随**永久失效**。现由两部分构成：意图 `ChatView._stick` 只随真实滚动位置变化（`watch_scroll_y`：用户滚走即关、滚回底部即开，提交自己的消息无条件置开）；贴底动作在 `_size_updated`（合成器排版之后调用，任何尺寸变化都触发）里按真实内容高度重算，故内容增长（含被节流推迟到后续布局的正文）、聊天区高度变化、工具结果 / 变更预览这类"就地把块改高"的增长全部覆盖，不必在各增长点各自判断。**不用 Textual 自带的 `anchor()`**：它把贴底位置算成 `内容底 − 容器高`，内容不足一屏时为负，而合成器经 `set_reactive`（文档明示绕过校验器与 watcher）写入，负偏移会把整块内容推到视口下方（欢迎 Logo 跑到底部、第一轮有输出后整屏上移）；自行赋值则走 `scroll_y` 校验器（夹在 `[0, max_scroll_y]`，不足一屏时即 0），并同步 `scroll_target_y`（滚轮 / 翻页按它做相对滚动，不同步会让贴底后第一下上滚跳到顶部）。

## 任务拆分与分步骤执行

借鉴 opencode 的 TodoWrite：模型用 `todo_write` 工具维护一份会话级步骤清单，把复杂任务拆成可追踪、可展示的步骤逐步执行。清单不是独立于循环的新架构——仍是同一个 Agentic Loop，只是多了"先列计划、边做边更"的纪律：

```
多步任务到达
   │
   ▼
todo_write(全量最新清单)  ── 首次调用：列出完整步骤（pending）
   │                         ▸ 新建清单：对话区展示一次可折叠计划详情
   ▼
逐步执行：开始某步 → todo_write(该步 in_progress) → 执行工具 → 验证
   │                         ▸ 完成 → todo_write(updated)：仅静默刷新侧边栏
   ▼
计划不合理 → todo_write(调整清单 + reason)；用户改主意 → 标 cancelled 保留
```

- **数据模型**：每项含服务端分配的稳定 `id` + `title`（标题，创建后不可变，侧边栏只显示它）+ `description`（可选详情，可改）+ `reason` + `status`（`pending` / `in_progress`（同一时刻仅一个）/ `completed` / `cancelled`）。`todo_write` 传**全量最新清单**（非增量），每次整体替换：带 `id` 的项按 id 匹配（标题不可变，其余字段可更新），无 `id` 时按标题匹配既有项，匹配不到视为新项并分配新 id；空标题忽略、非法状态降级为 `pending`，单份上限 50 步。
- **状态归属**：清单存于 `plan.py` 的进程内单例（会话口径），`/new` 时 `reset()`；`/plan` 命令随时查看当前计划。
- **展示**：仅**新建清单**（此前无未完结步骤）时才在对话区展示一次计划详情，并复用 `todo_write` 的工具块（`display: block`、静态清单图标 `☰`）承载——可展开 / 收起、默认展开；后续每步更新只静默刷新 TUI 侧边栏，不再生成工具行或对话块。REPL 也只在新建时打印 `[计划]`。TUI 侧边栏用 `render_titles` 只展示标题；回传给模型的工具结果保持明文清单，供后续轮次参考。
- **只读**：`todo_read` 随时拉取当前清单权威快照（含 id），支持 `status` 过滤与 `summary_only` 摘要；`todo_write` 与 `todo_read` 均默认 `allow`，可用 `deny` 规则禁用。
- **提示词纪律**：系统提示词要求多步任务（3 步以上）动手前先列清单、完成并验证后才标 completed、更新时用 `todo_read` 取 id 并保留、标题不可变、计划不合理时调整而非无视、单步简单任务不拆分。

## 持久目标（/goal）

借鉴 Codex CLI 的 /goal（Ralph loop 的产品化）：用户用 `/goal <目标>` 声明一个跨回合存活的使命，Agent 在每轮任务结束后自动接续推进，直到模型逐条核验真实证据后声明完成、用户暂停/清除，或回合预算用尽。续跑仍复用同一个 Agentic Loop——不是新的循环，而是"回合结束后是否再开一轮"的裁决与提示词注入：

```
/goal <目标> ──► goal.py（会话单例：状态机 + 提示词）
                   │  CommandResult.start_task
                   ▼
宿主 ──► Agent.run_with_goal()          ← REPL / TUI / 一次性任务共用
           │  run(text) → RunResult(tools_used…)
           │  goal active 且正常结束且用过工具？
           │    ├─ 是 → run(continuation_prompt()) ──► 循环
           │    ├─ 续跑零工具调用 → pause（防空转）
           │    ├─ interrupted → 保留 active、停止循环
           │    ├─ denied / max_iterations → pause
           │    └─ 回合达预算 → budget_limited + wrapup_prompt 收尾轮
           └─ 否 → 返回
```

- **状态归属**：`goal.py` 进程内单例（会话口径，`/new` 时 `reset()`）；字段含目标、状态（`active / paused / complete / blocked / budget_limited`）、回合数/预算、token 差值、证据。`goal_update` / `goal_read` 是模型侧入口（默认 `allow`，可 deny）；`/goal` 命令是用户侧入口（设定并立即开跑、查看、pause/resume/clear/budget）。
- **完成审计**：目标不自动宣告成功。续跑提示词要求模型"重述目标 → 逐条建立要求→证据清单 → 检查真实证据 → 代理信号只在覆盖全部要求时才算数 → 不确定即未完成"，只有证据完备才调用 `goal_update(status="complete", summary=...)`；测试通过、清单全勾、工作量本身都不足以标记完成。系统提示词的「持久目标」规则节与提示词文案两处同步维护。
- **阻碍审计**：`blocked` 需同一阻碍连续出现 3 个回合（中途有推进动作即重置连击）；工作困难/耗时/不完整不算受阻，防止模型用"受阻"逃避目标。
- **预算刹车（可选）**：`[limits].goal_max_turns` 默认 `-1`（不限，`/goal budget N` 或配置为正整数才生效）限制自动推进回合数；用尽后标记 `budget_limited` 并注入收尾提示词（总结进展/剩余/下一步，不得新开实质工作）后停止。默认不限时，目标持续自动推进，直到模型核验证据后声明完成/受阻、用户暂停/清除/中断，或空转刹车触发。中断保留目标（用户主动叫停）；权限被拒/迭代上限暂停目标（继续只会重复失败）。
- **双通道注入**：`session.sync_system()` 把 `goal.render_section()` 追加进系统提示词 `messages[0]`——只含稳定信息（状态/目标/证据），普通回合逐字节不变以保护提示缓存，压缩时 `messages[0]` 保留所以目标不丢；回合数/token 等易变信息放在每轮的续跑提示词（user 消息）里。
- **展示**：TUI 底栏常显 `◎ 目标 3/50`（暂停/完成/受阻换图标），侧边栏在计划区上方展示目标卡片（标题带进度/状态、目标文本、token/用时、证据），续跑、暂停、预算收尾经渲染层输出；`/goal` 查看完整状态（回合、用时、token、证据）。

## 技能（Skills）

借鉴 opencode / Claude Code 的 Agent Skills：技能是磁盘上的文件夹（`SKILL.md` 元数据 + 指令正文，可选 `scripts/`、`references/` 等资源），启动时只把 name + description 装进系统提示词，命中任务后再加载完整指令——渐进式披露让挂很多技能时上下文仍近乎恒定：

```
技能根目录（项目 .agents/skills / 用户 ~/.smithcode/skills / [skills].paths）
   │  启动
   ▼
skills.refresh() ──► frontmatter 宽容解析 ──► 优先级去重（附加 > 项目 > 用户）
   │                        │
   │                        └─ 项目级信任门控（ask/on/off + skills_trust.json）
   ▼
Session.sync_system() ──► messages[0]「可用技能」目录（name + 描述，字符预算降级）
   │
   ▼
模型 ──► use_skill(name) ──► 工具结果 = 第 2 层载荷（正文 + 资源清单，role=tool 进历史）
   │
   └──► 用户 /<技能名> [任务] ──► 同一载荷作为 user 消息进历史（无任务即开跑一回）
   │
   ▼
第 3 层：引用文件用 read_file（技能目录只读免确认），脚本用 run_command（照常权限）
```

- **扫描与优先级**：项目级只扫 `<工作区>/.agents/skills/`，用户级只扫 `~/.smithcode/skills/`，外加 `[skills].paths`；同名"先命中者生效"（附加 > 项目 > 用户），被遮蔽/跳过者进 `/skills` 诊断；单根限深度 4、2000 目录。
- **信任门控**：项目级技能随仓库分发、可能不可信，默认 `[skills].project="ask"` 交互确认（`[a]` 落盘 `~/.smithcode/skills_trust.json`、`[y]` 仅本会话、`[n]` 跳过）；非交互模式 fail-closed 跳过。
- **披露与加载**：目录段注入 `messages[0]`（同 goal 段机制，普通回合逐字节稳定）；加载技能只把第 2 层载荷（前言 + `<skill>` 包装 + 资源清单 + 正文）投进对话——模型侧 `use_skill`（`serial`、enum 约束技能名、默认 `allow`）直接把它作为工具结果返回，用户侧由命令注入为 user 消息。**正文不进系统提示词**：加载不改变 `messages[0]`，提示前缀缓存全程稳定；重复加载不重复注入正文（幂等）：模型侧 `use_skill` 重复调用回一句已加载提示（含历史回找指引 + `read_file` 兜底），用户侧 `/技能名 [任务]` 重复调用静默开跑（注入一句历史回找引导让模型定位此前的载荷，不向用户打印）；无可用技能时工具与目录一起隐藏。载荷超 `[limits].max_tool_output` 时截断并附 `read_file` 指引。
- **用户侧**：`/skills` 无参数弹出技能展示面板（只读：Enter 不确认，仅 ↑↓ 查看、Esc 关闭；加载请用 `/<技能名> [任务]` 直达），`/skills list` 查看来源分组/状态/诊断、`/skills refresh` 重扫磁盘（新装技能无需重启）；技能名即命令：`/技能名 [任务]` 由 commands.dispatch 的兜底分发加载——载荷经 `CommandResult.inject_history` 作为一条 user 消息注入历史，带任务时随后再发起任务消息（用户输入原文整体回显），无任务时载荷本身就是本轮 user 消息（加载后立即开跑一回）；技能名同时并入 `/` 输入补全（功能命令在前、技能按名称在后），与内置命令重名的技能不进命令面（内置命令优先，只能由模型用 `use_skill` 加载）；`disable-model-invocation: true` 的技能只允许手动加载。
- **安全边界**：技能根目录登记为**只读白名单**（`config.read_roots()`），读引用文件免越界确认、不弹权限框；写操作只认授权目录（`_resolve(write=True)`）；frontmatter 的 `allowed-tools` 不产生任何授权效果。
- **会话与压缩**：`/new` 时 `skills.reset()` 清空加载集合（发现结果保留）；正文在对话历史里，压缩会把中段摘要掉——`Agent.compact()` 随后调 `skills.prune_active()` 剔除正文已不在上下文中的技能（按载荷是否完整出现判定，从严）并注入一行提示（需要时重新 `use_skill`），剔除结果立即写入 `t=state` 投影；`/context` 的桶计量如实反映技能成本。

## 项目指令（AGENTS.md）

借鉴 Claude Code / Codex / opencode 的内存文件机制：启动时读取用户级 `~/.smithcode/AGENTS.md`，项目级沿目录链从 git 根（最近的含 `.git` 的祖先目录，`.git` 为文件也算）逐级向下探测到工作区（无 `.git` 时仅工作区），外加 `[instructions].paths` 追加文件，把仓库的开发约定装进系统提示词，模型不必靠猜或反复读说明文件：

- **注入通道**：`Session.sync_system()` 把 `instructions.render_section()` 作为动态段拼进 `messages[0]`（同 skills / goal 机制）——压缩天然保留、普通回合逐字节稳定、恢复会话按磁盘最新内容重建；不进 `t=state` 投影（指令与会话无关，无需持久化与重置）。
- **优先级**：用户级 < 项目级 < `[instructions].paths`（越具体越靠后渲染）；项目级链内 git 根在前、工作区在后（越深越具体）。段内 intro 声明冲突裁决（靠后优先）与安全边界（不得覆盖权限 / 沙箱 / fail-closed；与用户当前明确要求冲突时以用户为准）。
- **装载时机**：与 Codex「每会话装载一次」一致——仅在会话边界（`Agent.start()` / `new_session()`（`/new`）/ `resume()`）调用 `instructions.refresh()`；会话中途修改 / 新增 / 删除指令文件不影响进行中的会话，提示前缀缓存全程稳定，新会话或重启后生效；段内 intro 同步告知模型该语义。指纹（`path, scope, mtime_ns, size`）用于边界处去重，未变化时零读取。`[instructions].enabled=false` 整体关闭；`files`（默认 `["AGENTS.md"]`）控制各根目录探测的文件名，显式空列表表示只加载 `paths`。
- **预算**：`[instructions].max_chars`（默认 8000）按优先级分配——高优先级文件保证完整，低优先级按剩余额度截头并附 read_file 指引，放不下的整体省略并计数。
- **安全**：不做信任门控——注入是纯文本，无法影响代码强制的安全边界（权限引擎 / 路径沙箱 / 非交互 fail-closed）；显式配置的 `paths` 文件缺失 / 不可用警告一次，默认探测位置缺失静默。

## 会话与 /new

`/new` 命令开启新会话：所有会话口径状态的重置集中在 `Agent.new_session()`（`agent.py`），命令层只负责反馈与标记，不感知重置细节。覆盖项：

- `session.reset()`：消息历史清空（系统提示词随历史懒加载）、会话 id 轮换、会话用量清零（"应用启动以来"口径跨 `/new` 存活）
- `permission.new_session()`：清空会话内"总是允许"积累的规则（权限模式档位是用户手动选择，跨会话保留）
- `agent.roots.new_session()`：清空越界确认积累的信任目录（会话沙箱，见 `sandbox.py`）
- `context.new_session()`：压缩计数清零、上一会话的真实 token 锚点作废（旧锚点对新会话的估算对比无意义）
- `reset_read_tracking()`：清空工具侧「已读文件」记录（新会话中未读过的文件重新受 write/edit 前置校验约束）
- `plan.reset()`：清空步骤清单
- `goal.reset()`：清空持久目标（`/new` 即 `/clear` 语义，目标不跨会话保留）
- `skills.reset()`：清空技能加载集合（技能目录与项目信任决定保留，省一次磁盘扫描）

### 会话持久化与恢复

**日志就是事件流**：`~/.smithcode/projects/<项目 slug>/sessions/<会话 id>.jsonl`，一行一个信封
（`{seq, id, type: 类型.版本, version, created, session_id, durable, data}`）。懒物化（首条事件才建
文件）；`[sessions].enabled=false` 或 `--no-session-persistence` 可关闭，写失败只降级为纯内存会话
（记一条警告事件，不阻断 Agent）。`/new` 只闭合旧日志、开新会话——旧会话留在磁盘、仍可恢复。

**写入路径唯一**：`sessions/store.py::append_event(env)`（分配 seq → 序列化 → 追加 → flush）。
谁决定要不要写：`sessions/journal.py`（总线订阅者，只写 `durable=True` 的事件），所以"持久化"
不需要在业务代码里埋调用，新增事件也不会漏记。`sync()`（flush + fsync）只在两个语义点调用——
工具执行前（副作用屏障：本批 `tool_calls` 先落盘，工具才会真正执行）与每轮结束。

**状态由折叠得出**：`sessions/project.py` 的 `fold()` 把事件折成会话视图（消息 / 压缩基线 / 标题 /
模型 / 用量 / 状态检查点）。恢复 = 重放：`load()` 读日志 → 折叠 → 崩溃收尾。所以"上一次会话看到
什么"与"重放出来什么"必然一致（验收断言见 `tests/test_event_sourcing.py`）。

启动入口：`smith -c`（当前目录最近会话）、`--resume [id]`（指定 id/唯一前缀/`.jsonl` 路径；旧 `.json` 可导入），会话内用 `/sessions` 查看与切换（无参弹选择框、选中即切换；`list` 文本列表、`delete` 删除、`<id|序号>` 直接切换），另有 `/rename` 命名、`--name` 启动命名。

恢复时：system 段按最新提示词重建（不入日志）；`HistoryCompacted` 事件重置模型可见投影（旧消息仍在文件里，
可导出/审计）；尾部悬空 `tool_calls` 补「结果未知」占位，且**把占位结果作为事件写回日志**（崩溃修复：
不回写「未执行」——崩溃只证明结果没落盘，工具可能已生效；中段损坏则截断）；goal/plan/技能激活集从
`SessionCheckpoint` 事件恢复（goal 回合计数与 token 基线重置）；权限会话规则、越界信任目录、已读记录
**一律不恢复**（安全优先）。会话 id 沿用，`{$session}` 请求头跨进程稳定。标题在每轮正常结束后由后台模型自动生成（`[sessions].title_model`，用户标题优先），只取首个真实用户轮（中断回写 / 流中断回写 / 压缩提示 / 技能回找引导 / 迭代上限收尾五类记账消息跳过，不污染 payload、不错位轮次边界），单次请求失败不永久放弃——下一轮结束后补试，上限 `TITLE_MAX_ATTEMPTS`（3）次，失败发一条警告事件并说明是否还会补试；到顶后进入 `TITLE_RETRY_ROUNDS`（5）轮冷却再自动探一次，`/model` 切换模型立即重置计数（到顶提示一并告知这两条出路，另提示 `/rename` 手动命名）；TUI 侧边栏顶部常显当前会话标题（未生成时回退首轮 prompt 截断，无历史时隐藏；同内容跳过重写，Shift+Tab 只刷底栏模式段、不碰标题）。完整设计见本文件「会话持久化与恢复」节。

TUI 端的宿主动作由 `CommandResult.session_reset` 标记触发：**彻底清空聊天区**（含欢迎横幅，不追加任何提示文本——清空本身即反馈；REPL 仍打印「已开启新会话。」）、清空计划侧栏与残留的工具块映射、刷新状态栏。

**busy 守卫**（对齐 opencode）：任务运行中 `/new` 被 TUI 拦截，只提示「请等待完成或先按 Esc 中断」而不执行——后台线程仍在写消息历史，中途重置会撕裂进行中的轮次。命令执行时机被约束到 agent 空闲时，从机制上消灭竞态；REPL 为同步运行，命令天然只在空闲时执行，无需守卫。同一守卫覆盖会改动 MCP 配置的操作（`/mcp add|remove|enable|disable|reconnect`）与技能名直达（`/技能名`）：技能加载在分发期就登记进集合、正文要等宿主投递（busy 时被跳过），运行中放行会留下「已加载但正文没进对话」的坏状态。

## MCP（Model Context Protocol）

通过 stdio / Streamable HTTP / SSE 接入外部工具服务器（远程支持 OAuth2.1 授权）。完整设计（分层与职责、数据模型、配置/密钥/OAuth、运行时与竞态处理、与 Agent 交互、线程模型、测试策略）见 [mcp-architecture.md](mcp-architecture.md)。

- **配置双作用域**：用户级 `config.toml` 的 `[mcp.servers.<名称>]`（tomlkit 写入保注释，`env` / `headers` 用内联表使同一条目的属性聚合在同一段）；项目级 `<工作区>/.smithcode/mcp.json`（`mcpServers` 结构，兼容 Claude/Cursor/VS Code 片段写法）。传输类型 `type` 支持 `stdio`（别名 `local`）/ `http`（别名 `remote` / `streamable-http`）/ `sse`；stdio 用 `command` + `env` + `cwd`，远程用 `url` + `headers`（值可含 `${VAR}`）。同名服务器**项目条目整体覆盖**用户条目（字段不合并）；`enabled` 是服务器条目的普通字段，写在定义它的文件里（默认启用时省略），不做跨文件覆盖表。
- **密钥链**：配置只写 `${VAR}` / `${VAR:-default}` 引用（command / args / env / headers / cwd 均展开）；解析顺序为进程环境 > `credentials.json` 的 `mcp.<服务器>.<变量>`（原子写、POSIX 0600）> 向导交互补录；缺失即标记 `missing_env`、不带着空值拉起 server（非交互 fail-closed）。所有展开值登记全局 Redactor，工具结果 / stderr / 日志 / 预览统一过筛。
- **SDK 客户端**（`mcp/runtime.py` + `mcp/connection.py`）：连接基于官方 `mcp` SDK。`AsyncRuntime` 把唯一的 asyncio 事件循环关在专用线程里，`SdkConnection` 对服务层暴露同步门面（握手 / `list_tools` / `call_tool` / `close`），每次调用经 `run_coroutine_threadsafe` 投递并轮询当前线程取消令牌（Esc 取消、超时中断）。SDK v2 不推送「连接断开」事件，故用一层代读泵包装传输，原读流 EOF 即回调 `on_closed`（等价旧读线程的崩溃检测）；stdio 子进程 stderr 落临时文件供 `/mcp logs`；`notifications/tools/list_changed` 在旧协议经 SDK `message_handler` 触发工具刷新、现代协议（2026-07-28+）经 `Client.listen` 订阅流触发（不可用时静默跳过）；工具调用接入 `progress_callback`，按 10% 里程碑发 `Notice` 事件（`total` 未知不外显）；结果用 `model_dump(by_alias=True)` 归一为旧客户端同形 dict，`catalog` 零改动。传输由 `factory.py` 按 `cfg.type` 构造：stdio 走 SDK `stdio_client`；`http` 走 `streamable_http_client`（自持 `httpx2.AsyncClient`，headers 承载静态 token）；`sse` 走 SDK `sse_client`（legacy）；`oauth=True` 的远程服务器在同一 `httpx2.AsyncClient` 上挂 SDK `OAuthClientProvider`（见下条）。
- **OAuth2.1**（`mcp/auth.py`）：SDK 的 `OAuthClientProvider` 负责发现 / DCR / PKCE / 换 token / 刷新，本子系统补齐 token 持久化与浏览器回调。`FileTokenStorage` 把 token / client_info 存 `~/.smithcode/mcp_auth.json`（独立于 config，原子写、POSIX 0600，读写时值登记 Redactor）；交互授权的回调走固定本地端口（`127.0.0.1:3334`，被占用时退随机端口）的临时 HTTP 服务。**后台连接绝不弹浏览器**：无 token 时启动预检直接置 `needs_auth`，只有用户显式 `/mcp auth <名称>` 才以交互模式（`webbrowser.open` + 等回调）完成授权；非交互下 `redirect_handler` 一律抛 `McpAuthError` fail-closed；服务端返回的授权失败也归入 `needs_auth` 并给出可操作提示。
- **工具接入**：连接成功后经 `tools/base.register_dynamic()` 进入同一注册表，与静态工具共用权限 / serial / describe / 展示机制；暴露名 `mcp__<服务器>__<工具>`（非 `[A-Za-z0-9_-]` 替换 `_`、64 截断、冲突补后缀），默认 `ask` + `serial=True`（外部服务器状态未知）；断开或工具列表变化时原子替换注册项。
- **入口**：`/mcp`（状态与操作菜单，选择面板）、`/mcp auth <名称>`（OAuth 浏览器授权）、`/mcp add`（TUI 居中向导面板 / REPL 行式流程，命令层只返回 `CommandResult.wizard` 意图；向导支持模板 / 手动命令 / 远程 URL 三分支）、直通添加 `/mcp add <名称> -- <命令...>`（stdio）或 `--url <地址> [--type http|sse] [--header K=V] [--oauth]`（远程）；`mcp/templates.py` 提供常用模板（filesystem / github / playwright / memory / everything / Linear / Sentry）。
- **服务生命周期**：`Agent.start()` 后台并发连接（失败隔离、不阻塞启动）、`Agent.close()` 统一关闭；连接/刷新在专用线程池，注册表增删走 `tools/base` 的锁。

## 事件架构（event/ + frontend/ + sessions/）

**任何状态变化都是一条事件**：核心只 `publish`，前端 / 会话日志 / 终端标题都是它的
订阅者。会话状态不是被"改"出来的，而是**折叠事件**折出来的（事件溯源）。

```
LLM 流 / 工具 / 权限 / 会话写入（发布方，只 publish）
  │  event.publish(载荷)  ← Agent._emit 装信封（id/type/version/created/session_id/durable）
  ▼
event/bus.py（每会话一条总线：唯一扇出点）
  ├─→ 前端订阅者：frontend/console.py（终端）、tui/frontend.py（界面）
  ├─→ title.TerminalTitlePresenter（终端标题状态机）
  └─→ sessions/journal.py：durable 事件落盘（一行一个信封，带 seq）
                          + 折叠进会话视图（sessions/project.py）
```

- **信封与声明**：事件类只在 `event/catalog.py` 定义，用 `@declare(type, durable, version,
  aggregate)` 声明类型名（稳定契约，写进日志）、是否持久、聚合根字段、版本。信封由
  `event/envelope.py` 负责，`id` / `created` / `session_id` 由发布侧注入，调用方不手写。
- **持久 / 易失二分**：边界类事实（消息入历史、执行与步骤起止、工具起止、询问结果、
  标题、模型、用量、压缩基线、检查点）是 `durable=True`，进日志可回放；流式增量、
  通知、进度、忙碌态是易失的，前端丢一帧无所谓（重放骨架就能重建视图）。
- **唯一写入口**：`event.publish()`（发布）、`bus.subscribe()`（订阅）、`session.add()`
  （消息）、`session.record()`（会话状态）、`event.asks` 端口（询问）各只有一个；守卫
  测试静态扫描，散出去会红（见 `tests/guard/test_single_event_path.py`）。
- **询问端口（asked → replied）**：`event/asks.py` 发布 `PromptStarted` → await 当前前端
  作答 → 发布 `PromptFinished`。前端只实现一个 `async ask(AskRequest) -> AskAnswer`
  （终端读 stdin、TUI 挂面板 + await Future）；同步上下文（命令层）用 `ask_sync()` 借
  运行中的循环投回协程。收尾时 `AskPort.cancel_in_flight()` 统一取消挂起询问并按
  fail-closed 放行等待方——**没有阻塞在非 daemon 线程里的等待**，退出卡死那类故障从
  根上消失。
- **会话日志 = 事件日志**（`sessions/`）：一行一个信封（类型名带版本，读回按"不超过该
  版本的最新一个"取类），写失败降级为纯内存会话；`load()` 重放全部事件并折叠，崩溃
  收尾（尾部悬空 `tool_calls` 补占位）**作为事件写回日志**，于是那次恢复本身也可回放。
- **验真方式**：重放日志折叠出的视图必须与在线视图逐字段相等，且总线上发过的 durable
  事件一条不漏（`tests/test_event_sourcing.py`）。

### 每轮页脚：哪些必须存、哪些算出来

聊天区每条回复下面那行 `▣ 模型 · 思考强度 · 用时`（中断时带 `· 已停止`，流断开带
`· 输出中断`）**不是一条事件**——它是投影。事件溯源的通用原则在这里体现得最清楚：
**能算出来的不存，存了就会有两份真相**。

| 页脚字段 | 口径 | 存在哪 |
|---|---|---|
| `model` / `effort` | **本轮 `run()` 开头 pin 住的请求快照**（`TurnConfig.capture`），不是渲染时的全局配置——轮内 `/model` 切换不影响本轮，页脚不会误报 | `session.model.selected`（`ModelSelected`）；**只在 (model, effort) 与上一条不同时发**（调用方去重，避免同一模型连续调用刷屏） |
| `elapsed` | `终结事件.created − ExecutionStarted.created`，两者都是**信封里的墙钟时间戳**（跨进程可比、不受挂起影响） | 不存——从 `Execution*` 事件的 `created` 算 |
| 状态后缀 | `footer_suffix(status, hint)`（`tui/render.py`）：`interrupted` → 「已停止」；`stream_error` → 「输出中断」+ `format_stream_interrupted(reason)` 的可操作提示；其余状态无后缀 | `ExecutionInterrupted` / `ExecutionFailed(status, reason)` |

几个容易踩的口径细节：

- **用时含等待**：起点在 `run()` 进循环之前、终点在 `finally` 发终结事件时，所以
  「在权限框 / 提问面板前等你的时间」也算在这一轮里——它表达的是**墙钟**，不是模型耗时。
- **重算值 ≠ 实时值**：实时用 `time.monotonic() − _turn_start`（`_turn_start` 在宿主**提交任务时**
  打点、`ui_turn_end` 在 UI 侧执行，两头都略宽），所以实时值通常**略大**；量级一致，
  但别指望逐位相同。
- **崩在中间的轮次没有页脚**：没有终结事件就没有终点，重放时那一轮表现为"没结束"
  （崩溃收尾负责把消息历史补合法，但补不出那轮的用时）。
- **口径只有一处**：后缀映射写在 `footer_suffix` 里，实时（`ui_turn_end`）与重放
  （`_replay_events`）都调它——两处各写一份必然漂（曾经重放把 `denied` 标成「失败」、
  还丢了流断开的原因）。

**不在页脚里的**：token 用量在 `session.usage.updated` 与 `StepEnded.usage`；步数 / 工具数
是统计出来的派生量，页脚不显示。

**为什么恢复会话要按事件回放**（而不是遍历 `session.messages`）：消息只是事件的一种投影，
工具调用行（`ToolStart` / `ToolEnd` / `PlanUpdate`）与每轮页脚都只有事件才带得全——只走消息
就会出现"日志里存了、界面不显示"。TUI 的 `_replay_history` 因此按事件重建（`Agent.replayable_events()`
给出日志里的原始事件；将来远程客户端用同一批事件渲染）。

## 终端窗口标题（title.py）

标题**由事件驱动，前端只提供写入通道**：

```
Agent / 权限引擎 / 询问端口（事件源，只 publish）
  │  TitleChanged（标题变化）/ Execution*（忙闲）/ PromptStarted+PromptFinished（等待，按 id 配对）
  ▼
event/bus.py ─→ TerminalTitlePresenter（订阅者：状态机 + 合成 + 生命周期）
                      │  sink(seq): TUI = driver.write（Textual 写入队列，线程安全）
                      ▼            REPL = 真实 stdout 控制序列
```

- **装配入口**：`title.enable_title(sink=…)` 返回呈现器（幂等），宿主自己留着并作为**额外
  订阅者**装配：`frontend.attach(bus, 前端, extra_subscribers=(presenter.on_agent_event,))`。
  只想要事件、不要终端标题的 GUI 前端（desktop / web）不调它即可。
- **订阅入口**：呈现器收的是**信封**（`on_agent_event(env)`），只看 `env.data`；未知事件
  与连信封都不是的对象一律空操作（新增事件类型不得让订阅者抛错）。
- **忙闲与等待**：忙闲由 `ExecutionStarted` / `ExecutionSucceeded|Failed|Interrupted`
  计数（多回合期间不归零）；等待由 `PromptStarted` / `PromptFinished` **按 id 配对**判定
  （`open_prompts` 非空即在等），因此并发的两次提问先结束的那个不会把另一个的 `!` 抹掉。
  等待优先于运行中（前缀 `!` 胜过 `◐`）。
- **合成规则**：`Smith` / `Smith · <会话标题>` / 运行中 `◐ Smith · …` / 等待中 `! Smith · …`；
  空标题回退工作区目录名；写入前去控制字符（标题可能来自模型生成，须防"数据 → 转义序列"
  注入）并截断。
- **写入与恢复**：`OSC 0`（`ESC ] 0 ; … BEL`）设窗口标题 + 图标名；退出用 xterm 窗口标题栈
  （`CSI 22;2t` 压栈 / `CSI 23;2t` 出栈）恢复用户原标题——**不写空标题**（那会把原标题抹掉），
  也不读回原标题（读回要抢 stdin 解析终端应答，而 stdin 归 prompt_toolkit / Textual 独占）。
- **退出钩子**：`atexit`（覆盖 `/exit` / EOF / 第二次 Ctrl+C 的 `SystemExit(130)` / 未捕获异常）
  + `SIGTERM` / `SIGHUP`（链回原处理器，保持退出码语义）；**刻意不注册 SIGINT**——
  `cli._wait_for_task` 用它做「第一次取消任务、第二次退出」，抢占会破坏中断语义。
- **开关与降级**：`SMITHCODE_TERMINAL_TITLE` > `config.toml` 的 `terminal_title` > 默认开；
  非 tty（管道 / CI）或开关关闭时整体空操作，一个字节都不写。

## 模块职责

| 模块 | 职责 |
| ---- | ---- |
| `cli.py` | 参数解析、交互式 REPL、单次任务模式 |
| `commands/` | 斜杠命令框架：注册表（`@register` 装饰器）+ 统一 `dispatch()`，REPL 与 TUI 共用；命令元数据（`accepts_args` / `immediate` / `aliases`）驱动两端行为；`/help` 文案由注册表自动生成，新命令一个文件零改动接入 |
| `event/` | **事件层（L0）**：信封与声明（`envelope.py` / `registry.py` / `catalog.py`）、唯一发布口与订阅口（`bus.py`）、询问端口（`asks.py`）、事件流（`stream.py`）。本层不 import 其他 smithcode 包 |
| `frontend/` | 前端适配层（L3）：`Asker` 协议与上下文、`attach()` 装配入口（订阅事件 + 挂询问端口）、终端前端（`console.py`）与文本化纯函数（`render.py`） |
| `sandbox.py` | 会话沙箱授权目录：工作区快照 / 启动附加目录 / 越界信任目录 / 技能只读白名单 / 临时放行（ContextVar） |
| `agent/` | Agent 循环编排（包）：主循环与流式调度（`agent.py`）、会话外观（`agent_session.py`）、工具执行（`tools_run.py`）、边界钩子（`hooks.py`）、运行中排队（`queues.py`）、取消令牌（`signal.py`）、深层发事件通道（`emitter.py`）；`run_with_goal()` 是 `/goal` 唯一的续跑驱动器（无目标等价 `run()`）；`new_session()` 集中承担 `/new` 的全部会话级重置 |
| `cancel.py` | 协作式取消原语：`CancellationToken`（幂等 cancel / 线程安全查询）、当前令牌的 ContextVar 传播、`RunResult` 结构化结束状态；Esc / Ctrl+C 中断的唯一通道 |
| `process.py` | 外部命令执行的唯一出口：`Popen` 创建、轮询超时、取消判定与跨平台进程树终止（Windows `taskkill /T`、POSIX `killpg` 信号升级）、`ProcessResult` 结构化结果，取消令牌取自当前线程；工具层只负责组装命令与文案映射 |
| `textfile.py` | 文本文件读写的唯一出口：换行风格（LF / CRLF / CR）与 UTF-8 BOM 的探测、LF 归一化与写回还原、`TextFileError` 友好错误。文件工具（read_file / write_file / edit_file / apply_patch / grep）全部经此读写，保证**编辑不改动文件既有的换行风格与 BOM**（见「安全边界」的换行保真条目） |
| `llm/` | 模型交互子系统：`client.py` OpenAI 兼容接口封装（流式、自定义请求头注入、`/models` 拉取）、`retry.py` 重试策略与状态机（分类 / 预算 / 退避 / 状态文案的唯一权威，见「请求保护与重试」）、`models.py` 候选模型目录 `ModelCatalog`（`ModelSource` 三级组合，线程安全；启动同步装载、未配置后台刷新回写缓存）、`usage.py` token 用量、`prompts.py` 系统提示词（行为规则）；`__init__.py` 汇总公共 API |
| `session.py` | 会话聚合根 = **事件折叠出来的视图** + 写入路径（发事件）：`add` / `set_title` / `set_compacted` / 检查点都只发事件，`messages` / `title` / `usage` 是折叠结果；系统提示词装配与原地恢复 |
| `sessions/` | 会话**事件日志**子系统：`format.py`（一行一个信封、版本化类型名、坏行容忍）、`store.py`（唯一写入口 `append_event`、重放加载、项目级列表/查找/删除/导入/清理）、`project.py`（纯折叠：事件 → 会话视图）、`journal.py`（总线订阅者：durable 落盘 + 折叠），标题生成纯逻辑（见本文件「会话持久化与恢复」节） |
| `plan.py` | 任务拆分与分步骤执行：`todo_write` / `todo_read` 维护的会话级步骤清单（id 分配、标题不可变、状态机 + 全量/仅标题两种渲染 + `/plan` 查看） |
| `goal.py` | 持久目标（`/goal`）：跨回合使命的状态机（生命周期、回合预算、token 差值、阻碍审计连击）与续跑/收尾/开始提示词；会话级单例，`/new` 时重置 |
| `title.py` | 终端窗口标题：订阅事件（`TitleChanged` / `Execution*` / 询问事件对按 id 配对判定等待态）合成 `Smith · <会话标题>`（运行中加 `◐`、等待用户输入时加 `!` 且优先），经注入 sink 写 OSC 0，退出时用窗口标题栈恢复原标题（见「终端窗口标题」节） |
| `skills/` | 技能子系统（「技能（Skills）」节的设计落地）：`frontmatter.py` 宽容解析（无第三方 YAML）、`registry.py` 扫描/优先级/信任门控、`state.py` 会话级加载集合与载荷投递（含压缩后裁剪）、`render.py` 目录段与载荷渲染（字符预算降级）；`/new` 时重置加载集合 |
| `instructions.py` | 项目指令（AGENTS.md）装载与注入：用户级 + 项目级 + `[instructions].paths`、`(path, scope, mtime_ns, size)` 指纹变更检测、字符预算截断，渲染系统提示词动态段 |
| `context/` | 上下文计量与运行时压缩包：`meter` 计量（token 估算、`/context` 报告）、`compact` 压缩纯逻辑、`prompts` 压缩提示词 |
| `permission/` | 权限子系统：`engine.py` 规则引擎与确认流程（原 `permission.py`）、`shell_policy.py` Shell 命令静态分析（只读判定 + 前缀推导，命令规范表 `COMMANDS`）；`__init__.py` 汇总公共 API |
| `config.py` | 配置中心：`~/.smithcode/config.toml`（行为配置，含 `[provider.headers]` 自定义请求头与 `[provider].models` 候选模型列表）+ `credentials.json`（凭据），默认 < TOML < 环境变量（仅 `SMITHCODE_KEY/MODEL/URL`）三级解析 |
| `tools/base.py` | 工具注册表（`@register` 装饰器，支持 `pattern_arg` / `family` / `paths_from` / `describe` / `preview` / `serial`） |
| `tools/files.py` | 文件读写，含路径越界检查 |
| `tools/_shared_local.py` | 本地检索共享基座（glob / grep 共用：SKIP_DIRS、沙箱根判定、有序遍历、行截断；下划线前缀=非工具，不注册） |
| `tools/glob.py` | glob 文件名通配检索 |
| `tools/grep.py` | grep 文件内容正则检索 |
| `tools/webfetch.py` | webfetch 网页抓取转结构化文本（仅 http/https，支持批量并行）：HTTP 层走 `utils/http.py`（httpx2 + 环境代理），流式限流读取，用真实浏览器 UA 降低被按机器人 403 的概率；默认拒访内网 / 本机地址并按跳校验重定向（SSRF 防护） |
| `tools/websearch.py` | websearch 网页检索（多后端：Tavily / Brave / Bing，`[search].backend` 可切换；默认 auto 依次回退；默认放行）：与 webfetch 共用 `utils/http.py` 的客户端工厂；Tavily 走 JSON API（需 key），其余两个各一个 HTML 解析函数 |
| `utils/http.py` | 网络工具的 HTTP 客户端工厂：统一代理语义（`normalize_proxy_env` 先归一化 `socks://`，`trust_env` 读 `ALL_PROXY`/`HTTP(S)_PROXY`，socks5 由 socksio 支持）+ 屏蔽 ALPN（历史遗留：早期后端 DuckDuckGo 按 TLS 指纹判定反爬，已移除该后端，屏蔽保留无害）+ SSRF 地址判定（`is_public_address` / `private_target`，见「网络出网」节） |
| `utils/htmltext.py` | HTML → 结构化 Markdown（标准库 `HTMLParser`）：保留标题 / 链接 / 代码块 / 列表 / 表格 / 引用，供 webfetch 输出可读正文 |
| `tools/shell.py` | 命令执行，含超时保护 |
| `tools/patch.py` | apply_patch 批量原子改文件 |
| `tools/ask.py` | ask_user 任务中途向用户提问（复数入参：一个面板一次问 1-N 个问题，可手动切题） |
| `tools/todo.py` | todo_write / todo_read 任务拆分与分步骤执行的状态机与只读快照 |
| `tools/goal.py` | goal_update / goal_read 持久目标的状态声明与权威快照（complete 证据核验、blocked 阻碍门槛），默认放行 |
| `tools/skills.py` | use_skill 技能加载工具（正文作为本次调用的结果返回） + `sync_schema()`（按技能集合同步 enum 与可见性，零技能时隐藏） |
| `mcp/` | MCP 子系统：`config.py` 双作用域配置（env / headers 内联表、条目 `enabled` / `oauth`）、`secrets.py` 引用展开/凭据库/Redactor、`auth.py` OAuth token 持久化与浏览器回调、`runtime.py` 共享 asyncio loop 线程、`connection.py` 基于官方 SDK 的同步门面、`factory.py` 按传输构造连接、`catalog.py` 命名与结果映射、`service.py` 连接生命周期与动态注册、`wizard.py` + `templates.py` 添加向导（TUI/REPL 共用纯状态机）；`commands/mcp.py` 提供 `/mcp` 命令 |
| `tui/` | Textual 全屏聊天界面（仅交互终端加载）：`app.py` 组装层（`SmithTUI` 布局接线 + 集中 CSS）、`chat.py` 对话区语义消息模型（`Level` + `ChatItem`，纯数据，`ChatView.apply` 是唯一打印入口）、`widgets.py` 自包含控件（消息区/折叠块/侧边栏/命令菜单/输入框 + `UiAction` 消息）、`bridge.py` 线程桥（`TuiRenderer`，worker 线程经 `post_message` 投递 UI 事件）、`panels.py` 弹窗面板（权限/提问/通用选择/MCP 向导）、`render.py` 纯函数工具（markdown 渲染、git 分支、token 缩写） |

## 网络出网（utils/http.py）

webfetch / websearch 两个网络工具共用 `utils/http.py` 的客户端工厂 `client()`，与 LLM 客户端（httpx2 + `provider.headers`）共用同一套代理语义：

- **代理**：`trust_env=True` 读取 `ALL_PROXY` / `HTTP(S)_PROXY`，socks5 由 socksio 支持；构造前先跑 `utils.proxy.normalize_proxy_env()`——系统代理（Clash / FlClash 等）常写非标准的 `socks://`，httpx 在构造期即急切校验 scheme 并抛 `ValueError`。**改造前这两个工具走 urllib**，而 urllib 既不认 `ALL_PROXY` 也不支持 socks，于是「LLM 能连、搜索与抓取连不上」。
- **ALPN 屏蔽**（`_NoAlpnContext`）：httpcore 握手时默认发送 ALPN 扩展 `["http/1.1"]`。这是为 DuckDuckGo 时代修的——它据此判定为机器人并返回反爬 challenge 页（实测：带 ALPN 被拦、不带则正常返回结果，与 UA / Accept 等请求头无关）。该后端已移除，屏蔽作为历史对策保留：对现有多后端（Brave / Bing）无害，也免得日后新后端再踩同一个坑。屏蔽时**证书校验照常**（`CERT_REQUIRED` + 主机名校验 + 系统 CA），这也正是改造前 urllib 的既有行为（urllib 不发 ALPN）。回归测试用本地 TLS 服务器在握手层断言（`tests/test_utils_http.py`），不依赖外网。
- **限额**：`read_limited()` 在 `client.stream(...)` 内读满上限即停（webfetch 2MB），超大页面不占满内存。
- **SSRF 防护**：webfetch 默认可免确认放行，模型又可能被网页内容诱导，故默认**拒访非公网地址**（`utils/http.private_target` 判定：回环 / 私网 / 链路本地含云元数据 169.254.169.254 / CGNAT / 保留段 / 多播；域名按 DNS 全部解析结果判定，防「公网域名指向内网」）。**重定向逐跳校验**（`follow_redirects=False` 手动跟随，上限 5 跳），堵住「公网地址 302 到内网」的绕过；解析失败不拦（请求本就连不上，交网络层报常规错误）。开关：`config.toml` 的 `allow_private_urls = true` 或 `SMITHCODE_ALLOW_PRIVATE_URLS=1` 放行，供本地开发抓 localhost 文档。websearch 端点是固定的，无需防护。
- **内容转换**：`utils/htmltext.to_markdown()` 用标准库 `HTMLParser` 把 HTML 转成结构化 Markdown（标题层级 / 链接地址 / 代码块与语言 / 列表 / 表格 / 引用，`<pre>` 公共缩进去除），替代原来的正则去标签——读文档时「链接去哪」「代码长什么样」往往正是重点，正则版会全部丢掉。刻意保守：脚本 / 样式 / `<head>` 一律丢弃，环形引用（畸形标签）不抛异常只降级。**已知局限**：不做正文提取，站点侧边栏 / ToC 等装饰元素会计入 `max_chars` 预算（实测 docs.python.org 的 pathlib 页正文起始于约 1.5 万字符处，改造前后同样如此）。
- **反爬识别**：websearch 未解析出结果且页面符合各后端的反爬特征时（Bing 缺结果容器 `id="b_results"`、HTTP 202/401/403/429），判定为被拦截并返回明确错误，而不是伪装成「（无搜索结果）」——后者会让用户以为关键词不对。webfetch 对 403/429 也在错误文案里说明是被反爬拦截、重试无效并给出替代手段。

## websearch 多后端（tools/websearch.py）

免 key 的搜索引擎在可用性、反爬强度、结果质量上差异极大，且**随网络环境（是否走代理）整体翻转**，故不写死单一后端：

- **Tavily**（`api.tavily.com/search`，JSON API，**需 key**）：免费 1000 次/月，专为 LLM 设计，结果最干净、不受反爬影响，故在 auto 里排第一。未配 key 时直接跳过（不算失败）。key 解析优先级：`SMITHCODE_TAVILY_KEY` > `config.toml` 的 `[search].tavily_key` > `credentials.json` 的 `search.tavily_key`（见 `config.load_tavily_key()`；`smith setup` 可选采集，写入凭据文件并与 LLM key 共存于不同字段）。key 绝不进入工具输出。
- **免 key 后端**：`brave`（`search.brave.com`，结果质量最好，但有速率限制，连打约 6 次会 429）、`bing`（`www.bing.com`，可达性最广，但对部分查询会返回与查询完全无关的「软降级」结果，工具层无法识别）。两者页面结构不同，各自一个解析函数（`_parse_brave` / `_parse_bing`）。
- **选择**：`config.load_search_backend()` 读 `[search].backend`（env `SMITHCODE_SEARCH_BACKEND` 优先），可选 `auto` / `tavily` / `brave` / `bing`，默认 `auto`。`auto` 按 `tavily → brave → bing` 依次尝试，某后端报错、解析为空或未配 key 就试下一个，全失败时在错误里汇总各后端原因。
- **解析为空的二义性**：页面正常但确实没结果是「（无搜索结果）」；命中反爬特征才报错。两者靠各后端的特征区分（见上一条）。


## 安全边界

- **路径沙箱**：所有文件操作经 `_resolve()` 检查，用 `Path.is_relative_to` 确认解析后的真实路径位于工作区内（目录名共享前缀的兄弟路径不会被误判为放行）。
- **权限规则引擎**：三级动作 `allow / ask / deny`，规则 = (工具名, 参数模式, 动作)，通配符匹配，最后一条匹配的规则生效，无匹配默认 `ask`。规则三层叠加：内置默认 < `~/.smithcode/config.toml` 用户规则 < 会话内"总是允许"（命令记 argv 前缀，其余工具按模式串）；内置默认放行只读文件工具与 `webfetch` / `websearch`（抓取/检索网页只读、无本地副作用），用户规则可精确收紧。匹配在 Windows 下大小写不敏感（对齐 opencode v2）。
- **保护路径**：内置默认规则将 `.git` 目录设为只读（禁止写入与编辑），读取放行。
- **技能目录只读**：`skills.refresh()` 把技能根目录写入只读白名单（`config.read_roots()`），读工具（read_file / list_dir / glob / grep）访问技能引用文件免越界确认；写工具与 `apply_patch` 仍只认授权目录（`_resolve(write=True)`），技能文件不可被静默改写。
- **项目指令只读注入**：`AGENTS.md` 等指令文件仅作为文本进入系统提示词（`messages[0]` 动态段），不产生任何授权效果——权限 / 沙箱仍由代码强制，文件内容无法绕过（故不做信任门控）；显式配置的不可用路径警告一次，默认探测位置缺失静默。
- **MCP 密钥与脱敏**：MCP 配置只保存 `${VAR}` 引用，值存 `credentials.json`（`mcp.<服务器>.<变量>`，原子写、POSIX 0600）；展开值登记全局 Redactor，工具结果 / stderr / 日志 / 向导预览统一脱敏；缺失密钥标记 `missing_env` 不拉起 server（非交互 fail-closed）。OAuth 的 token / client_info 存独立文件 `mcp_auth.json`（原子写、POSIX 0600），读写时值同样登记 Redactor，不进入 `config.toml` 与终端输出。
- **MCP 进程与不可信内容**：stdio server 以本机用户权限运行；项目级 `.smithcode/mcp.json` 随仓库分发，启动时对项目服务器给出一次可见警示（按项目决定不做信任门控，删除或停用见 `/mcp`）；MCP 工具默认 `ask` 且串行，工具描述 / annotations / 返回内容按「不可信内容」规则处理（系统提示词有专门行为节）。
- **变更预览**：`write_file` / `edit_file` 在**执行前**（路径预检与权限确认之前）把 unified diff 推送到**工具调用块**——pending 态就地展开，审核时改动内容已可见，权限申请框只展示工具摘要（`describe`）与带说明的选项、不重复 diff；执行后 diff 保留在调用详情里回看（超 40 行截断，失败/被拒不重复展示），写/编辑工具的调用详情**默认展开**。TUI 中该 diff 以**左右对照**渲染——旧/新两栏、各带真实行号，改动行带 `-`/`+` 前缀并红/绿配色，整块统一底色、上下各 1 行 padding、块内不展示文件名（`tui/render.py` 的 `side_by_side_diff`，宽度不足时回退逐行统一 diff），REPL 仍按 `+/-` 行着色打印。`.env` 等敏感文件不生成预览避免密钥回显。其他工具可在注册时声明 `preview` 函数接入同一机制。
- **换行保真**：文件工具读写一律经 `textfile.py`——读时探测换行风格（LF / CRLF / CR）与 UTF-8 BOM 并**归一化为 LF** 交给模型，写时**按原文风格还原**（新建文件默认 LF，不随 `os.linesep` 漂移）。模型因此永远只看 LF 内容，既不会往 CRLF 文件里插 LF 行，也不会出现「只改一行却整个文件换行被重写」的全文件 diff——等价 Claude Code Edit 工具的 encoding/line-ending 策略。混合换行的文件统一为占多数的风格；`.editorconfig` / `.gitattributes` 尚未接入。
- **越界确认**：路径落在授权根之外时先交互确认（`[y]` 仅本次 / `[a]` 本会话总是 / `[n]` 拒绝）。`-y`（approved_all）按"仅本次"静默放行越界访问，不弹确认、不留会话级信任；`deny` 依然生效。
- **非交互 fail-closed**：标准输入非终端（管道 / CI）时无法询问，所有 `ask` 一律拒绝并回传模型，不因 `EOFError` 崩溃。
- **超时保护**：shell 命令默认 60 秒超时；超时或被中断时终止整个进程树（`process.py`）。
- **请求保护与重试**：LLM 请求的 `[limits].llm_timeout`（默认 120s）是**空闲超时**——两次收到数据之间静默超过它就断开，不是整个请求的总时长上限（推理型模型首字延迟长时尤其要按这个口径理解）。重试策略收敛在 `llm/retry.py`（分类 / 预算 / 退避 / 状态文案的唯一权威）：限流、断网、服务端 5xx 与流中途的传输层错误（对端掐断连接 `RemoteProtocolError`、读流超时等）按指数退避重试，2s 起步 ×2、25% 抖动、本地退避上限 30s，服务端 `Retry-After` 优先且只受宽上限（300s）约束——限流窗口常有 60s 量级，砍到 30s 提前重试只会再撞一次；默认 3 次（`[limits].max_retries`）。可重试判定顺序：取消 → 否；5xx → 是；已知永久错误（参数 / 鉴权 / 无权限 / 404）→ 否；已知传输层类型 → 是；最后按**错误文本**兜底（对齐 opencode 的 `RETRYABLE_MESSAGE_PATTERNS`：服务商把瞬时故障包成普通异常时只能看文案，且放在永久错误之后，4xx 响应体里含 `timeout` 不会被误判）。上下文溢出单独排除，由压缩恢复处理。**正文已输出过也照常重试**——整轮报废的代价大于重复显示，上游（opencode / Codex / Claude Code）同为整请求重发；重复的正文由 Agent 侧按「每次尝试的正文都记进同一条 assistant 消息」处理（对齐 opencode 的 text part 累积，见「Agentic Loop」第 5 步）。重试进行态（尝试序号 / 原因 / 恢复时刻）经 `Renderer.retry_started` 上报：TUI 显示在输入框上方的运行动画行（`Working… · 正在重试 2/3 · 8s 后`，倒计时由组件自刷），其他后端降级为一行提示；`retry_finished` 与之一一配对并带 owner，前台任务与后台标题的重试互不误清。退避等待可被取消打断（分段 sleep 查令牌），Esc 不必等满退避时长。
- **输出截断**：单次工具返回超过 `MAX_TOOL_OUTPUT`（默认 2 万字符）时保留头尾、省略中间，防止超长输出撑爆上下文窗口。
- **迭代上限**：默认不限（`[limits].max_iterations = -1`）——只要模型持续请求工具就继续，直到模型给出纯文本回复或用户中断。配置为正整数时封顶；达到上限不再硬中止，而是强制最后一轮纯文本总结收尾（见「Agentic Loop」第 6 步）。

### 权限求值细节

1. 每个工具注册时通过 schema 的 `pattern_arg` 声明权限模式来源（如 `run_command` 用 `command` 参数、文件工具用 `path`），该键不会发送给 LLM。
2. 求值顺序：`DEFAULT_RULES` → `config.toml` 规则 → 会话内 `always` 规则，取**最后一条**匹配的规则。因此配置文件中宽泛规则写在前、精确规则写在后。
3. `deny` 不询问用户直接拒绝；`-y`（approved_all）跳过所有 `ask`（含越界访问确认），但显式声明的 `deny` 依然生效。
4. **权限族（family）**：规则匹配同时看「工具名」与「family」。`apply_patch` 声明 `family="edit_file"`，因此自动继承 `edit_file` 全部规则（含 `.git` 保护路径），避免"换个工具绕过规则"；工具名精确规则排在 family 规则之后可单独收紧。
5. **多资源聚合**：多路径工具（apply_patch）逐路径求值后聚合——任一 `deny` → 整体拒绝，任一 `ask` → 询问一次（逐条列出待确认路径），全部放行才执行；会话内"总是允许"按每个待确认路径的精确模式逐条记忆，同路径后续调用直接放行、新路径仍走确认。
6. **审核前展示变更**：工具的变更预览（如 `write_file` / `edit_file` 的 unified diff）在权限确认与执行**之前**推送到工具调用块（pending 态就地展开）——审核时改动内容已可见；权限申请框则统一展示工具摘要（`describe`，如 `command git status` / `fetch <url>` / `write <path>`）与带说明的 y/n/a 选项；`run_command` 的 `description` 参数会排在命令摘要前，摘要与选项小字均截断到 80 字符，避免长命令 / 长路径撑满弹窗。
7. **复合命令拆分求值**：`run_command` 的命令串按顶层操作符（`&&` / `||` / `;` / `|` / `&` / 换行）切分为子命令逐段匹配规则（引号内不切），聚合语义与多路径一致——任一段 `deny` → 拒绝，任一段 `ask` → 询问，全部放行才放行；含 `$()` 或反引号命令替换（双引号内仍算，单引号内不算）无法静态求值，强制 `ask`。防止"放行 A 后借 `&&` 偷渡 B"绕过规则。
8. **安全只读命令免确认**：内置一批只读命令（`ls` / `cat` / `git status` 等，POSIX 与 cmd.exe 各一套安全集），在内置默认 `ask` 下自动放行以减少确认疲劳。安全层位于「内置默认规则」与「用户规则」之间——**任何用户/会话规则命中都优先**（无论 `ask` 还是 `deny`），因此可用精确 `ask`（如 `git push *`）在保留安全集的同时收紧个别命令，也可用宽泛 `ask` 整体关闭。判定是独立纯函数 `permission/shell_policy.is_safe_command`（按顶层段逐段求值，复用命令拆分），拿不准即回退 `ask`：解析失败、inline 环境变量前缀（`CI=true git commit`）、路径限定的 argv[0]（`./sed`、`/usr/bin/ls`）、写文件/读文件重定向（仅放行 `/dev/null`、`NUL`、`2>&1`）、命令替换、危险标志（`find -delete` / `sort -o` / `git -c` 等）、对有写/exec 能力命令的未加引号 glob、网络与 `env`/`awk`/`docker`/`sed` 等命令一律不纳入。开发工具链仅放行**版本查询 / 只读枚举 / 静态检查**（`python --version`、`pip list` / `show` / `freeze`、`npm ls`、`uv pip list`、`poetry show`、`ruff check` 等）；**真正运行代码的用法一律不放行**（`pytest`、`python x.py`、`node -e`、`npm run`、`uv run`、`cargo test` 等）——没有 OS 沙箱时默认放行任意执行等于放弃安全边界。`cd` 目标须落在授权目录内，同一复合命令里 `cd` 改变目录 + `git` 组合整体降级为询问（git 会执行新目录 hooks）。
9. **"总是允许"命令前缀记忆**：`run_command` 选"总是允许"时不再记整条命令字符串，而是记 **argv 前缀**：`permission/shell_policy.derive_prefix` 把命令归一化成稳定 key（剥 inline 环境变量前缀与 `env`/`command` 包装器；解释器保留 `-m 模块` / 脚本名；其余取从头连续的非标志 token），再按命令规范表的 `arity` / `sub_arity` 切出前缀（`python -m pytest tests/a.py` → `("python","-m","pytest")`、`git commit -m x` → `("git","commit")`、`npm run test` → `("npm","run","test")`）。匹配时对段重新计算 `command_key` 做 token 前缀比较，因此文件/参数变化（`tests/b.py`、`-k foo`、`CI=1` 前缀）都能命中。**拿不准即精确**：未登记命令、标志截断 arity（`git --no-pager log`）、inline 解释器（`python -c`）、shell（`bash -c`）、含危险标志（`ruff check --fix`）一律退回整段精确记忆；`BANNED_PREFIXES`（`uv run` / `python` / `npm run` 裸前缀等）作为最后安全网。复合命令逐段各自记忆；`cd` 改变目录 + `git` 守卫场景不提供"总是允许"。配置文件的字符串规则仍按通配匹配，行为不变。

## 如何新增一个工具

在 `tools/` 下新建文件，用 `@register` 声明 schema，并把模块名加入 `tools/__init__.py` 的导入列表（导入即注册）：

```python
from .base import register

@register({
    "name": "search_code",
    "description": "在工作区内搜索代码片段",
    "parameters": {
        "type": "object",
        "properties": {"pattern": {"type": "string"}},
        "required": ["pattern"],
    },
})
def search_code(pattern: str) -> str:
    ...
```

若该工具属于敏感操作，可通过 schema 的 `pattern_arg` 声明权限模式来源，并在 `~/.smithcode/config.toml` 中为它配置规则：

```python
@register({
    "name": "search_code",
    "pattern_arg": "pattern",
    "description": "在工作区内搜索代码片段",
    ...
})
```

若新工具与既有工具权限语义一致，用 `family` 继承其规则（`apply_patch` 即继承 `edit_file`）；若一次操作触及多个路径（如 patch），用 `paths_from` 提供 `(args) -> [路径...]` 提取函数，走逐路径预检 + 聚合权限检查。

终端展示的短摘要用 `describe` 声明，签名 `(args) -> str`，格式为「短名 + 目标」（如 `read src/a.py`、`command git status`）；未声明时回退为 `[Tool] 名字(参数)` 格式。该摘要统一紧跟权限申请框标题（`允许执行 <工具名>?`）同排展示（灰色小字），因此每种工具的申请都带同样的目标信息：

```python
@register({
    "name": "search_code",
    "describe": lambda args: f"search {args.get('pattern', '?')}",
    ...
})
```

若该工具执行时值得让用户看清改动（如写文件、改文件），用 `preview` 声明 `(args) -> str | None` 的变更预览（如 unified diff，None 表示无可预览内容）。预览由 Agent 在**工具执行前**快照生成（执行后文件已变更，diff 恒为空），推送到工具调用块——pending 态就地展开，权限审核时改动已可见；生成失败只影响展示、不影响确认与执行：

若该工具有跨调用状态或线程不安全（单会话 shell、交互确认、写文件、会话级状态机），用 `serial=True` 声明禁止并行：批量执行时该工具在主线程串行运行、作为顺序屏障，其余只读工具进线程池并发。

未声明 `pattern_arg` 的工具，其权限模式固定为 `*`；默认规则中未覆盖的新工具按 `ask` 处理。
