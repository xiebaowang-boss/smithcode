# Changelog

本项目的所有显著变更都记录在本文件中。

## [未发布]

### 新增

- **`websearch` 网页检索工具**：新增 `tools/websearch.py`，用 DuckDuckGo HTML 版检索网页并返回若干结果的标题 / 链接 / 摘要，纯标准库实现（`urllib` + 正则，无 API key、无新依赖，与 webfetch 同款哲学）；DuckDuckGo 的跳转链接（`//duckduckgo.com/l/?uddg=<真实地址>`）自动还原为真实 URL，结果按 URL 去重，`max_results` 控制条数（默认 5、上限 10）。与 webfetch 分工：websearch 给候选，需要正文时再对结果链接调用 webfetch。默认权限 `allow`（只读、无本地副作用，用户可收紧或 deny），可与只读工具并行。此前系统提示词与 CHANGELOG 已提及 `websearch` 但工具并不存在（幽灵能力），本次补齐

- **ask_user 支持一次提多个问题**（复数入参 + 单面板切换 + 答完确认页）：`ask_user` 入参由单数 `question` 改为复数 `questions`（1-N 项，每项 `{question, options?, multiple?}`），可把相关的多个决策一次问完，避免来回打断。工具把入参归一化后交给 `Renderer.ask_form`——CLI 逐题串行提问（多题带 `(i/n)` 前缀），TUI 用**一个 `QuestionPanel` 承载全部问题**：标题显示当前题号与已答标记（`(2/3) ✔ …`），←/→ 或 Tab / Shift+Tab 手动翻页（已答题可回跳修改），单选 Enter 即答、多选空格勾选 + Enter 提交，答完**按顺序进下一题**（回改中间某题也一样，不会跳到确认页；只有提交最后一题时才回头补前面漏答的题）。**多个问题时全部答完后进入确认页**（标题「确认提交」，不计入 `(i/n)` 编号，只是沿用同样的翻页交互）：把各题答案列在**其问题下方**供核对，Enter 直接提交整组、←/→ 可切回任一题修改，无需选中某行——避免最后一题答完即提交、没有修改余地；单问题不进入确认页（直接提交）。Esc 取消整组。回传格式：单题直接返回答案（与旧行为一致），多题按「编号. 问题 → 答案」逐行列出、未答项标「已取消」。`QuestionPanel` 的结果键由 `value` 改为 `values`（列表），TUI 侧新增 `TuiRenderer.ask_form`、`show_question_panel` 签名改为 `(questions, result, evt)`，系统提示词同步补充多题用法与返回格式

- **TUI「已探索」上下文汇总**（对齐 opencode）：TUI 中连续的读取 / 搜索工具（`read_file` / `list_dir` 计入读取，`glob` / `grep` 计入搜索）不再逐条平铺，而是汇总成一个可折叠块——头行进行中显示 `⠋ ⚙ 正在探索 · 3 次读取，2 次搜索`，完成后 `▸ ⚙ 已探索 · …`（只列非零类别），展开可见逐条明细；遇到非上下文工具、助手正文/思考流或回合结束时封口，之后的上下文工具另起一组。分组只影响 TUI 展示，`ConsoleRenderer` 与回传给模型的内容完全不变。实现：`Renderer.tool_call` 新增可选 `name` 参数（Agent 预检传入工具名，不靠解析摘要猜工具），TUI 侧新增 `ContextGroup` 控件与 `ChatView.place_tool` / `mark_tool_done` 分组生命周期，分类与中文汇总为 `tui/render.py` 纯函数（`CONTEXT_TOOLS` / `context_category` / `context_summary`）
- **技能（Agent Skills）**：兼容 agentskills.io 开放格式（`SKILL.md`：YAML frontmatter 的 `name` + `description`，正文写指令，可选 `scripts/`、`references/` 等资源），采用渐进式披露——启动只把技能名与描述装进系统提示词（约 100 token/技能，带字符预算三级降级），命中任务后由模型调用 `use_skill` 加载完整指令，资源文件按需读取：
  - 新增 `skills/` 子系统：`frontmatter.py` 自研宽容解析器（支持引号、`|`/`>` 块标量、`description` 内冒号，不引入 PyYAML；缺 `description` 才跳过，`name` 不符目录名等只告警）；`registry.py` 扫描发现（项目 `.agents/skills/` + 用户 `~/.smithcode/skills/` + `[skills].paths`，递归深度 4、跳过 `.git`/`node_modules`、同名"附加 > 项目 > 用户"先命中生效并记诊断）；`state.py` 会话级激活集合；`render.py` 目录段与已激活段渲染
  - 项目级技能来自可能不可信的仓库，默认 `[skills].project = "ask"` 首次发现时确认（`[a]` 落盘 `~/.smithcode/skills_trust.json` 始终信任、`[y]` 仅本会话、`[n]` 跳过；非交互 fail-closed 跳过）；`[skills].enabled` 总开关、`[skills].disabled` 通配禁用（整条从目录与工具 enum 隐藏，仅 `/skills` 诊断可见）、frontmatter `disable-model-invocation: true` 仅允许手动加载；`allowed-tools` 不产生任何授权效果
  - 激活正文注入 `messages[0]` 的「已激活技能」段（复用压缩保留 system 的既有语义，技能指令不会被摘要掉、重复激活自动去重）；`Agent._run_loop` 每轮同步系统提示词使激活下一轮即生效；`/new` 清空激活集合（目录与信任保留）
  - 用户侧新增 `/skills`（无参数直接弹出技能选择框，选中即加载；`/skills list` 查看来源分组、激活/禁用状态与扫描诊断，`/skills refresh` 重扫磁盘、新装技能免重启）与 `/skill <名称> [任务]`（直接加载且保持静默、不打印"已加载技能"提示，带任务时用户输入原文（含技能指令）整体回显为消息后立即开跑）；技能名并入 `/` 输入补全（TUI 命令菜单与 REPL 补全共用，功能命令在前、技能在后；同名技能不重复），`/技能名 [任务]` 直达与 `/skill` 等价
  - 安全边界：技能根目录进只读白名单（读引用文件免越界确认），写工具/`apply_patch` 仍只认授权目录；无可用技能时 `use_skill` 工具与目录段一起隐藏（`tools/base.py` 新增 `HIDDEN` / `visible_schemas()`）
- **持久目标 `/goal`（对齐 Codex CLI 的 goal 模式）**：`/goal <目标>` 设定一个跨回合存活的使命，Agent 在每轮任务结束后自动接续推进，直到逐条证据核验通过后声明完成、用户暂停/清除，或回合预算用尽。续跑复用既有 Agentic Loop（`Agent.run_with_goal()`，REPL / TUI / 一次性任务共用），不是新的循环：
  - 新增 `goal.py` 会话级状态机（`active / paused / complete / blocked / budget_limited`、回合预算、token 差值、证据），`/new` 时随会话重置；`/goal` 无参查看完整状态（回合/用时/token/证据），`pause / resume / clear`（别名 `stop` `off` `cancel` `reset`）`/ budget N` 控制生命周期；设定与恢复返回新的 `CommandResult.start_task` 让宿主立即在后台开跑（TUI 底栏常显 `◎ 目标 3/50`，暂停/完成/受阻换图标；侧边栏在计划区上方展示目标卡片：标题带进度/状态、目标文本、token/用时、证据，`refresh_status` 驱动）
  - 模型侧新增 `goal_update`（仅 `complete` / `blocked` 两个状态）与 `goal_read`（权威快照）工具，默认 `allow`（可 deny）：完成必须逐条核验真实证据（文件内容、命令输出、测试结果），测试通过/清单全勾等代理信号只在覆盖全部要求时才算数，不确定即未完成；`blocked` 需同一阻碍连续 3 个回合且无用户输入无法继续（中途有推进动作即重置连击），工作困难/耗时/不完整不算受阻
  - 自动续跑刹车：续跑轮没有任何工具调用即暂停（防空转）；用户中断保留目标但停止循环；权限被拒/迭代上限暂停目标（继续只会重复失败）；回合预算（`[limits].goal_max_turns`，默认 50）用尽后标记 `budget_limited` 并注入收尾提示词（总结进展与下一步、不得新开实质工作）后停止
  - 提示词双通道注入：系统提示词新增「持久目标」行为规则节，`Session.sync_system()`（原 `ensure_system`）在目标变更时把 `goal.render_section()` 追加进 `messages[0]`——只含稳定信息，普通回合逐字节不变以保护提示前缀缓存，压缩保留 `messages[0]` 所以目标不丢；续跑/收尾/开始提示词（Codex continuation / budget_limit 的中文化，含完成审计清单与剩余预算）作为 user 消息注入
  - `RunResult` 新增 `tools_used`（本任务执行过的工具名，去重保序），供续跑裁决与阻碍连击重置使用
- **"总是允许"改为命令前缀记忆**：`run_command` 选"总是允许"时不再记整条命令字符串（此前 `pytest tests/a.py` 换成 `tests/b.py` 就失效），而是记 **argv 前缀**——`shell_policy.derive_prefix` 先把命令归一化成稳定 key（剥 inline 环境变量前缀与 `env`/`command` 包装器；解释器保留 `-m 模块` / 脚本名；其余取从头连续的非标志 token），再按命令规范表切出前缀：`python -m pytest tests/a.py` → `("python","-m","pytest")`、`git commit -m x` → `("git","commit")`、`npm run test --watch` → `("npm","run","test")`。匹配时对段重新计算 `command_key` 做 token 前缀比较，因此文件/参数变化（`tests/b.py`、`-k foo`、`CI=1` 前缀）都能命中；前缀只覆盖该命令族，`python -m http.server` 不会被 `python -m pytest` 的记忆放行。**拿不准即精确**：未登记命令（不做首 token 放宽，避免 opencode 的 `rtk *` 类问题）、标志截断 arity（`git --no-pager log`）、inline 解释器（`python -c`）、shell（`bash -c`）、含危险标志（`ruff check --fix`）一律退回整段精确记忆；`BANNED_PREFIXES`（`uv run` / 裸 `python` / 裸 `npm run` 等）作为最后安全网。复合命令逐段各自记忆；`cd` 改变目录 + `git` 组合守卫场景不提供"总是允许"（只给 y/n）。确认框会显示 `总是允许将记住: python -m pytest *`。用户 `config.toml` 的字符串规则仍按通配匹配，行为不变
- **权限相关代码收拢为 `permission/` 包，命令策略合并为 `shell_policy.py`**：原 `permission.py` 与 `shell_policy.py` 移入 `permission/` 包（`engine.py` / `shell_policy.py`），`__init__.py` 汇总公共 API（外部 `from smithcode.permission import ...` 不变）；命令策略把散落的 per-command 判定（`POSIX_SAFE` / `WINDOWS_SAFE` / `_HANDLERS` / `UNSAFE_FLAGS` / `GLOB_RISK` / `GIT_READONLY`）统一成一张命令规范表 `COMMANDS`（每个命令一行，声明 `safe` / `platforms` / `arity` / `sub_arity` / `mode_flags` / `inline_flags` / `script` / `unsafe_flags` / `glob_risk`），安全只读判定与前缀推导共用同一套 tokenizer/守卫/规范，新增命令只改表。`permission/engine.py` 命令路径改为基础 `(动作, 待确认段, 是否可记忆)` 三元组，`_ask` 按段生成记忆候选
- **模型交互代码收拢为 `llm/` 包**：`llm.py` / `models.py` / `usage.py` / `prompts.py` 移入 `llm/`（分别变成 `client.py` / `models.py` / `usage.py` / `prompts.py`），`__init__.py` 汇总公共 API（`from smithcode.llm import LLMClient / ModelCatalog / ...`）；`session.py`（消息历史）与 `context/`（上下文计量与压缩）留在顶层，`agent.py`（编排层）与 `renderer` / `cancel` / `config` 等共享基础设施同样在顶层

- **安全只读命令免确认**（对齐 Claude Code 的内置只读命令集）：`run_command` 在内置默认 `ask` 下，对一批只读命令（`ls` / `cat` / `head` / `grep` / `git status` 等，POSIX 与 cmd.exe 各一套安全集）自动放行，减少确认疲劳。安全层插在「内置默认规则」与「用户规则」之间——**任何用户/会话规则命中都优先**，因此可用精确 `ask`（如 `git push *`）在保留安全集的同时收紧个别命令，也可用宽泛 `ask` 整体关闭。判定为独立纯函数模块 `shell_policy`（`is_safe_command` 按顶层段逐段求值，复用既有命令拆分），拿不准即回退 `ask`：解析失败、inline 环境变量前缀（`CI=true git commit`）、路径限定的 argv[0]（`./sed`、`/usr/bin/ls`）、写文件/读文件重定向（仅放行 `/dev/null`、`NUL`、`2>&1`）、命令替换（`$()`/反引号）、危险标志（`find -delete` / `sort -o` / `date -s` / `git -c` 等）、对有写/exec 能力命令的未加引号 glob（`find *` 类绕过）、网络工具（`curl`/`wget`）以及 `env` / `awk` / `uniq` / `docker` / `sed`（脚本 `w`/`e` 可写文件/执行）一律不纳入。开发工具链只放行**版本查询 / 只读枚举 / 静态检查**（`python --version`、`pip list` / `show` / `freeze`、`npm ls`、`uv pip list`、`poetry show`、`ruff check` 等，并屏蔽 `--outdated`/`-i`/`--fix` 等网络与写入标志）；**真正运行代码的用法一律不放行**（`pytest`、`python x.py`、`node -e`、`npm run`、`uv run`、`cargo test` 等）——没有 OS 沙箱时默认放行任意执行等于放弃安全边界；`cd` 目标须落在授权目录内，同一复合命令里 `cd` 改变目录与 `git` 同时出现时整体降级为询问（git 会执行新目录 hooks）

- **Esc 中断当前任务**（对齐 Claude Code / opencode 的 Esc 语义）：TUI 中任务运行时按 Esc 即时中断——LLM 流被取消令牌立即截停（打开流时把 `stream.close` 登记为令牌监听，取消线程直接关流，即使正阻塞在等待下一块数据也会即刻解除；已收到的正文以**部分消息**保留入库），工具批在**每个预检项**与波次边界截停（预检阶段中断时剩余确认框不再弹出、已确认未执行的同样跳过、确认期间中断优先于拒绝语义；正在执行的让其跑完、未执行的补占位结果，`tool_call_id` 永不悬空），会话历史始终合法、可直接继续追问；空闲时按 Esc 清空输入框。REPL 中 Ctrl+C 走同一协作式取消通道（任务移入后台线程，主线程专职取消；此前 Ctrl+C 会直接退出整个程序），第一次 Ctrl+C 触发中断、再按一次直接退出进程（长命令最长可能要等 300s，提供明确逃生通道，对应 TUI 的 Ctrl+Q）。配套机制：
  - 新增 `cancel.py`：`CancellationToken`（幂等 cancel、线程安全查询、监听回调）+ `RunResult` 结构化结束状态（`ok / interrupted / denied / max_iterations`，`partial` 标记流中截停）——`Agent.run()` 返回值从字符串改为 `RunResult`，终止语义不再靠哨兵文本
  - 取消令牌经 ContextVar 沿调用链隐式传播（`run()` 全程同线程，LLM 流层 / 工具调度层按需读取），`chat_stream` 接口零改动
  - 中断与权限被拒共用同一会话修复路径（补占位结果收敛为 `_placeholder` / `_interrupt_batch`），`_execute_batch` 终止状态改为枚举（`denied` / `interrupted`）
  - TUI 收尾（运行动画停止、轮次页脚、输入框聚焦）复用既有 `finally` 路径，中断后自动归位；权限 / 提问面板与选择弹窗的 Esc 取消语义不受影响
  - **运行中的 shell 命令可被即时终止**：新增 `process.py` 作为外部命令执行的唯一出口——`run_command` 改走 `Popen` + 轮询，超时或被中断时终止整个进程树（Windows `taskkill /F /T`、POSIX 先 SIGTERM 宽限 2s 再 SIGKILL），不再等命令自然结束或撞 300s 超时；取消令牌由进程层自读（serial 工具运行在 run 线程），工具侧零接线，工具层只做结果文案映射
  - **中断后不再发起新请求**：`compact` 的摘要重试循环与 `_stream_once` 打开流之前都先查令牌，中断时静默跳过压缩（不再白跑空请求、不再打印「摘要未按模板生成」噪音），溢出恢复重试也不会再发请求

- **批量工具调用两阶段并发执行**（对齐 Claude Code 的分区 + 读写锁思路）：模型一次返回的多个 tool_calls 改为两阶段处理——第一阶段（主线程串行）按接收顺序逐个预检（解析参数、渲染摘要、路径预检、权限确认），任一被拒即终止任务，此时还没有任何工具被执行（比旧版"执行到一半被拒"更干净，且所有确认框仍逐个弹出、不会交错）；第二阶段按序分段执行——连续的可并行工具合并为一个波次扔进 `ThreadPoolExecutor`（`[limits].max_tool_concurrency` 上限，默认 5），有状态工具在主线程串行、作为顺序屏障（保证串行工具看见之前所有副作用、后续工具又看见它的改动），结果一律按提交顺序收集、回传给模型的顺序与请求顺序严格一致。配套机制：
  - 工具注册表新增 `serial=True` 声明（`tools/base.py` 的 `SERIAL` 注册表）：`run_command`（单会话 shell，cd/环境变量跨调用携带状态）、`todo_write`（会话级状态机 + 渲染）、`ask_user`（终端交互抢 stdin）、`write_file` / `edit_file` / `apply_patch`（写操作默认串行偏安全）已标注；未声明的只读工具（read_file / list_dir / glob / grep / webfetch / websearch / todo_read）默认可并行
  - "仅本次"越界放行（`widen_roots`）的调用强制串行——临时放行目录全局生效，并行窗口内其他线程会意外获得该目录访问权
  - worker 线程只计算结果字符串，渲染（`tool_result`）全部留在主线程按序执行，TUI / renderer 无需任何线程安全改造
  - 权限被拒时，此前已过预检但未执行的计划一并补占位结果（防悬空 `tool_call_id`）；`max_tool_concurrency=1` 时整条路径退化为纯串行（不启用线程），行为与旧版逐个执行完全一致
  - 系统提示词同步告知模型：只读调用尽量合并在同一条回复里发出、有状态调用永远串行、结果按调用顺序回传

- **流式回复按块增量渲染 Markdown**（对齐 Claude Code / opencode，丝滑无"后渲染"感）：助手正文流式期间即按 Markdown 块边界增量渲染——新增 `split_md_blocks` 切分器（空行分段 + ``` 围栏开/闭状态机：围栏内空行不切、未闭合前整体留在尾部、末尾单个换行不算完结），已完结的段落/代码块用 rich 渲染一次即缓存复用、此后永不重排，尾部未完结块整块重渲染并节流（16ms ≈ 一帧）；流结束时走同一条渲染路径，只剩尾部块的一次定型——消除此前"流式纯文本 → 结束瞬间整段变样式"的整屏跳变。完结块与已渲染前缀按**内容逐块比对**对齐（而非数量计数）：流式 chunk 可能把 `  -` 列表项的前导空格先送来被误判为块边界、下个 chunk 又使其缩回未完结，内容比对可自动发现并重建前缀，任何分块粒度都不丢内容（含回归测试：逐 3 字符分块喂入嵌套列表场景，条目完整上屏）。思考（reasoning）流保持纯文本不受影响

- **webfetch 支持批量并行抓取**：`url` 参数新增列表形式（string 或 string 数组），一次调用最多并行抓取 5 个地址（`MAX_URLS`，超限直接报错并提示拆分），用 `ThreadPoolExecutor` 并发请求，每个地址独立走既有的单页抓取逻辑（协议校验/重定向检查/截断/错误处理互不影响，单个失败不拖垮整批）；多地址结果按 `===== [序号] url =====` 分段返回、顺序与入参一致，纯字符串单地址输出格式不变。要抓很多网页时由系统提示词引导 LLM 并行多次调用本工具、每次不超过 5 个地址
- **输入 `/` 弹出命令菜单**（对齐 Claude Code / opencode）：TUI 中在输入框敲 `/` 即在输入框正上方弹出命令列表（`/命令名 + 中文描述`，**悬浮层**——弹出/收起不挤压聊天区与输入框布局，菜单盖住聊天区底部；候选超过固定展示行数（8 行）时菜单封顶并出现滚动条，↑↓ 移动时选中项自动滚进可视区），继续输入实时前缀过滤，↑↓ 循环移动高亮、Enter/Tab 将选中命令填入输入框（尾随空格，不直接发送）、Esc 关闭；菜单开着时 Enter 只补全不发送，关闭后按键行为不变；命令列表读自命令注册表，新增命令自动进菜单。REPL（prompt_toolkit 行式界面）同步支持：斜杠补全菜单带右侧描述列，Enter 在选中候选时先应用补全、否则照常发送。普通消息文本不受影响（非 `/` 前缀不触发）
- **斜杠命令框架**：新增 `commands/` 包，命令用 `@register` 装饰器声明、导入即注册（与工具注册表同款机制），REPL 与 TUI 共用同一个 `dispatch()` 入口与 `CommandResult` 结果协议（文本/渲染形态/着色/退出/状态刷新等标记），`/help` 文案由注册表自动生成；原先 `cli.py` 与 `tui/app.py` 各自维护的两份 if/elif 命令链与硬编码 HELP 文案删除，新增命令只需一个文件、两端零改动接入。附带行为微调：以 `/` 开头的输入一律走命令分发——未知命令给出提示（此前 REPL 会把未匹配的 `/xxx` 当普通消息发给模型）；无参数命令携带多余参数时提示用法而非静默忽略
- 既有 8 个斜杠命令（`/help` `/new` `/plan` `/save` `/usage` `/context` `/compact` `/exit`）迁移到命令框架，语义与两端表现不变
- **会话级权限模式**（对齐 Claude Code 的模式分派）：Shift+Tab 在 TUI 中循环切换 `Smith`（默认逐个确认）/ `Accept Edits`（编辑族——`edit_file` / `write_file` / `apply_patch`——自动放行，命令仍确认）/ `Auto`（全部 ask 自动放行，等价 `-y`）三档；输入框底行最左侧以 `权限模式 · 模型 · 思考强度` 形式常显当前档位（灰/黄/橙按风险着色），权限/提问面板弹出期间不响应切换。规则引擎之上新增独立模式分派层（`_dispatch_ask`），deny 任何模式都拒绝；`approved_all`（`-y`）改为 `mode == "auto"` 的兼容别名，既有引用零改动
- `run_command` **复合命令拆分求值**（对齐 Claude Code 的命令审批）：命令串按顶层操作符（`&&` / `||` / `;` / `|` / `&` / 换行）切分为子命令逐段匹配权限规则（引号内不切），任一段 `deny` → 整体拒绝（`-y` 也不放行），任一段 `ask` → 整体询问，全部放行才放行——堵住"放行 `git status` 后借 `&&` 偷渡 `rm -rf /`"的绕过洞；含 `$()` 或反引号命令替换的命令无法静态求值，即使外层命令被 allow 也强制 `ask`（POSIX 语义：双引号内仍算替换、单引号内不算）。系统提示词同步告知 agent 逐段求值语义
- 工具调用块展示**变更预览（diff）**，审核前先看清改动，写/编辑工具**默认展开**：
  - `write_file` / `edit_file` 在**执行前**（权限确认之前）把 unified diff 推送到**工具调用块**：pending 态就地展开，审核 y/n/a 时改动内容已可见（权限申请框只负责决策、保持纯净）；执行后 diff 保留在调用详情里回看（diff 在前、执行确认语在后，REPL 与 TUI 一致）。REPL 中按行着色打印（增行绿、删行红、`@@` 位置头青色），TUI 工具块内嵌着色 diff，超 40 行自动截断并提示省略行数；执行确认语（如「已编辑 c.txt」）在**真正调用后**展示
  - `write_file` / `edit_file` 的调用详情在 TUI 中**默认展开**（diff 直接可见，可手动收起）；`read_file` 等读取工具维持默认收起（整文件内容不上屏）
  - 机制上工具注册表新增 `preview` 声明（`describe` 同款零侵入模式），其他工具可按需接入；`.env` 等敏感文件不生成预览避免密钥回显终端，预览生成失败只影响展示、不影响确认与执行
- 自定义请求头 `[provider.headers]`：随每个 LLM 请求发送任意 HTTP 头，值为字符串原样发送、含 `{$session}` 占位符时替换为**当前会话 id**（会话开始与 `/new` 时自动轮换，一次对话内稳定）。适配要求会话级请求头的 OpenAI 兼容网关，如 OpenCode Go 需要每会话稳定的 `x-opencode-session`（`[provider.headers]` 下写 `x-opencode-session = "{$session}"`）。未配置则完全不发送额外请求头，既有行为不变
- **`/effort` 命令：调整思考强度**（交互与 `/model` 一致）：TUI 中输入 `/effort`（无参）弹出居中选择框（选中即执行），`/effort <档位>` 直接切换，底栏「思考强度」实时刷新。候选为**本地默认维护**的完整档位列表（`models.DEFAULT_EFFORTS`，不调用远端接口）：`none / minimal / low / medium / high / xhigh / max`（即 OpenAI `reasoning.effort` 的完整支持范围）。思考强度改为**始终显式下发**，内置默认 `high`（`config.DEFAULT_EFFORT`，`config.toml` 的 `[provider].reasoning_effort` 可覆盖），不再提供「不发送」档。带参切换不校验取值，服务商不支持时可改传其他值
- **`/model` 命令、模型目录与通用选择面板**（对齐 Claude Code / opencode）：TUI 中输入 `/model`（无参）弹出**居中遮罩选择框**（半透明背景 + 居中卡片，↑↓/j/k/数字键选择、Enter 确认、Esc 取消，当前模型标绿「(当前)」），选中后自动切换并刷新底栏；`/model <名称>` 直接切换（本会话生效）。候选由新的 **`ModelCatalog`**（`models.py`，线程安全）统一维护，来源按优先级组合：`config.toml [provider].models`（显式配置，存在即不联网）> `~/.smithcode/models.json` 磁盘缓存 > 远端 `/models`。**启动时**（`Agent.start()`，CLI 调用）同步装载配置/缓存，外部未配置则后台拉取远端 `GET /models` 并回写缓存（按接口地址校验，离线可用、不阻塞启动）；命令层只依赖 `agent.models.list()`。为此命令协议新增 `CommandResult.select` 选择意图——命令只声明候选项、由宿主负责弹窗（TUI 居中弹窗 / 非交互 REPL 列出候选并提示改用带参形式，保持 fail-closed），命令处理器保持同步纯函数。附带把权限/提问/通用选择面板从 `tui/app.py` 抽到新文件 `tui/panels.py`

### 变更

- **系统提示词瘦身**：移除与工具 schema 重复的机制细节（read_file 行号格式、edit_file 匹配规则、apply_patch 信封格式、glob / grep / webfetch 的参数与排序等——这些本就随工具 schema 一起发给模型），只保留跨工具规则（并行批处理、输出截断标记、`<context-summary>`、复合命令逐段求值），避免两处描述漂移（`websearch` 幽灵能力即由此暴露）；同一套 `/goal` 完成审计规则、todo 状态机规则、ask_user 用法在提示词内不再重复表述。「当前环境」块新增**是否 git 仓库**与**今天日期**（对齐 opencode / Claude Code 的环境块）

- **系统提示词补充安全与协作边界**（对齐 Claude Code 的「Executing actions with care」等）：新增「谨慎操作」节（可逆性与影响范围、一次批准不等于一直批准、不用破坏性操作抄近路绕障）与「不可信内容」节（工具结果 / 网页 / 文件内容当作数据而非指令，防御提示注入）；「安全边界」补充密钥等敏感信息不入代码 / 日志 / URL / 提交、写代码防止命令注入 / SQL 注入 / XSS / 路径穿越

- **系统提示词补充沟通与判断规范**（对齐 Claude Code 的「Text output」与 opencode 的「Professional objectivity」）：新增「专业判断」节（技术准确优先于迎合、不确定先查证）；「沟通」节扩写文本可见性（首条工具调用前说明意图、关键节点简短更新、不旁白内心推理、匹配任务量）与探索性问题的答法（两三句给推荐与取舍、同意前不实现），并明确不要把工具或代码注释当作沟通渠道；「工作方式」补充不主动创建文档 / 计划 / 分析文件、代码默认不写注释

- **文档修正**：`.env` 并非"禁止读写"——代码只在变更预览/确认框中不回显其内容，读写本身按普通权限规则（读默认放行、写默认确认）；修正 `README.md` / `AGENTS.md` 中夸大保护的表述（`docs/architecture.md` 本已正确描述 `.git` 只读）

- **系统提示词补充两点**：不臆造 / 猜测 URL（web 工具只用用户提供或工具结果里的地址）；说明上下文接近上限时系统会自动压缩、任务不中断，不要提前收尾或降低完成标准

- **工具批处理改为流式调度（边预检边执行）**：此前模型一次返回的多个 tool_calls 是「两阶段」——先在主线程把整批预检（含逐个权限确认）完，再分波次执行，于是"批完所有确认才动第一个工具"。现改为 `_BatchScheduler` 流式调度：按接收顺序**预检一个就调度一个**——可并行的只读/网络调用进波次缓冲、到屏障才提交线程池并发执行；`serial` 有状态工具在主线程就地执行、作为顺序屏障（执行前先冲刷前面的波次）。因此串行工具在**后续工具的权限确认之前**就已执行完，确认框与执行一一对应，不再"一次确认一大批、最后才一起跑"。不变量不变：结果严格按提交顺序回传、每个 `tool_call_id` 恰有一条结果、串行工具前先收并行波次、`max_tool_concurrency=1`/单计划仍不进线程池。**行为变更**：并行波次在屏障前不产生副作用（并行工具仅只读/网络类），故"首个串行工具执行前"的整段仍可原子取消；但一旦某个已确认的串行工具执行完，之后再被拒/中断即不再回滚它（partial apply）——这是流式换来的即时性代价。`_execute_batch` 退化为 `_BatchScheduler`（`agent.py`）的适配器；中断/拒绝的占位补齐与会话完整性保持不变
- **ask_user 工具块展示优化**：工具块头部由原来的 `[Tool] ask_user({...})` 改为「提问：<问题>」（注册 `describe`），用户回答独立成块（`display: block`）并**默认展开**——页面只展示「提问 + 问题」，正文展示所选选项 / 自定义回答。`_finish` 的默认展开集合由 `FILE_EXPAND_TOOLS` 更名为 `DEFAULT_EXPAND_TOOLS` 并纳入 `ask_user`（write/edit/apply_patch 行为不变）
- **TUI 变更预览改为 IDEA 式左右对照 diff（带行号）**：write/edit/apply_patch 的调用详情不再用 `+/-` 各占一行的统一 diff（改动稍多就拖很长），改为左右两栏对照——左栏旧文件、右栏新文件，各自显示真实行号；删除行在左栏、新增行在右栏，配对到同一行、未配对的一侧留白，**行内容带 `-` / `+` 前缀并做红 / 绿配色**（不依赖颜色也能看出增删）；整块 diff（含上下文与留白侧）铺统一底色 `#141414`、每行等宽铺满，块内**不展示文件名**（工具摘要已含路径），上下各留 1 行 padding，`@@` hunk 头不再占行。实现为零新依赖的纯 stdlib（difflib 解析）+ rich `Text`（`tui/render.py` 的 `side_by_side_diff` / `_parse_unified`）；终端太窄放不下两栏时自动回退为原逐行统一 diff。`ToolCall` 的详情控件改为 `_ToolBody`，在 `render()` 里按当前宽度实时生成，窗口缩放自动重排；折叠态仍按行数截断（新增省略提示）。带 diff 的工具执行成功后不再重复展示「已编辑/已写入/已应用」确认语（审核时已看过 diff），失败仍展示错误。REPL 的 `+/-` 着色打印保持不变
- **TUI 中断提示改挂到「用时」行尾（不再占用对话区）**：按 Esc 中断时不再往对话区打「（正在停止…）」或「⏹ 已中断」行——改为底部运行动画行尾**动态**追加「· 正在停止…」（随动画每 100ms 刷新，计时继续），任务真正收尾后轮次页脚行尾显示「· 已停止」。Agent 不再经 renderer 打印中断行（`INTERRUPTED_NOTE` 保留给 REPL / 一次性任务，由宿主按 `RunResult.status == "interrupted"` 渲染）；TUI 侧 `_run_task` 把最终 status 传给 `ui_turn_end`，`RunningIndicator.mark_stopping` 置停止态，`ChatView.add_turn_footer` 新增可选 `status`
- **`list_dir` 输出规范化**：每行三列「名称  大小  修改时间」——目录在前（以 `/` 结尾、大小列留空），文件在后并标注大小，末尾附本地时间 `YYYY-MM-DD HH:MM`；去掉了每行重复的 `[文件]` / `[目录]` 前缀，与 `glob` 的「相对路径 + 目录带 `/`」约定一致，更省 token 也更好扫读。文件名按终端显示宽度对齐（中文全角按 2 列计），中文名不再错位。工具 schema 描述与系统提示词同步说明各列含义，让模型理解返回信息
- **`/new` 重置收敛与 TUI 清屏**：会话级状态的重置逻辑原先散落在命令层（`commands/session.py` 直接清 session / 权限规则 / 信任目录 / 压缩计数 / 计划清单），现集中为 `Agent.new_session()` 一处，并补齐两项漏网状态——工具侧「已读文件」记录（漏清会让新会话绕过 write/edit 前的已读校验）与上下文计量中的真实 token 锚点 `last_actual`（漏清会让 `/context` 用旧会话的真实值误导对比）。TUI 中执行 `/new` 现在会**彻底清空聊天区**（含欢迎横幅，连带清掉残留的工具块映射与思考块），且不再追加「已开启新会话。」提示文本——清空本身即反馈；REPL 仍打印该提示。**任务运行中 `/new` 会被拦截**（对齐 opencode 的 busy 拒绝）：只提示「请等待完成或先按 Esc 中断」，避免后台线程写历史时中途重置撕裂轮次。新增 `Agent.new_session` / `Permission.new_session` / `ContextMeter.new_session` 三个重置入口与对应测试
- **修复运行计时动画首次显示不可见**：`RunningIndicator` 的 `width: auto` 空组件初始宽度为 0，而每次 tick 的更新走 `layout=False` 免重排——首次 `display=True` 不会触发布局，导致**第一轮任务的「Working…」计时全程渲染了却看不见**（第二次起 `display` 翻转强制重排才恢复）。修复为 `start()` 时立即渲染初始文案并触发一次布局定宽，组件状态初始化挪入 `__init__`；新增回归测试断言首次显示即有宽度
- **时长显示统一为分级格式**：运行中动画与轮次页脚共用新的 `render.format_duration`——不足 1 分钟只显示秒（`42s`），不足 1 小时显示分+秒（`5m 30s`），再往上时+分+秒（`1h 12m 30s`），各级到点才出现；页脚此前超 1 小时也只显示分钟（如 `62m 5s`）。动画文案左对齐定宽，时长逐级变长不改变组件宽度，保持 `layout=False` 免重排的前提
- **`tui/` 包结构化拆分**（纯代码搬移，行为零变化）：此前全部 TUI 布局与逻辑集中在 `tui/app.py`（约 1350 行），现按职责拆为四个文件——`app.py` 只留组装层（`SmithTUI` 布局接线、消息路由、命令分发与集中 CSS），`widgets.py` 收纳全部自包含控件（`ChatView` / `RunningIndicator` / `ThinkingBlock` / `ToolCall` / `Sidebar` / `CommandMenu` / `ChatInput` 与线程安全的 `UiAction` 消息），`renderer.py` 单独承载线程桥 `TuiRenderer`，`render.py` 收纳零 Textual 依赖的纯函数工具（`render_markdown` / `git_branch` / `human_tokens` / `format_duration`）；依赖方向单向（render → widgets → app，panels.py 沿用既有拆分），测试导入同步改为从各模块直连
- TUI 命令菜单支持**选中即执行**：命令可用 `@register(..., immediate=True)` 声明（`/model` 已启用），在菜单里 Enter/Tab 或**鼠标点击**后立即执行——`/model` 直接弹出选择框，不再先填入输入框；其余命令维持原行为（填入 `/{名称} ` 待补参数或回车）。菜单项此前点击无反应，现与键盘接受走同一入口。命令注册表新增 `Command.immediate` 元数据
- 多路径工具（如 `apply_patch`）权限确认的"总是允许"改为**按路径精确模式逐条记忆**：此前会话规则记录的是「多文件（N 个目标）」这种含具体数量的死模式，下次文件数一变就匹配不上、等于失效；现在确认时逐条列出待确认路径，选 `a` 后同路径的后续调用直接放行，新路径仍走确认
- TUI 输入区最终布局：输入框（3 行、左侧绿色竖线）下仅一条底行——**最左依次是模型（蓝色 `#7aa2f7`）· 思考强度（黄色 `#e0af68`）· agent 运行提示**，最右为上下文占用条 + git 分支；模型/思考不再单独占行，去除了此前的状态行、半行淡出收边等中间方案
- TUI 底部状态栏的项目名列改为**上下文占用进度条**（`上下文 ██████░░░░ 60%`，占用率越高由绿→琥珀→红变色），项目名挪进侧边栏底部版本号下方的文件夹路径前，形如 `项目名 | 完整路径`——路径过长被截断时名字仍可见；侧边栏上半的用量/上下文区块同步改版为**「用量」「上下文」两张小卡片**（浅色底、各带小标题）：用量卡为会话人性化摘要（`调用 N · 输入 1.2M · 输出 12.3K`，无调用显示灰色「尚无调用」，有缓存命中时追加一行 `缓存命中 X`）；上下文卡为 20 格彩色占用进度条 + 百分比（与底栏同款 绿→琥珀→红 配色，`████░░  60%`）+ 元信息行 `当前 235K / 预算 128K · 已压缩 M`，不再展示完整 token 数字（需要精确值随时可用 /usage 与 /context 查看）
- 侧边栏两张卡片的标题与正文进一步精简（对齐 opencode 侧边栏）：**「上下文」卡标题改为 `上下文 · 占用百分比`**（百分比彩色，紧跟标题、`·` 分隔），正文只留 `当前 X / 预算 Y`（含压缩次数）；**「用量」卡标题改为 `用量 · 调用 N`**（调用次数上移到标题、`·` 分隔），正文只留 `输入 X · 输出 Y`（缓存命中另起一行）
- TUI 底部状态栏精简：去掉「上下文」与「git」前缀字样，仅保留占用条 + 分支名，分隔符由 ` | ` 改为 `·`（如 `░░░░░░░░░░ 0% · main`）
- 系统提示词改为**懒加载**：`Session` 建会话 / `/new` 时消息历史为空、不再预置系统提示词，真正第一次发请求前（`Agent.run`）才拼装并放入 `messages[0]`。效果：TUI 一打开与 `/new` 后上下文占用显示 0%，只有真实对话才开始计数；消息顺序、压缩、/context 报告等语义不变（`messages[0]` 仍是 system）
- 任务清单升级（对齐 opencode `todowrite` 方向）：
  - 每项拆为 `title`（标题，**创建后不可变**，TUI 侧边栏只展示标题）+ `description`（可选详情，可改）+ `reason` + `status`，并引入**服务端分配的稳定 `id`**——`todo_write` 更新时带 `id` 的项保留原标题、只更新其余字段；无 `id` 时按标题匹配既有项，匹配不到视为新项分配新 id
  - 新增只读工具 `todo_read`：随时拉取当前清单权威快照（含 id），支持 `status` 过滤与 `summary_only` 摘要；权限与 `todo_write` 一致默认 `allow`
  - 聊天 [计划] 块保持全量渲染（标题+描述+reason），TUI 侧边栏改用仅标题渲染；系统提示词补充"更新前 `todo_read` 取 id、标题不可变"的使用纪律
- TUI 侧边栏任务区改为 **opencode 式按需展示**：仅存在未完结步骤（`pending` / `in_progress`）时显示「计划」区块，无任务或全部完成 / 取消时整块自动隐藏（含标题）；聊天消息区的 [计划] 块行为不变
- TUI 修复侧边栏与聊天区分界处的**锯齿/残影**：聊天区 `#chat` 与侧边栏计划区 `#sidebar-plan` 改为 `scrollbar-gutter: stable` 预留固定滚动条槽道，覆盖式滚动条不再反复出现/消失并盖住紧贴分界线的列（部分终端如 PyCharm 内嵌终端对 overlay 滚动条重绘清不干净导致毛边）
- TUI 修复侧边栏**无任务时底部信息被顶到上方**：计划区隐藏（`display:none`）后 Sidebar 内不再有弹性占位，版本号与工作区路径随卡片从顶部堆起。现将用量/上下文卡片与计划区整体包进常驻的 `#sidebar-top`（`height: 1fr`）弹性容器，无论计划区是否展示，底部版本号 / `项目名 | 路径` 始终钉在侧边栏底部
- TUI 修复运行期间**输入框左侧竖线底部抖动**：三处 100ms 动画（运行提示 / 工具行转轮 / 思考转轮）此前每次 `Static.update()` 都触发全屏布局重排（Textual 默认 `layout=True`），输入框 `border-left` 底格紧贴动画所在底行，缝两侧被分批清空重绘导致抖动。现转轮类更新改为 `layout=False`（内容尺寸不变时跳过重排，只做 cell 级 diff 重绘），运行提示的耗时文案改为分段定宽格式（`59s` / `5.3m` / `1.2h`，右对齐恒 5 列），任意时长下文案宽度恒定、全程零重排

- **权限 / 提问面板改为 opencode 式「标题 + 带说明的编号选项」**：两个 composer 位面板顶部只展示标题，选项统一为竖排编号列表（选中行暗色底），有提示或副作用的选项在其下方附一行灰色小字。
  - 权限确认：标题统一为「允许执行 <工具名>?」，工具摘要（`describe`，如 `command git status` / `fetch <url>` / `write <path>`）**紧跟标题同排展示**（灰色小字，与标题留间隔）——此前只有带 `pattern_arg` 的工具（edit / write / run_command 等）有目标、webfetch / todo 等没有，现所有工具一致；「本会话将记住: …」作为「总是允许」项的小字。`Permission.check` / `check_paths` 新增 `content` 参数（由 Agent 传入 describe 摘要），`Renderer.confirm_choice` 新增可选 `descriptions`（按选项键索引）与 `content`；权限信息不再先用 `renderer.info()` 打到聊天区再弹框（去掉重复）；越界路径授权、项目技能信任同样收口
  - 版式：标题与选项区之间增加一行间隔（`.perm-title` / `.ask-title` 加 `margin-bottom`）
  - 提问面板：`ask_user` 每个 option 的 `description` 经 `ask_choice(..., descriptions=...)` 透传并渲染在选项下方（此前被丢弃），标题去掉 `[提问]` 前缀
  - REPL 行为不变：`ConsoleRenderer` 仍把说明打印在提示前 / 选项下方；导航键统一为 ↑↓ / j / k，字母键 y/n/a 与数字键仍是隐藏快捷键
  - 版式对齐：两个面板加 `margin: 0 2` 与输入框左右缩进保持一致；选项说明与「输入自定义回答」输入框的缩进对齐选项文字

- **`webfetch` 默认放行**：`DEFAULT_RULES` 新增 `("webfetch", "*", ALLOW)`，抓取网页不再逐个弹确认（此前无匹配规则默认 `ask`）。抓取是只读网络操作、对本地文件无副作用，故默认放行以减少确认疲劳；需要收紧时用 `[permissions]` 写 `webfetch = "ask"` / `"deny"`（用户规则命中优先于内置默认），`-y` / auto 档与 `deny` 语义均不变。

### 修复

- **中断事件回写上下文，下一轮模型可见**：此前手动中断（TUI Esc / REPL Ctrl+C）后，模型在下一轮只看到被截断的部分输出，并不知道任务是被用户主动叫停的，容易把未完成的中间结果当成最终结果。现 `Agent.run` 在返回 `interrupted` 时追加一条 user 消息（`INTERRUPTED_CONTEXT`：任务未完成、部分输出可能不完整、未执行的工具已标记为未执行），**不触发任何新请求**，只写入会话历史；下一轮用户提问时模型即可看到，作为后续决策依据
- **权限被拒 / 中断时，已预检未执行的 TUI 工具块停在 pending 转轮**：这类计划此前只补了会话占位结果（防悬空 `tool_call_id`），没有更新渲染后端，TUI 对应的工具行——以及「已探索」汇总组里的子项——会一直转轮。现 `_placeholder` 增加可选的渲染器配对 id，拒绝路径与 `_interrupt_batch` 对**已预检**的计划补一次 `tool_result` 收尾；尚未预检的剩余 `tool_calls` 没有控件、行为不变，回传模型的消息序列完全不变（REPL 的 summary 模式输出也不受影响）
- **TUI「已探索」汇总块展开时撑满可用高度**：明细容器 `.group-body` 是 `Vertical`，而 Textual 容器默认 `height: 1fr`，展开即吃掉整块高度。现显式改为 `height: auto` 按内容自适应，并加回归测试断言展开后高度与明细行数相当
- **TUI 因工具摘要/提问文本含方括号而被 Textual markup 解析崩溃**：`Static` 默认按 console markup 解析字符串，当模型给的自由文本含 `[link=https://...]` 一类方括号结构（如 webfetch 的 `fetch <url>` 摘要）时，Textual 抛 `MarkupError` 直接崩掉整个界面（且异常常在退出排布时才暴露）。现把展示动态文本的控件统一禁用 markup：工具调用块头部、思考块头部、运行动画、权限/提问/选择面板标题与提示（正文本就是 rich `Text`，不受影响）——原样展示方括号，不再当样式标签解析
- **TUI 执行 `/new` 后欢迎横幅（Logo）不再消失**：`reset_chat` 清空聊天区后漏了重新渲染欢迎语，导致新会话屏幕只剩空白、启动时的 Logo 不见了。现将欢迎横幅渲染抽为 `SmithTUI._show_welcome`，`on_mount` 与 `reset_chat` 共用——`/new` 后聊天区回归会话起点、Logo 与问候语重新出现
- **read_file 行号分隔符由两个空格改为 `│`，消除 old_string 复制的隐性陷阱**：此前输出形如 `12  code`，行号与正文间的两个空格是**排版分隔符、不属于文件内容**，却极易被当成正文的缩进一并复制进 `old_string`（列首的行 + 长行场景尤甚，如 CHANGELOG 的列表项），导致 `text.count(old_string) == 0` 报「old_string 未找到」而反复踩坑。现改为醒目非空白分隔符 `12│code`，并同步 read_file / edit_file 的工具描述、系统提示词示例与报错文案（`（注意不要把「行号│」前缀复制进去）`），测试断言一并更新

- **多题提问面板回改中间题时直接跳到确认页**：`QuestionPanel._advance` 此前是「跳到下一道**未答题**」（自当前题向后环绕扫描），回改中间某题时因后面都已答完而扫描不到未答题，直接落到确认页——与「改完接着看下一题」的预期不符。现改为**按顺序进下一题**（回改中间题同样进下一题），只有提交**最后一题**时才回头补前面漏答的题（补齐后再进确认页，避免带着空答案提交）；单问题仍直接提交、不进确认页

## [0.7.0] - 2026-09-07

### 新增

- 文件工具对齐成熟 agent（Claude Code / opencode）：
  - `read_file` 返回**带行号**的内容（形如 `12  code`），新增 `offset` / `limit` 分段读取参数（默认一次最多 2000 行、单行截断 2000 字符，尾部附「显示第 X-Y 行，共 N 行」续读提示）；拒绝读取二进制文件、目录，文件不存在返回友好错误（替代裸异常）
  - `write_file` **覆盖已存在文件前必须先 read_file**（会话级「已读文件」追踪，新会话由 `Agent` 初始化时重置；新建文件不受限），防止覆盖未查看的内容
  - `edit_file` 编辑前同样强制先读；新增 `replace_all` 参数（重命名等全部替换场景）；多处匹配的报错改为列出各匹配行号并提示用 replace_all；新增 old_string 为空与「行号前缀勿复制」的防御性提示
  - 系统提示词同步更新：read_file 行号输出与分段读取、edit_file 行号前缀注意事项、write_file 覆盖前先读（工具强制校验）
- `run_command` 新增 `timeout` 参数：默认 60 秒不变，可延长（上限 300 秒，`[limits] command_timeout_max` 可配），超时报错提示延长方式；短摘要同步显示 `timeout=N`。系统提示词改为"跑测试/构建前按预估设置 timeout"
- 系统提示词修正权限拒绝语义与实际行为不一致的矛盾：拒绝即终止任务（对齐 opencode 默认），删除"改用其他方式"的无效承诺
- P1 检索与工具增强（对齐 Claude Code / opencode）：
  - `grep` 新增 `output_mode`（content 默认 / files_with_matches 只列命中文件 / count 每文件匹配数）、`ignore_case` 忽略大小写、`context=N` 显示匹配上下文行（rg 风格：匹配行 `:` 分隔、上下文行 `-` 分隔、组间 `--`）
  - `glob` 结果改为按修改时间新→旧排序（最近改动的文件排前面，定位相关代码更准）
  - `list_dir` 文件带大小标注、跳过 `.git`/`.venv`/`node_modules` 等无关目录
  - `todo_write` 的步骤项 `status` 改为必填（非法值仍降级 pending）
- 新工具 `webfetch`：抓取网页转纯文本（标准库实现，无新依赖），仅放行 http/https（拒绝 file:// 等协议与重定向逃逸），默认最多返回 20000 字符（`max_chars` 可调，下限 500），HTTP 错误/网络失败返回友好错误；默认权限 `ask`（可在 `[permissions]` 配置 allow）

- **全屏聊天 TUI**（Textual 实现，复刻 Claude Code 风格）：交互终端启动 `smithcode` 直接进入全屏界面——上半消息区（助手回复**无前缀纯文本流式**、**思考过程折叠块**：思考时只显示「▸ [思考] 思考中…（N 字符）」计数不刷屏，点击/Enter 展开看全文，与工具调用折叠块同款交互、**用户消息面板**：opencode 式——面板底色 `#141414` + 左侧角色色竖线 + 上下 1 行/左 2 格内边距，无「你>」前缀、**轮次元数据页脚**：每轮任务结束追加 opencode 式「▣ 模型 · 用时 Ns」（▣ 用主色、模型亮色、用时弱化、缩进 3 格））、输入框下方**同一行**：最左运行动画（执行时旋转符 +「运行中…」，结束隐藏）+ 最右状态信息（模型名 / 思考强度 `[provider] reasoning_effort`（可选，同时传给模型）/ 项目名 / git 分支（读取 `.git/HEAD`，无仓库自动省略））、右侧常驻侧边栏（上半为用量/上下文：会话 token 用量、上下文占用百分比、压缩次数；中间为当前任务计划清单，`todo_write` 实时刷新；底部版本号 + 当前工作区路径）、底部多行输入框（Enter 发送、Shift+Enter/Ctrl+J 换行），不带底部操作按钮提示。配色取自 opencode 默认主题源码（`opencode.json`：背景 `#0a0a0a` / 面板 `#141414` / 元素 `#1e1e1e` / 主色 `#fab283` / 弱化 `#808080`），各模块带与 opencode 一致的 padding（消息区左右 2 上下 1、输入框顶部 1、用户消息面板上下 1 左 2）。权限确认与 `ask_user` 改为弹窗（y/n/a 按键选择 / 输入框回答，Esc 拒绝）。`/exit` `/new` `/plan` `/save` `/usage` `/context` `/compact` `/help` 全部在 TUI 内可用。Agent 保持同步流式在后台线程运行，经 `Renderer` 接口桥接（`renderer.py`：CLI 用 `ConsoleRenderer` 保持原 print/input 行为，TUI 用 `TuiRenderer` 以线程安全的 `post_message` 投递 UI 更新 + ModalScreen 弹窗）。一次性任务、管道/CI 仍走控制台模式，一行不改。新增依赖 `textual`
- 交互输入层改用 prompt_toolkit：粘贴多行自动合并为一条消息（原生粘贴检测，不再依赖内核队列探测）、输入历史持久化到 `~/.smithcode/history`、中文按显示宽度编辑（替代 readline hack，消除 Linux 下退格错乱）。Enter 发送消息，Ctrl+Enter 手动插入换行（支持多行编辑；Alt+Enter 在 Windows 下会被终端拦截用于全屏切换，故不用它）。新增依赖 `prompt_toolkit`，仅交互模式加载，非交互 stdin（管道/CI）仍退回普通 `input()`，行为不变
- 任务拆分与分步骤执行（opencode 式 TodoWrite）：新工具 `todo_write` 让模型维护会话级步骤清单（状态 `pending` / `in_progress` / `completed` / `cancelled`，传全量最新清单而非增量、非法状态降级为 pending、单份上限 50 步）。多步任务动手前模型先列出完整计划，逐步执行并实时更新状态。计划在终端实时渲染（进行中加粗高亮、完成/取消置灰），不受 `tool_display` 粒度影响；回传给模型的工具结果保持明文清单，供后续轮次参考。系统提示词新增「任务拆分与分步骤执行」规则段：同一时刻仅一个 in_progress、真正完成（含验证）后才标 completed、计划不合理时调整清单并在 reason 说明而非无视、取消的步骤保留清单、单步简单任务不需要拆分
- 新增 `/plan` 命令随时查看当前任务计划，`/new` 开启新会话时同步清空；`todo_write` 默认 `allow`（与 `ask_user` 一致，确认一个"追踪步骤"是荒谬的），可用 `deny` 规则禁用

## [0.6.1] - 2026-09-04

### 修复

- 修复 Linux 下中文退格错乱（删一字残留空格、需按两次、删到一半整行卡死）：加载 readline 按字符宽度擦除双宽中文（替代原 `IUTF8` termios 方案，readline 处理更完整）；同时关闭 bracketed paste，让 Linux/macOS 下多行粘贴能像 Windows 一样合并为同一条消息（`select` 探测内核队列）
- 权限确认前丢弃排队输入改为 POSIX 也生效（`termios.tcflush`），避免粘贴残留被误当成确认回答

## [0.6.0] - 2026-09-03

### 新增

- 配置体系重构：配置统一收敛到用户目录 `~/.smithcode/`（路径经 `Path.home()` 跨平台，`SMITHCODE_HOME` 环境变量可覆盖供测试隔离）。行为配置 `config.toml`（`[provider]` 模型/接口地址、`[context]` 预算与压缩阈值、`[limits]` 迭代轮数/超时/重试/输出截断、`[permissions]` 权限规则、`tool_display`）与凭据 `credentials.json`（仅 `key`，写入即 `0600`）分文件存放，前者不含秘密可安全分享。解析优先级「内置默认 < config.toml < 环境变量（仅 `SMITHCODE_KEY` / `SMITHCODE_MODEL` / `SMITHCODE_URL`）< CLI 参数」，任何配置缺失回退内置默认值；文件损坏打印警告并整体降级，单个非法值警告后回退，不中断启动。原 `smithcode.json`、`.env` 机制与 `OPENAI_*` / `ROOT` / `CONTEXT_BUDGET` 等环境变量移除（`python-dotenv` 依赖移除，3.9/3.10 经 `tomli` 读 TOML）
- `smithcode setup` 初始化向导：交互式采集接口地址 / 模型名 / API Key（`getpass` 不回显）/ 上下文预算（支持 `128k` 后缀写法），写入 `~/.smithcode/` 下两个配置文件；重跑幂等——提示符默认值取当前生效配置、回车即保留，已存在的 `[permissions]` 等用户手写段落与注释经 `tomlkit` 原样保留。缺 API Key 启动时的报错指引改为指向 `smithcode setup` 或 `SMITHCODE_KEY` 环境变量，以退出码 1 结束，不抛 OpenAI SDK 裸 traceback
- 运行时上下文压缩（opencode 式 checkpoint）：估算越过阈值（预算 × `COMPACT_TRIGGER`）自动把中段历史替换为结构化摘要（目标/关键决策/已完成/阻碍/下一步/相关文件，缺必需标题自动重试一次，仍失败则放弃压缩原样继续），保留系统提示词与近期尾部（`COMPACT_KEEP_TOKENS` 默认 15000，尾部超长工具结果截断到 2000 字符）；摘要以 `<context-summary>` 标记注入。provider 返回上下文溢出错误时压缩后重试一次（每步至多一次）。新增 `/compact` 手动压缩命令、`/context` 显示压缩次数；压缩只改运行时上下文，`/save` 行为不变
- 上下文计量：新增 context 模块与 `/context` 命令，按角色分桶（系统提示词 / 用户 / 助手 / 工具结果）展示当前上下文的 token 占用；估算以字符构成启发式计算，并用上次请求的真实 `prompt_tokens` 锚点校准偏差。任务中估算越过压缩阈值 90% 时提醒一次。新增配置 `[context] budget` 与 `compact_trigger`（无效值打印警告并降级默认），为后续压缩功能预留
- 工具调用展示粒度可配置：配置新增顶层 `tool_display` 键（`summary` / `detail`，默认 `summary`）。`summary` 模式下每个工具调用只打印一行「短名 + 目标」摘要（`read src/agent.py`、`edit src/cli.py`、`glob **/*.py`、`command git push`、`patch a.txt b.txt`），不再展示结果内容；`detail` 模式保留原有 `[Result]` 内容展示。失败信息（`错误: ...`、用户拒绝）无论何种粒度始终原样展示
- 工具注册表支持 `describe` 钩子：`(args) -> str` 生成终端短摘要，与 `pattern_arg` / `family` / `paths_from` 同为注册时可声明的可选扩展点，不会发送给 LLM；未声明的工具回退为原 `[Tool] 名字(参数)` 格式
- 新工具 `apply_patch`：opencode 信封格式的批量多文件修改（Add / Update / Delete），逐文件解析后**原子落盘**（任一文件失败整体不生效）；声明 `family="edit_file"` 自动继承其权限规则与 `.git` 保护路径
- 新工具 `ask_user`：Agent 任务中途向用户提问，回答作为工具结果回传；与 REPL 共用 `read_user_input`（多行粘贴合并），非交互 stdin 下 fail-closed 返回"已取消"
- 权限族（family）机制：`register` 支持 `family` 声明，规则匹配同时看「工具名」与「family」，继承工具可复用既有权限规则；多路径工具支持 `paths_from` 提取 + 逐路径预检与**聚合权限检查**（任一 deny → 整体拒绝，任一 ask → 询问一次）
- 非交互 fail-closed：标准输入非终端（管道 / CI）时，权限确认与 `ask_user` 一律拒绝/取消，不再因 `EOFError` 崩溃

### 变更

- 去掉每轮任务的 `[tokens]` 用量打印（单次交互行与结束时的会话累计速览），用量统计改由 `/usage` 命令按需查看
- 项目更名为 **SmithCode**：Python 包 `codeagent` → `smithcode`，CLI 命令同步更名
- 工具调用展示由 `[Tool] 名字(原始 JSON 参数)` 改为一行短摘要（超长截断到 80 字符显示）；回传给模型的结果（含 2 万字符截断）完全不变
- `-y`（approved_all）覆盖工作区外路径访问确认（按"仅本次"静默放行），显式 `deny` 依然生效
- 权限匹配大小写行为对齐 opencode v2：Windows 下大小写不敏感
- `read_user_input` / `confirmations_available` 迁入 `utils/terminal.py`，REPL 与 `ask_user` 共用同一输入逻辑

## [0.5.0] - 2026-08-30

### 新增

- token 用量统计：新增 usage 模块，按「应用启动以来 / 当前会话」双口径累计；对服务商返回的 usage 全量字段容错读取（缺失、为 null、非数值一律按 0），统计过程绝不影响对话主流程
- 流式用量捕获：LLM 请求启用 `stream_options.include_usage`，流内始终记住最新一份用量并在流结束后下发 `("usage", ...)` 事件；不支持该参数的服务自动降级重连（只是拿不到用量）
- 用量展示：新增 `/usage` 命令查看双口径累计与非零细节（思维链、缓存写入等）；每轮任务结束打印 `[tokens]` 速览，本轮与会话累计分两行，缓存命中内联在输入之后——嵌套 `cached_tokens` 与 DeepSeek 平铺的 `prompt_cache_hit_tokens` 语义相同，取大者去重
- 多行输入：REPL 中粘贴的多行文本自动合并为一条消息（首次回车后持续读取至输入缓冲区静默），不再被逐行消费成多条对话；非交互 stdin（管道）行为不变

### 修复

- 权限确认遇非法输入（空行、粘贴文本等）改为重新询问并回显收到的内容，不再静默按拒绝处理——此前多行粘贴会被 `input()` 逐行吞掉，导致 edit 请求被莫名拒绝
- 弹出权限确认前清空控制台输入缓冲区，提前键入或粘贴的排队内容不会被误当成确认回答

## [0.4.0] - 2026-08-30

### 新增

- 多根授权：`--add DIR` 可重复传入，把其他项目加入授权目录列表，一个会话内跨项目读写与检索；`smithcode.json` 权限模式对附加授权根同样生效
- 越界访问确认：工具路径落在授权目录之外时先交互确认——`[y]` 仅本次 / `[a]` 本会话总是（按 `.git` 向上识别项目根作为信任范围）/ `[n]` 拒绝；`/new` 时清空会话级信任
- 系统提示词 V2：按节组织（工作方式 / 工具细节 / 错误处理 / 安全边界 / 沟通），运行时注入主工作区与全部授权目录，新增 glob/grep 定位路由、edit_file 精确匹配规范、截断输出应对、"拒绝后不得绕道 shell" 等规则

### 变更

- 工具沙箱：路径解析锚定主工作区，落点在任一授权根内即放行；检索结果相对命中根展示
- 权限模式归一化：路径参数（相对 / 绝对 / 跨根）统一归一化为相对命中授权根的 POSIX 路径后再匹配规则，command 参数保持原文
- 思考过程展示：颜色由 dim（2m）改为灰色（90m），Windows 终端兼容性更好；思考与正文各占一行，正文行同样带 `助手>` 前缀
- `/new` 同时清空权限"总是允许"积累（session_rules），与"仅本会话有效"语义对齐

## [0.3.0] - 2026-08-29

### 新增

- 代码检索工具：`glob`（文件名通配搜索，支持 `**` 递归）与 `grep`（内容正则搜索，支持 `include` 文件名过滤），自动跳过 `.git` / `node_modules` / `__pycache__` 等无关目录，默认放行
- 流式输出接入 Agent 循环：模型正文逐字打印；思考内容（`reasoning_content`，如 DeepSeek-R1 类模型）以暗色实时展示且不写入会话
- LLM 请求健壮性：限流 / 断网 / 服务端 5xx 按指数退避自动重试（默认 3 次），请求超时 120 秒，均可在 `config.py` 调整

### 变更

- `llm.py` 重构：移除从未接线的非流式 `chat()`，`chat_stream` 重写为统一事件流（`reasoning` / `content` / `message`）；已开始输出的流不重试，避免重放已打印内容
- Agent 主循环统一走流式路径，CLI 不再二次打印最终回复
- 系统提示词新增规则：定位代码优先用 glob / grep 搜索

## [0.2.1] - 2026-08-29

### 修复

- 路径沙箱逃逸：原 `startswith` 前缀匹配可被共享前缀的兄弟目录绕过（如 `../smithcode-evil/x` 会被误判为工作区内），改用 `Path.is_relative_to` 精确判断
- Python 3.9 兼容：`permission.py` / `agent.py` 的 `X | Y` 类型注解要求 3.10+，补充 `from __future__ import annotations`

### 新增

- 工具输出截断：单次工具返回超过 `MAX_TOOL_OUTPUT`（默认 2 万字符）时保留头尾各半、省略中间，防止超长输出撑爆模型上下文；上限可在 `config.py` 调整

### 变更

- 清理存量 lint 问题（适配 ruff 0.16 新规则），`ruff check src tests` 恢复全绿
- 移除无引用的遗留常量 `SAFE_TOOLS`（其职责已由 0.2.0 的权限规则引擎接管）

## [0.2.0] - 2026-08-29

### 新增

- 权限规则引擎（参考 opencode 模型）：三级动作 `allow / ask / deny`，规则 = (工具名, 参数模式, 动作)，通配符匹配，最后一条匹配的规则生效
- `smithcode.json` 配置文件：用户可自定义权限规则，支持字符串简写与按模式细分两种写法
- 交互确认升级为 `[y]本次 / [n]拒绝 / [a]总是允许该模式`，"总是允许"按参数模式记忆（仅当前会话）
- 工具注册支持 `pattern_arg` 声明权限模式来源（该字段不会发送给 LLM）
- `-y` 参数语义收紧：跳过所有 `ask`，但显式 `deny` 规则依然生效

### 变更

- `Permission.check` 签名改为 `check(tool_name, args)`，Agent 在权限检查前先解析工具参数

## [0.1.0] - 2026-08-29

### 新增

- Agent 循环：模型自主调用工具直至任务完成，可配置最大迭代轮数
- 5 个内置工具：read_file / write_file / edit_file / list_dir / run_command
- 权限控制：敏感操作逐个确认，支持会话内记忆与 `-y` 全自动模式
- 会话管理：多轮对话、`/new` 重置、`/save` 导出 JSON
- 路径沙箱与命令超时保护

### 变更

- 采用 src 布局重组项目结构，新增 tests / docs / examples 目录
- 工具改为注册表机制（`tools/base.py`），新增工具无需改汇总代码
- 系统提示词从 `session.py` 拆分到 `prompts.py`
- 终端编码处理统一到 `utils/terminal.py`，移除根目录 `main.py`
