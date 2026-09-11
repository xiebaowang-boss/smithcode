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

1. `cli.py` 接收用户输入，交给 `Agent.run()`。
2. `Agent` 把消息列表（含系统提示词）发给 LLM。
3. 模型要么返回纯文本（任务完成，循环结束），要么返回工具调用。
4. 工具调用先经 `Permission` 确认（读文件/列目录免确认），再由 `tools/` 执行；同一批调用**两阶段执行**——预检（解析参数、渲染摘要、路径预检、权限确认）全部在主线程按接收顺序串行完成，之后可并行的只读调用进线程池并发执行（`MAX_TOOL_CONCURRENCY` 上限），声明 `serial` 的有状态工具（shell / 写文件 / 交互确认）在主线程串行、作为顺序屏障；结果一律按提交顺序回传。
5. 执行结果以 `role: tool` 消息回传给模型，进入下一轮循环。
6. 循环超过 `MAX_ITERATIONS` 次则强制终止，防止失控。

### 中断（Esc）

任务可随时被用户中断（TUI 按 Esc、REPL 按 Ctrl+C），走同一协作式取消通道：

1. **发起**：宿主层调 `Agent.interrupt()` 触发当前轮次的取消令牌（`cancel.py`），令牌经 ContextVar 沿调用链隐式传播（`run()` 全程同线程，生成器内可见）。
2. **LLM 流截停**：`llm.py` 打开流后把 `stream.close` 登记为令牌监听——取消线程**直接关流**，即使正阻塞在等下一块数据（模型静默期 / 网络慢）也立即解除；流消费每块数据前再查一次令牌。取消后不再产出 message/usage，关流引发的读错误按取消吞掉；已收到的正文由 Agent 拼成**部分消息**保留入库（残缺的工具调用不回传）。打开流之前也先查令牌——中断后**不再发起任何新请求**（含压缩摘要、溢出恢复重试）。
3. **工具批截停**：`_execute_batch` 在**每个预检项**与每个波次/串行项边界查令牌——预检阶段（含权限确认）中断时剩余确认框不再弹出、当前项即使刚答 y/n 也不执行、已确认未执行的同样跳过（中断 = 不再发起任何新工作，且优先于拒绝语义）；未执行的计划补占位结果（与权限被拒共用同一会话修复路径，`tool_call_id` 永不悬空），**非 shell** 的正在执行工具让其自然跑完（线程不可强杀）并照常收集结果。
4. **运行中命令强杀**：正在执行的 shell 命令是例外——`run_command`（serial，跑在 run 线程）经 `process.py` 执行命令，`process.run` 自读当前线程令牌并在轮询中判定取消，触发即**终止整个进程树**（Windows `taskkill /F /T`、POSIX 先 SIGTERM 宽限后 SIGKILL），无需等命令自然结束或撞超时；超时路径共用同一终止逻辑。并行 worker 读不到令牌时安全降级为不响应取消。
5. **收尾**：`run()` 返回结构化 `RunResult`（`ok / interrupted / denied / max_iterations`，`partial` 标记流中截停），令牌在 finally 中复位，下一任务不受残留状态影响；会话历史始终合法，可直接继续追问。

模型输出以流式方式逐字显示；思考内容（如 DeepSeek-R1 类模型的 `reasoning_content`）以暗色实时展示，但不写入会话——多数 OpenAI 兼容服务不接受它被回传。

工具调用在执行前打印一行短摘要（`read src/agent.py`、`command git push`，由各工具注册的 `describe` 生成），粒度由 `~/.smithcode/config.toml` 的 `tool_display` 控制：`summary`（默认）到此为止（附带展示 write/edit 的变更预览 diff），`detail` 再以 `[Result]` 追加结果内容（前 500 字符）。展示粒度只影响终端，回传给模型的内容始终是截断后的完整结果；失败信息（`错误: ...`、用户拒绝）无论粒度都原样展示。

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

## 会话与 /new

`/new` 命令开启新会话：所有会话口径状态的重置集中在 `Agent.new_session()`（`agent.py`），命令层只负责反馈与标记，不感知重置细节。覆盖项：

- `session.reset()`：消息历史清空（系统提示词随历史懒加载）、会话 id 轮换、会话用量清零（"应用启动以来"口径跨 `/new` 存活）
- `permission.new_session()`：清空会话内"总是允许"积累的规则（权限模式档位是用户手动选择，跨会话保留）
- `config.SESSION_EXTRA_ROOTS.clear()`：清空越界确认积累的信任目录
- `context.new_session()`：压缩计数清零、上一会话的真实 token 锚点作废（旧锚点对新会话的估算对比无意义）
- `reset_read_tracking()`：清空工具侧「已读文件」记录（新会话中未读过的文件重新受 write/edit 前置校验约束）
- `plan.reset()`：清空步骤清单

TUI 端的宿主动作由 `CommandResult.session_reset` 标记触发：**彻底清空聊天区**（含欢迎横幅，不追加任何提示文本——清空本身即反馈；REPL 仍打印「已开启新会话。」）、清空计划侧栏与残留的工具块映射、刷新状态栏。

**busy 守卫**（对齐 opencode）：任务运行中 `/new` 被 TUI 拦截，只提示「请等待完成或先按 Esc 中断」而不执行——后台线程仍在写消息历史，中途重置会撕裂进行中的轮次。命令执行时机被约束到 agent 空闲时，从机制上消灭竞态；REPL 为同步运行，命令天然只在空闲时执行，无需守卫。

## 模块职责

| 模块 | 职责 |
| ---- | ---- |
| `cli.py` | 参数解析、交互式 REPL、单次任务模式 |
| `commands/` | 斜杠命令框架：注册表（`@register` 装饰器）+ 统一 `dispatch()`，REPL 与 TUI 共用；命令元数据（`accepts_args` / `immediate` / `aliases`）驱动两端行为；`/help` 文案由注册表自动生成，新命令一个文件零改动接入 |
| `agent.py` | Agent 循环编排；`new_session()` 集中承担 `/new` 的全部会话级重置（消息历史、会话用量、权限会话规则、信任目录、上下文快照、已读记录、步骤清单） |
| `cancel.py` | 协作式取消原语：`CancellationToken`（幂等 cancel / 线程安全查询）、当前令牌的 ContextVar 传播、`RunResult` 结构化结束状态；Esc / Ctrl+C 中断的唯一通道 |
| `process.py` | 外部命令执行的唯一出口：`Popen` 创建、轮询超时、取消判定与跨平台进程树终止（Windows `taskkill /T`、POSIX `killpg` 信号升级）、`ProcessResult` 结构化结果，取消令牌取自当前线程；工具层只负责组装命令与文案映射 |
| `llm/` | 模型交互子系统：`client.py` OpenAI 兼容接口封装（流式、自动重试、自定义请求头注入、`/models` 拉取）、`models.py` 候选模型目录 `ModelCatalog`（`ModelSource` 三级组合，线程安全；启动同步装载、未配置后台刷新回写缓存）、`usage.py` token 用量、`prompts.py` 系统提示词（行为规则）；`__init__.py` 汇总公共 API |
| `session.py` | 消息历史的增删存取与系统提示词装配 |
| `plan.py` | 任务拆分与分步骤执行：`todo_write` / `todo_read` 维护的会话级步骤清单（id 分配、标题不可变、状态机 + 全量/仅标题两种渲染 + `/plan` 查看） |
| `context/` | 上下文计量与运行时压缩包：`meter` 计量（token 估算、`/context` 报告）、`compact` 压缩纯逻辑、`prompts` 压缩提示词 |
| `permission/` | 权限子系统：`engine.py` 规则引擎与确认流程（原 `permission.py`）、`shell_policy.py` Shell 命令静态分析（只读判定 + 前缀推导，命令规范表 `COMMANDS`）；`__init__.py` 汇总公共 API |
| `config.py` | 配置中心：`~/.smithcode/config.toml`（行为配置，含 `[provider.headers]` 自定义请求头与 `[provider].models` 候选模型列表）+ `credentials.json`（凭据），默认 < TOML < 环境变量（仅 `SMITHCODE_KEY/MODEL/URL`）三级解析 |
| `tools/base.py` | 工具注册表（`@register` 装饰器，支持 `pattern_arg` / `family` / `paths_from` / `describe` / `preview` / `serial`） |
| `tools/files.py` | 文件读写，含路径越界检查 |
| `tools/search.py` | 文件名与内容检索（glob / grep） |
| `tools/shell.py` | 命令执行，含超时保护 |
| `tools/patch.py` | apply_patch 批量原子改文件 |
| `tools/ask.py` | ask_user 任务中途向用户提问 |
| `tools/todo.py` | todo_write / todo_read 任务拆分与分步骤执行的状态机与只读快照 |
| `tui/` | Textual 全屏聊天界面（仅交互终端加载）：`app.py` 组装层（`SmithTUI` 布局接线 + 集中 CSS）、`widgets.py` 自包含控件（消息区/折叠块/侧边栏/命令菜单/输入框 + `UiAction` 消息）、`bridge.py` 线程桥（`TuiRenderer`，worker 线程经 `post_message` 投递 UI 事件）、`panels.py` 弹窗面板（权限/提问/通用选择）、`render.py` 纯函数工具（markdown 渲染、git 分支、token 缩写） |

## 安全边界

- **路径沙箱**：所有文件操作经 `_resolve()` 检查，用 `Path.is_relative_to` 确认解析后的真实路径位于工作区内（目录名共享前缀的兄弟路径不会被误判为放行）。
- **权限规则引擎**：三级动作 `allow / ask / deny`，规则 = (工具名, 参数模式, 动作)，通配符匹配，最后一条匹配的规则生效，无匹配默认 `ask`。规则三层叠加：内置默认 < `~/.smithcode/config.toml` 用户规则 < 会话内"总是允许"（命令记 argv 前缀，其余工具按模式串）。匹配在 Windows 下大小写不敏感（对齐 opencode v2）。
- **保护路径**：内置默认规则将 `.git` 目录设为只读（禁止写入与编辑），读取放行。
- **变更预览**：`write_file` / `edit_file` 在**执行前**（路径预检与权限确认之前）把 unified diff 推送到**工具调用块**——pending 态就地展开，审核时改动内容已可见，权限申请框保持纯净；执行后 diff 保留在调用详情里回看（超 40 行截断，失败/被拒不重复展示），写/编辑工具的调用详情**默认展开**。`.env` 等禁读文件不生成预览避免密钥回显。其他工具可在注册时声明 `preview` 函数接入同一机制。
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
6. **审核前展示变更**：工具的变更预览（如 `write_file` / `edit_file` 的 unified diff）在权限确认与执行**之前**推送到工具调用块（pending 态就地展开）——审核时改动内容已可见，权限申请框只负责 y/n/a 决策。
7. **复合命令拆分求值**：`run_command` 的命令串按顶层操作符（`&&` / `||` / `;` / `|` / `&` / 换行）切分为子命令逐段匹配规则（引号内不切），聚合语义与多路径一致——任一段 `deny` → 拒绝，任一段 `ask` → 询问，全部放行才放行；含 `$()` 或反引号命令替换（双引号内仍算，单引号内不算）无法静态求值，强制 `ask`。防止"放行 A 后借 `&&` 偷渡 B"绕过规则。
8. **安全只读命令免确认**：内置一批只读命令（`ls` / `cat` / `git status` 等，POSIX 与 cmd.exe 各一套安全集），在内置默认 `ask` 下自动放行以减少确认疲劳。安全层位于「内置默认规则」与「用户规则」之间——**任何用户/会话规则命中都优先**（无论 `ask` 还是 `deny`），因此可用精确 `ask`（如 `git push *`）在保留安全集的同时收紧个别命令，也可用宽泛 `ask` 整体关闭。判定是独立纯函数 `permission/shell_policy.is_safe_command`（按顶层段逐段求值，复用命令拆分），拿不准即回退 `ask`：解析失败、inline 环境变量前缀（`CI=true git commit`）、路径限定的 argv[0]（`./sed`、`/usr/bin/ls`）、写文件/读文件重定向（仅放行 `/dev/null`、`NUL`、`2>&1`）、命令替换、危险标志（`find -delete` / `sort -o` / `git -c` 等）、对有写/exec 能力命令的未加引号 glob、网络与 `env`/`awk`/`docker`/`sed` 等命令一律不纳入。开发工具链仅放行**版本查询 / 只读枚举 / 静态检查**（`python --version`、`pip list` / `show` / `freeze`、`npm ls`、`uv pip list`、`poetry show`、`ruff check` 等）；**真正运行代码的用法一律不放行**（`pytest`、`python x.py`、`node -e`、`npm run`、`uv run`、`cargo test` 等）——没有 OS 沙箱时默认放行任意执行等于放弃安全边界。`cd` 目标须落在授权目录内，同一复合命令里 `cd` 改变目录 + `git` 组合整体降级为询问（git 会执行新目录 hooks）。
9. **"总是允许"命令前缀记忆**：`run_command` 选"总是允许"时不再记整条命令字符串，而是记 **argv 前缀**：`permission/shell_policy.derive_prefix` 把命令归一化成稳定 key（剥 inline 环境变量前缀与 `env`/`command` 包装器；解释器保留 `-m 模块` / 脚本名；其余取从头连续的非标志 token），再按命令规范表的 `arity` / `sub_arity` 切出前缀（`python -m pytest tests/a.py` → `("python","-m","pytest")`、`git commit -m x` → `("git","commit")`、`npm run test` → `("npm","run","test")`）。匹配时对段重新计算 `command_key` 做 token 前缀比较，因此文件/参数变化（`tests/b.py`、`-k foo`、`CI=1` 前缀）都能命中。**拿不准即精确**：未登记命令、标志截断 arity（`git --no-pager log`）、inline 解释器（`python -c`）、shell（`bash -c`）、含危险标志（`ruff check --fix`）一律退回整段精确记忆；`BANNED_PREFIXES`（`uv run` / `python` / `npm run` 裸前缀等）作为最后安全网。复合命令逐段各自记忆；`cd` 改变目录 + `git` 守卫场景不提供"总是允许"。配置文件的字符串规则仍按通配匹配，行为不变。

## 如何新增一个工具

在 `tools/` 下新建文件，用 `@register` 声明 schema 即可，`tools/__init__.py` 无需修改：

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

终端展示的短摘要用 `describe` 声明，签名 `(args) -> str`，格式为「短名 + 目标」（如 `read src/a.py`、`command git status`）；未声明时回退为 `[Tool] 名字(参数)` 格式：

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
