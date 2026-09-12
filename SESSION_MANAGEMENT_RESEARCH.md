# 会话管理调研与改进计划

本文分两部分：先梳理成熟 agent（Claude Code / Codex CLI / opencode / Gemini CLI / Aider）
的会话管理架构与本地持久化实现，再对照 SmithCode 现有 session 能力给出分阶段改进计划。

> 现状一句话：SmithCode 的 `Session` 只做**内存消息历史**，`save()` 是**手动全量 dump 一段
> JSON**，`load()` 是**无人调用的死代码**——没有自动落盘、没有 resume 入口、没有元数据。

---

## 一、成熟 agent 的会话管理架构

### 1.1 核心范式：会话 = 持续追加的转录文件（append-only transcript）

所有成熟实现都不把会话当成"退出时导出"，而是**每产生一条消息就追写到本地文件**：

| 产品 | 存储位置 | 介质 / 格式 |
| ---- | ---- | ---- |
| Claude Code | `~/.claude/projects/<project>/<session-id>.jsonl` | 文件：JSONL，每行一个消息 / tool_use / 元数据项 |
| Codex CLI | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` | 文件：JSONL rollout（真相源）**+ SQLite 元数据镜像**（`codex-rs/state`：从 JSONL 抽取线程/项目/目标元数据镜像进本地 SQLite，用于列举与索引） |
| opencode | 用户数据目录的 SQLite 库（`opencode db path`） | **DB：SQLite**（drizzle 定义 `Session/Message/Part/Todo` 表；旧的按 key 落 JSON 的 `storage.ts` 只剩迁移用途） |
| Crush（Charm） | 数据目录下 `crush.db` | **DB：SQLite**（WAL + goose 迁移） |
| Gemini CLI | `~/.gemini/tmp/<projectHash>/chats/` | 文件：追加式 JSONL 记录（含 `$rewindTo` 回退、`$set` 元数据更新），**不是 DB**；影子 git（`~/.gemini/history`）属 checkpoint 功能，非会话存储 |
| Aider | 仓库内 `.aider.chat.history.md` / `.aider.input.history` | 文件：Markdown / 文本追加 |

**关于"是否用数据库"的印象纠正**：并非只有 opencode 用 DB——**Crush 整个会话存 SQLite**，
**Codex 是 JSONL（真相源）+ SQLite（元数据索引）的混合**，opencode 也已把会话表迁进 SQLite；
而 **Claude Code / Gemini CLI / Aider 仍是纯文件**（JSONL / Markdown）。主流里**文件追加式
（JSONL）是默认选择**，DB 多出现在"要做列举 / 搜索 / 跨会话统计"时，或干脆作为唯一存储。

要点与收益：

- **append-only**：写一条消息是一次追加，不需要重写整个文件；进程崩溃 / 被 kill 也能恢复到
  最后一条已落盘的消息（Claude Code 明确说"会话在你工作时持续保存到本地转录文件"）。
- **JSONL / 行式结构**：天然支持增量写与流式读，损坏容忍度高于单个大 JSON。
- **一个会话一个文件，文件名含会话 id**：便于列举、按 id 直连、单独删除。

### 1.2 会话与"项目 / 工作目录"绑定

- Claude Code：`<project>` = 工作目录路径做非法字符替换（如 `/` → `-`），超 200 字符截断
  并附全路径 hash。**picker 与 `--continue` 默认只看当前项目**，跨项目需显式（`Ctrl+A`）。
- Codex / Gemini：同样按项目（cwd）分区存放。
- 意义：避免不同项目的会话互相淹没；恢复时能按 cwd 定位。

### 1.3 入口设计（三层）

| 入口 | 语义 | 示例 |
| ---- | ---- | ---- |
| `--continue` / `-c` | 恢复**当前目录最近一次**会话 | `claude -c`、`opencode -c` |
| `--resume [<id\|name\|path>]` | 无参弹 **picker**；带参**直连** | `claude --resume auth-refactor`、`opencode -s <id>` |
| 会话内 `/resume` | 运行中**切换**到另一个会话 | Claude Code `/resume` |

- `--resume <id>` 在 Claude Code 里支持**跨项目搜索**（当前项目 + worktree 优先，再全机；
  仅当唯一命中才解析，避免名字撞车恢复错会话）。
- Claude Code 会把 `-p`/SDK 创建的会话**排除在 picker 之外**（否则自动化任务会污染人类
  会话列表）——这是个值得借鉴的"会话可见性"过滤。

### 1.4 恢复的**内容边界**（最关键的设计，直接决定实现）

Claude Code 的文档把"恢复什么 / 不恢复什么"讲得最清楚，值得照抄这份清单：

**会恢复：**
- 完整对话历史（含 tool calls 与结果）；**进程结束时仍在跑的工具不会重跑**，直接跳过其输出。
- 使用的 **model**（除非被 CLI flag/环境变量覆盖，或模型已下线）。
- **agent 身份**（自定义 agent 的工具限制与模型）。
- **权限模式**（终端路径下恢复；但 picker 选中 / 会话内 `/resume` 路径**不恢复**，见下）。
- **active goal**：目标仍在活跃则延续；但**回合计数、计时器、token 基线都重置**。
- 未过期的 scheduled tasks（后台 Bash / monitor 任务不恢复）。

**不恢复：**
- 进程内临时状态（在途工具、后台进程）。
- 启动期 CLI flags（`--mcp-config` / `--settings` / `--add-dir` 等需重新传）。
- 权限的"allow for this session"授权：**fork 到新进程时不带过去**，要重新批准。

> 结论：恢复不是"把整个进程状态存盘"，而是**明确列出哪些会话级状态跨进程存活**，
> 其余一律从干净默认值起步。设计 SmithCode 时必须先定这张表（见 §3.3）。

### 1.5 元数据、命名与 picker

- Claude Code 每个会话有：`session-id`（uuid）、**用户命名**（`--name` / `/rename`）、
  **自动生成标题**（后台用小/快模型对首轮 prompt 做摘要）、对话摘要、**距上次活动时间**、
  **git 分支**、**文件大小**。picker 每行展示这些，支持按分支 / 全项目 / 全 worktree 过滤。
- 自动标题与对话摘要是**独立的后台请求**产出，不占用主对话上下文。

### 1.6 分支（branch / fork）

- 会话内 `/branch [名称]`：**复制当前转录**到新 session id 并切换过去写，原会话不变。
- CLI：`--continue --fork-session`。
- 用途：同一上下文试不同方案而不破坏原路径。分支是否继承"本次会话授权"取决于**是否同进程**
  （同进程继承，fork 新进程不继承）。
- 副作用提醒：同一会话在两个终端 resume 且不 fork，消息会**交错写入同一转录**。

### 1.7 与上下文管理的关系（clear / compact / checkpoint 三者分工）

| 操作 | 对会话文件 | 对上下文 |
| ---- | ---- | ---- |
| `/clear` | **保留**旧会话（可 resume） | 开一段全新空上下文 |
| `/compact` | 仍写**同一会话** | 历史替换为摘要 |
| Gemini checkpoint | 额外存一份**文件快照**（影子 git 仓库）+ 对话 + 待执行工具调用 | 用 `/restore` 回滚文件与对话 |

- Gemini 的 checkpoint 是**会话级时间旅行**：每次"批准文件修改类工具前"自动打点，用
  `/restore` 能同时还原项目文件和对话历史。
- SmithCode 已有 `compact`（对应 `/compact`）但**没有 `/clear` 语义**——`/new` 会直接丢弃
  当前会话且不可恢复，这是要补的点。

### 1.8 导出与脚本接口

- Claude Code `/export`：人读的纯文本（消息 + 工具输出渲染为可读文本）。
- 脚本接口：`-p --output-format json/stream-json` 返回 `session_id` / `usage` / `cost`；
  hooks 与 statusline 能拿到 `transcript_path`；`claude -p --resume <id> "..."` 可对已有会话
  发追问脚本化。
- **明确警告**：转录的**内部格式随版本变化**，脚本应走 `/export` 或结构化接口，不要直接
  parser-poke JSONL。→ SmithCode 应把 `session.messages` 的**内存契约**当作稳定接口，把
  磁盘格式当作可演进实现。

### 1.9 保留、清理与隐私

- 保留：Claude Code 默认 **30 天**清理（`cleanupPeriodDays`），opencode 有 prune。
- 删除：`claude project purge`（清整个项目的转录/日志/编辑历史）、opencode `session delete`。
- 隐私：会话含工作区内容与对话，可能敏感 → opencode `export --sanitize` 脱敏；Claude Code
  转录**默认落在用户目录 `~/.claude` 而非仓库内**，且可 `--no-session-persistence` 关闭落盘。

### 1.10 相对 SmithCode 的可抄点（汇总）

1. **自动、增量落盘**（append-only），而非手动全量 dump。
2. **落用户目录 + 按项目分区**（`~/.smithcode/projects/<slug>/sessions/`），别落在仓库里。
3. **三层入口**：`-c` / `--resume [id]` / 会话内 `/resume`，带 picker。
4. **先定义恢复语义表**（恢复什么、不恢复什么），尤其是 goal / plan / 权限的取舍。
5. **元数据 + 用户命名 + 自动标题**，让 picker 可读。
6. **branch/fork** 与 **`/clear` 保留旧会话**、**保留期清理**、**导出**。
7. **稳定接口对内（messages）、可演进格式对外（磁盘 JSONL）**。

---

## 二、SmithCode 现状与差距

### 2.1 现状（代码路径）

| 位置 | 现状 |
| ---- | ---- |
| `session.py` `Session` | `messages`（内存）、`created_at`、`usage`；`add` / `sync_system` / `reset` |
| `session.py` `save()` | 手动把 `messages` 全量 dump 成 `<workspace>/sessions/<YYYYMMDD_HHMMSS>.json` |
| `session.py` `load()` | **死代码**：`src/` 内无任何调用点，也没有 `/resume` / CLI flag |
| `commands/session.py` | `/new` `/save` `/compact` `/exit`，无 `/resume` `/sessions` `/rename` `/branch` `/export` |
| `config.py` | `SESSION_ID`（uuid4，仅进程内轮换，**不与任何落盘文件关联**）；`smithcode_home()` = `~/.smithcode` |
| 会话级状态 | `goal` / `plan` / `skills` 激活集 / `permission` 会话规则 / `read-tracking`（`tools/files.py`）/ `context` 压缩计数与锚点——**全部只在内存，`/new` 即丢** |
| `.gitignore` | 已忽略 `sessions/` |

### 2.2 差距清单

| 维度 | 成熟做法 | SmithCode 现状 | 差距 |
| ---- | ---- | ---- | ---- |
| 落盘时机 | 每轮追加，崩溃可恢复 | 仅 `/save` 手动、全量 | ✗ 无自动保存，崩溃/关窗即丢 |
| 存储位置 | 用户目录 + 按项目分区 | 仓库内 `./sessions/` | ✗ 污染工作区；跨项目混在一起 |
| 格式 | JSONL 增量、行式 | 单个大 JSON | ✗ 无法增量、损坏即全丢 |
| 文件名 | 含 session id | 仅时间戳（同秒可覆盖） | ✗ 不可按 id 定位 |
| 元数据 | id/命名/自动标题/cwd/model/时间/分支/用量 | **无** | ✗ 无法列举与选择 |
| 恢复入口 | `-c` / `--resume` / `/resume` + picker | **无** | ✗ 存了也读不回来 |
| 恢复语义 | 明确定义恢复/不恢复 | **未定义** | ✗ |
| 会话切换 | `/resume` 运行中切换 | 无 | ✗ |
| 分支 | `/branch` / `--fork-session` | 无 | ✗ |
| 状态恢复 | goal / plan 等随会话恢复 | 全丢 | ✗ |
| 保留清理 | 自动过期 + 手动删除 | 无 | ✗ |
| 导出 | `/export` + 结构化接口 | 无（`/save` 只存原始 JSON） | ✗ |
| 脚本接口 | json/stream-json + session_id | 无 | ✗ |

**结论**：当前"保存会话"只是一个**原始消息数组的转存**，既不自动、也不可恢复、更不可列举，
离"会话管理"还有一整层。改进应以"**自动落盘 + 可列举 + 可恢复**"为主线。

---

## 三、改进计划

### 3.0 设计决策（建议值 + 待定项）

| 决策 | 建议 | 理由 / 备选 |
| ---- | ---- | ---- |
| 存储位置 | `~/.smithcode/projects/<slug>/sessions/<id>.jsonl` | 对齐 Claude Code；不污染仓库；跨项目统一。**备选**：保留 `<workspace>/sessions/`（向后兼容但污染仓库） |
| `<slug>` 生成 | cwd 非法字符→`-` + 末尾附 8 位路径 hash | 比 Claude Code 的定长截断更稳（长路径改名不冲突） |
| 格式 | **JSONL，append-only** | 崩溃可恢复、可增量、标准库零依赖（符合"能标准库就标准库"） |
| 会话 id | 复用 `config.SESSION_ID` 并**写进文件名与首行 meta** | 现状 id 是进程内临时值，需与落盘绑定 |
| 旧数据 | 兼容读取 `<workspace>/sessions/*.json`，可选一次性迁移 | 不破坏已有会话 |
| 依赖 | 不引入 SQLite / 新依赖 | 项目红线：交互层都尽量延迟加载，能标准库就标准库 |

**待你拍板的开放项**（影响面不同，建议先定 1、3）：

1. 存储位置：用户目录（建议）还是保留 workspace 内？
2. 是否要**自动标题**（对首轮 prompt 起一次后台小模型请求，产生额外 token/延迟）？
3. 恢复时是否**恢复 goal / plan / skills 激活集**（建议恢复 goal+plan，skills 因项目信任门控需重授权）？
4. 是否恢复**权限会话规则**（建议**不**恢复，对齐 Claude Code，安全优先）？

### 3.1 目标架构（模块与数据模型）

```
src/smithcode/
├── session.py          # Session：内存事件模型（messages + 恢复用的会话级状态句柄）
└── sessions/           # 新增：持久化子系统
    ├── __init__.py     #   公共 API：store / list_sessions / load / delete / rename ...
    ├── store.py        #   落盘读写：SessionStore（append_event / load / list / delete / rename / sweep）
    └── paths.py        #   slug 编码、projects 目录、保留期（复用 config.smithcode_home）
```

**JSONL 事件行**（每行一个 JSON 对象，首行 meta）：

```jsonc
{"t":"meta","id":"<32hex>","cwd":"F:\\proj","created":1737000000.0,"model":"...","version":"0.7.0"}
{"t":"msg","message":{"role":"user","content":"..."}}
{"t":"msg","message":{"role":"assistant","content":"","tool_calls":[...]}}
{"t":"msg","message":{"role":"tool","tool_call_id":"...","content":"..."}}
{"t":"state","goal":{...},"plan":{...},"skills":["a","b"]}   // 关键点写入，恢复时取最后一条
{"t":"usage","prompt_tokens":123,...}                        // 可选：用量增量
{"t":"title","title":"迁移模块到新 API"}                       // 命名 / 自动标题
```

> ⚠️ 上面是**概览**。逐字段规范、slug 算法、加载/压缩/恢复算法与模块接口见
> **[docs/session-architecture.md](docs/session-architecture.md)**（§3.2–§3.10）；
> 以该设计文档为准，此处不再维护细节。

**内存契约不变**：`Session.messages` 仍是 `list[dict]`（OpenAI 消息格式），磁盘格式可演进
（对齐 Claude Code 对"内部格式随版本变"的告诫）。`Session` 增加一个可选 `store` 引用，
`add()` / `messages.append` 后由 Agent 触发 `store.append`。

### 3.2 分阶段路线

> 每阶段独立可交付、可测试；Phase 1 是"存得下、找得到、续得上"的最小闭环。

#### Phase 1 —— 自动落盘 + 基础 resume（MVP）

- `sessions/` 子系统：`SessionStore`（每轮 append 消息、写首行 meta、`load` 重建 `messages`、
  `list` 列举、按 id `delete`）。
- `Session`：构造时绑定 store；`Agent` 在每轮消息入库后调用落盘（**崩溃可恢复**）。
- 存储迁移到 `~/.smithcode/projects/<slug>/sessions/<id>.jsonl`，并**兼容读取**旧
  `<workspace>/sessions/*.json`。
- CLI：`-c/--continue`（恢复当前项目最近会话）、`--resume <id>`（直连）。
- 命令：`/sessions`（列出：时间 / id 短号 / 首轮摘要 / 消息数）、`/resume <id|序号>`。
- 恢复内容：`messages`（含 system 重建）+ 沿用 session id。其余状态从默认值起步。
- **改动文件**：`session.py`、新增 `sessions/*`、`agent.py`、`cli.py`、`commands/session.py`、
  `config.py`（路径 API）、`tests/test_session.py`（+`tests/test_sessions_store.py`）、
  `docs/architecture.md`、`CHANGELOG.md`。
- **验收**：跑一轮任务 → 关进程 → `smithcode -c` 能续上且历史一致；`/sessions` 列出；
  旧格式文件仍可读；`pytest` 全绿。

#### Phase 2 —— 好找、好认（元数据 + picker）

- 元数据补全：用户命名（`/rename`）、首轮 prompt 摘要、cwd、model、创建/更新时间、
  git 分支、消息数、用量汇总。
- TUI **picker**（`/resume` 无参）与 REPL 列表；复用 `tui/panels.py` 的居中遮罩选择框。
- 可选：**自动标题**（后台小模型摘要首轮 prompt，不占主上下文）——取决于 §3.0 决策 2。
- 会话可见性过滤：一次性任务（`-p` 等价）创建的会话默认不进 picker（对齐 Claude Code）。
- **改动文件**：`sessions/store.py`（meta 聚合）、`commands/session.py`（`/rename` `/resume`
  无参 → `CommandResult(select=...)`）、`tui/app.py` + `tui/panels.py`、`llm/`（自动标题，可选）。
- **验收**：picker 可选并恢复；重命名生效；一次性任务不出现在列表。

#### Phase 3 —— 分支与会话级状态恢复

- `/branch [名称]`（会话内）与 `--fork-session`（CLI）：复制转录到新 id、切换写入源。
- 恢复语义落地（§3.3）：`goal`（回合计数/token 基线**重置**）、`plan`（todo 清单）、
  `skills`（激活集，受项目信任门控约束需重授权）。
- 会话内 `/resume`：卸载当前会话状态并加载目标（注意 goal/plan/skills 的卸载与装载对称性）。
- **改动文件**：`goal.py` / `plan.py` / `skills/state.py`（`snapshot()` / `restore()`）、
  `agent.py`（`load_session` / `unload_session`）、`sessions/store.py`（branch 复制）。
- **验收**：分支后原会话不变、新会话可续；恢复后 `/plan` `/goal` 状态与存盘一致；
  权限规则**不**被恢复（安全断言）。

#### Phase 4 —— 运维与脚本接口（可选）

- `/export [文件]`：人读文本（消息 + 工具输出）；结构化 `--output-format json` 暴露
  `session_id` / `usage`。
- 保留期清理（配置项 `[sessions] cleanup_days`，默认如 30）+ `/sessions delete <id>` +
  `--no-session-persistence`。
- 可选：`smithcode sessions list|delete|export` 子命令。
- **验收**：过期会话被清理；导出可读且可脚本解析。

### 3.3 恢复语义表（SmithCode 版，需在 Phase 1/3 落地）

| 会话级状态 | 是否恢复 | 说明 |
| ---- | ---- | ---- |
| 消息历史（含 tool calls/结果） | ✅ 是 | 核心；system 段按最新提示词重建（沿用现有 `sync_system`） |
| session id | ✅ 是（沿用） | 保证 `{$session}` 请求头跨进程稳定 |
| model | ⚠️ 记录，CLI 可覆盖 | 对齐 Claude Code |
| goal（`/goal`） | ✅ 是，**回合计数/token 基线重置** | 对齐 Claude Code active goal |
| plan（`todo_write`） | ✅ 是 | 与 goal 一致 |
| skills 激活集 | ⚠️ 恢复列表，项目级需**重新过信任门控** | 复用到已有 `skills/registry` 信任逻辑 |
| permission 会话规则（"总是允许"） | ❌ 否 | 安全优先，对齐 Claude Code 的 fork 语义 |
| read-tracking（已读文件） | ❌ 否 | 恢复后重读更安全（w/e 前强制先读） |
| context 压缩计数/锚点 | ⚠️ 重建 messages 后重算 | 摘要已在 messages 内，计数仅用于展示 |
| usage 累计 | ⚠️ 可选：展示历史会话用量 | 不并入"应用启动以来"口径 |

### 3.4 兼容与迁移

- 首次启动检测 `<workspace>/sessions/*.json`：可**惰性读取**（不强制迁移），或提供
  `smithcode sessions migrate` 一次性转入新布局。
- `config.SESSION_ID` 与落盘 id 统一（构造/`reset` 时写入 meta）。
- 保持 `sessions/` 在 `.gitignore`（防旧格式回落到仓库）。

### 3.5 风险与"不做"的事

- **安全边界不动**：落盘的是消息历史，可能含工作区内容 → 默认落**用户目录**，不落仓库；
  `.env` 等禁读内容本就进不了会话，但导出/`--sanitize` 仍应作为后续项。
- **不解析磁盘格式的稳定性承诺**：对外只承诺 `/export` 与内存 `messages` 契约。
- **非交互 fail-closed 不受影响**：picker 在非 tty 下退化为列表 + 需显式 id（沿用
  `commands` 的 `_print_select` 既有降级路径）。
- **性能**：append 是 O(1) 追加；`load` 读写整个文件（会话规模可控）。不做索引 DB，
  需要时再引入（超出现阶段）。
- **测试隔离**：`sessions/` 走 `config.smithcode_home()`，用 `SMITHCODE_HOME` 环境变量隔离
  （既有测试已用此法，见 `tests/test_session.py`）。

---

## 四、建议的落地顺序

1. **先定 §3.0 的 4 个开放项**（尤其存储位置、是否恢复 goal/plan）。
2. Phase 1（MVP）：`sessions/` 子系统 + 自动落盘 + `-c`/`--resume <id>` + `/sessions`
   —— 这一步做完，"会话管理"就有了骨架。
3. Phase 2 picker 与元数据 → Phase 3 分支与状态恢复 → Phase 4 运维/导出，按需推进。

每阶段收尾按仓库约定：跑 `pytest`、更新 `CHANGELOG.md` 的 `[未发布]` 段、同步
`docs/architecture.md`。

---

## 附录 A：存储结构实例（实测 schema）

落地时最该抄的是"结构"，这里给出各家真实结构与字段。

### A.0 两种范式

| 范式 | 代表 | 特点 |
| ---- | ---- | ---- |
| **行式事件日志（JSONL）** | Claude Code、Codex（主）、Gemini CLI、Aider | 一行一个事件，追加即写；可流式读、损坏容忍高、可人工查看；靠"控制记录"表达回退/元数据更新 |
| **关系表（SQLite）** | opencode、Crush；Codex 另有一份 | 列举/搜索/统计快；需迁移、并发串行化、级联删除 |

两家常见的**混合**做法（Codex）：JSONL 是真相源，SQLite 只做**可重建的元数据索引**。

### A.1 opencode（SQLite，drizzle schema，`core/session/sql.ts`）

表：`session` / `message` / `part` / `todo` / `session_message` / `session_input` /
`session_context_epoch`。核心是**小表存元数据、大字段存 JSON blob**：

```sql
session(
  id TEXT PK, project_id -> project.id ON DELETE CASCADE, workspace_id, parent_id,
  slug, directory, path, title, version, share_url,
  summary_additions, summary_deletions, summary_files, summary_diffs JSON,
  metadata JSON, cost REAL, tokens_input, tokens_output, tokens_reasoning,
  tokens_cache_read, tokens_cache_write,
  revert JSON, permission JSON, agent, model JSON,
  time_created, time_updated, time_compacting, time_archived
)
message(id TEXT PK, session_id -> session.id CASCADE, time_created, time_updated, data JSON)
part(id TEXT PK, message_id -> message.id CASCADE, session_id, time_created, time_updated, data JSON)
todo(session_id -> session.id CASCADE, content, status, priority, position,
     PK(session_id, position))
session_message(id, session_id CASCADE, type, seq, data JSON,
     UNIQUE(session_id, seq))                      -- 有序事件流
session_input(id, session_id CASCADE, prompt JSON, delivery, admitted_seq,
     promoted_seq, time_created)                   -- 排队中的用户输入
session_context_epoch(session_id PK, baseline, snapshot JSON, baseline_seq)
```

可借鉴的点：**会话级 token/cost 计数与 model/agent/permission/revert 直接落在 session 行**；
`session_message(session_id, seq)` 提供**有序事件流**；外键 `ON DELETE CASCADE` 让删会话干净；
索引统一以 `(session_id, time_created, id)` 为主。

### A.2 Codex CLI（JSONL 真相源 + SQLite 索引）

- **文件**：`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`，另有 `archived_sessions/`。
- **每行**（`rollout/src/lib.rs` 的 `RolloutLine`）：扁平化的一行 = `timestamp` + 可选 `ordinal`
  （序号，用于定位/分页）+ 一个 `item`：

  ```jsonc
  {"timestamp":"...","ordinal":12,"<item 字段平铺在此>"}
  ```

- **首行是 `SessionMeta`**（`read_session_meta_line`），列举/摘要只读头部（`read_head_for_summary`），
  不必读全文——**大文件的列举优化**。
- **写入**：单写者（`writer_lock.rs` 的 `WriterLockCoordinator`）；支持**压缩 rollout** +
  `materialize`（读时按需展开）。
- **SQLite（`codex-rs/state`）**：只镜像元数据——线程/项目（`Project`、`ProjectRoot`）、
  线程名（命名/按名查找）、搜索索引、`ThreadGoal`（目标）、排队用户输入、附件；损坏可重扫
  JSONL 重建。设计原则："**history remains format-neutral**"。

### A.3 Gemini CLI（JSONL，`chatRecordingTypes.ts`）

文件：`~/.gemini/tmp/<projectHash>/chats/session-<uuid>.jsonl`（**项目绑定的 hash 目录**）。记录类型：

```ts
MessageRecord = BaseMessageRecord & (
  { type: 'user'|'info'|'error'|'warning' } |
  { type: 'gemini'; toolCalls?: ToolCallRecord[]; thoughts?: [...];
    tokens?: TokensSummary|null; model?: string }
)
BaseMessageRecord = { id, timestamp, content: PartListUnion, displayContent? }
ToolCallRecord   = { id, name, args, result?, status, timestamp, agentId?,
                     displayName?, description?, resultDisplay? }
ConversationRecord = { sessionId, projectHash, startTime, lastUpdated,
                       messages[], summary?, memoryScratchpad?, directories?, kind? }
TokensSummary = { input, output, cached, thoughts?, tool?, total }
```

**控制记录**（同一文件内的"元记录"，非消息）：

```jsonc
{"$rewindTo":"<message id>"}     // 回退：截断到此消息（删除其后记录）
{"$set":{ "summary":"...", "memoryScratchpad":{...} }}   // 增量更新会话元数据
```

上限常量：`MAX_HISTORY_MESSAGES = 50`、`MAX_TOOL_OUTPUT_SIZE = 50KB`。

### A.4 Claude Code（JSONL，格式未公开）

`~/.claude/projects/<project>/<session-id>.jsonl`，**每行一个 JSON 对象**（消息 / tool_use /
元数据项）。官方明确"**内部格式随版本变化，脚本不要直接解析**"，因此这里不列字段；对外用
`/export` 或 `-p --output-format json`。可抄的是**形态**：单文件按 session id 命名、行式追加、
按项目目录分区。

### A.5 Crush（SQLite）

单库 `<data-dir>/crush.db`，`journal_mode=WAL`、`secure_delete=ON`、goose 迁移、
`SetMaxOpenConns(1)`（**串行化写**避免 WAL/header desync），有 per-data-dir 文件锁防多进程竞争。

### A.6 Aider（文本追加）

仓库内 `.aider.chat.history.md`（人读的 markdown 转录）+ `.aider.input.history`（输入历史）。

### A.7 贯穿各家的共性结构（SmithCode 应对齐）

1. **会话头**：id + 项目标识（path/hash）+ 起始时间 + model/agent/version（Codex `SessionMeta`、
   Gemini `ConversationRecord` 前几字段、opencode `session` 行）。
2. **消息记录**：id + 时间戳 + 角色/类型 + 内容 +（assistant 的）tool_calls + 每消息 token。
3. **控制记录**：回退（`$rewindTo`）、元数据更新（`$set`）、压缩/summary、命名/标题。
4. **会话级计数**：token/cost（opencode 落 session 行；Gemini 落 gemini 消息的 `tokens`）。
5. **可枚举性**：能只读头部列出（Codex）或直接查表（opencode）——**不要为列举读全文**。
6. **删除语义**：级联/整目录删除。
7. **格式中立**：真相源与索引分层（Codex 明文的"history remains format-neutral"）。

> 对 SmithCode §3.1 的 JSONL 设计建议据此微调两条：
> (a) **首行必须是 meta**（id/cwd/created/model/version），列表页只读首行 + 尾部摘要；
> (b) 追加**控制记录** `{"t":"state",...}` 与 `{"t":"title",...}`，与 Gemini 的 `$set` 同思路，
> 避免为改一个字段重写整文件。
