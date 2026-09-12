# 会话管理架构设计

> 状态：**设计稿（未实施）**。调研原始材料见仓库根目录 `SESSION_MANAGEMENT_RESEARCH.md`；
> 本文是其落地版：给出架构、模块接口、磁盘格式 v1、恢复语义、集成改动与分阶段路线。
> 文中代码坐标以当前工作区为准。

---

## 1. 调研：成熟 agent 的会话管理怎么做

### 1.1 三家主线的实现对比

| 维度 | Claude Code | Codex CLI | opencode（V2） |
| ---- | ---- | ---- | ---- |
| 真相源 | 文件：`~/.claude/projects/<sanitized-cwd>/<session-id>.jsonl`，append-only | 文件：`~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<id>.jsonl`，append-only；**SQLite 只做元数据索引**（可重扫重建） | SQLite（`~/.local/share/opencode/opencode.db`），`message`/`part`/`session_message(seq)` 表 |
| 写路径 | 批量队列 + 100ms drain，逐条 append JSONL | 单写者（writer lock），事件经 RolloutCmd 缓冲/落盘/刷盘 | 每步事务写入（message + part） |
| 消息模型 | 行式「记录」+ `parentUuid` 链（树） | `SessionMeta` + 平铺事件项（`response_item` / `event_msg` / …） | message + part（text/reasoning/tool-call/…），V2 另有带 `seq` 的有序事件流 |
| 压缩 | `/compact` 摘要作为 `summary` 记录；新链 `parentUuid=null` 表达边界；**旧记录保留在磁盘** | `Compacted` 摘要项；保留原始 rollout | 生成 checkpoint：结构化摘要 + token 受限的近期上下文；`session_context_epoch` 记录边界；**durable 历史不删** |
| 恢复 | `-c` / `--resume [picker]` / 会话内 `/resume`；链回溯重建；支持从摘要恢复 | `codex resume [--last] [id] [--all]`；picker；`codex exec resume`（headless） | `/sessions` picker、`-c`、`--session <id>`；V2 从最近 checkpoint + 其后消息装配请求 |
| 分支 | `/branch`、`--fork-session`：复制转录、新 id，原会话不动 | `codex fork`：新 ThreadId + `forked_from_id`，复制到 fork 点 | `/fork`：深拷贝全部消息；子会话 `parent_id` |
| 会话级状态 | 恢复：历史、model、agent、active goal（**回合/计时/token 基线重置**）、权限模式；**不恢复**：临时状态、CLI flags、"仅本次会话"授权 | 恢复：转录、计划历史、**审批历史**（本地 CLI 语义） | 恢复：消息、todo、compaction 边界；权限独立成表 |
| 元数据 / 列举 | `session-id` + 用户命名 + 自动标题 + git 分支 + 大小 + 时间；picker 只读头/索引 | `SessionMeta` 首行；列举只读头部；线程名/项目索引在 SQLite | session 行带 title/cost/tokens/model/agent/permission；直接查表 |
| 隐私 / 保留 | 默认落用户目录；`--no-session-persistence`；30 天清理；`project purge` | 归档 + `.zst` 冷压缩 | `export --sanitize`；prune |

其他参考：Gemini CLI 用追加式 JSONL + 控制记录（`$rewindTo` 回退、`$set` 元数据更新）；
Crush 整体 SQLite（单连接串行写）；Aider 用仓库内 Markdown 追加。

### 1.2 三条被验证的结论

1. **append-only 转录（JSONL）是主流默认**：写一条是一行追加、崩溃可恢复到末条完整记录、
   行级损坏容忍、可人工查看；SQLite 只在"要列举/搜索/统计"的规模压力下才值得引入，
   且通常做**可重建的索引**而非唯一真相源（Codex 的明确定位："history remains format-neutral"）。
2. **durable 历史与"模型可见上下文"分离**：压缩（compact）是给模型看的**有损检查点**，
   不是物理删除。checkpoint 之后的请求从「最近检查点 + 其后消息」装配；完整历史仍可导出/审计。
3. **恢复语义必须显式定义**：恢复的不是"整个进程状态"，而是一张明确的清单——什么跨进程
   存活、什么从零开始（尤其权限授权、临时状态、回合计数）。

### 1.3 对 SmithCode 的启示

- 选型：**JSONL append-only 单文件 + 项目分区**，不引入数据库（符合"能标准库就标准库"）。
- 压缩改造为**自包含 checkpoint 记录**，与现有 `compact()` 的"system + 摘要 + 尾部"结构同构。
- 恢复必须包含**崩溃修复**：进程可能在 `assistant(tool_calls)` 落盘后、`tool` 结果落盘前被杀，
  直接回放会构造出服务商拒绝的非法消息序列（悬空 `tool_call_id`）。
- 先写死**恢复语义表**（§3.9），权限会话规则一律不恢复。
- 内存契约 `Session.messages: list[dict]`（OpenAI 格式）保持稳定，磁盘格式才允许演进。

---

## 2. 现状与约束

### 2.1 现有实现（代码坐标）

| 位置 | 现状 |
| ---- | ---- |
| `session.py:10` | `Session` = 内存 `messages` + `created_at` + `usage`；`add` / `sync_system` / `reset` |
| `session.py:47` | `save()` 手动全量 dump `<workspace>/sessions/<时间戳>.json`（同秒可覆盖） |
| `session.py:57` | `load()` 死代码（无调用点、无入口） |
| `agent.py:260,351,642` | user / assistant / tool 消息直接 `append` 进 `session.messages`（多个写入点） |
| `agent.py:428` | 压缩整体替换列表：`self.session.messages = assemble(...)` |
| `agent.py:226` | `new_session()` 集中重置全部会话口径状态（goal/plan/skills/permission/…） |
| `config.py:32,140` | `smithcode_home()`；`SESSION_ID` 仅进程内轮换，与落盘无关 |
| `commands/base.py:24` | `CommandSelect` 已支持"命令返回选择意图，宿主弹选择器" |
| `tui/app.py:447` | `show_selection()` 通用选择弹窗（`/skills`、`/model` 在用） |

### 2.2 必须保持的约束

- **内存契约**：`Session.messages` 仍是 `list[dict]`（OpenAI 消息格式）；system 段由
  `sync_system()` 按最新提示词重建，不依赖落盘内容。
- **前缀缓存**：普通回合 `messages[0]` 逐字节稳定；落盘不得引入会影响该段的副作用。
- **历史合法性不变量**：每个 `tool_call_id` 必有配对 `tool` 结果（`agent.py:123` 起的不变量表）。
- **fail-closed 只针对权限**：会话持久化相反——任何读写失败都必须降级为纯内存会话，
  绝不阻断 agent（§3.11）。
- **兼容红线**：Python 3.9、零新依赖、交互层延迟加载、`SMITHCODE_HOME` 测试隔离可用。

---

## 3. 目标架构

### 3.0 设计原则

1. **转录是事实，上下文是投影**：磁盘保留完整事件；发给模型的消息由「system 重建 +
   最近一次 compact 检查点 + 其后消息」投影得到。
2. **一个写入咽喉**：所有消息追加收敛到 `MessageLog` 钩子，`Agent` 各追加点零改动。
3. **懒物化**：没有真实消息就不建文件（启动、只看 `/help` 不产生噪声文件）。
4. **持久化是尽力而为**：失败降级、损坏行容忍，永不阻断任务。
5. **对内稳定、对外可演进**：`messages` 是稳定接口；JSONL 记录带 `v` 版本，未知记录忽略。

### 3.1 总体结构

```
                        ┌────────────────────────── src/smithcode/sessions/ ──────────────────────────┐
                        │  paths.py   项目 slug、目录布局、文件权限                                  │
用户输入 / 工具结果       │  format.py  记录编解码（v1 schema、容错解析、checkpoint 构造）              │
        │               │  store.py   SessionStore（单会话追加写）+ 项目级查询 list/find/delete/branch │
        ▼               │  __init__.py 公共 API                                                     │
┌───────────────┐  append│                                                                           │
│ Session       │───────▶│  <home>/projects/<slug>/sessions/<session-id>.jsonl                       │
│ MessageLog ───┘        └───────────────────────────────────────────────────────────────────────────┘
│  messages(投影) │                                    ▲
└───────┬────────┘                                    │ load
        │ sync_system / run                           │
┌───────▼────────┐   resume  ┌──────────────┐   ┌─────┴──────────┐
│ Agent           │◀─────────│ CLI / 命令层  │   │ TUI picker     │
│ run / compact   │  report  │ -c / --resume│   │ /resume 选择框  │
└────────────────┘          └──────────────┘   └────────────────┘
```

数据流一句话：**消息从 `Session` 单向追加到转录；恢复时从转录反向装配 `Session` 并重建状态**。

### 3.2 存储布局与路径

```
<smithcode_home()>/projects/<slug>/
└── sessions/
    ├── <session-id>.jsonl        # 一个会话一个文件，append-only
    └── <session-id>.jsonl.lock   # 可选（Phase 4 并发锁，见 §3.11）
```

- `slug` 生成（`paths.py`）：cwd 规范化 → 非法字符（非 `[A-Za-z0-9-]`）替换为 `-` → 截断 48 字符
  → 追加 `-` + `sha1(cwd)[:8]`。确定性、可读、长路径不冲突（Windows 盘符/中文路径均安全）。
- 会话 id 复用 32 位 hex（`uuid.uuid4().hex`）；与 `config.SESSION_ID` **统一为一个值**：
  - 新建：`store.create()` 使用 `config.new_session_id()`；
  - 恢复：新增 `config.use_session_id(id)`，让 `[provider.headers]` 的 `{$session}` 占位符
    跨进程稳定（对齐服务商会话路由 / 提示缓存的预期）。
- 权限：POSIX 下目录 `0o700`、文件 `0o600`（`os.open`/`os.chmod` best-effort，Windows 忽略）。
- 旧数据不迁移也能用：`<workspace>/sessions/*.json` 保留读入口（§3.13）。

### 3.3 转录格式 v1（JSONL 记录规范）

每行一个 JSON 对象，恒有 `v`（格式版本）与 `t`（记录类型）。**只持久化非 system 消息**，
system 每次由 `sync_system()` 重建。

```jsonc
// ① meta：必须是首行；懒物化（首次真实写入时与第一条消息同批写入）
{"v":1,"t":"meta","id":"8f14e45f...","cwd":"F:\\Dev\\Agentic\\smithcode",
 "created":1757582000.12,"model":"deepseek-v4-flash","effort":"high","app":"0.8.0","oneshot":false}

// ② msg：消息（不含 system）；assistant 的 tool_calls / tool 的 tool_call_id 原样保留
{"v":1,"t":"msg","m":{"role":"user","content":"帮我重构 session.py"}}
{"v":1,"t":"msg","m":{"role":"assistant","content":"","tool_calls":[{"id":"call_1","type":"function",
   "function":{"name":"read_file","arguments":"{\"path\":\"src/smithcode/session.py\"}"}}]}}
{"v":1,"t":"msg","m":{"role":"tool","tool_call_id":"call_1","content":"..."}}

// ③ compact：压缩检查点。语义：从本行起，活动上下文 = summary + tail（自包含、原子）
{"v":1,"t":"compact","summary":{"role":"user","content":"<context-summary>\n…\n</context-summary>"},
 "tail":[{...后续保留的消息...}],"before":51234,"after":17320,"ts":1757582300.5}

// ④ state：会话级状态快照（取最后一条；[sessions].persist_state=false 时不写）
{"v":1,"t":"state","goal":{"status":"active","objective":"…","max_turns":50,"evidence":"", ...}|null,
 "plan":{"items":[{...}]},"skills":["pdf","excel"],"ts":1757582400.0}

// ⑤ title：命名（取最后一条；source=user 为用户命名，auto 为自动摘要）
{"v":1,"t":"title","title":"session 持久化改造","source":"user","ts":1757582500.0}

// ⑥ branch：血缘（fork 时写入新文件，紧邻 meta 之后）
{"v":1,"t":"branch","from":"<父会话 id>","ts":1757582600.0}

// ⑦ usage：可选，一次模型调用的用量增量（列表页/token 统计用，不参与上下文装配）
{"v":1,"t":"usage","prompt_tokens":1234,"completion_tokens":567,"total_tokens":1801,
 "model":"deepseek-v4-flash","ts":1757582700.0}
```

解析容错（`format.py` 纯函数，全部可单测）：

| 情况 | 行为 |
| ---- | ---- |
| 空行 | 跳过 |
| 中间行 JSON 解析失败 | 计数警告并跳过（文件仍可用） |
| **末行**解析失败 | 视为崩溃残行，静默忽略（预期内） |
| 未知 `t` / 未知字段 | 忽略（前向兼容） |
| `v` 高于当前支持 | 尽力读取，提示"文件由更新版本写入，部分内容可能被忽略" |
| `msg.m.role == "system"` | 防御性忽略（旧格式/手改） |
| `compact` 缺 `tail` | 退化为仅 `summary` |
| 首行缺失/损坏 | 从文件名与 mtime 合成 meta，会话仍可恢复（降级标注） |

### 3.4 写路径

**核心：`MessageLog(list)` + 单一钩子**，避免在 `agent.py` 的 5 处追加点逐个埋落盘调用
（漏一处就是"恢复后历史缺一段"的隐蔽 bug）：

```python
class MessageLog(list):
    """带落盘钩子的消息列表：append 即追加转录。"""
    def __init__(self, items=(), on_append=None): ...
    def append(self, msg): super().append(msg); self._on_append and self._on_append(msg)

class Session:
    def bind_store(self, store): self._store = store; self._messages._on_append = ...
    @property
    def messages(self): return self._messages
    @messages.setter                      # 赋值也重新包裹，钩子永不丢失（安全网）
    def messages(self, value): self._messages = MessageLog(value, self._on_append)

    def set_compacted(self, summary, tail):   # 压缩专用显式入口（替掉 agent.py:428 的直接赋值）
        self._messages = MessageLog([summary] + list(tail), self._on_append)
        self._store and self._store.append_compaction(summary, tail, before=..., after=...)
```

- `Session._on_append(msg)`：`role == "system"` 直接跳过；其余 `store.append_message(msg)`。
  因此 `session.add()`、`agent.py:351/628/642` 的 `messages.append`、占位结果全部自动落盘。
- `sync_system()` 的 `insert(0)` 与首段内容刷新不落盘（system 不入转录）。
- `reset()` 轮换 id 并切换 store（旧文件关闭、新文件懒物化）。
- 每条记录一次 `write` + `flush`（崩溃最多丢最后一条不完整行）；文件句柄在会话期持有，
  `close()` 于 `/new`、切换会话、进程退出时调用。
- `compact()` 改造：`self.session.set_compacted(summary, tail)`（一处改动，测试同步）。

### 3.5 读路径

**项目级查询（`store.py` 模块函数）**：

```python
@dataclass
class SessionSummary:
    id: str; path: Path; cwd: str; created: float; updated: float   # updated = mtime
    model: str; title: str; first_prompt: str; oneshot: bool; size: int

def list_sessions(cwd=None, limit=20, include_oneshot=False) -> list[SessionSummary]
def find_last(cwd=None) -> SessionSummary | None            # -c / --continue
def find(session_id_prefix, cwd=None, all_projects=False)   # --resume <id>（前缀唯一才命中）
def load(summary) -> LoadedSession                          # (messages, meta, state, title)
def delete(session_id) -> bool
def rename(session_id, title) -> None                       # 追加 title 记录
def branch(session_id, title=None) -> SessionSummary        # 复制转录 + 新 id + branch 记录
def import_json(path) -> SessionSummary                     # 旧格式（§3.13）
def sweep(cleanup_days) -> int                              # 保留期清理（§3.12）
```

- **列举不读全文**：只读文件头（≤8KB 拿 meta + 首条 user 消息做摘要）与文件尾（≤8KB 找最后
  一条 `title`），配合 `size/mtime`——对齐 Codex `read_head_for_summary` 的做法。
- **加载为单遍流式**：

```
messages, meta, state, title = [], {}, {}, ""
for rec in parse_lines(path):            # 容错见 §3.3
    meta  = rec                          # t=meta
    msg   → messages.append(rec.m)       # t=msg
    compact → messages = [rec.summary] + rec.tail      # t=compact：投影重置
    state → state = rec                  # t=state：保留最后一条
    title → title = rec.title            # t=title
repair_dangling_tool_calls(messages)     # §3.6：崩溃修复
# 交给 Agent：strip system（防御）→ sync_system() 重建 → Session(messages=…)
```

### 3.6 崩溃修复（恢复正确性的关键）

进程可能在任意写入点被杀。转录合法性问题只有一种：**尾部悬空 `tool_calls`**
（assistant 已落盘、结果未落盘）。恢复时扫描并补占位，内容与中断路径同款：

```
已执行 id 集合 = {m.tool_call_id | m.role == "tool"}
for tc in 尾部 assistant 消息的 tool_calls:            # 按出现顺序
    if tc.id not in 已执行 id 集合:
        messages.append({"role":"tool","tool_call_id":tc.id,
                         "content":"（未执行：上次会话中断）"})
```

- 若悬空出现在历史中段（文件被手改/截断等异常）：从该 assistant 消息起截断尾部并警告，
  保证「每个 `tool_call_id` 恰有一条结果」的不变量（`agent.py:123`）。
- 尾部是 user 消息（模型还没回）：不补消息，靠续跑提示词（用户在恢复后自然继续输入；
  设计上不做 Claude Code 式自动 "Continue from where you left off."，留作可选项）。

### 3.7 压缩与转录的关系

- `compact()` 语义不变（system + 摘要 + 尾部），只把"内存替换"升级为
  **自包含 checkpoint 落盘**（§3.3 ③）。相比"记一条边界 + 重新追加尾部"的方案：
  单行原子、加载简单（O(1) 投影重置）、不产生重复行。
- 旧消息在磁盘上**永久保留**：`/export` 与审计用完整历史；模型只见 checkpoint 之后的投影。
- 连续多次压缩：每次 checkpoint 覆盖上一次的投影，`tail` 不含更早 checkpoint 内容，文件不膨胀。
- `ContextMeter.compact_count` 恢复时 = checkpoint 记录条数；真实 token 锚点在下次请求后重建。
- `/compact` 手动触发与阈值自动压缩走同一条路径，无分叉。

### 3.8 会话生命周期

```
              create（懒物化，首条非 system 消息时）
    ┌─────────────────────┐
    ▼                     │ /new（旧会话闭合，仍在磁盘）
  active ── /branch ──▶ 新的 active（父会话不动）
    │  ▲                  │
    │  └── /resume ───────┤
    │                     ▼
    └─ 退出/崩溃 ─▶ stopped（转录可恢复，尾部可能需 §3.6 修复）
                      │
                      └─ /sessions delete / 保留期 sweep ─▶ 文件移除
```

- 同一时刻只有一个活动会话文件可写；切换（`/resume`、`/new`、`/branch`）必须先闭合旧 store。
- TUI 沿用 busy 守卫：任务运行中不允许 `/resume` / `/new`（`tui/app.py:650` 既有逻辑）。

### 3.9 恢复语义表（设计决策）

| 会话级状态 | 恢复 | 说明 |
| ---- | ---- | ---- |
| 消息历史（含 tool 配对） | ✅ | 核心；system 段由 `sync_system()` 按最新提示词重建，转录不存 system |
| session id | ✅ | 沿用并写入 `config.SESSION_ID`，`{$session}` 请求头跨进程稳定 |
| 会话用量 | ⚠️ | 不并入当前会话口径；`usage` 记录用于列表/统计展示 |
| model / effort | ⚠️ | meta 记录；**用户显式 `-m` / 环境变量优先**，否则恢复会话原模型 |
| goal（`/goal`） | ✅ | 保留目标文本、状态、预算、证据；**回合计数与 token 基线重置**（对齐 Claude Code） |
| plan（`todo_write`） | ✅ | items 原样恢复（id 保留，标题不可变语义不受影响） |
| skills 激活集 | ✅ | 仅恢复名称 + 重新校验（禁用/不可用则丢弃）；项目级技能仍走既有信任门控 |
| permission 会话规则（"总是允许"） | ❌ | 安全优先，跨进程不继承（对齐 Claude Code fork 语义） |
| `SESSION_EXTRA_ROOTS` / read-tracking | ❌ | 越界信任与"已读"都重新建立 |
| context 计量 | ⚠️ | 由恢复后的 messages 重算；锚点待下次请求 |
| 一次性任务会话（`oneshot`） | ⚠️ | 默认不进 picker / `-c`（对齐 Claude Code 排除 `-p` 会话）；`--resume <id>` 仍可直达 |

### 3.10 接口与集成

**模块公共 API（`sessions/__init__.py`）**：`SessionStore`、`SessionSummary`、`LoadedSession`、
`list_sessions` / `find_last` / `find` / `load` / `delete` / `rename` / `branch` / `import_json` / `sweep`。

**`session.py`**：

```python
class Session:
    def __init__(self, store=None): ...
    def bind_store(self, store) -> None
    def set_compacted(self, summary: dict, tail: list, before: int, after: int) -> None
    def state_snapshot(self) -> dict            # goal/plan/skills → store.append_state
    def save(self) -> Path                      # 语义变更：flush + 返回转录路径（不再是 workspace dump）
```

**`agent.py`**：

```python
class Agent:
    def __init__(..., store=None)               # 默认按 [sessions].enabled 创建
    def resume(self, target) -> ResumeReport    # 装配：load → repair → 重建 Session + 恢复状态
    def new_session(self) -> None               # 既有：+ 闭合旧 store、开新 store
    def _snapshot_state_if_changed(self)        # 每轮结束/状态变更时写 state 记录（去重）
    def close(self) -> None                     # flush/close（cli 退出路径）
```

- `_run_loop` 每轮结束时调 `store.append_usage(...)`（若 provider 给出用量）与
  `_snapshot_state_if_changed()`（序列化比较，不变不写）。
- `run_once` 创建的 store 标记 `oneshot=True`（仅元数据差异）。

**命令层（`commands/session.py`）**：

| 命令 | 行为 |
| ---- | ---- |
| `/sessions [n\|delete <id>]` | 列出当前项目最近会话（短 id / 更新时间 / 标题或首轮摘要 / 模型 / 大小）；`delete` 删除文件 |
| `/resume [<id\|序号>]` | 无参：返回 `CommandSelect`（TUI 弹 `SelectionPanel`，REPL 走既有 `_print_select` 降级）；带参直连 |
| `/rename <名称>` | 追加 `title` 记录，列表与 picker 即刻生效 |
| `/branch [名称]` | 复制转录到新 id 并切换（原会话闭合但保留） |
| `/new` | 语义微调：旧会话**保留在磁盘**（从"破坏性清空"变为"开新会话"）；TUI 行为不变（清聊天区） |
| `/save` | 语义变更：手动 flush 并显示转录路径（旧 workspace dump 由 `sessions import/export` 承接） |
| `/export [文件]` | Phase 4：人读文本导出（消息 + 工具输出），可选 `--sanitize` |

**CLI（`cli.py`）**：

```
-c, --continue              恢复当前目录最近会话（无则提示后开新会话）
-r, --resume [ID|路径]      ID/唯一前缀直连；无参 → TUI picker / 非 tty 列表+明确 id（fail-closed 降级）
--no-session-persistence    本次运行不落盘（隐私；等价 [sessions].enabled=false）
```

- `-c` 与 `--resume` 互斥（argparse `mutually_exclusive_group`）；可与一次性任务组合
  （`smithcode -c "继续上次的任务"`：先恢复最近会话，再把该文本作为下一轮任务执行）。
- 非交互 REPL 的 `/resume` 无参：打印列表 + 要求显式 id，不弹框（复用 `_print_select` 路径）。
- `--resume <path>` 兼容旧 `.json`：走 `import_json` 导入后继续（§3.13）。

**TUI（`tui/`）**：

- picker 复用 `SelectionPanel` / `show_selection`（`tui/app.py:447`），选项 value = 会话短 id。
- 切换会话时不复用聊天区增量逻辑，新增 `replay_history(messages)`：user/assistant 文本静态渲染，
  tool 调用折叠为块（结果截断预览），占位/中断结果灰显；随后 `reset_chat()` 再回放。
- 状态栏/侧边栏按恢复后的 goal/plan/token 刷新；busy 守卫拒绝运行中切换。

### 3.11 失败、并发与降级

- **写失败**（只读 home、磁盘满、权限）：打印一次警告（含原因与关闭方式），`store.disable()`，
  本次会话纯内存运行；下次启动不提示（除非持续失败）。测试覆盖。
- **读失败**（文件缺失/损坏）：`resume` 返回结构化错误，宿主提示后回到新建会话，不崩溃。
- **编码失败**：单条消息序列化异常时跳过该条并警告（消息契约本应是 JSON 原生类型）。
- **并发**：Phase 1-3 **单写者假设**——同一会话文件被两个进程同时 resume 时后写者交错写入，
  文档明确不支持；检测手段（锁文件 + pid/时间戳）与 `--force` 放 Phase 4。
  `O_APPEND` 单次 `write()` 写入整行，≥4KB 的行在极端情况下也可能交错，风险同文档声明。

### 3.12 安全、隐私与保留

- **落用户目录**（`~/.smithcode/projects/…`），不污染仓库、不随 git 扩散；权限收紧 `0700/0600`。
- 转录可能包含读到的工作区内容与 `.env` 类敏感文件内容（现有读放行策略不变）。
  措施：`--no-session-persistence`、`[sessions].cleanup_days`（默认 30，0=不清理，启动时 best-effort
  执行 `sweep`）、`/sessions delete`；**不做自动脱敏**（会破坏恢复一致性），`/export --sanitize`
  后续可选。
- 删除语义：整文件删除（转录无外键依赖，无需级联）。

### 3.13 兼容与迁移

- 旧 `<workspace>/sessions/*.json`（`Session.save` 产物，纯 messages 数组）：
  `sessions.import_json(path)` 一次性转为 v1 转录（合成 meta，逐条写 msg，原文件保留）；
  也可通过 `--resume <path.json>` 惰性导入。
- 旧 `session.load()` 死代码移除，功能由 `sessions.load` + `import_json` 取代。
- `.gitignore` 中 `sessions/` 保留（防旧版本回写仓库）。
- API 层面：`Session.messages` 语义不变；唯一破坏性变更是 `Agent.compact` 内部改为
  `set_compacted`（对测试的影响在 §5 说明）。

### 3.14 配置项（`config.toml`）

```toml
[sessions]
enabled = true          # false = 等价 --no-session-persistence（永久关闭自动落盘）
cleanup_days = 30       # 保留天数；0 = 不自动清理
persist_state = true    # 是否持久化 goal / plan / skills 激活集（false = 只存消息）
list_limit = 20         # /sessions 与 picker 默认展示条数
```

解析沿用 `config.py` 的"类型不对警告并回退默认值"模式。

---

## 4. 分阶段实施

### Phase 1 —— 自动落盘 + 基础 resume（MVP）

- 新增 `sessions/`（`paths` / `format` / `store` / `__init__`）；`Session` 接 `MessageLog` +
  `bind_store` + `set_compacted`；`Agent` 创建/绑定 store、`resume()`；`compact()` 改一行。
- CLI：`-c` / `--resume <id|path>` / `--no-session-persistence`。
- 命令：`/sessions` 列表、`/resume <id>`（带参直连）、`/save` 改为 flush + 路径。
- 崩溃修复（§3.6）与容错解析（§3.3）。
- 验证：跑一轮任务 → 退出 → `smithcode -c` 历史一致且模型可见；杀进程后恢复能修复悬空
  `tool_calls`；权限确认不恢复；`pytest` 全绿。

### Phase 2 —— 好找好认（元数据 + picker + 回放）

- `title`（手动 `/rename`；首轮 prompt 截断作默认展示名）、oneshot 过滤、`list_sessions` 头部优化。
- `/resume` 无参选择器（TUI picker / REPL 列表降级）；TUI `replay_history`。
- model/effort 恢复规则落地（显式 CLI/env 优先）。
- 保留期 `sweep` 接入启动路径；`/sessions delete`。
- 验证：picker 可选中恢复、标题生效、一次性任务不进列表、过期会话被清理。

### Phase 3 —— 状态恢复 + 分支

- `goal.snapshot()/restore()`、`plan.snapshot()/restore()`、`skills` 激活集恢复（含重新校验）。
- 状态快照写盘（去重）与恢复后的 `/goal` `/plan` 展示一致。
- `/branch` 与 `--resume --fork`（可选）；会话内 `/resume` 切换。
- 验证：恢复后 goal 回合/token 计数重置、plan 内容一致、权限规则**不**被恢复（安全断言）、
  分支后父会话不变。

### Phase 4 —— 运维与脚本接口（可选）

- `/export [--sanitize]`；`--output-format json`（暴露 `session_id` / `usage`）。
- 并发锁（锁文件 + 活跃检测）与 `--force`；`sessions` 子命令（list/delete/import/export）。
- 仅在确有必要时评估 SQLite 索引（当前判断：不需要）。

---

## 5. 测试策略

| 测试文件 | 覆盖 |
| ---- | ---- |
| `tests/test_sessions_format.py`（新） | 记录编解码、未知类型/字段、损坏行与末行残行、`v` 兼容、compact 缺 tail、meta 合成 |
| `tests/test_sessions_store.py`（新） | 创建/追加/加载往返、懒物化、checkpoint 投影、列举头/尾读取、rename/branch/delete、sweep、写失败降级 |
| `tests/test_session.py`（扩展） | `MessageLog` 钩子、system 不入盘、`set_compacted`、`bind_store` |
| `tests/test_agent_resume.py`（新） | 崩溃修复（悬空 tool_calls）、恢复语义表逐项断言（权限不恢复、goal 计数重置、skills 重新校验） |
| `tests/test_commands.py`（扩展） | `/sessions` `/resume` `/rename` `/save` 文案与标记；非交互降级 |
| `tests/test_cli_sessions.py`（新） | `-c` / `--resume` / `--no-session-persistence` 解析与互斥；旧 `.json` 导入 |

- 全部持久化测试经 `SMITHCODE_HOME` 指向 tmp 目录隔离（沿用既有测试做法），不依赖真实 API。
- 回归红线：现有 `test_agent*` / `test_context.py` / `test_tui.py` 必须全绿；`compact` 改动同步
  更新相关断言。

实现收尾按仓库约定：`pytest` 全绿、更新 `CHANGELOG.md` `[未发布]` 段、同步
`docs/architecture.md` 的「会话与 /new」「模块职责」两节及 `prompts.py`（若行为可见）。

---

## 6. 风险与不做的事

- **不做数据库**（SQLite 是标准库但引入迁移/并发/级联复杂度；JSONL 在会话规模下足够）。
- **不做自动脱敏**（会破坏恢复一致性；以 opt-out + 保留期 + 手动 delete 替代）。
- **不做跨设备同步 / 云存储**（超出范围；`SessionStore` 接口未来可扩展镜像后端，对齐
  Claude Code 的 `SessionStore` 适配器思路）。
- **不做运行中会话热切换**（busy 守卫不变；切换只在空闲时）。
- **同会话多进程并发**：明确不支持；Phase 4 再考虑锁。
- 次要风险：磁盘增长率（长会话 + 不清理）、Windows 句柄占用导致文件删除失败（先 close 再删），
  均在测试与文档中固化。

---

## 附录 A：典型时序

**写入（正常回合）**

```
用户输入 → Agent.run → Session.add("user") ─┐
                                            ├─ MessageLog.append → store.append_message → 转录行
LLM 流式回复 → Session.messages.append(助手) ┤
工具执行     → _collect/_placeholder ────────┘
轮末 → store.append_usage(如有) + append_state(变更时)
```

**恢复（`smithcode -c`）**

```
CLI 解析 -c → sessions.find_last(cwd) → 无则提示开新会话
   └─ sessions.load(summary) → 逐行解析（compact 重置投影）→ repair(悬空 tool_calls)
        └─ Session(messages) + config.use_session_id(id) + goal/plan/skills 恢复
             └─ sync_system() 重建 system → 进入 REPL/TUI（TUI 回放历史）
```

**压缩（自动/手动）**

```
阈值触发 → 摘要请求 → validate_summary
   └─ session.set_compacted(summary, tail, before, after)
        ├─ 内存：messages = [summary] + tail
        └─ 转录：追加一行 compact 检查点（旧消息保留，模型只见新投影）
```

## 附录 B：主要参考

- Claude Code：`code.claude.com/docs/en/sessions`；append-only JSONL + `parentUuid` 链 + 批量写队列 +
  `--resume`/`/branch`/`/compact` 语义。
- Codex CLI：rollout JSONL 真相源 + SQLite 元数据索引、`resume`/`fork`/`archive`、`SessionMeta` 头部。
- opencode：V2 session spec（有序事件 + `session_context_epoch` checkpoint，durable 历史不删）；
  `message`/`part` 模型与 `session_input`/`todo` 表；`/sessions`、`/fork`、`parent_id`。
- Gemini CLI / Crush / Aider：控制记录（`$rewindTo`/`$set`）、SQLite 单写者、文本追加等对照实现。
- 本仓库：`SESSION_MANAGEMENT_RESEARCH.md`（调研原始材料与差距清单）。
