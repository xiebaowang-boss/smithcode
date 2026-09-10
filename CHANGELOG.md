# Changelog

本项目的所有显著变更都记录在本文件中。

## [未发布]

### 新增

- **批量工具调用两阶段并发执行**（对齐 Claude Code 的分区 + 读写锁思路）：模型一次返回的多个 tool_calls 改为两阶段处理——第一阶段（主线程串行）按接收顺序逐个预检（解析参数、渲染摘要、路径预检、权限确认），任一被拒即终止任务，此时还没有任何工具被执行（比旧版"执行到一半被拒"更干净，且所有确认框仍逐个弹出、不会交错）；第二阶段按序分段执行——连续的可并行工具合并为一个波次扔进 `ThreadPoolExecutor`（`[limits].max_tool_concurrency` 上限，默认 5），有状态工具在主线程串行、作为顺序屏障（保证串行工具看见之前所有副作用、后续工具又看见它的改动），结果一律按提交顺序收集、回传给模型的顺序与请求顺序严格一致。配套机制：
  - 工具注册表新增 `serial=True` 声明（`tools/base.py` 的 `SERIAL` 注册表）：`run_command`（单会话 shell，cd/环境变量跨调用携带状态）、`todo_write`（会话级状态机 + 渲染）、`ask_user`（终端交互抢 stdin）、`write_file` / `edit_file` / `apply_patch`（写操作默认串行偏安全）已标注；未声明的只读工具（read_file / list_dir / glob / grep / webfetch / websearch / todo_read）默认可并行
  - "仅本次"越界放行（`widen_roots`）的调用强制串行——临时放行目录全局生效，并行窗口内其他线程会意外获得该目录访问权
  - worker 线程只计算结果字符串，渲染（`tool_result`）全部留在主线程按序执行，TUI / renderer 无需任何线程安全改造
  - 权限被拒时，此前已过预检但未执行的计划一并补占位结果（防悬空 `tool_call_id`）；`max_tool_concurrency=1` 时整条路径退化为纯串行（不启用线程），行为与旧版逐个执行完全一致
  - 系统提示词同步告知模型：只读调用尽量合并在同一条回复里发出、有状态调用永远串行、结果按调用顺序回传

- **webfetch 支持批量并行抓取**：`url` 参数新增列表形式（string 或 string 数组），一次调用最多并行抓取 5 个地址（`MAX_URLS`，超限直接报错并提示拆分），用 `ThreadPoolExecutor` 并发请求，每个地址独立走既有的单页抓取逻辑（协议校验/重定向检查/截断/错误处理互不影响，单个失败不拖垮整批）；多地址结果按 `===== [序号] url =====` 分段返回、顺序与入参一致，纯字符串单地址输出格式不变。要抓很多网页时由系统提示词引导 LLM 并行多次调用本工具、每次不超过 5 个地址
- **输入 `/` 弹出命令菜单**（对齐 Claude Code / opencode）：TUI 中在输入框敲 `/` 即在输入框正上方弹出命令列表（`/命令名 + 中文描述`，**悬浮层**——弹出/收起不挤压聊天区与输入框布局，菜单盖住聊天区底部；候选超过固定展示行数（8 行）时菜单封顶并出现滚动条，↑↓ 移动时选中项自动滚进可视区），继续输入实时前缀过滤，↑↓ 循环移动高亮、Enter/Tab 将选中命令填入输入框（尾随空格，不直接发送）、Esc 关闭；菜单开着时 Enter 只补全不发送，关闭后按键行为不变；命令列表读自命令注册表，新增命令自动进菜单。REPL（prompt_toolkit 行式界面）同步支持：斜杠补全菜单带右侧描述列，Enter 在选中候选时先应用补全、否则照常发送。普通消息文本不受影响（非 `/` 前缀不触发）
- **斜杠命令框架**：新增 `commands/` 包，命令用 `@register` 装饰器声明、导入即注册（与工具注册表同款机制），REPL 与 TUI 共用同一个 `dispatch()` 入口与 `CommandResult` 结果协议（文本/渲染形态/着色/退出/状态刷新等标记），`/help` 文案由注册表自动生成；原先 `cli.py` 与 `tui/app.py` 各自维护的两份 if/elif 命令链与硬编码 HELP 文案删除，新增命令只需一个文件、两端零改动接入。附带行为微调：以 `/` 开头的输入一律走命令分发——未知命令给出提示（此前 REPL 会把未匹配的 `/xxx` 当普通消息发给模型）；无参数命令携带多余参数时提示用法而非静默忽略
- 既有 8 个斜杠命令（`/help` `/new` `/plan` `/save` `/usage` `/context` `/compact` `/exit`）迁移到命令框架，语义与两端表现不变
- **会话级权限模式**（对齐 Claude Code 的模式分派）：Shift+Tab 在 TUI 中循环切换 `Smith`（默认逐个确认）/ `Accept Edits`（编辑族——`edit_file` / `write_file` / `apply_patch`——自动放行，命令仍确认）/ `Auto`（全部 ask 自动放行，等价 `-y`）三档；输入框底行最左侧以 `权限模式 · 模型 · 思考强度` 形式常显当前档位（灰/黄/橙按风险着色），权限/提问面板弹出期间不响应切换。规则引擎之上新增独立模式分派层（`_dispatch_ask`），deny 任何模式都拒绝；`approved_all`（`-y`）改为 `mode == "auto"` 的兼容别名，既有引用零改动
- `run_command` **复合命令拆分求值**（对齐 Claude Code 的命令审批）：命令串按顶层操作符（`&&` / `||` / `;` / `|` / `&` / 换行）切分为子命令逐段匹配权限规则（引号内不切），任一段 `deny` → 整体拒绝（`-y` 也不放行），任一段 `ask` → 整体询问，全部放行才放行——堵住"放行 `git status` 后借 `&&` 偷渡 `rm -rf /`"的绕过洞；含 `$()` 或反引号命令替换的命令无法静态求值，即使外层命令被 allow 也强制 `ask`（POSIX 语义：双引号内仍算替换、单引号内不算）。系统提示词同步告知 agent 逐段求值语义
- 工具调用块展示**变更预览（diff）**，审核前先看清改动，写/编辑工具**默认展开**：
  - `write_file` / `edit_file` 在**执行前**（权限确认之前）把 unified diff 推送到**工具调用块**：pending 态就地展开，审核 y/n/a 时改动内容已可见（权限申请框只负责决策、保持纯净）；执行后 diff 保留在调用详情里回看（diff 在前、执行确认语在后，REPL 与 TUI 一致）。REPL 中按行着色打印（增行绿、删行红、`@@` 位置头青色），TUI 工具块内嵌着色 diff，超 40 行自动截断并提示省略行数；执行确认语（如「已编辑 c.txt」）在**真正调用后**展示
  - `write_file` / `edit_file` 的调用详情在 TUI 中**默认展开**（diff 直接可见，可手动收起）；`read_file` 等读取工具维持默认收起（整文件内容不上屏）
  - 机制上工具注册表新增 `preview` 声明（`describe` 同款零侵入模式），其他工具可按需接入；`.env` 等禁读文件不生成预览避免密钥回显终端，预览生成失败只影响展示、不影响确认与执行
- 自定义请求头 `[provider.headers]`：随每个 LLM 请求发送任意 HTTP 头，值为字符串原样发送、含 `{$session}` 占位符时替换为**当前会话 id**（会话开始与 `/new` 时自动轮换，一次对话内稳定）。适配要求会话级请求头的 OpenAI 兼容网关，如 OpenCode Go 需要每会话稳定的 `x-opencode-session`（`[provider.headers]` 下写 `x-opencode-session = "{$session}"`）。未配置则完全不发送额外请求头，既有行为不变
- **`/effort` 命令：调整思考强度**（交互与 `/model` 一致）：TUI 中输入 `/effort`（无参）弹出居中选择框（选中即执行），`/effort <档位>` 直接切换，底栏「思考强度」实时刷新。候选为**本地默认维护**的完整档位列表（`models.DEFAULT_EFFORTS`，不调用远端接口）：`none / minimal / low / medium / high / xhigh / max`（即 OpenAI `reasoning.effort` 的完整支持范围）。思考强度改为**始终显式下发**，内置默认 `high`（`config.DEFAULT_EFFORT`，`config.toml` 的 `[provider].reasoning_effort` 可覆盖），不再提供「不发送」档。带参切换不校验取值，服务商不支持时可改传其他值
- **`/model` 命令、模型目录与通用选择面板**（对齐 Claude Code / opencode）：TUI 中输入 `/model`（无参）弹出**居中遮罩选择框**（半透明背景 + 居中卡片，↑↓/j/k/数字键选择、Enter 确认、Esc 取消，当前模型标绿「(当前)」），选中后自动切换并刷新底栏；`/model <名称>` 直接切换（本会话生效）。候选由新的 **`ModelCatalog`**（`models.py`，线程安全）统一维护，来源按优先级组合：`config.toml [provider].models`（显式配置，存在即不联网）> `~/.smithcode/models.json` 磁盘缓存 > 远端 `/models`。**启动时**（`Agent.start()`，CLI 调用）同步装载配置/缓存，外部未配置则后台拉取远端 `GET /models` 并回写缓存（按接口地址校验，离线可用、不阻塞启动）；命令层只依赖 `agent.models.list()`。为此命令协议新增 `CommandResult.select` 选择意图——命令只声明候选项、由宿主负责弹窗（TUI 居中弹窗 / 非交互 REPL 列出候选并提示改用带参形式，保持 fail-closed），命令处理器保持同步纯函数。附带把权限/提问/通用选择面板从 `tui/app.py` 抽到新文件 `tui/panels.py`

### 变更

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
