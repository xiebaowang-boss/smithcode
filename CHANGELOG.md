# Changelog

本项目的所有显著变更都记录在本文件中。

## [未发布]

### 修复

- **TUI 对话区贴底后仍不跟随新输出**：滚动跟随此前是"贴底就 `scroll_end()`"，而 `scroll_end` 的目标行号取自发出那一刻的滚动区高度——流式正文按帧节流重排（落在 16ms 窗口内的 chunk 不触发布局，目标行号因此算在旧高度上）、输入区高度可变（运行动画出现 / 命令菜单弹出 / 输入框增高都会让聊天区变矮）都会让实际落点比预期差出 1 行以上；一旦超过 1 行容差，"是否贴底"的判断此后恒为假，跟随**永久失效**（需手动滚一下才恢复）。现改用 Textual 原生底部锚定（`ChatView._follow` → `Widget.anchor()`）：贴底位置交给合成器在**每次布局**时按真实内容高度重算，上述偏差自动纠正；用户滚轮翻阅历史仍不会被打断（Textual 在用户滚动时自动解除锚定），滚回底部或提交自己的消息则重新锚定。同时补齐工具结果与变更预览两条"就地把块改高"的跟随路径（此前这两类增长完全不走贴底判断，在"翻阅过历史又滚回底部"的状态下同样会丢跟随）。

## [0.9.1] - 2026-09-17

### 新增

- **`run_command` 新增可选 `description` 参数，权限确认改为截断展示**：调用命令工具时推荐用 5-10 个字符说明运行目的（如「运行单元测试」）；权限确认框标题后先展示该描述，再跟截断后的命令摘要，长命令不再全量撑满弹窗 / 终端。确认框内的工具摘要与「总是允许」等选项小字统一限制为 80 字符（超出加 `...`），长路径的越界授权选项说明同样受益。
- **webfetch 的 SSRF 防护（默认拒访内网，`allow_private_urls` 可放行）**：webfetch 内置默认免确认，而模型可能被网页内容诱导去读本机服务或云凭据（`http://127.0.0.1:11434/`、`http://169.254.169.254/latest/meta-data/`），此前一律照抓。现默认拒访**非公网地址**——回环、私网、链路本地（含云元数据）、CGNAT、保留段与多播；域名按 DNS 全部解析结果判定（防「公网域名指向内网」），解析失败不拦（请求本就连不上，交网络层报常规错误）。重定向改为**逐跳手动跟随**（`follow_redirects=False`，上限 5 跳）并对每一跳重新校验，堵住「公网地址 302 到内网」的绕过；重定向到非 http/https 仍在第二跳前中止。开关：`config.toml` 的 `allow_private_urls = true` 或环境变量 `SMITHCODE_ALLOW_PRIVATE_URLS=1`，供本地开发抓 localhost 文档。配套 `utils/http.py` 新增 `is_public_address` / `resolve_host` / `private_target`（`resolve_host` 单独成函数以便测试替换，SSRF 用例不依赖真实 DNS）。
- **webfetch 输出结构化 Markdown（新增 `utils/htmltext.py`）**：抓取结果此前用正则去标签，标题层级、链接地址、代码块、列表与表格全部丢失——读文档时「链接去哪」「代码怎么用」往往正是重点。现改用标准库 `HTMLParser` 做结构保留的转换：标题 → `#` 前缀、链接 → `[文本](href)`、`<pre><code>` → 带语言的围栏代码块（并去掉页面统一缩进）、列表 → 带嵌套缩进的 `-` / `1.`、表格行 → `|` 分隔、引用 → `>` 前缀，实体解码与 `<br>` 换行照常；脚本 / 样式 / `<head>` 一律丢弃，畸形标签不抛异常只降级。不引入新依赖（仍是标准库）。工具描述同步更新。已知局限：不做正文提取，站点侧边栏 / 目录等装饰元素会占用 `max_chars` 预算（docs.python.org 实测正文起始于约 1.5 万字符处，改造前后一致）。

### 变更

- **系统提示词去重复（两轮合并），收敛冗余表述**：删除末尾整节「反模式与行为锚点」——其四条否定式禁令（写脚本证明简单改动、为简单改动加测试、重复读同一文件、把简单任务拆成多个 todo）全部是前面「行动原则」「工作方式」「任务拆分」的复述，示例三条亦可由既有规则直接推出。第二轮把「工作方式」与「行动原则」的重合收敛：`做最小改动：遵循现有风格，不重构…` 原在两节各写一遍，现并入「行动原则」为一条（`做最小改动、不扩大范围`），「工作方式」只留工具定位与输出纪律等独有细节；同时删去「行动原则」的 `可逆的小改动先做后验；高风险或不可逆操作先说明并确认`——与「安全边界」的 `本地可逆改动可直接做；删除、force push、reset --hard…先确认` 同义。真正独有的增量各归近邻节：`不重复运行同一命令` 归入「工作方式」读取纪律，`不无限验证` 归入「角色与使命」成功标准（同时补上原在「错误与停止」的 `无已知回归`，避免信息丢失）。「不确定性与验证」的四级成本阶梯由列表压成一句（`读代码 → 搜用法 → 跑命令 → 临时脚本`），与下方 L0–L4 验证分级各司其职；`简单任务不拆分` 由「行动原则」与「任务拆分」两处收敛为一处。规则正文由 2624 字符降至 2417 字符（减少 7.9%，段数 12 → 11），行为约束信息量不变。

- **websearch 改为多后端（默认 auto 自动回退），并新增 Tavily 后端**：免 key 搜索引擎的可用性、反爬强度与结果质量差异极大，且随网络环境（是否走代理）整体翻转——实测同一批查询，无代理时 DuckDuckGo 域名不可达，走代理后 Bing 又会对约 10% 的查询返回与查询完全无关的「软降级」结果、而 Brave 稳定（15 条开发者查询命中 Brave 10 / Bing 6 / DDG 被反爬拦截）。现支持 4 个后端，配置 `[search].backend`（环境变量 `SMITHCODE_SEARCH_BACKEND` 优先），默认 `auto`：按 `tavily → brave → bing → ddg` 依次尝试，某后端报错 / 被限流 / 反爬拦截 / 解析为空 / 未配 key 就自动试下一个，全失败时在错误里汇总各后端原因；固定后端失败时提示可改用 `auto`（不静默回退，尊重用户选择）。**新增 Tavily**（`api.tavily.com/search`，JSON API，免费 1000 次/月）——专为 LLM 设计、结果最干净且不受反爬影响，故在 auto 里排第一；key 解析优先级 `SMITHCODE_TAVILY_KEY` > `[search].tavily_key` > `credentials.json` 的 `search.tavily_key`，`smith setup` 可选采集（写入凭据文件、与 LLM key 共存于不同字段），未配 key 时在 auto 里直接跳过（不算失败）。key 绝不进入工具输出。免 key 的三个后端页面结构不同，各自一个解析函数（Brave 解析 `data-type="web"` 结果块，Bing 解析 `<li class="b_algo">` 并还原 `/ck/a` 跳转链接，DDG 解析 `result__a` 并还原 `uddg` 跳转链接）；反爬识别按各后端特征区分（Bing 缺结果容器 `id="b_results"`、DDG 命中验证页文案、HTTP 202/403/429），与「页面正常但没有结果」分开报。原 DuckDuckGo 单后端在部分网络下 100% 失败的问题由此解决。

- **webfetch 改用真实浏览器 UA，403/429 给出可操作提示**：此前 UA 自曝身份（`Mozilla/5.0 (compatible; SmithCode/1.0; ...)`），部分站点（如 Stack Overflow）据此直接 403；现改为常见 Chrome UA，与 websearch 共用。403/429 不再只报「HTTP 403 Forbidden」，而是说明目标站点按反爬规则拒绝、重试通常无效，并提示改用 websearch 找镜像/缓存或换其他来源地址。
- **TUI 侧边栏计划区不再绘制滚动条**：`Sidebar #sidebar-plan` 此前是界面里唯一还画滚动条的容器（步骤超出时右侧占用 2 列），现改用 `scrollbar-size-vertical: 0`，与聊天区 / 命令菜单 / 选择面板 / MCP 向导保持一致，滚动功能不受影响（仍可用滚轮、依赖选中项自动滚入可视区）。同时移除原先为修分界残影加的 `scrollbar-gutter: stable` 固定槽道——它与 `scrollbar-size-vertical: 0` 同用会让 `virtual_size` 塌缩、滚动失效（`#chat` 处注释已有记录）；滚动条已完全不绘制，该槽道所修的「覆盖式滚动条反复出现消失盖住分界列」也就不再发生。

- **移除子代理（Subagents）子系统与 `task` 工具**：删除 `subagents/` 包（类型目录 + 执行编排）、`tools/task.py`、`/agents` 命令、`[subagents]` 配置段及系统提示词的「子代理」行为节；`Renderer` 的 `Scope` 维度、线程级 scoped 代理与 TUI 的 `SubAgentBlock`/scope 路由一并移除，渲染事件签名恢复为无 scope 参数。已写有 `~/.smithcode/agents/` 或项目 `.smithcode/agents/` 定义、`[subagents]` 配置的用户升级后将被忽略。

- **终端窗口标题随会话同步（退出恢复原标题）**：交互模式（TUI / REPL）下窗口标题显示 `Smith · <会话标题>`（无标题时回退工作区目录名），运行中加 `◐` 前缀（模型请求、工具执行），**等待用户确认或回答时优先显示 `!` 前缀**——切走窗口再回来能一眼看出 agent 是卡在等自己，而不是还在跑。等待态以 `Renderer` 总线事件的形式提供：基类新增 `turn_waiting_started` / `turn_waiting_finished`（与 `turn_started` / `turn_finished` 同风格的成对事件，默认空实现），由 `title.Relay` 在 ask 类方法（`ask_text` / `ask_choice` / `ask_form` / `confirm_choice`）进出时广播——这些方法本身同步阻塞，包住入口即覆盖权限确认、越界授权、技能信任确认与 `ask_user` 提问，调用点零改动；事件同时喂给标题呈现器与内层后端，**任何前端覆盖这两个方法即可成为消费方**（当前只有终端标题消费）。装配入口分两个：终端宿主沿用 `attach()`（总线 + 接管终端标题），只想要事件、不要终端标题的 GUI 前端（desktop / web）用新的 `bus()`——不创建呈现器、不装退出钩子、不写任何控制序列；`attach()` 即 `bus()` + `enable_title()` 的组合，两个既有装配点（REPL / TUI）一行未改。等待与忙闲分开计数（确认可嵌套、面板异常退出也在 `finally` 收尾）。标题**由 agent 事件驱动**——`title_changed` / `turn_started` / `turn_finished` 三个新事件经渲染后端广播（`Agent.run` / `run_with_goal` 首尾、`new_session`、`resume`、自动标题与 `/rename`），前端只提供写入通道（TUI 走 Textual 的 `driver.write` 队列、REPL 直写真实 stdout），为后续接入 desktop 等新前端留出解耦。新增 `title.py` 承载合成与生命周期：标题净化（剥控制字符——标题可能来自模型生成，须防"数据 → 转义序列"注入）与截断、相同标题去重、忙闲计数（多回合续跑期间不闪烁）、非 tty 自动失效（管道 / CI 一个字节都不写）。退出用终端窗口标题栈（`CSI 22;2t` 压栈 / `CSI 23;2t` 出栈）**恢复用户原标题**而非清空，钩子统一挂 `atexit` + `SIGTERM`/`SIGHUP`（链回原处理器保住退出码；刻意不注册 SIGINT，保留 Ctrl+C「第一次取消任务、第二次退出」语义）。配置：`config.toml` 的 `terminal_title = false` 或环境变量 `SMITHCODE_TERMINAL_TITLE=0` 关闭；已知限制：tmux 等复用器会拦截标题栈序列，退出后窗口名由下一个 shell 提示符覆盖。

- **TUI 提问面板统一走确认页（单问题也需确认）**：此前只有多问题在答完后进入确认页，单问题（`ask_user` 只问一项）是答完即提交，两者交互不一致。现统一为**答完一律先进确认页**——列出问题与答案供核对，Enter 提交、←/→ 返回修改，单问题同样如此（确认页提示行相应显示「←→ 返回修改」而非「←→ 切换问题」），不再保留「单问题直达提交」的特例。`QuestionPanel._advance` 去掉按问题数分叉（`_total > 1` 才进确认页），`_switch_question` 去掉单问题早退（单问题也构成「问题 + 确认页」两页环）。

- **TUI 外观微调**：侧边栏固定宽度由 46 列收窄到 40 列（窄屏断点 `SIDEBAR_BREAKPOINT` 仍为 120，省下的列还给聊天区）；输入框、user 消息面板、权限确认/提问面板的左侧竖线统一加粗一档，由 `solid`（`│`）改为 `heavy`（`┃`），颜色仍按语义区分（绿=输入/用户、橙=待确认）；每轮对话页脚开头的 `▣` 图标由橙色改为与输入框左竖线一致的绿色。Textual 边框固定 1 格宽、无宽度属性，加粗通过换字形实现。

- **`/goal` 回合预算改为可选（默认不限）**：`[limits].goal_max_turns` 默认值由 `50` 改为 `-1`（不限制）——目标持续自动推进，直到模型核验证据后声明完成/受阻、用户暂停/清除/中断，或空转刹车触发。配置为正整数时仍按预算收尾（`budget_limited` + 收尾提示词）；`/goal budget <N>` 设上限，新增支持 `/goal budget unlimited`（或 `off` / `none` / `-1`）取消上限。进度显示随之自适应：不限时底栏/侧边栏/状态块只显示回合数（如 `◎ 目标 3`），有预算时显示 `N/M`；续跑提示词区分「预算不限」与「剩余 N 回合」。新会话默认不限，恢复旧快照（含旧的 50）不受影响。

- **迭代上限改为可选（默认不限），达上限改为强制总结收尾**：`[limits].max_iterations` 默认值由 `30` 改为 `-1`（不限制），对齐 opencode 的 `steps` 缺省「无限迭代」语义——未配置时只要模型持续请求工具就继续循环，直到模型给出纯文本回复或用户中断。配置为正整数时封顶；达到上限不再静默硬中止，而是注入收尾提示、以 `tools=None`（不暴露工具）强制模型用纯文本总结已完成工作、剩余任务与下一步，随后以 `max_iterations` 状态结束——总结正文在流式过程中展示，终端另可见一条「已达上限」警告。若收尾轮模型仍返回 `tool_calls`，一律剥离不执行，避免历史中留下悬空 `tool_call_id`。`--max-iterations` CLI 参数与 README / 初始化向导示例同步更新。

- **新增发布文档 `docs/release.md`**：收录版本号规则（语义化版本 + 需同步的三处：`pyproject.toml` / `__init__.py` / `uv.lock`）、发布流程 SOP（归档 CHANGELOG → 升版本 → `uv lock` → 全量验证 → `uv build` → 隔离校验 → 更新全局安装 → 打标签 → 建 GitHub Release）、GitHub Release 约定（正文来源 `docs/release-notes-X.Y.Z.md`、附件上传与 `gh release create` 示例）与各版本面向使用者的发布说明；0.9.1 条目含发布产物的 SHA-256。README 文档区新增链接。

- **`/compact` 改为后台压缩并给出进度 / 完成反馈**：此前手动压缩在宿主主线程同步执行，摘要请求期间 TUI 整个界面卡死（REPL 也无法输入）；现在命令层只声明 `CommandResult.start_compact` 意图，宿主在后台线程执行 `Agent.compact_manual()` 并复用任务的忙守卫与运行动画——发送后立即提示「正在压缩上下文…」，完成后提示「上下文压缩完成。」（无中段可压 / 摘要两次不合格提示「没有可压缩的上下文」）。手动压缩自建轮次取消令牌，压缩期间 Esc / Ctrl+C 可截停摘要请求，中断后提示「已取消压缩。」；任务运行中 `/compact` 被忙守卫拦截，避免与后台线程改写历史冲突。

### 修复

- **TUI 正文永远按 80 列折行，窗口缩放后不重排**：`tui/render.py` 的 `render_markdown` 用 `Console(width=…, force_terminal=True)` 渲染 markdown，但 rich 的 `Console.size` 只在 width / height **都显式**给出时才直接返回给定尺寸，否则先做终端探测——`TERM=dumb`（Emacs shell、部分 CI / 极简终端）会短路成固定 `(80, 25)`，把传入的宽度整个丢掉。正文折行因此固化在 80 列：`MessageBody` 按 `self.size.width` 重新渲染（窗口缩放、侧边栏显隐）也拿到同样的结果，表现为拉宽不重排、聊天区宽于或窄于 80 列时折行位置都不对。现额外传 `height=25` 锁住尺寸（height 与换行无关），换行宽度始终以调用方给的 `width` 为准。

- **SSRF 防护漏掉「嵌入内网 IPv4」的过渡 / 转换地址**：`is_public_address` 此前只看 IPv6 地址自身的 `is_global`，而 `64:ff9b::/96`（NAT64 well-known 前缀，RFC 6052）与 `::ffff:0:0:0/96`（IPv4-translated，RFC 2765）的末 32 位承载一个 IPv4 地址、`is_global` 却为 `True`——在 DNS64 网络里 `http://[64:ff9b::a9fe:a9fe]/latest/meta-data/` 会实际连到 `169.254.169.254`（云元数据），即「换一种写法就绕过内网拦截」。现对这三类固定布局（含 IPv4-mapped）按嵌入的 IPv4 判定：嵌入内网即拒绝，嵌入公网仍放行（不连坐）。只认布局固定的前缀，不做 RFC 6052 可变前缀长度的推算——布局不固定就无法可靠判断嵌的是哪个地址，与其猜错不如交给下一层（6to4 / Teredo / NAT64 local-use 等前缀 CPython 已判为非全局）。`private_target` 的字面量与 DNS 两条路径同时受益。

- **webfetch 遇到非法 charset 标签时抛未处理异常**：响应头的 `charset` 直接喂给 `bytes.decode`，畸形站点给出 `x-bogus` 这类非法标签时抛 `LookupError`，而它不在 `_fetch_one` 的异常捕获范围内（那里只收 `RequestError` / `InvalidURL` / `OSError`）——整次抓取以未处理异常结束，模型收到的是裸异常名而非可读结果。现新增 `_decode_body`：先 `codecs.lookup` 校验标签，非法则回落 UTF-8，`errors="replace"` 保留原行为（编码正确但字节残缺时用替换字符，不中断）。

- **webfetch 的表格输出不是合法 GFM，模型读不出表格结构**：此前单元格内容只是用 ` | ` 拼在一行里，既没有 `| --- |` 分隔行、也没有首尾竖线，渲染器／模型都无法识别为表格；单元格里含 `<p>` 等块级标签时还会把一行拆成多行，进一步破坏结构。现改为「先缓冲整行单元格、遇到 `</table>` 再渲染」：输出标准 GFM（`| a | b |` + `| --- | --- |` + 数据行），支持 `align` 属性与 `style="text-align: …"` 的对齐标记（`:---` / `---:` / `:---:`）、单元格竖线转义、列数不齐时补齐空列；无 `<th>` 的表格把首行当表头（GFM 必须有表头行）；单元格内的块级标签降级为行内文本、不再拆分该行。`thead` / `tbody` 包裹（真实页面的普遍写法）正常处理。

- **TUI 输入框换行「看起来不生效」**：`Shift+Enter` 其实一直能插入换行（`ChatInput.on_key` 有对应分支，真实 PTY 实测 `text='abc\ndef'` 成立），但输入框高度写死 `height: 3`，多出的行被挤进内部滚动区，而滚动条又在同一轮改动里去掉了——既看不到新行、也看不到滚动条，于是表现得像「按键没反应」。现输入框改为**高度随内容自适应**（`height: auto` + `min-height: 3` 保持单行时与原先一致 + `max-height: 13`，超出后才内部滚动），并去掉滚动条。连带处理：① 底部留白此前靠 `min-height` 撑出的空行充数（`padding: 1 2 0 2` 底部为 0），内容一到 2 行就被填满、"留白消失"，现改为**上下对称的 `padding: 1 2 1 2`**，每行都有稳定留白（`max-height` 相应由 12 提到 13，可容纳的内容行数不变）；② 命令菜单原先靠写死的 `offset: 0 -5 / -6` 锚在输入框上方，输入框一变高就错位，现改由 `anchor_command_menu` 按输入区实时几何（`#input-wrap` + `#bottom` 高度）重算，并在输入框 `on_resize` 与运行动画显隐时触发；③ 空输入时给出 placeholder 提示「Enter 发送 · Shift+Enter 换行（不支持的终端用 Ctrl+J）」。**关于"个别终端不生效"**：`Shift+Enter` 与 `Enter` 在终端层是否可区分取决于终端是否支持 **kitty 键盘协议**（Ghostty / kitty / WezTerm / foot 支持；传统 xterm 等不支持）。不支持的终端里 `Shift+Enter` 发出的字节与 `Enter` 完全相同（都是 `\r`），应用层无从分辨，只能发送；`Ctrl+J` 发的是 `\n`（LF），所有终端都可靠，故作为兜底键一并提示。

- **TUI 选择弹窗（`/model` 等）大列表按住方向键闪烁**：选择面板此前每行是一个子控件（`Horizontal` + 两个 `Static`），滚动与行内容刷新分属 Textual 的两次刷新周期——按住 ↑/↓ 时每键要**连续两次**把内容写往终端（一次滚动、一次行刷新），两次之间的中间态（新滚动位置仍配旧行内容）被真实渲染出来，表现为逐键闪烁；600 项实测 2 次写入/键、每键约 1.2 万字符，且开销随列表长度增长。现把行区改为**单个自渲染控件**（新增 `SelectionRows`，`ScrollView` + `virtual_size` + `render_line`，与 Textual 自带 `OptionList` 同构）：滚动与行内容由同一个控件、同一次刷新产出，600 项实测 **2 次/键 → 1 次/键、写入字符降到约 1/3**。副产物是结构大幅简化——`_row_widgets` 行组件缓存、`_update_row()`、逐行 CSS 类（`.selection-row` / `.selection-label` / `.selection-trailing`）全部删除，行样式（选中反白、分组表头蓝、`trailing` 右对齐）在 `render_line` 里直接落到 `Strip` 上，渲染成本只与可见行数相关、与列表总长度无关。选中态、分组表头、间隔行、数字键快选均保持不变；`trailing` 仍贴行尾右对齐，且**溢出时优先保住它**——label 超出可用宽度才截断（加省略号），与旧两列布局（label `1fr` / `trailing` `auto`）同优先级，`/sessions` 的时间、`/mcp` 的状态不会被长标题整段挤掉。**行区每行（含行计划之外的留白）的底色都显式取自控件自身解析后的背景色**（`#1e1e1e`，跟随 CSS / 主题）——旧结构里每行是子控件、背景自动继承面板，自渲染后若把空白段留成「无背景」会透出半透明遮罩与下层界面，看起来就是中间颜色与外部不一致；底色用 `Strip.apply_style` 叠加而非 `Text.style` 赋值（rich 的 `Text` 在构造时已把样式落到各 span，事后赋值不传播，实测会导致 `trailing` 文字段丢失背景）。留白必须带样式还有第二个原因：`style=None` 的段会让 Textual 的 Monochrome 滤镜（`NO_COLOR` + 默认主题）对 `None` 解引用抛 `AttributeError`，而列表短于可视区时这些行每次渲染都会产出——即 `NO_COLOR` 下一开选择面板整个 TUI 就崩（已补回归测试）。

- **websearch 被反爬拦截时误报「（无搜索结果）」**：DuckDuckGo 判定请求来自机器人时会返回一张验证页（"Unfortunately, bots use DuckDuckGo too…Select all squares containing a duck"），页面里没有任何结果锚点，于是结果被解析为空、工具回「（无搜索结果）」——用户以为关键词不对，实际是被拦，排查方向被完全误导（本项目的代理问题排查中就被它误导过一次）。现新增验证页识别：**「无结果锚点 + 命中特征文案」双条件**成立即返回明确错误（含「可稍后重试 / 改用 webfetch 抓已知网址」的下一步建议）；条件设计避免误伤——正常结果页里出现这些词（例如你搜的就是这句话）不受影响。已补测试：验证页报错、结果页含相同词不误判。

- **webfetch / websearch 不认系统代理（LLM 能连、搜索与抓取连不上）**：两个网络工具此前走标准库 `urllib`，而 urllib 既不读 `ALL_PROXY`、也不支持 socks——用户用 Clash / FlClash 等开系统代理后，LLM（httpx2）正常、`websearch` 却只报「搜索请求失败: timed out」；且桌面环境的「系统代理」开关并不会写进终端进程的环境变量，用户无从判断。现新增 `utils/http.py` 客户端工厂统一出网语义：`trust_env` 读 `ALL_PROXY` / `HTTP(S)_PROXY`（socks5 经 socksio），构造前先跑 `normalize_proxy_env()` 归一化非标准的 `socks://`（否则 httpx 构造期即抛 `Unknown scheme for proxy URL`）；两个工具改用 `httpx2` 流式读取（保留 2MB 上限与超时），对外行为与错误文案不变。顺带修掉一个隐蔽的封禁源：httpcore 会在 TLS 握手发送 ALPN `["http/1.1"]`，DuckDuckGo 据此判定为机器人并返回反爬 challenge 页（表现为「（无搜索结果）」而非报错；实测带 ALPN 被拦、不带则正常，与 UA / Accept 等请求头无关），现显式屏蔽 ALPN（`_NoAlpnContext`），证书校验照常（`CERT_REQUIRED` + 主机名校验 + 系统 CA），并回到改造前 urllib 的行为（urllib 不发 ALPN）。测试：`tests/test_utils_http.py`（本地 TLS 服务器在握手层断言「不发 ALPN」+ 未信任证书仍被拒，不联网）、`conftest.py` 的本地假 HTTP 代理 fixture（目标用 `.invalid` 保留域，不走代理必然 DNS 失败）、两个工具测试改用 `httpx2.MockTransport`（补 charset / UA / 字节上限断言）。
- **TUI 复制内容到系统剪贴板失效（VTE 系终端）**：Textual 只通过 OSC 52 序列（`\x1b]52;c;<base64>`）写系统剪贴板，能否生效完全取决于终端——VTE 系（GNOME Terminal / Console / Tilix / xfce4-terminal，环境带 `VTE_VERSION`）不支持该序列，于是「鼠标选中 + ctrl+c」静默失败：应用内部拿到了文本，系统剪贴板却没变。现新增 `tui/clipboard`：`SmithTUI.copy_to_clipboard` 改为优先调用系统剪贴板命令（Linux 按 `wl-copy` → `xclip` → `xsel`，macOS `pbcopy`，Windows `clip`），全部不可用或执行失败时再退回原 OSC 52 实现；文本一律经 stdin 传入，不拼进命令行（对话内容含引号 / 换行 / `$()`，拼接即命令注入）；上次成功的工具会被记住，避免每次逐个探测。配套给 `process.run` 增加 `input`（经 stdin 喂数据）与 `capture_output=False`（不捕获输出）——剪贴板工具 fork 后长驻并继承输出管道，捕获管道会让 `communicate` 等不到 EOF（实测每次复制从 0.12s 恶化到 6s 超时）。已补测试：候选顺序与降级、记住上次成功的工具、文本只走 stdin、不捕获输出、失败退回 OSC 52。
- **TUI「已探索」汇总头过早显示「已探索」**：状态此前取决于「组是否已封口」（`_finalized`），而封口在遇到非上下文工具（`run_command` / `write_file` 等）时立刻发生——read / grep 与命令混批在真实会话里很常见，于是组一创建就被判定完成，转轮也随即停掉，仍在跑的读取明明还没出结果却标成「已探索」。现状态只看组内是否还有**没出结果的子工具**：封口只决定分组边界（后续只读工具另起一组），头行继续显示「⠋ ⚙ 探索中 · N 次读取」+ 转轮，直到最后一个子工具收工才变「▸ ⚙ 已探索 · …」；轮次结束由 `ui_turn_end` 调 `drain_context_groups()` 兜底收尾（防结果缺失时转轮永转）。已补回归测试（混批封口后仍「探索中」、出结果后「已探索」、轮次结束兜底收尾）。
- **TUI「已探索」块的子项挂错位置、折叠失效**：`ContextGroup` 用来缓冲「compose 前到达的子控件」的列表取名 `_pending_children`，与 Textual `Widget` 的内部属性（compose 前注册子节点的缓冲）同名，两者实为同一个列表。后果有二：子控件被 Textual 直接当成 compose 子节点注册在 header **之前**（明细渲染在「▸ ⚙ 探索中 · N 次读取」标题上方），且 `_compose` 消费后会 `clear()` 该列表，`on_mount` 的补挂循环拿到空列表——`.group-body` 恒为空，导致明细常驻可见、Enter / 点击折叠完全失效（`ContextGroup` 的 `height: auto` 样式也在空转）。现缓冲改名为 `_buffered_children`：明细回到正文容器内、标题之下，折叠/展开恢复正常。已补回归测试（子项归属 `.group-body`、标题在明细之上、折叠真的收起明细）。
- **自动标题一次失败即永久放弃**：标题请求由后台线程发起，此前采用「只尝试一次」——`_title_attempted` 在发起前置位，异常被静默吞掉，于是首轮一次网络抖动（或标题模型配置有误、瞬时错误重试耗尽）就让整个会话再也不会自动命名，且用户看不到任何提示。现改为**有限次跨轮次补试**：失败保留计数，下一轮任务正常结束后再试，上限 3 次（`TITLE_MAX_ATTEMPTS`），成功后自然停止；失败改经 renderer 提示原因并说明「将在下一轮结束后重试」或「已停止重试，可用 `/rename` 手动命名」，不再静默。计数在 `/new` 与恢复会话时重置；用户已命名（`/rename` / `--name` / 恢复的标题）时不消耗尝试次数。另注：`llm/client.py` 的瞬时错误重试（`MAX_RETRIES`）对标题请求只在「尚未输出任何正文」时生效（避免正文重放），4xx 类错误不重试——补试机制正是为兜住这类失败。

- **TUI 转轮行每 tick 整行重绘（webfetch 等长耗时工具闪烁）**：工具行与「已探索」汇总行 pending 时以 10 次/秒推进转轮，而 `Static.update()` 的脏区是整个控件——摘要带上 URL 后就是整行宽，终端因此每 100ms 重写整行（实测 100 列终端下每次 89 列）。短工具毫秒级结束看不出，`webfetch` 这类网络工具 pending 可持续数十秒，慢终端（SSH / tmux / 大字号）上表现为闪烁。现改为只把转轮那一格标脏（实测终端写入从整行降到 1 格，画面文本不变）；仅用于行内其他字段静止的转轮行——底部运行动画的秒数每 tick 都在变，仍走整控件更新。顺带把「已探索」汇总行的进行中文案由「正在探索」改为「探索中」，与完成态「已探索」成两态语义。
- **TUI 提问面板多选题一道未勾选时卡住无法前进**：多选题此前要求至少勾选一项或输入自定义回答，未勾选时按 Enter 既不提交也不给任何反馈（静默 no-op），只能取消整组，进不了下一题。现允许空提交——列表态未勾选任何选项时按 Enter 记为「（未选择）」并照常前进（空串在全局表示「取消」，故用显式标记区分），对齐 opencode「空答案合法、工具结果标 Unanswered」的语义；输入框里的空文本回车仍只退回选项列表，不直接跳过本题。多选题底部提示同步改为「enter 提交（未勾选=未选择）」。
- **TUI 底栏 git 分支只显示末段**：底部状态栏读取 `.git/HEAD` 时误按 `/` 取了最后一段，`feat/tui-statusbar` 这类带 `refs/heads/` 子路径的分支名只剩 `tui-statusbar`；现只剥离 `refs/heads/` 前缀，展示完整分支名。
- **LLM 流中途断连自动重试**：推理模型思考时对端（服务商 / 网关 / 代理）可能因空闲超时掐断长连接（`RemoteProtocolError: incomplete chunked read`），此前这类流中途的传输层错误不在重试范围内，会直接中断任务；现将 `RemoteProtocolError` / `ReadError` / `ReadTimeout` 纳入瞬时错误重试，每次重试前在终端打印错误详情，重试次数用尽仍失败则照常报错。已输出正文后不重试（重放会重复打印）；仅输出过思考内容时可安全重算（思考只展示、不写入会话）。
- **系统代理写入 `socks://` 导致启动即崩**：Clash / FlClash / V2RayN 等设置系统代理时会往环境里写 `ALL_PROXY=socks://host:port`（少了版本号），而 httpx 只认 `socks5://` / `socks5h://`，在构造客户端时即抛 `Unknown scheme for proxy URL`，且该校验发生在 `NO_PROXY` 匹配之前——用户一开系统代理，SmithCode 启动就失败。新增 `utils/proxy.py` 的 `normalize_proxy_env()`，在 CLI 入口与 `LLMClient` 构造前把所有代理环境变量里的 `socks://` 就地归一化为 `socks5://`（幂等、失败不阻断启动，MCP 的 HTTP 传输共用同一份环境变量一并受益）；依赖新增 `httpx2[socks]` 以带上 `socksio`，真正支持 SOCKS 代理。
- **grep 输出吞掉行首缩进，内容照抄作 `old_string` 必然失败**：`grep` 渲染匹配行与上下文行时用了 `line.strip()`，行首缩进被抹掉。而 Agent 的常见用法是「grep 定位 → 复制内容作 edit_file 的 old_string」：单行锚点碰巧仍能命中（去缩进后仍是原行的子串），**多行锚点则必然报「old_string 未找到」**——尤其在「在某行后纯新增一行」这种看似不可能失败的场景里，续行缩进对不上直接导致编辑失败。现改为按文件原文输出（`tools/search.py` 的 content 与 context 两处渲染），`grep` 工具描述与系统提示词同步说明「输出保留缩进、去掉『路径:行号: 』前缀后可直接作 `old_string`」。

- **编辑不再改写整个文件的换行符（跨平台保真）**：此前文件工具走 `read_text()` / `write_text()` 的默认参数——读时把 CRLF 归一化成 LF，写时又按 `os.linesep` 翻译回去，于是 **Linux 上编辑 CRLF 文件会把整个文件变成 LF、Windows 上编辑 LF 文件会整体变成 CRLF**：只改一行却整个文件 diff 全红，git 里引入换行符噪音，且模型从 `read_file` 完全看不出来（也无法自救：显式写 `\r\n` 锚点必然匹配不上）。这是所有跨平台 Agent 的共性坑（Claude Code / Codex / Gemini CLI / opencode 都有同类 issue）。现按 Claude Code 的策略实现：**读时探测换行风格（LF / CRLF / CR）与 UTF-8 BOM 并归一化为 LF 交给模型，写时按原文风格还原**——模型永远只看 LF 内容，写回由工具负责，`edit_file` / `write_file` / `apply_patch` 均只改动被编辑的行。新增 `textfile.py` 作为文本文件读写的唯一出口（对标 `process.py`），`tools/files.py` / `tools/patch.py` / `tools/search.py` 全部接入；新建文件默认 LF（平台无关），混合换行的文件统一为占多数的风格。顺带修掉两个同源问题：**UTF-8 BOM 被当成正文**（`read_file` 会显示 `1│\ufeffhello`）现在读写两侧都保留 BOM 且不进正文；**非 UTF-8 文件抛裸 `UnicodeDecodeError`**（`edit_file` / `apply_patch` 会直接中断 agent 循环）现改为中文友好错误串。注意读侧不能用 `Path.read_text(newline=...)`——该参数 3.13 才加入（`write_text` 的 newline 是 3.10 加的），故统一用内置 `open(newline="")` 保住 3.10 兼容。

## [0.9.0] - 2026-09-14

### 新增

- **MCP（Model Context Protocol）支持（MVP：stdio）**：Agent 可接入外部 MCP 服务器，连接成功后其工具以 `mcp__<服务器>__<工具>` 出现在模型工具列表，与内置工具共用权限（默认逐个确认）、串行调度、输出截断与终端展示。新增 `mcp/` 子系统：
  - `config.py` 双作用域配置：用户级 `~/.smithcode/config.toml` 的 `[mcp.servers.<名称>]`（tomlkit 写入保注释，`env` 用内联表使 command / env / cwd / timeout 聚合在同一段）；项目级 `<工作区>/.smithcode/mcp.json`（`mcpServers` 结构，兼容 Claude/Cursor 片段）；同名项目条目整体覆盖用户条目（字段不合并）；启停状态是条目内的 `enabled` 字段（默认启用时省略）；坏条目只警告跳过、不阻断启动
  - `secrets.py` 密钥链：配置只写 `${VAR}` / `${VAR:-default}` 引用（command / args / env / cwd 均展开），解析顺序为进程环境 > `credentials.json` 的 `mcp.<服务器>.<变量>`（原子写、POSIX 0600）> 向导补录；缺失标记 `missing_env` 不拉起 server（非交互 fail-closed）；展开值登记全局 Redactor，工具结果 / stderr / 日志 / 预览统一脱敏
  - `runtime.py` + `connection.py` + `factory.py`：客户端改基于官方 `mcp` SDK。`AsyncRuntime` 把唯一的 asyncio 事件循环关在专用线程（所有连接共用），`SdkConnection` 对上层暴露同步门面（`run_coroutine_threadsafe` 投递 + 当前线程取消令牌轮询，Esc / 超时中断），结果用 `model_dump(by_alias=True)` 归一为同形 dict。SDK 不推送「连接断开」事件，故用一层代读泵包装传输、读到 EOF 即回调 `on_closed`（等价旧读线程的崩溃检测）；stdio 子进程 stderr 落临时文件供 `/mcp logs`；`notifications/tools/list_changed` 经 SDK `message_handler` 刷新工具；传输由 `factory.py` 按 `cfg.type` 构造（当前 stdio，HTTP/SSE/OAuth 留后续）
  - `catalog.py` 命名与结果映射（非法字符替换、64 截断、冲突补后缀；content / structuredContent / isError → 文本，脱敏 + 截断）；`service.py` 会话级连接管理（`Agent.start()` 后台并发连接、失败隔离、重连 / 启停 / 日志、`tools/list_changed` 自动刷新、动态注册与反注册、项目配置启动警示）；`templates.py` + `wizard.py` 添加向导（模板 / 手动、作用域、密钥三模式、预览确认；纯状态机 + REPL 行式渲染器）
- **`/mcp` 命令与添加向导**：`/mcp` 弹状态选择框（各服务器状态 + 查看工具 / 重连 / 停用 / 日志 / 删除）、`/mcp list` 文本列表、`/mcp tools|logs|reconnect|enable|disable|remove <名称>` 子命令；`/mcp add` 在 TUI 打开居中向导面板（添加方式 → 模板/命令 → 名称 → 作用域 → 密钥（掩码输入）→ 预览 → 保存并后台连接），REPL 走同一状态机的行式问答；`/mcp add <名称> -- <命令...> [-e KEY=VALUE] [--scope user|project]` 支持非交互直通；向导完成后先落凭据再写配置并连接，Esc / q 取消全程无副作用。任务运行中会改动 MCP 配置的命令被 busy 守卫拦截
- **系统提示词新增「MCP 工具」行为节**：`mcp__` 命名来源、外部描述与输出按不可信内容处理、服务器未连接时不臆造调用；`tools/base.py` 新增线程安全的动态注册 API（与静态工具共用注册表）
- **MCP 远程传输（Streamable HTTP / SSE）与请求头鉴权**：`type` 支持 `http`（别名 `remote` / `streamable-http`，走 Streamable HTTP）与 `sse`（legacy），远程条目用 `url` + `headers`（值支持 `${VAR}` 引用，解析链与 stdio 一致）；`http` 由自持的 `httpx2.AsyncClient` 承载静态 token（`Authorization` 等请求头）；`load_servers` 兼容 Claude/Cursor/VS Code 的 `type` / `url` / `headers` 写法；配置指纹纳入传输 / 端点 / 请求头，变更即触发重连；`/mcp add <名称> --url <地址> [--type http|sse] [--header K=V]` 支持直通添加（`--header` 字面值自动存入凭据库、配置只留 `${...}` 引用）
- **MCP OAuth2.1 授权**：远程服务器可配 `oauth = true`，由官方 SDK 的 OAuth 提供者完成发现 / 动态注册 / PKCE / 换 token / 刷新；token 与 client_info 存独立的 `~/.smithcode/mcp_auth.json`（原子写、POSIX 0600，值全程登记脱敏器），重启后静默复用、无需再次授权。**后台连接绝不弹浏览器**：无 token 时状态为「需要授权」，用户显式 `/mcp auth <名称>` 才打开浏览器并在固定本地端口等回调；非交互 / CI 下授权请求 fail-closed。配置解析支持 `oauth` 字段（兼容 Claude / opencode 写法），`/mcp add ... --oauth` 直通添加，`/mcp` 服务器菜单新增「OAuth 授权」项
- **MCP 远程向导与进度 / 订阅增强**：添加向导新增「远程服务器（HTTP / SSE URL）」分支（URL → 传输类型 → 鉴权方式（OAuth / 请求头）→ 请求头，字面值自动存入凭据库、`${VAR}` 原样引用），模板新增 Linear / Sentry 远程示例；工具调用接入 SDK 进度回调（按 10% 里程碑展示，`total` 未知时不展示）；现代协议（2026-07-28+）经订阅流监听 `tools/list_changed` 自动刷新工具，旧协议仍走通知回调

- **项目指令（AGENTS.md）自动注入**：启动时读取用户级 `~/.smithcode/AGENTS.md`；项目级沿目录链从 git 根（最近的含 `.git` 的祖先目录，`.git` 为文件也算，兼容 worktree）逐级向下探测到工作区（无 `.git` 时仅工作区），`[instructions].files` 可增加 `CLAUDE.md` 等文件名、`paths` 可追加任意指令文件，作为系统提示词动态段注入 `messages[0]`——与 skills / goal 同通道，压缩天然保留、恢复会话按磁盘最新内容重建。优先级「越具体越优先」（用户级 < 项目链 git 根 → … → 工作区 < 追加文件），段内声明冲突裁决（靠后优先）与安全边界（不得覆盖权限 / 沙箱 / fail-closed，用户当前明确要求优先）。装载时机为会话边界（启动 / `/new` / 恢复），由 `Agent` 调用 `instructions.refresh()`（对齐 Codex「每会话装载一次」）：会话中途修改 / 新增 / 删除指令文件不影响进行中的会话，提示前缀缓存全程稳定，修改在新会话或重启后生效；指纹（`path, scope, mtime_ns, size`）用于边界处去重，未变化时零读取。`[instructions].max_chars`（默认 8000）预算内高优先级文件完整保留、低优先级截断并提示用 read_file 查看全部，放不下的文件省略并计数。不做信任门控：注入是纯文本，无法影响代码强制的安全边界；显式配置的 `paths` 文件缺失 / 不可用会警告一次，默认位置缺失静默

### 变更

- **运行环境要求升至 Python 3.10+**：MCP 子系统改用官方 `mcp` SDK（要求 Python ≥3.10），`pyproject.toml` 的 `requires-python` 与 `AGENTS.md` 的兼容性红线同步调整；不再需要为 3.9 做联合类型的延迟求值规避

- **MCP 客户端改基于官方 `mcp` SDK**：自研 stdio 客户端（`mcp/client.py`）移除，新增 `runtime.py`（共享 asyncio loop 线程）/ `connection.py`（同步门面）/ `factory.py`（按传输构造）；stdio 行为与工具注册、权限、调度、展示语义保持不变

- **TUI 运行动画改到输入框上方（固定一行）**：`#running`（`⠋ Working… 12s` 计时行）由底行 `#bottom` 移入 `#input-wrap`，固定在输入框正上方（`padding-left: 3` 与键入文字左对齐）；start / stop / 计时 / Esc 后「· 正在停止…」的逻辑不变（仍由轮次边界驱动）。输入框的左侧绿竖线同步从容器 `#input-wrap` 下移到输入框 `#input` 自身——竖线只框住输入框 3 行，上方动画行不再被框住。因动画占位时输入框上方多出一行，命令菜单的悬浮锚点在动画可见期间随之上移一行（`#command-menu.running`，`offset` -5 → -6），避免弹出菜单盖住动画行。权限 / 提问面板弹出期间输入框整体隐藏（原有语义），动画行随之一并隐藏、答完恢复显示并继续计时

- **TUI 对话区随窗口缩放自适应**：助手正文此前在 `Static.update()` 里按当时的可用宽度用 rich 完成折行，断行被固化进文本——窗口缩放后 Textual 只能对已折行的文本再软换行 / 裁切，无法把折行并回去，表现为变宽时右侧留白、变窄时断行位置错乱；且流式首次渲染时组件尚未布局、宽度为 0，回退按 80 列折行，与真实窗口宽度无关。现新增 `MessageBody`（`tui/widgets.py`），与工具正文的 `_ToolBody` 同策略：在 `render()` 里按 `self.size.width` 实时渲染，宽度变化即整体重排（旧宽度下的折行与分块缓存失效重建，宽度不变时照旧复用，流式开销不变）。用户消息、通知、工具头等直接上屏原始文本的块与左右对照 diff 块本就随容器自适应，不受影响

- **系统提示词「工作方式」补收尾一步（第六步）**：含工具调用的任务必须以一条纯文本总结结束回合（不允许在工具结果后直接停止、也不得以工具调用结束），总结覆盖完成项 / 改动文件（`file_path:line_number`）/ 验证结果 / 遗留与存疑，无法验证时如实说明；纯问答不套总结模板。原「沟通」节里的一句话总结要求并入该步，验证如实说明的要求也由第 5 步移入，避免多点漂移；顺带压缩了第 3 步的重复风格措辞

- **启动命令更名为 `smith`**：安装入口由 `smithcode` 改为 `smith`（不再提供旧名），`--help` 的 usage 与 `-V` 输出的程序名同步变为 `smith`；初始化向导与「缺少 API Key」提示里的命令示例一并更新。包名 `smithcode`、`python -m smithcode`、配置与历史路径（`~/.smithcode/`）、`SMITHCODE_*` 环境变量均不变。

- **MCP 用户配置渲染修正（属性聚合）**：`write_user_server` 的 `env` 改用 tomlkit 内联表——此前赋值 dict 会被渲染成独立的 `[mcp.servers.<名称>.env]` 子表，把同一服务器的属性拆成两段；现在 `command` / `env` / `cwd` / `timeout` / `enabled` 都在同一段。启停状态改为服务器条目自己的 `enabled` 字段（写在定义它的文件：用户 `config.toml` 或项目 `.smithcode/mcp.json`；默认启用时省略该键），不再使用跨文件的 `[mcp.enabled]` 覆盖表——属性只在一处、语义单一；`enabled` 始终排在条目最前
- **选择面板支持逐级返回（TUI）**：选择面板的层级改由宿主 `SmithTUI._select_stack` 维护——进入下级菜单时把父级 `CommandSelect` 与本次选中的值压栈，Esc 未选中时逐级弹回上一级并把光标锚定回原行，根级 Esc 才关闭，执行实际动作后清空栈。命令结果处理收敛为 `_apply_outcome(outcome, text, nested)`，`handle_command` 只做 busy 守卫 + 分发，`_present_select` 统一挂载面板（`/model` / `/skills` / `/sessions` 等单级选择行为不变）。MCP 服务器操作菜单因此支持「Esc 返回服务器列表」
- **`/mcp` 交互展示调整**：无参 `/mcp` 不再区分是否已配置，一律弹出选择面板（无服务器时仅「添加 MCP」一项；添加项与已有服务器之间留一个不可选中的间隔行，↑↓ 自动跳过）。服务器行左侧依次为名称、工具数量、级别（用户级显示为「全局」、项目级显示为「项目」），状态文字贴行尾右对齐并按状态着色（已连接绿、连接中黄、缺少密钥橙、失败红、停用/断开灰）。对话区通知与命令结果重新分级：连接成功为绿色 ✓（新增 `Renderer.success()` 语义方法），新增 / 重连 / 启用等后台进行中状态为蓝色 ↻（`retry`），停用 / 删除为中性通知——不再出现「正在重连」绿色打勾、「已连接」默认色无状态的问题
- **TUI 选择弹窗宽度改为按档位声明（对齐 opencode）**：通用选择面板 `SelectionPanel` 的宽度不再写死 64 列，而是四档定值——`small` 40 / `medium` 64（默认）/ `large` 88 / `xlarge` 116，由调用方在 `CommandSelect.size` 上声明（宿主不测量内容），未知档位回退 `medium`；窄终端仍由 `max-width: 90%` 夹取。`/sessions` 因选项行较长声明 `large`；`/model` / `/skills` / `/effort` 保持默认 `medium`。非交互 REPL 只列候选、不受影响
- **TUI 选择面板改为两列行布局（`/sessions` 展示调整）**：每项由整块文本改为「左列（标记 + 标题 + 说明，占满剩余宽度）+ 右列 trailing（贴行尾右对齐）」的两列行，用列布局而非手工补空格，宽度随档位 / 终端自适应（`CommandChoice` / `SelectionItem` 新增 `trailing` 字段，选中行底色移到行上使高亮贯通整行）。`/sessions` 选择框据此调整：标题后紧跟短 id、更新时间右对齐、不再展示模型信息；列表本就按更新时间倒序（`list_sessions`）
- **技能已激活段补充裁决声明（提示词）**：`ACTIVE_INTRO` 补上与项目约定段（`INSTRUCTIONS_INTRO`）等价的裁决规则——技能指令不能覆盖安全边界与权限规则，与用户当前明确要求冲突时以用户要求为准。技能正文来自可能不可信的仓库，且「已激活技能」段渲染在项目约定段之后，此前缺这句声明，段间位置容易被误读为「越靠后优先级越高」

- **内置 GitHub 模板改指官方 server（远程 HTTP）**：向导模板 `github` 由已归档的 `@modelcontextprotocol/server-github` 改为官方 `https://api.githubcopilot.com/mcp/`（Streamable HTTP + `Authorization: Bearer ${GITHUB_PERSONAL_ACCESS_TOKEN}` 请求头），工具数由 25 增至 44，且不再需要本机 Node 运行期。`Template` 新增 `headers` 字段承载远程模板的请求头；远程模板的 `env` 语义扩展为「请求头引用的变量名」，向导据此自动补出密钥步骤（存入凭据库 / 引用环境变量），并在确认页展示密钥处置说明。**注意**：官方 server 的 release 能力目前只读（`list_releases` / `get_latest_release` / `get_release_by_tag`），**不提供创建 release**；认证沿用原有 `GITHUB_PERSONAL_ACCESS_TOKEN`（现在作为请求头引用），token 本身无需换类型，但若此前只在 `env` 里引用、未落凭据库，需按向导重新存一次（或改用「引用环境变量」模式继续引用环境变量）

### 修复

- **MCP 连接生命周期竞态**：修复三处异步竞态——① 重连 / 断开后再次崩溃会被静默吞掉（状态卡在「已连接」且工具不反注册）；② 连接握手期间 `disable` 被结果覆盖、服务器仍被拉起并注册工具；③ 握手期间 `remove` 遗留孤儿进程与已注册工具。现在以「连接代际 + 连接身份」校验回写，握手结果在失效时直接丢弃；`_connect` 兜底捕获非预期异常并落到 FAILED（不再停在 CONNECTING）

## [0.8.0] - 2026-09-12

### 新增

- **会话持久化与恢复（自动落盘 + 崩溃修复 + 标题）**：会话不再依赖手动 `/save`——每条非 system 消息实时追加到用户目录的 append-only JSONL 转录（`~/.smithcode/projects/<项目 slug>/sessions/<会话 id>.jsonl`；懒物化，没有消息不建文件），进程崩溃/关窗也能恢复。新增 `sessions/` 子系统（`paths` / `format` / `model` / `store` / `title`，格式与容错可离线单测），`Session` 升级为会话聚合根（`MessageLog` 追加钩子、`restore_state` 原地装载、`set_compacted`、`set_title`，对象身份跨 `/new` 与恢复不变）：
  - **入口**：`smithcode -c` 恢复当前目录最近会话、`-r/--resume [ID]` 指定恢复（支持唯一前缀、`.jsonl` 路径与旧 `.json` 导入；不带值恢复最近）、`--name` 启动即命名、`--no-session-persistence` 本次不落盘；命令侧新增 `/sessions`（无参弹选择框、**选中即切换**，与 `/model` / `/skills` 一样在命令菜单里选中即弹；`list` 文本列表、`delete <id>` 删除、`<id|序号>` 直接切换）、`/rename`，`/new [名称]` 支持带名新建，`/save` 改为"立即写盘并显示转录路径"（会话内不提供 `/resume`，恢复入口只有 CLI 的 `-c`/`--resume`）；TUI 切换后清屏回放历史（busy 时拒绝切换会话），REPL 打印切换摘要
  - **恢复语义**：消息历史 + goal/plan/技能激活集（`t=state` 投影缓存，损坏/缺失只降级不阻断）复原；权限"总是允许"、越界信任目录、已读记录一律不恢复（安全优先）；goal 回合计数与 token 基线重置；会话 id 沿用（`{$session}` 请求头跨进程稳定）
  - **崩溃修复**：工具结果落盘前进程被杀会留下悬空 `tool_calls`，恢复时按序补「未执行：上次会话中断」占位并写回转录（悬空在中段则截断），保证「每个 `tool_call_id` 恰有一条结果」不变量，下一次请求不会被服务商拒绝
  - **压缩升级为自包含检查点**：自动压缩与 `/compact` 追加一行 `compact` 记录（摘要 + 尾部 + token 前后值），旧消息保留在转录供导出/审计；加载只装配「最近检查点 + 其后消息」（`ContextMeter.compact_count` 按检查点恢复）
  - **会话标题**：首轮正常结束后后台线程用 `title_model`（空 = 当前模型）生成 3-6 词标题，JSON 输出校验、失败静默；`/rename` 的用户命名优先且自动标题不可覆盖；列表展示"标题或首轮 prompt 截断"。解析走 typed JSONL，不会被工具输出中的字符串污染。TUI 侧边栏顶部常显当前会话标题（未生成时回退首轮 prompt 截断、无历史时整段隐藏），`/rename` 与后台生成完成经渲染层即时刷新
  - **配置**：新增 `[sessions]` 段——`enabled`（总开关）、`cleanup_days`（保留期清理，默认 30 天，启动时 best-effort）、`persist_state`、`list_limit`、`auto_title`、`title_model`、`title_max_chars`；`format.SessionStore` 写失败只警告一次并降级为纯内存会话，绝不阻断任务
  - 兼容：旧 `<workspace>/sessions/*.json` 可经 `--resume <路径>.json` 导入；落盘位置在用户目录，不污染工作区

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

- **TUI 对话区消息打印统一（唯一入口 + 级别化通知 + 统一缩进）**：此前对话区内容由多条路径零散产生（renderer 事件、宿主回显、命令输出、欢迎横幅、错误兜底各写各的），且通知行走 `_mk` 无缩进类、与工具/正文块左起点不一致，导致格式漂移。本次收口：
  - 新增 `tui/chat.py` 语义消息模型（`Level` + `User` / `Assistant` / `Notice` / `Block` / `Footer` / `StreamDelta` / `Thinking*` / `Tool*` / `Welcome`，纯数据无 Textual 依赖）；`ChatView.apply(item)` 成为**唯一打印入口**，工具块映射与思考块引用一并收归消息区，宿主只负责路由。
  - 统一布局：所有顶层消息带 `.chat-item`（缩进 3 / 上间距 1 的唯一来源），用户消息左边框占 1 列故其 padding-left 为 2，正文与其它消息左对齐；修复信息行齐左、与块不对齐的问题。
  - `Renderer` 新增 `warn()` / `error()`（默认降级为 `info`，TUI 按级别着色 + 固定 1 格图标），LLM 重试、权限拒绝、会话降级、目标暂停等改走对应级别，调用点不再手写 `\n` / `⛔` / `[LLM]` 前缀；命令层旧 `style` 字符串由集中映射兼容，命令零改动。
  - 宿主旁路（欢迎横幅、历史回放、用户回显、轮次页脚、任务异常兜底、命令输出）全部改经 `apply`；新增 `tests/test_tui_chat.py` 覆盖级别映射、唯一入口与「所有顶层消息都带 `chat-item`」的对齐回归。
  - **命令工具执行中状态**：`run_command` 耗时不确定，pending 期头部显式显示「执行中」+ 转轮（`⠋ 执行中 · command …`），执行完成后转回静态行（`▸ ⚙ command … · N 行`）；其它工具保持原转轮摘要。`ToolCall` 新增 `running_label`，`ToolStart` 透传。

- **`/effort` 切换改为静默**：不再打印「思考强度已切换」，反馈由输入框底栏「模型 · 思考强度」的即时刷新承担（与 `/skills` 手动加载的静默语义一致）

- **TUI 命令菜单隐藏滚动条**：候选超出固定展示行数时仍可用滚轮 / 键盘滚动，只是不再绘制右侧滚动条（与聊天区一致），避免菜单紧贴输入框时把滚动条位置误看成"整块悬浮层"

- **`/new` 与 `/save` 语义微调**：`/new` 从"破坏性清空、旧会话不可找回"变为"开新会话，旧会话仍在磁盘、可 `/sessions`/`-c` 找回"（TUI 仍清屏、busy 守卫不变）；`/save` 从"导出原始 JSON 到工作区 `sessions/`"变为"立即 flush 并显示转录路径"（旧格式可用 `--resume <路径>.json` 导入）

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
- **计划（todo_write）只在新建立时打印到对话区**：此前每次 `todo_write` 调用都会往对话区追加一份完整 `[计划]` 块，而模型每完成一步就更新一次，导致完成进度反复刷屏。现在仅**新建清单**时展示一次，并复用 plan 工具块（`display: block`）承载详情——可 Enter / 空格 / 点击**展开与收起**、**默认展开**；后续每步更新只静默刷新侧边栏，不再生成工具行或对话块。REPL 侧同样只在新建时打印 `[计划]`。`Renderer.plan` 新增 `created` / `tool_id` 参数，`TuiRenderer.plan` 不再向聊天区发 `[计划]` 块；plan 工具块改用静态清单图标 `☰`（pending 期也不再显示转轮，避免被误认为一直转）
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

- **欢迎小贴士去掉不存在的 Ctrl+O 快捷键**：启动欢迎语的小贴士池里写着「Ctrl+O 切换计划侧边栏显示」，但 TUI 从未绑定该按键（侧边栏按终端宽度 ≥ 120 列自动显隐），照做按不出任何反应。现删除该条贴士，并修正 `tui/app.py` 文件头注释里同样的过时描述。

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
