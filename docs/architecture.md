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
│                 下一轮循环（受 max_iterations 限制）
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
6. 循环超过 `MAX_ITERATIONS` 次则强制终止，防止失控。

### 中断（Esc）

任务可随时被用户中断（TUI 按 Esc、REPL 按 Ctrl+C），走同一协作式取消通道：

1. **发起**：宿主层调 `Agent.interrupt()` 触发当前轮次的取消令牌（`cancel.py`），令牌经 ContextVar 沿调用链隐式传播（`run()` 全程同线程，生成器内可见）。
2. **LLM 流截停**：`llm.py` 打开流后把 `stream.close` 登记为令牌监听——取消线程**直接关流**，即使正阻塞在等下一块数据（模型静默期 / 网络慢）也立即解除；流消费每块数据前再查一次令牌。取消后不再产出 message/usage，关流引发的读错误按取消吞掉；已收到的正文由 Agent 拼成**部分消息**保留入库（残缺的工具调用不回传）。打开流之前也先查令牌——中断后**不再发起任何新请求**（含压缩摘要、溢出恢复重试）。
3. **工具批截停**：`_BatchScheduler` 在**每个待预检项**与每个串行屏障前查令牌——预检阶段（含权限确认）中断时剩余确认框不再弹出、当前项即使刚答 y/n 也不执行、已预检未执行的（缓冲区波次）同样跳过（中断 = 不再发起任何新工作，且优先于拒绝语义）；未执行的计划与剩余 `tool_calls` 补占位结果（与权限被拒共用同一会话修复路径，`tool_call_id` 永不悬空），**非 shell** 的正在执行工具让其自然跑完（线程不可强杀）并照常收集结果。
4. **运行中命令强杀**：正在执行的 shell 命令是例外——`run_command`（serial，跑在 run 线程）经 `process.py` 执行命令，`process.run` 自读当前线程令牌并在轮询中判定取消，触发即**终止整个进程树**（Windows `taskkill /F /T`、POSIX 先 SIGTERM 宽限后 SIGKILL），无需等命令自然结束或撞超时；超时路径共用同一终止逻辑。并行 worker 读不到令牌时安全降级为不响应取消。
5. **收尾**：`run()` 返回结构化 `RunResult`（`ok / interrupted / denied / max_iterations`，`partial` 标记流中截停），令牌在 finally 中复位，下一任务不受残留状态影响；会话历史始终合法，可直接继续追问。中断时 `run()` 还会把事件作为一条 user 注释（`INTERRUPTED_CONTEXT`：任务未完成、部分输出可能不完整）追加进会话历史——**不触发任何新请求**，只是让模型在下一轮提问时知道上一轮是被主动叫停的，避免把部分输出当成完整结果。

模型输出以流式方式逐字显示；思考内容（如 DeepSeek-R1 类模型的 `reasoning_content`）以暗色实时展示，但不写入会话——多数 OpenAI 兼容服务不接受它被回传。

工具调用在执行前打印一行短摘要（`read src/agent.py`、`command git push`，由各工具注册的 `describe` 生成），粒度由 `~/.smithcode/config.toml` 的 `tool_display` 控制：`summary`（默认）到此为止（附带展示 write/edit 的变更预览 diff），`detail` 再以 `[Result]` 追加结果内容（前 500 字符）。展示粒度只影响终端，回传给模型的内容始终是截断后的完整结果；失败信息（`错误: ...`、用户拒绝）无论粒度都原样展示。TUI 侧另有一层纯展示的**上下文汇总**（对齐 opencode 的「已探索」）：连续的读取 / 搜索工具（`read_file` / `list_dir` 计入读取，`glob` / `grep` 计入搜索）汇总成一个可折叠块（头行按类别计数，展开看逐条明细），遇到非上下文工具、助手正文/思考流或回合结束时封口；分组不改变 `ConsoleRenderer` 行为与回传模型的内容。

## 任务拆分与分步骤执行

借鉴 opencode 的 TodoWrite：模型用 `todo_write` 工具维护一份会话级步骤清单，把复杂任务拆成可追踪、可展示的步骤逐步执行。清单不是独立于循环的新架构——仍是同一个 Agentic Loop，只是多了"先列计划、边做边更"的纪律：

```
多步任务到达
   │
   ▼
todo_write(全量最新清单)  ── 首次调用：列出完整步骤（pending）
   │                         ▸ [计划] 共 N 步 实时渲染到终端
   ▼
逐步执行：开始某步 → todo_write(该步 in_progress) → 执行工具 → 验证
   │                         ▸ 完成 → todo_write(该步 completed, 下一步 in_progress)
   ▼
计划不合理 → todo_write(调整清单 + reason)；用户改主意 → 标 cancelled 保留
```

- **数据模型**：每项含服务端分配的稳定 `id` + `title`（标题，创建后不可变，侧边栏只显示它）+ `description`（可选详情，可改）+ `reason` + `status`（`pending` / `in_progress`（同一时刻仅一个）/ `completed` / `cancelled`）。`todo_write` 传**全量最新清单**（非增量），每次整体替换：带 `id` 的项按 id 匹配（标题不可变，其余字段可更新），无 `id` 时按标题匹配既有项，匹配不到视为新项并分配新 id；空标题忽略、非法状态降级为 `pending`，单份上限 50 步。
- **状态归属**：清单存于 `plan.py` 的进程内单例（会话口径），`/new` 时 `reset()`；`/plan` 命令随时查看当前计划。
- **展示**：`todo_write` 的计划无论 display_mode 都完整渲染聊天 [计划] 块（标题 + 描述 + reason，in_progress 加粗、完成/取消置灰），不走 `tool_result` 的粒度分支；TUI 侧边栏用 `render_titles` 只展示标题；回传给模型的工具结果保持明文清单，供后续轮次参考。
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
- **预算刹车**：`[limits].goal_max_turns`（默认 50，`/goal budget N` 可改）限制自动推进回合数；用尽后标记 `budget_limited` 并注入收尾提示词（总结进展/剩余/下一步，不得新开实质工作）后停止。中断保留目标（用户主动叫停）；权限被拒/迭代上限暂停目标（继续只会重复失败）。
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
模型 ──► use_skill(name) ──► 激活集合 ──► 下一轮 messages[0]「已激活技能」正文 + 资源清单
   │
   ▼
第 3 层：引用文件用 read_file（技能目录只读免确认），脚本用 run_command（照常权限）
```

- **扫描与优先级**：项目级只扫 `<工作区>/.agents/skills/`，用户级只扫 `~/.smithcode/skills/`，外加 `[skills].paths`；同名"先命中者生效"（附加 > 项目 > 用户），被遮蔽/跳过者进 `/skills` 诊断；单根限深度 4、2000 目录。
- **信任门控**：项目级技能随仓库分发、可能不可信，默认 `[skills].project="ask"` 交互确认（`[a]` 落盘 `~/.smithcode/skills_trust.json`、`[y]` 仅本会话、`[n]` 跳过）；非交互模式 fail-closed 跳过。
- **披露与激活**：目录段与已激活正文都注入 `messages[0]`（同 goal 段机制，压缩天然保留、普通回合逐字节稳定）；`use_skill` 工具（`serial`、enum 约束技能名、默认 `allow`）只标记激活，正文由下一轮 `sync_system()` 注入；无可用技能时工具与目录一起隐藏。
- **用户侧**：`/skills` 无参数直接弹出技能选择框（TUI 选择弹窗，选中即加载），`/skills list` 查看来源分组/状态/诊断、`/skills refresh` 重扫磁盘（新装技能无需重启）；`/skill <名称> [任务]` 直接加载（激活成功保持静默、不打印提示），带任务时用户输入原文（含技能指令）整体回显为消息后开跑；技能名并入 `/` 输入补全（功能命令在前、技能按名称在后，同名技能不重复），`/技能名 [任务]` 直达与 `/skill` 等价；`disable-model-invocation: true` 的技能只允许手动加载。
- **安全边界**：技能根目录登记为**只读白名单**（`config.read_roots()`），读引用文件免越界确认、不弹权限框；写操作只认授权目录（`_resolve(write=True)`）；frontmatter 的 `allowed-tools` 不产生任何授权效果。
- **会话与压缩**：`/new` 时 `skills.reset()` 清空激活集合（发现结果保留）；正文在 `messages[0]` 所以压缩不丢；`/context` 的 system 桶如实计量技能成本。

## 会话与 /new

`/new` 命令开启新会话：所有会话口径状态的重置集中在 `Agent.new_session()`（`agent.py`），命令层只负责反馈与标记，不感知重置细节。覆盖项：

- `session.reset()`：消息历史清空（系统提示词随历史懒加载）、会话 id 轮换、会话用量清零（"应用启动以来"口径跨 `/new` 存活）
- `permission.new_session()`：清空会话内"总是允许"积累的规则（权限模式档位是用户手动选择，跨会话保留）
- `config.SESSION_EXTRA_ROOTS.clear()`：清空越界确认积累的信任目录
- `context.new_session()`：压缩计数清零、上一会话的真实 token 锚点作废（旧锚点对新会话的估算对比无意义）
- `reset_read_tracking()`：清空工具侧「已读文件」记录（新会话中未读过的文件重新受 write/edit 前置校验约束）
- `plan.reset()`：清空步骤清单
- `goal.reset()`：清空持久目标（`/new` 即 `/clear` 语义，目标不跨会话保留）
- `skills.reset()`：清空技能激活集合（技能目录与项目信任决定保留，省一次磁盘扫描）

TUI 端的宿主动作由 `CommandResult.session_reset` 标记触发：**彻底清空聊天区**（含欢迎横幅，不追加任何提示文本——清空本身即反馈；REPL 仍打印「已开启新会话。」）、清空计划侧栏与残留的工具块映射、刷新状态栏。

**busy 守卫**（对齐 opencode）：任务运行中 `/new` 被 TUI 拦截，只提示「请等待完成或先按 Esc 中断」而不执行——后台线程仍在写消息历史，中途重置会撕裂进行中的轮次。命令执行时机被约束到 agent 空闲时，从机制上消灭竞态；REPL 为同步运行，命令天然只在空闲时执行，无需守卫。

## 模块职责

| 模块 | 职责 |
| ---- | ---- |
| `cli.py` | 参数解析、交互式 REPL、单次任务模式 |
| `commands/` | 斜杠命令框架：注册表（`@register` 装饰器）+ 统一 `dispatch()`，REPL 与 TUI 共用；命令元数据（`accepts_args` / `immediate` / `aliases`）驱动两端行为；`/help` 文案由注册表自动生成，新命令一个文件零改动接入 |
| `agent.py` | Agent 循环编排；`run_with_goal()` 是 `/goal` 唯一的续跑驱动器（无目标等价 `run()`）；`new_session()` 集中承担 `/new` 的全部会话级重置（消息历史、会话用量、权限会话规则、信任目录、上下文快照、已读记录、步骤清单、持久目标） |
| `cancel.py` | 协作式取消原语：`CancellationToken`（幂等 cancel / 线程安全查询）、当前令牌的 ContextVar 传播、`RunResult` 结构化结束状态；Esc / Ctrl+C 中断的唯一通道 |
| `process.py` | 外部命令执行的唯一出口：`Popen` 创建、轮询超时、取消判定与跨平台进程树终止（Windows `taskkill /T`、POSIX `killpg` 信号升级）、`ProcessResult` 结构化结果，取消令牌取自当前线程；工具层只负责组装命令与文案映射 |
| `llm/` | 模型交互子系统：`client.py` OpenAI 兼容接口封装（流式、自动重试、自定义请求头注入、`/models` 拉取）、`models.py` 候选模型目录 `ModelCatalog`（`ModelSource` 三级组合，线程安全；启动同步装载、未配置后台刷新回写缓存）、`usage.py` token 用量、`prompts.py` 系统提示词（行为规则）；`__init__.py` 汇总公共 API |
| `session.py` | 消息历史的增删存取与系统提示词装配 |
| `plan.py` | 任务拆分与分步骤执行：`todo_write` / `todo_read` 维护的会话级步骤清单（id 分配、标题不可变、状态机 + 全量/仅标题两种渲染 + `/plan` 查看） |
| `goal.py` | 持久目标（`/goal`）：跨回合使命的状态机（生命周期、回合预算、token 差值、阻碍审计连击）与续跑/收尾/开始提示词；会话级单例，`/new` 时重置 |
| `skills/` | 技能子系统（`skills-architecture.md` 的设计落地）：`frontmatter.py` 宽容解析（无第三方 YAML）、`registry.py` 扫描/优先级/信任门控、`state.py` 会话级激活集合、`render.py` 目录段与已激活段渲染（字符预算降级）；`/new` 时重置激活集合 |
| `context/` | 上下文计量与运行时压缩包：`meter` 计量（token 估算、`/context` 报告）、`compact` 压缩纯逻辑、`prompts` 压缩提示词 |
| `permission/` | 权限子系统：`engine.py` 规则引擎与确认流程（原 `permission.py`）、`shell_policy.py` Shell 命令静态分析（只读判定 + 前缀推导，命令规范表 `COMMANDS`）；`__init__.py` 汇总公共 API |
| `config.py` | 配置中心：`~/.smithcode/config.toml`（行为配置，含 `[provider.headers]` 自定义请求头与 `[provider].models` 候选模型列表）+ `credentials.json`（凭据），默认 < TOML < 环境变量（仅 `SMITHCODE_KEY/MODEL/URL`）三级解析 |
| `tools/base.py` | 工具注册表（`@register` 装饰器，支持 `pattern_arg` / `family` / `paths_from` / `describe` / `preview` / `serial`） |
| `tools/files.py` | 文件读写，含路径越界检查 |
| `tools/search.py` | 文件名与内容检索（glob / grep） |
| `tools/web.py` | webfetch 网页抓取转纯文本（仅 http/https，支持批量并行） |
| `tools/websearch.py` | websearch 网页检索（DuckDuckGo HTML，纯标准库，返回标题/链接/摘要；默认放行） |
| `tools/shell.py` | 命令执行，含超时保护 |
| `tools/patch.py` | apply_patch 批量原子改文件 |
| `tools/ask.py` | ask_user 任务中途向用户提问（复数入参：一个面板一次问 1-N 个问题，可手动切题） |
| `tools/todo.py` | todo_write / todo_read 任务拆分与分步骤执行的状态机与只读快照 |
| `tools/goal.py` | goal_update / goal_read 持久目标的状态声明与权威快照（complete 证据核验、blocked 阻碍门槛），默认放行 |
| `tools/skills.py` | use_skill 技能激活工具 + `sync_schema()`（按技能集合同步 enum 与可见性，零技能时隐藏） |
| `tui/` | Textual 全屏聊天界面（仅交互终端加载）：`app.py` 组装层（`SmithTUI` 布局接线 + 集中 CSS）、`widgets.py` 自包含控件（消息区/折叠块/侧边栏/命令菜单/输入框 + `UiAction` 消息）、`bridge.py` 线程桥（`TuiRenderer`，worker 线程经 `post_message` 投递 UI 事件）、`panels.py` 弹窗面板（权限/提问/通用选择）、`render.py` 纯函数工具（markdown 渲染、git 分支、token 缩写） |

## 安全边界

- **路径沙箱**：所有文件操作经 `_resolve()` 检查，用 `Path.is_relative_to` 确认解析后的真实路径位于工作区内（目录名共享前缀的兄弟路径不会被误判为放行）。
- **权限规则引擎**：三级动作 `allow / ask / deny`，规则 = (工具名, 参数模式, 动作)，通配符匹配，最后一条匹配的规则生效，无匹配默认 `ask`。规则三层叠加：内置默认 < `~/.smithcode/config.toml` 用户规则 < 会话内"总是允许"（命令记 argv 前缀，其余工具按模式串）；内置默认放行只读文件工具与 `webfetch` / `websearch`（抓取/检索网页只读、无本地副作用），用户规则可精确收紧。匹配在 Windows 下大小写不敏感（对齐 opencode v2）。
- **保护路径**：内置默认规则将 `.git` 目录设为只读（禁止写入与编辑），读取放行。
- **技能目录只读**：`skills.refresh()` 把技能根目录写入只读白名单（`config.read_roots()`），读工具（read_file / list_dir / glob / grep）访问技能引用文件免越界确认；写工具与 `apply_patch` 仍只认授权目录（`_resolve(write=True)`），技能文件不可被静默改写。
- **变更预览**：`write_file` / `edit_file` 在**执行前**（路径预检与权限确认之前）把 unified diff 推送到**工具调用块**——pending 态就地展开，审核时改动内容已可见，权限申请框只展示工具摘要（`describe`）与带说明的选项、不重复 diff；执行后 diff 保留在调用详情里回看（超 40 行截断，失败/被拒不重复展示），写/编辑工具的调用详情**默认展开**。TUI 中该 diff 以**左右对照**渲染——旧/新两栏、各带真实行号，改动行带 `-`/`+` 前缀并红/绿配色，整块统一底色、上下各 1 行 padding、块内不展示文件名（`tui/render.py` 的 `side_by_side_diff`，宽度不足时回退逐行统一 diff），REPL 仍按 `+/-` 行着色打印。`.env` 等敏感文件不生成预览避免密钥回显。其他工具可在注册时声明 `preview` 函数接入同一机制。
- **越界确认**：路径落在授权根之外时先交互确认（`[y]` 仅本次 / `[a]` 本会话总是 / `[n]` 拒绝）。`-y`（approved_all）按"仅本次"静默放行越界访问，不弹确认、不留会话级信任；`deny` 依然生效。
- **非交互 fail-closed**：标准输入非终端（管道 / CI）时无法询问，所有 `ask` 一律拒绝并回传模型，不因 `EOFError` 崩溃。
- **超时保护**：shell 命令默认 60 秒超时；超时或被中断时终止整个进程树（`process.py`）。
- **请求保护**：LLM 请求默认 120 秒超时；限流、断网、服务端 5xx 按指数退避自动重试（默认 3 次），已开始输出的流不重试。
- **输出截断**：单次工具返回超过 `MAX_TOOL_OUTPUT`（默认 2 万字符）时保留头尾、省略中间，防止超长输出撑爆上下文窗口。
- **迭代上限**：默认 30 轮，防止 Agent 无限循环消耗 token。

### 权限求值细节

1. 每个工具注册时通过 schema 的 `pattern_arg` 声明权限模式来源（如 `run_command` 用 `command` 参数、文件工具用 `path`），该键不会发送给 LLM。
2. 求值顺序：`DEFAULT_RULES` → `config.toml` 规则 → 会话内 `always` 规则，取**最后一条**匹配的规则。因此配置文件中宽泛规则写在前、精确规则写在后。
3. `deny` 不询问用户直接拒绝；`-y`（approved_all）跳过所有 `ask`（含越界访问确认），但显式声明的 `deny` 依然生效。
4. **权限族（family）**：规则匹配同时看「工具名」与「family」。`apply_patch` 声明 `family="edit_file"`，因此自动继承 `edit_file` 全部规则（含 `.git` 保护路径），避免"换个工具绕过规则"；工具名精确规则排在 family 规则之后可单独收紧。
5. **多资源聚合**：多路径工具（apply_patch）逐路径求值后聚合——任一 `deny` → 整体拒绝，任一 `ask` → 询问一次（逐条列出待确认路径），全部放行才执行；会话内"总是允许"按每个待确认路径的精确模式逐条记忆，同路径后续调用直接放行、新路径仍走确认。
6. **审核前展示变更**：工具的变更预览（如 `write_file` / `edit_file` 的 unified diff）在权限确认与执行**之前**推送到工具调用块（pending 态就地展开）——审核时改动内容已可见；权限申请框则统一展示工具摘要（`describe`，如 `command git status` / `fetch <url>` / `write <path>`）与带说明的 y/n/a 选项。
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
