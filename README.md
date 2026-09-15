# SmithCode

终端 AI 编程助手：让大模型调用工具，帮你读写文件、执行命令、完成编程任务。

你用自然语言描述任务，SmithCode 自主规划步骤、调用工具、根据结果继续推理，直到任务完成——全程流式输出，敏感操作逐个向你确认。交互终端下是一个全屏聊天界面（复刻 Claude Code 风格），管道 / CI 下自动退化为行式 REPL。

> **Harbormaster:** Hold up there, you. It's a shilling to tie up your boat at the dock... and I shall need to know your name.
>
> **Jack Sparrow:** What do you say to three shillings and we forget the name?
>
> **Harbormaster:** *Welcome to Port Royal, Mr. Smith.*

## 功能特性

### 全屏 TUI

交互终端启动 `smith` 直接进入全屏聊天界面（Textual 实现）：

- **消息区**：助手回复无前缀纯文本流式输出；思考过程折叠成块（只显示字符计数，不刷屏，Enter / 空格展开）；工具调用折叠成块；连续的读取 / 搜索工具汇总成一个可折叠的「已探索」块；用户消息带面板底色与角色色竖线；每轮任务结束追加「▣ 模型 · 用时 Ns」页脚
- **输入区**：多行输入框（Enter 发送，Shift+Enter / Ctrl+J 换行）；下方底行最左显示「权限模式 · 模型 · 思考强度」与运行状态，最右显示 git 分支与上下文占用
- **侧边栏**（终端宽 ≥ 120 列时出现）：会话标题、token 用量 / 上下文占用 / 压缩次数、持久目标卡片、任务计划清单、版本号与工作区路径
- **弹窗**：权限确认与 `ask_user` 提问以模态弹窗呈现，↑↓ 选择、Enter 确认、Esc 取消
- **中断**：任务运行时按 Esc 即时中断（正在进行的长命令会被终止进程树），空闲时按 Esc 清空输入框

管道 / CI 等非交互环境不走 TUI，退回行式 REPL，行为不变。

### 智能体与工具

内置 17 个工具（`use_skill` 仅在发现可用技能时向模型开放，`task` 仅在有可用子代理类型时开放），模型自主决定调用哪些、调用几次（可配置单次任务最大迭代轮数，默认不限）：

| 工具 | 说明 |
| ---- | ---- |
| `read_file` | 读文件，返回带行号内容（`12│code`），支持 `offset` / `limit` 分段读取；拒绝二进制与目录 |
| `write_file` | 创建 / 覆盖写入；覆盖已存在文件前强制先 `read_file` |
| `edit_file` | 精确替换文本，`old_string` 须逐字符一致；支持 `replace_all` |
| `apply_patch` | patch 信封格式的批量多文件修改（Add / Update / Delete），原子落盘 |
| `list_dir` | 列目录（名称 / 大小 / 修改时间），跳过 `.git`、`.venv`、`node_modules` 等 |
| `glob` | 按通配符搜文件名，支持 `**` 递归，结果按修改时间新→旧排序 |
| `grep` | 按正则搜内容，支持忽略大小写、上下文行、只列文件 / 计数模式 |
| `run_command` | 执行 shell 命令，默认 60 秒超时（可延长至 300 秒），跨平台终止进程树 |
| `webfetch` | 抓取网页（仅 http/https）转纯文本，支持一次并行抓多个 URL |
| `websearch` | DuckDuckGo 网页检索，返回标题 / 链接 / 摘要 |
| `ask_user` | 任务中途向你提问（一次可提 1-4 个，带候选项） |
| `todo_write` / `todo_read` | 维护 / 读取任务步骤清单 |
| `goal_update` / `goal_read` | 更新 / 读取持久目标状态 |
| `use_skill` | 加载某个技能的完整指令 |
| `task` | 派发隔离子代理执行开放式调查或独立子任务，只把最终报告回传（不占用主对话上下文） |

多个工具调用**流式调度**：边预检边执行，可并行的只读 / 网络调用进线程池并发跑，有跨调用状态的工具（shell、写文件、交互确认）在主线程串行执行并作为顺序屏障，结果按提交顺序回传。

### 任务拆分与持久目标

- **任务拆分**：多步任务动手前，模型先用 `todo_write` 列出步骤清单，逐步执行并实时更新状态（进行中 / 已完成 / 取消）。清单在终端实时渲染，TUI 侧边栏随做随更；`/plan` 随时查看
- **持久目标 `/goal`**：`/goal <目标>` 设定一个跨回合存活的使命，Agent 在每轮任务结束后自动接续推进，直到逐条核验证据后声明完成、你暂停 / 清除，或回合预算（可选，默认不限）用尽。`/goal` 查看状态，`/goal pause | resume | clear | budget <N>` 控制生命周期

### 技能（Agent Skills）

兼容 agentskills.io 开放格式：技能是含 `SKILL.md`（YAML frontmatter 的 `name` + `description`，正文写指令，可附 `scripts/`、`references/` 资源）的目录。

- **渐进式披露**：启动只把技能名与描述装进系统提示词（约 100 token/技能），命中任务后由模型调用 `use_skill` 按需加载完整指令
- **发现位置**：项目 `.agents/skills/`、用户 `~/.smithcode/skills/`，以及 `[skills].paths` 追加的目录
- **信任门控**：项目级技能来自可能不可信的仓库，默认首次发现时确认（可「始终信任」落盘）
- **用户操作**：`/skills` 弹选择框（选中即加载）、`/skills list` 查看来源与诊断、`/skills refresh` 重扫磁盘；`/skill <名称> [任务]` 或 `/技能名 [任务]` 直达

### 子代理（Subagents）

模型可以把开放式多轮调查或可独立完成的子任务派发给**隔离子代理**执行：子代理拥有独立上下文与独立 Agentic Loop，只把最终报告回传给主对话，中间几十次工具调用不占用主上下文；同一回复里派发多个只读子代理会并发执行。

- **内置类型**：`explore` 只读侦察（搜索、阅读、网络查证，只回结论与 `file:line`）；`general` 通用执行（可读写文件、运行命令，作为顺序屏障串行）
- **自定义**：项目 `.smithcode/agents/*.md`、用户 `~/.smithcode/agents/*.md`，frontmatter 声明 `name` / `description` / `tools`（白名单）/ `model` / `max_turns`，正文为角色提示词；项目级定义复用技能的项目信任门控，`[subagents].paths` 追加目录，`[subagents].disabled` 按名禁用
- **安全与权限**：子代理共享你的权限模式与会话规则，写 / 命令照常确认（确认框带 `[类型]` 前缀）；工具白名单双重强制，不能提问、不能再派子代理，MCP 工具默认关闭（`[subagents].allow_mcp`）
- **用户操作**：`/agents` 查看类型目录，`/agents refresh` 重扫磁盘；Esc 中断会级联取消所有子代理

### 项目约定（AGENTS.md）

自动读取用户级 `~/.smithcode/AGENTS.md` 与项目级 `<工作区>/AGENTS.md`，把仓库的开发约定注入系统提示词，模型不再靠猜或反复读说明文件。

- **优先级**：用户级 < 项目级 < `[instructions].paths` 追加文件，越具体越靠后；段内声明「不得覆盖权限 / 沙箱，用户当前要求优先」
- **向上发现**：项目级从 git 根沿目录链探测到工作区（`.git` 工作树标记同样识别），monorepo 子目录自动继承仓库根与中间层的约定；无 `.git` 时只看工作区
- **会话边界装载**：与 Codex 一致，启动 / `/new` / 恢复时读取一次；会话中途修改文件不影响进行中的会话（提示前缀缓存全程稳定），新会话或重启后生效
- **配置**：`[instructions]` 段可改探测文件名（`files`，如加 `CLAUDE.md`）、追加文件（`paths`）、字符预算（`max_chars`，默认 8000）与总开关（`enabled`）；预算内高优先级文件完整保留，超出的截断并提示用 `read_file` 查看

### MCP 服务器

通过 Model Context Protocol 接入外部工具服务器（官方文件系统、GitHub、Playwright 等）：

- **添加**：`/mcp add` 打开交互向导（模板 / 手动命令 / 远程 URL、作用域、密钥、预览确认；TUI 为居中面板，REPL 为行式问答）；也可带参直通：`/mcp add <名称> -- <命令...> [-e KEY=VALUE]` 或 `/mcp add <名称> --url <地址> [--type http|sse] [--header K=V] [--oauth]`（`--scope user|project` 可选）
- **双作用域**：用户级 `~/.smithcode/config.toml` 的 `[mcp.servers.<名称>]`；项目级 `<工作区>/.smithcode/mcp.json`（`mcpServers` 结构，随仓库共享，兼容 Claude / Cursor / VS Code 写法）。同名时项目条目整体覆盖用户条目；启停状态是服务器条目的 `enabled` 字段（写在定义它的文件里，默认启用时省略）
- **传输**：stdio（npx / uvx 生态）、Streamable HTTP（推荐远程）、SSE（旧版）；远程用 `url` + `headers`
- **密钥与授权**：配置只写 `${VAR}` 引用；值存 `~/.smithcode/credentials.json` 的 `mcp.<服务器>.<变量>`（0600），或引用进程环境变量；OAuth 服务器配 `oauth = true`，token 存 `~/.smithcode/mcp_auth.json`（0600、自动刷新），首次用 `/mcp auth <名称>` 完成浏览器登录，之后静默复用；密钥与 token 全程脱敏
- **工具**：连接成功后以 `mcp__<服务器>__<工具>` 注册，与内置工具共用权限（默认逐个确认）与串行调度；工具列表变化自动刷新
- **管理**：`/mcp` 选择框（查看工具 / OAuth 授权 / 重连 / 停用 / 日志 / 删除）、`/mcp list` 文本列表；启动时后台连接、失败隔离，缺密钥或需授权时给出状态与操作提示

### 上下文管理

- **占用可视化**：`/context` 按角色分桶查看上下文 token 占用与压缩阈值距离
- **自动摘要压缩**：越过阈值时自动把早期历史压缩为结构化摘要（自包含检查点，保留系统提示词与近期尾部），任务不中断
- **手动压缩**：`/compact` 随时释放上下文空间
- **溢出自愈**：服务商返回上下文超限错误时，自动压缩后重试
- **用量统计**：`/usage` 查看 token 消耗（按「应用启动以来 / 当前会话」两个口径）

### 会话管理

- **自动持久化**：每条非 system 消息实时追加到本地 JSONL 转录（`~/.smithcode/projects/<项目>/sessions/<会话 id>.jsonl`），崩溃 / 关窗也能恢复
- **恢复入口**：`-c` 恢复当前目录最近会话、`-r/--resume [ID]` 指定恢复（支持唯一前缀）；会话内用 `/sessions` 无参弹选择框（选中即切换）、`/sessions <id|序号>` 直接切换、`/sessions list` 文本列表、`/sessions delete <id>` 删除
- **崩溃修复**：工具结果落盘前进程被杀留下的悬空 `tool_calls`，恢复时自动补占位或截断，保证历史合法
- **自动标题**：首轮结束后后台生成会话标题，`/rename <名称>` 可随时覆盖
- **其它**：`/new [名称]` 开新会话（旧会话保留在磁盘）、`/save` 立即写盘并显示路径、`--no-session-persistence` 本次不落盘

### 权限与安全

- **三级权限规则**：`allow` / `ask` / `deny`，按工具与参数通配符匹配，通过 `~/.smithcode/config.toml` 自定义
- **权限模式**：TUI 中 Shift+Tab 在 `Smith`（逐个确认）→ `Accept Edits`（编辑族自动放行）→ `Auto`（全部放行）间循环切换
- **安全命令免确认**：一批内置只读命令（`ls` / `cat` / `git status` 等，POSIX 与 cmd.exe 各一套）在默认 `ask` 下自动放行；真正运行代码的用法（`pytest`、`python x.py`、`npm run` 等）不放行
- **命令前缀记忆**：选「总是允许」时记住 argv 前缀（如 `python -m pytest *`），文件 / 参数变化仍命中
- **保护路径**：`.env` 内容不在预览与确认框中回显；`.git` 目录只读（禁止写入 / 编辑，读取放行）
- **工作区沙箱**：文件操作默认限制在授权目录内，越界需逐次确认；技能目录只读白名单
- **非交互 fail-closed**：管道 / CI 下无法弹确认时，所有需确认的操作一律拒绝，不挂起、不崩溃

### 跨平台与模型无关

- Windows / Linux / macOS，自动适配系统编码与 shell 风格（Windows 下提醒模型用 cmd 语法）
- 任何 OpenAI 兼容接口均可接入（DeepSeek、通义、Kimi 等），支持自定义请求头与思考强度档位

## 快速开始

### 1. 安装

要求 Python >= 3.10。

```bash
pip install -e .
```

### 2. 初始化配置

运行向导，按提示填入接口地址、模型名、API Key 与上下文预算（直接回车保留默认 / 已有值）：

```bash
smith setup
```

配置写入用户目录（所有平台同一位置）：

| 文件 | 内容 |
| ---- | ---- |
| `~/.smithcode/config.toml` | 行为配置：模型、接口地址、上下文预算、权限规则等，可安全分享 |
| `~/.smithcode/credentials.json` | 仅 API Key，只归本机 |

也可以跳过向导，直接用环境变量（优先级高于文件）：`SMITHCODE_KEY` / `SMITHCODE_MODEL` / `SMITHCODE_URL`。

### 3. 运行

```bash
smith                            # 交互模式：交互终端进全屏 TUI，管道下走行式 REPL
smith 帮我写个斐波那契函数        # 单次任务模式，完成即退出
python -m smithcode              # 等价的另一种启动方式
```

## 使用方法

### TUI 快捷键

| 按键 | 作用 |
| ---- | ---- |
| `Enter` | 发送消息 |
| `Shift+Enter` / `Ctrl+J` | 输入换行 |
| `↑` / `↓` | 翻输入历史（命令菜单弹出时为移动高亮） |
| `Ctrl+W` | 删除前一个词 |
| `/` | 唤出命令 / 技能补全菜单，↑↓ 选择后回车 |
| `Shift+Tab` | 循环切换权限模式 |
| `Esc` | 任务运行中：中断；空闲时：清空输入框 |
| `Ctrl+Q` | 退出 |

弹窗与折叠块：权限 / 提问弹窗用 ↑↓（或 `j`/`k`）选择、Enter 确认、Esc 取消；工具调用、思考块用 Enter / 空格展开收起。

### 交互模式命令

| 命令 | 说明 |
| ---- | ---- |
| `/help` | 显示帮助（自动列出全部命令） |
| `/new [名称]` | 开启新会话（可带名称），旧会话保留在磁盘、仍可恢复 |
| `/sessions [list [数量]\|delete <id>\|<id\|序号>]` | 历史会话：无参弹选择框（选中即切换）、`list` 文本列表、`delete` 删除、直接切序号 / id |
| `/rename <名称>` | 重命名当前会话 |
| `/plan` | 显示当前任务计划（步骤清单） |
| `/save` | 立即写盘并显示会话转录路径 |
| `/usage` | 查看 token 用量统计 |
| `/context` | 查看上下文占用分布与压缩次数 |
| `/compact` | 手动压缩上下文 |
| `/model [名称]` | 切换模型（无参弹候选列表） |
| `/effort [档位]` | 调整思考强度（none / minimal / low / medium / high / xhigh / max） |
| `/goal <目标> \| pause \| resume \| clear \| budget <N>` | 设定 / 管理持久目标；无参查看状态 |
| `/skills [list\|refresh]` | 无参弹技能选择框（选中即加载）、`list` 查看列表与诊断、`refresh` 重扫磁盘 |
| `/skill [名称] [任务]` | 加载技能；带任务时加载后立即开跑 |
| `/技能名 [任务]` | 技能名直达（等价 `/skill`） |
| `/agents [list\|refresh]` | 查看子代理类型目录；`refresh` 重扫用户 / 项目定义 |
| `/exit` | 退出程序 |

### 命令行参数

| 参数 | 说明 |
| ---- | ---- |
| `task` | 一次性任务描述；留空则进入交互模式；`setup` 进入初始化向导 |
| `-w, --workspace` | 指定工作区目录（默认当前目录） |
| `--add DIR` | 追加授权目录（可重复传入），跨项目访问用 |
| `-m, --model` | 指定模型名（覆盖配置文件与环境变量） |
| `-c, --continue` | 恢复当前目录最近一次的交互会话 |
| `-r, --resume [ID]` | 恢复指定会话（id 或唯一前缀；不带值时恢复最近一次） |
| `--name 名称` | 给新会话命名（仅新会话可用） |
| `--no-session-persistence` | 本次运行不保存会话记录（不可与 `-c/--resume` 同用） |
| `-y, --yes` | 自动批准所有确认（等价 Auto 档，`deny` 规则依然生效），慎用 |
| `--max-iterations N` | 单次任务最大迭代轮数（默认 -1 不限；配置后达上限会请求模型总结收尾） |
| `-V, --version` | 显示版本号 |

### 权限确认与权限模式

默认情况下：读取 / 搜索 / 抓取类工具自动放行；写文件、编辑、执行命令需要你确认：

```
⚠️  Agent 请求执行: run_command
   模式: git push origin main
   允许? [y]本次 / [n]拒绝 / [a]总是允许该模式:
```

选 `a` 后该命令前缀在本会话内静默放行（`/new` 或退出后清零）。

TUI 中按 **Shift+Tab** 在三档权限模式间循环切换，输入框底行最左侧实时显示当前档位：

| 模式 | 行为 |
| ---- | ---- |
| `Smith` | 默认，所有需确认的操作逐个询问 |
| `Accept Edits` | 文件编辑 / 写入（含 `apply_patch`）自动放行，命令执行仍确认 |
| `Auto` | 全部自动放行（等价 `-y`），`deny` 规则依然生效 |

模式仅当前会话有效，`/new` 或退出后回到默认。

### 自定义权限规则（~/.smithcode/config.toml）

在 `~/.smithcode/config.toml` 的 `[permissions]` 段配置（`smith setup` 首次生成时自带注释示例）。动作支持 `allow` / `ask` / `deny`，通配符匹配（文件工具匹配路径、`run_command` 匹配命令串），**写在前面的先生效，精确规则请放在宽泛规则之后**：

```toml
[permissions]
read_file = "allow"
write_file = { "*" = "ask", "*.env" = "deny" }
run_command = { "*" = "ask", "git *" = "allow", "rm -rf*" = "deny" }
```

上例含义：文件读取放行；写文件需确认、写 `.env` 直接拒绝；git 命令放行、`rm -rf` 直接拒绝、其余命令需确认。

### 配置项

主要配置项写在 `~/.smithcode/config.toml`，均有内置默认值：

| 配置项 | 默认 | 说明 |
| ---- | ---- | ---- |
| `[provider] model` | `deepseek-v4-flash` | 模型名 |
| `[provider] url` | `https://api.deepseek.com/v1` | OpenAI 兼容接口地址（向导预填此默认值，按你的服务商修改） |
| `[provider] reasoning_effort` | `high` | 思考强度档位，同时作为请求参数下发给模型 |
| `[provider] models` | 空 | `/model` 候选模型列表（空则尝试远端 `/models`） |
| `[provider.headers]` | 空 | 随每个请求发送的自定义请求头，值内 `{$session}` 替换为当前会话 id |
| `[context] budget` | 65536 | 上下文预算（token），建议不超过模型窗口大小 |
| `[context] compact_trigger` | 0.8 | 占预算的比例，越过即触发自动压缩 |
| `[context] compact_keep_tokens` | 15000 | 压缩时尾部原样保留的 token 数 |
| `[limits] max_iterations` | -1 | 单次任务最大迭代轮数，-1 为不限制；达上限后请求模型总结收尾 |
| `[limits] command_timeout` | 60 | `run_command` 默认超时（秒） |
| `[limits] command_timeout_max` | 300 | `run_command` 超时参数上限（秒） |
| `[limits] max_tool_output` | 20000 | 单次工具输出进入上下文的最大字符数 |
| `[limits] max_retries` | 3 | LLM 瞬时错误自动重试次数 |
| `[limits] llm_timeout` | 120 | 单次 LLM 请求超时（秒） |
| `[limits] goal_max_turns` | -1 | 持久目标回合预算，-1 为不限；正整数达上限后收尾 |
| `[limits] max_tool_concurrency` | 5 | 一轮内可并行工具的最大并发数 |
| `[sessions] enabled` | true | 会话自动保存总开关 |
| `[sessions] cleanup_days` | 30 | 转录保留天数；0 = 不自动清理 |
| `[sessions] persist_state` | true | 是否随会话持久化 goal / plan / 技能激活集 |
| `[sessions] list_limit` | 20 | `/sessions` 默认展示条数 |
| `[sessions] auto_title` | true | 首轮结束后自动生成会话标题 |
| `[sessions] title_model` | 空 | 标题专用模型（空 = 当前模型，建议配廉价快模型） |
| `[sessions] title_max_chars` | 60 | 标题长度上限 |
| `[skills] enabled` | true | 技能子系统总开关 |
| `[skills] paths` | 空 | 追加的技能扫描目录 |
| `[skills] project` | `ask` | 项目级技能信任策略：`ask` / `on` / `off` |
| `[skills] max_catalog_chars` | 8000 | 技能目录注入系统提示词的字符预算 |
| `[skills] disabled` | 空 | 按通配符禁用技能 |
| `[subagents] enabled` | true | 子代理子系统总开关（关闭即隐藏 `task` 工具） |
| `[subagents] max_concurrency` | 3 | 同时运行的子代理上限 |
| `[subagents] max_turns` | 25 | 单个子代理默认迭代上限；0 = 继承主 Agent |
| `[subagents] timeout` | 0 | 单个子代理墙钟超时（秒）；0 = 不限 |
| `[subagents] parallel` | true | 只读子代理进并行波次；false 时全部串行 |
| `[subagents] display` | `summary` | 子代理展示粒度：`summary` 只显示工具摘要与报告，`detail` 连流式一起显示 |
| `[subagents] allow_mcp` | false | 子代理白名单是否允许 `mcp__` 工具 |
| `[subagents] paths` | 空 | 追加的子代理定义目录（最高优先级） |
| `[subagents] disabled` | 空 | 按通配符禁用子代理类型（内置亦可） |
| `tool_display` | `summary` | 工具调用终端展示粒度：`summary` 只显示短摘要，`detail` 追加结果内容 |

配置优先级：**代码内置默认 < `~/.smithcode/config.toml` < 环境变量（`SMITHCODE_KEY` / `SMITHCODE_MODEL` / `SMITHCODE_URL`）< CLI 参数**。`SMITHCODE_HOME` 可覆盖配置根目录。

### 典型用法

```bash
# 修 bug：在当前项目里描述现象即可
smith 运行 pytest 里有 3 个失败，帮我修掉

# 跨项目操作：主工作区之外再授权一个目录
smith --add ../frontend 重构前端里所有调 /api/v1 的地方，改成 /api/v2

# CI / 脚本中无人值守运行并恢复最近会话
smith -c -y 跑一遍测试并总结失败原因
```

## 安全说明

- API Key 只存在两处：本机的 `~/.smithcode/credentials.json`，或环境变量 `SMITHCODE_KEY`；`config.toml` 不含秘密、可安全分享
- 所有需确认的操作在非交互环境（管道 / CI）下一律拒绝，不挂起、不崩溃
- `-y` / `Auto` 档跳过所有 `ask` 确认（含工作区外路径访问），但显式 `deny` 规则依然生效——仅在信任任务时使用
- shell 命令默认 60 秒超时；LLM 请求 120 秒超时，限流 / 断网自动重试
- 单次工具输出超长时自动头尾截断，防止撑爆上下文
- 技能根目录只读；技能的 `allowed-tools` 声明不产生任何授权效果
- 子代理在独立会话中运行且不能再派子代理；共享权限引擎，写 / 命令照常确认，非交互环境同样拒绝

## 开发

```bash
pip install -e ".[dev]"   # 安装测试与 lint 工具
pytest                    # 运行测试（不依赖真实 API）
ruff check src tests      # 代码检查
```

项目用 [uv](https://docs.astral.sh/uv/) 管理（有 `uv.lock`），也可用 `uv run pytest` / `uv run ruff check src tests`。

## 文档

- [架构说明](docs/architecture.md)：模块划分、Agent 循环、安全边界设计、如何新增一个工具
- [更新日志](CHANGELOG.md)
