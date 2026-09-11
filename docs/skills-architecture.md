# 技能（Skills）子系统架构设计

> 状态：P1 已实现。落地时扫描范围有意收窄：项目级只扫 `<工作区>/.agents/skills/`，
> 用户级只扫 `~/.smithcode/skills/`（外加 `[skills].paths`），其他客户端目录与
> monorepo 祖先目录兼容列入 P2。本文描述整体设计，第 5 节已按实际实现修订。
> 前置调研（格式规范、客户端对比、MCP 对照）见根目录 `SKILL_AND_MCP_RESEARCH.md`；
> 与 `docs/architecture.md` 同一文档风格。

## 1. 调研结论：成熟实现长什么样

### 1.1 Skill 的本质

Skill 是 **文件系统上的"知识包"**：一个目录，至少含一个 `SKILL.md`，YAML frontmatter 声明
`name` + `description`，Markdown 正文写"怎么做"，可选带 `scripts/`、`references/`、`assets/`。
Anthropic 于 2025-10-16 发布、2025-12-18 捐为开放标准（agentskills.io），Claude Code、Codex、
Cursor、VS Code、Gemini CLI、opencode、Cline 等均已接入。它解决的是 **"Agent 知道怎么做"**，
与 MCP 的 **"Agent 能连到什么"** 互补。

核心机制是**渐进式披露（progressive disclosure）**，三层加载：

| 层 | 内容 | 时机 | 成本 |
| -- | ---- | ---- | ---- |
| 1. Catalog | 每个技能的 `name` + `description` | 会话开始 | 约 50–100 token/技能 |
| 2. Instructions | `SKILL.md` 正文 | 技能被激活时 | 建议 < 5000 token |
| 3. Resources | 脚本 / 参考资料 / 模板 | 指令引用时才读 | 实际无上限 |

### 1.2 各客户端的实现差异（决定我们抄哪一段）

| 客户端 | 发现 | 激活 | 值得借鉴的点 |
| ------ | ---- | ---- | ------------ |
| **Claude Code** | `~/.claude/skills/`、`.claude/skills/`、插件、企业目录 | 模型自动 + `/skill-name` | 动态上下文注入（命令输出内联进技能）、压缩保护、`allowed-tools` |
| **Codex** | `SKILL.md` + `agents/openai.yaml`（UI/策略/依赖） | 隐式匹配 + `$mention` / `/skills` | **目录预算：最多取上下文 2% 或 8000 字符**，超出先压描述再省略并告警；插件分发 |
| **Cursor** | `.cursor/skills/`、`.agents/skills/`、兼容 `.claude`、`.codex` | 自动 + `/skill-name` | 递归嵌套 + monorepo 就地 scope、`paths` glob 限定、`disable-model-invocation` |
| **Cline** | `.cline/skills/`、`.claude/skills/` 等 | 专用 `use_skill` 工具 + `/skill-name` | 专用工具激活、每技能独立开关 |
| **opencode** | 从工作目录向上走到 git worktree，`.opencode/skills` + `.claude` + `.agents` | 原生 `skill` 工具 | **目录塞进工具 description**；**权限按技能名通配（allow/ask/deny），deny 整条隐藏**；零技能时连目录和工具一起省掉 |
| **agentskills.io 指南** | 项目级 + 用户级（含跨客户端 `.agents/skills/`） | 文件读取或专用工具 | 五步生命周期：发现 → 解析 → 披露 → 激活 → 长期管理；宽容解析；项目级信任门控；激活内容结构化包裹；压缩保护；去重 |

### 1.3 提炼给 SmithCode 的六条原则

1. **格式完全兼容开放标准**：目录位置暂定 `.agents/skills/`（项目级互通标准）与
   `~/.smithcode/skills/`（用户级原生），技能文件本身与生态完全兼容，后续可无痛
   增加 `.claude/skills/` 等兼容目录。
2. **只在系统提示词里放 name + description**，正文按需加载——挂很多技能而上下文近乎恒定。
3. **激活走专用工具**（枚举约束技能名），用户侧另有斜杠命令直达；不做关键词匹配。
4. **激活正文必须抗压缩**：技能指令是持久行为准则，被摘要掉会静默降级。
5. **项目级技能不可信**：仓库可能自带恶意指令，需要信任门控；技能脚本执行仍走既有权限，
   frontmatter 的 `allowed-tools` **绝不**自动放行。
6. **拿不准就省略**：没有技能时不要留空目录块；解析失败 / 缺 description 的技能直接跳过并记诊断。

## 2. 设计目标与非目标

**目标**

- 零新依赖（不引入 PyYAML）、Python 3.9 兼容、Windows / Linux / macOS 一致。
- 复用既有架构：`prompts.py` 动态段（同 goal 段）、`@register` 工具注册表、`commands/` 斜杠命令、
  `permission/` 规则引擎、`context/compact` 的"保留 `messages[0]`"语义。
- 会话级状态与 `/new` 语义对齐：激活集合在 `Agent.new_session()` 里重置。
- 安全边界不降级：技能目录**只读**放行，写操作照常确认；非交互 fail-closed。

**非目标**（后续可另立设计）

- MCP 客户端（见 `SKILL_AND_MCP_RESEARCH.md` 的 P3–P5 路线）。
- 技能市场 / 远程安装 / 版本管理 / 组织级托管。
- 子代理隔离执行技能（advanced pattern，P3 以后再评估）。
- 按 frontmatter `allowed-tools` 自动放行工具。

## 3. 总体架构

```
                        Agent.start()                        Session.sync_system()（每轮）
         ┌────────────────────────────────┐      ┌──────────────────────────────────────┐
         │ skills.refresh()               │      │ goal.render_section()                │
         │  ├ 扫描技能根目录（项目/用户/附加）│      │ skills.render_section()              │
         │  ├ frontmatter 宽容解析         │      │  ├ 第 1 层：可用技能目录（name+desc） │
         │  ├ 优先级去重 + 禁用过滤        │      │  └ 第 2 层：已激活技能正文 + 资源清单 │
         │  ├ 项目级信任门控               │      └───────────────┬──────────────────────┘
         │  └ 注册只读白名单               │                      │ messages[0]
         │ tools.skills.sync_schema()     │                      ▼
         └────────────────────────────────┘            ┌───────────────────┐
                                                       │ build_system_prompt│
   模型 ── 读目录匹配描述 ──▶ use_skill(name) ──────▶ │  （动态段拼接）      │
                                                       └───────────────────┘
            标记激活（会话单例）──► 下一轮请求 messages[0] 注入正文
                                                       │
            第 3 层：脚本 / 参考资料按需 read_file、脚本经 run_command（照常权限确认）
```

分层职责：

| 层 | 模块 | 职责 |
| -- | ---- | ---- |
| 格式层 | `skills/frontmatter.py` | 极简 YAML frontmatter 宽容解析（纯函数） |
| 发现层 | `skills/registry.py` | 扫描、优先级、信任门控、禁用过滤，产出 `Skill` 列表 |
| 状态层 | `skills/state.py` | 会话级单例：已发现索引、激活集合、生命周期 |
| 披露/激活层 | `skills/render.py`、`tools/skills.py` | 目录段 / 已激活段渲染；`use_skill` 工具 |
| 宿主层 | `commands/skills.py`、`session.py`、`agent.py` | `/skills`、`/skill`、提示词接线、重置 |
| 治理层 | `permission/`、`config.py` | 技能目录只读白名单、信任存储、`[skills]` 配置 |

## 4. 格式与解析

### 4.1 支持的 frontmatter 字段

严格对齐 agentskills.io specification，**只解释需要的字段，其余忽略**：

| 字段 | 必需 | 处理 |
| ---- | ---- | ---- |
| `name` | 是 | 索引键；非法（超长、非 `^[a-z0-9]+(-[a-z0-9]+)*$`、与目录名不符）只告警不拒绝 |
| `description` | 是 | 触发依据；**缺失或为空 → 跳过该技能并记诊断** |
| `license` / `compatibility` | 否 | 记录，`/skills` 详情展示 |
| `metadata` | 否 | 解析时跳过（不存入索引） |
| `disable-model-invocation` | 否 | `true` 时从目录隐藏，仅允许用户用 `/skill <name>` 显式激活 |
| `allowed-tools` | 否 | **解析但忽略**：不自动放行任何工具（安全红线） |

### 4.2 极简 frontmatter 解析器（决策：不引入 PyYAML）

SmithCode 的依赖纪律是标准库优先（webfetch 即范例），而目录披露只需要少量标量字段。
`skills/frontmatter.py` 实现一个受限解析器：

- 归一化 BOM / CRLF；定位首尾 `---`，中间为 frontmatter，其余为正文。
- 逐行解析 `key: value`（`split(":", 1)`），天然容忍 `description: Use when: xxx` 这类
  其他客户端产出的"非法但常见"写法（agentskills.io 指南明确要求宽容）。
- 支持引号标量（`'` / `"`）与块标量（`|` / `|-` / `>` / `>-`，取缩进块）。
- 缩进行归属上一个键；未知键、嵌套映射整体忽略（不报错）。
- 返回 `Frontmatter(meta: dict, body: str, warnings: list)`；完全解析失败时 `meta` 为空，
  由发现层决定跳过。**解析器不依赖文件系统，纯字符串输入，便于单测。**

`description` 渲染前折叠空白为单行；超过 1024 字符只告警（预算层会截断）。

### 4.3 数据模型

```python
@dataclass
class Skill:
    name: str                    # frontmatter.name 或（缺失时）目录名
    description: str             # 单行化后的触发描述
    location: Path               # SKILL.md 绝对路径
    base: Path                   # 技能根目录（解析正文内相对路径、列资源用）
    body: str                    # 去 frontmatter 的正文（发现时读入，激活零 IO）
    scope: str                   # "config" / "project" / "user"
    root_label: str              # 来源展示（如 ".agents/skills"、"~/.smithcode/skills"）
    license: str = ""
    compatibility: str = ""
    model_invocable: bool = True # disable-model-invocation 取反
    warnings: list = field(default_factory=list)
```

## 5. 发现与信任

### 5.1 扫描位置与优先级

每个技能根目录内**递归查找"含 `SKILL.md` 的目录"**（找到即不再下探），深度上限 4 层。

| 优先级 | 位置 | 说明 |
| ------ | ---- | ---- |
| 1（最高） | `[skills].paths` 配置的目录 | 用户显式配置，覆盖一切 |
| 2 | `<工作区>/.agents/skills/` | 跨客户端互通标准位置（P1 唯一的项目级来源） |
| 3 | `~/.smithcode/skills/` | 用户级原生位置（P1 唯一的用户级来源） |

暂不兼容：项目/用户的 `.claude/skills/`、`.opencode/skills/`、`~/.agents/skills/`，
以及 monorepo 沿祖先目录向上扫描（P2 扩展；技能文件格式本身已兼容，加目录即可）。

规则：

- 同名技能"先命中者生效"（附加路径 > 项目级 > 用户级），后者记入诊断（"被遮蔽"）；
  同一根目录内按名称排序确定扫描顺序。
- 跳过 `.git`、`node_modules`、`__pycache__`、`.venv`、`venv`、`dist`、`build`；
  单个根目录最多扫 2000 个目录、深度 4；`SKILL.md` 超过 1 MB 跳过。
- 支持软链接指向的技能目录（跟随 `resolve()`，按真实路径去重防环）。
- 扫描结果按发现顺序（根目录优先级 + 名称排序）确定，目录、`/skills`、工具 enum
  顺序完全确定，保护提示缓存。

### 5.2 项目级信任门控

项目级技能来自"可能不可信的仓库"，是一类**静默提示词注入通道**（SmithCode 目前不加载
仓库里的 AGENTS.md，技能会是第一条仓库可控的指令通道）。策略由 `[skills].project` 控制：

| 值 | 行为 |
| -- | ---- |
| `ask`（默认） | 交互模式：首次发现项目技能时弹一次确认，`[a] 始终信任` 写入信任库，`[y]` 仅本会话，`[n]` 跳过；非交互模式：跳过并打印一行说明（fail-closed） |
| `on` | 直接加载（用户显式选择信任所有项目） |
| `off` | 永不加载 |

- 信任库 `~/.smithcode/skills_trust.json`：`{"version": 1, "projects": {"<规范化路径>": true}}`，
  键取工作区的 git 根（无 `.git` 则取工作区）的 `resolve()` + `normcase` 结果，同项目子目录共享信任。
- 确认走 `renderer.confirm_choice`（REPL 打印 y/a/n；TUI 启动前即 `Agent.start()`，此时
  渲染后端还是 `ConsoleRenderer`，行为一致且不引入 Textual 弹窗时序问题）。
- 用户级与配置路径的技能视为可信，不弹确认。

### 5.3 配置与禁用过滤

`~/.smithcode/config.toml` 新增段落（loader 参照 `load_permissions()` 的宽容降级风格）：

```toml
[skills]
enabled = true            # 总开关：false 时目录段与 use_skill 工具一起隐藏
paths = []                # 额外技能目录（最高优先级）
project = "ask"           # 项目级信任策略：ask / on / off
max_catalog_chars = 8000  # 目录段字符预算（对齐 Codex 的 2% 上限思路）
disabled = ["internal-*"] # fnmatch 通配，命中者整条从目录与工具 enum 中隐藏
```

被禁用的技能在模型侧**完全不可见**（agentskills.io 指南要求：不要列出来再在激活时拦，
否则模型会浪费轮次），但 `/skills` 会显示"已禁用"及命中规则，便于用户排查。

### 5.4 发现时机与生命周期

| 时机 | 动作 |
| ---- | ---- |
| `Agent.start()` | `skills.refresh()` + `tools.skills.sync_schema()`（cli 在 REPL / TUI / 一次性任务前统一调用，见 `cli.py:160`） |
| `Session.sync_system()` | `skills.render_section()`：**纯读**，未装载过返回空串（技能由 `Agent.start()` 显式装载，提示词装配不做磁盘 IO） |
| `_run_loop` 每轮 | 再次 `session.sync_system()`：激活后下一轮立刻注入正文（见 7.2） |
| `/skills refresh` | `Agent.refresh_skills()`：重扫 + 同步 enum（新装技能无需重启进程） |
| `Agent.new_session()` | `skills.reset()`：清空激活集合（发现结果保留，省 IO；目录段内容不变所以提示缓存不受 `/new` 影响） |

## 6. 披露：目录段（第 1 层）

`build_system_prompt(goal_section="", skills_section="")` 新增第二个动态段参数（保持
向后兼容的默认值）；`Session.sync_system()` 传入 `skills.render_section()`。
拼接顺序为：静态规则节 → 技能段 → 目标段（目标优先级最高，放最后）。

目录段形态（无技能时返回空串，**整段省略**）：

```markdown
## 可用技能
以下技能提供特定任务的专门指令。当任务与某个技能的描述相符时，先用 use_skill
加载其完整指令再动手，不要凭描述猜测内容，也不要编造技能名。已激活的技能无需重复加载。

- pdf-processing（项目）: 提取 PDF 文本、填充表单、合并文档。处理 PDF 时使用。
- sql-explain（用户）: 解释 SQL 执行计划并给出优化建议。分析慢查询时使用。
```

**不注入 `location`**：激活工具会在结果里给出技能目录，模块省下每个技能几十 token；
代价是不支持"直接 read_file 读取 SKILL.md"的兜底激活——正文注入策略本就要求走
`use_skill`（见 7.2），所以这是有意的取舍。

**预算控制**（`max_catalog_chars`，默认 8000）分三级降级，确定性算法便于测试：

1. 全量条目（`name（scope）: description`，按 name 排序）。
2. 超预算：均分剩余预算截断各条 description。
3. 仍超：只保留 `name（scope）` 列表；再超则按顺序截断并追加
   `（另有 N 个技能未列出）`。

前缀缓存保护：目录内容只在技能集合 / 激活集合变化时变化，普通回合逐字节稳定
（沿用 `sync_system` 已有的"内容不同才刷新"逻辑，`session.py:35-38`）。

## 7. 激活（第 2 层）与资源（第 3 层）

### 7.1 模型侧：`use_skill` 工具

`tools/skills.py` 新增一个状态型工具（与 `goal_update` 同类，参考其会话单例用法）：

```python
@register({
    "name": "use_skill",
    "pattern_arg": "name",       # 权限规则可按技能名通配
    "describe": lambda args: f"skill {args.get('name', '?')}",
    "serial": True,              # 写系统提示词 + 会话状态，禁止并行
    "description": "加载某个技能的完整指令。任务与「可用技能」描述相符时先调用本工具，"
                   "再按加载的指令执行；name 必须是可用技能名之一。",
    "parameters": {
        "type": "object",
        "properties": {"name": {"type": "string", "enum": [...]}},  # 发现后动态填充
        "required": ["name"],
    },
})
def use_skill(name: str) -> str: ...
```

- `name` 用**枚举**约束为"可被模型调用的技能名"（排除 `disable-model-invocation` 与禁用项），
  防止模型幻觉不存在的名字；技能集合变化时由 `sync_schema()` 原地更新枚举。
- **无可用技能时隐藏本工具**：`tools/base.py` 增加 `HIDDEN` 集合与 `visible_schemas()`，
  `Agent._chat()` 改用 `visible_schemas()`（`agent.py:352`）。这是一处通用能力，MCP
  动态工具以后也能复用。
- 权限默认 `("use_skill", "*", ALLOW)` 加进 `DEFAULT_RULES`（与 todo/goal 同理由：确认一次
  "加载指令"是荒谬的）；用户可按技能名写精确 `deny` / `ask`。
- 返回文本（短，可正常走 `tool_result` 展示）：
  - 成功："技能 pdf-processing 已激活，完整指令已写入系统提示词的「已激活技能」段，
    技能目录为 `<路径>`，请按指令继续。"
  - 已激活："技能 ... 已激活，无需重复加载。"
  - 未知："错误: 未找到技能 ...。可用技能: ...。"

### 7.2 正文注入策略：走系统提示词（关键决策）

激活正文**不放在工具结果里**，而是由 `skills` 会话状态标记激活后，经 `render_section()`
注入 `messages[0]` 的「已激活技能」段：

```
## 已激活技能
以下指令已加载，按其中步骤执行；除非用户要求，不要重复调用 use_skill。

<skill name="pdf-processing" scope="project" location="C:\repo\.agents\skills\pdf-processing">
可用资源（相对技能目录；用 read_file 读取，脚本用 run_command 执行）：
  scripts/extract.py
  references/form-spec.md

（SKILL.md 正文，原样）
</skill>
```

理由（针对 SmithCode 现状）：

- **抗压缩零新增机制**：现有 `context/compact.py` 的 `assemble()` 无条件保留 `messages[0]`
  （`agent.py` 压缩时取 `messages[0]` 的 system 内容），正文放系统提示词天然免疫摘要/截断；
  放工具结果则需要给压缩器加"保护位"、改 `pick_tail` / `assemble`，复杂且容易漏。
- **天然去重**：激活集合是状态，重复调用直接返回"已激活"。
- **可计量**：`/context` 的 system 桶如实包含技能成本，用户看得到。
- **代价**：激活瞬间系统提示词变化，会使一次前缀缓存失效（低频事件，可接受）；
  压缩前后由 `SyncSystem` 幂等重建，不会与历史内容分叉。

配套改动：`Agent._run_loop` 每轮开始处调用 `self.session.sync_system()`（幂等，内容不变
时不改消息），保证同一任务内"激活 → 下一轮请求携带正文"。

### 7.3 资源清单与按需读取

- 激活段列出技能目录内的资源文件（排除 `SKILL.md`；按名称排序；最多 50 条，
  超出追加"等 N 个文件"），**只列不读**，具体文件由模型按指令用 `read_file` 按需加载。
- 技能目录对读工具**只读放行**（见 8.1），避免每次读引用文件都弹越界确认。
- 脚本执行没有捷径：`run_command` 照常走权限规则，`serial` 与超时保护不变。

### 7.4 用户侧激活：`/skills` 选择框与 `/skill` 直达

`commands/skills.py` 注册两个命令：

- `/skills`（无参数）：直接弹出技能选择框（`CommandSelect` → TUI SelectionPanel，
  含「已激活 / 仅手动」标记，选中后自动重分发 `/skill <名称>`）；无可用技能时同样
  返回空选择框，不打印任何提示文字。
- `/skills list`：文本列表（名称、来源分组、激活/禁用状态、描述首行、诊断信息）；
  `/skills refresh`：重扫磁盘并同步工具（新装技能即时可见）。
- `/skill <name> [任务...]`：激活指定技能；激活成功保持静默（不打印"已加载"提示），
  带后续文本时把用户输入原文（含技能指令）整体回显为消息（`CommandResult.echo_input`）
  后立即 `start_task` 执行（等价"加载技能并开跑"），不带则仅加载、等用户下一条指令。
  未知名称给出可用列表。被 `disable-model-invocation` 排除的技能仍可在这里显式激活（用户意图优先）。

技能同时作为**动态条目**并入 `/` 输入补全（`commands/base.complete_commands()` 合并，
TUI 命令菜单与 REPL 补全共用）：功能命令按名称在前、技能按名称在后，同名技能不重复
出现，条目选中后填入 `/技能名 `，用户可继续补任务；`commands.dispatch()` 对未命中
注册表的命令名兜底查技能并手动激活，`/技能名 [任务]` 与 `/skill` 等价。`/help` 只列
注册命令，尾部一行提示技能用法。

## 8. 权限与安全

### 8.1 技能目录只读白名单

用户级技能目录在工作区之外，模型读引用文件会触发"越界确认"，体验与安全成本都不合理。
方案是给沙箱加一个**只读白名单**（这是对安全边界的有意扩展，按 AGENTS.md 需全量回归）：

- `config` 增加 `SKILL_READ_ROOTS`（由 `skills.refresh()` 写入技能根目录，`/new` 保留）。
- `tools/files.py._resolve(path, write=False)`：读路径检查 `allowed_roots() + SKILL_READ_ROOTS`；
  **写工具（`write_file` / `edit_file`，`patch` 经 `paths_from` 走 `_preflight_path`）显式传
  `write=True`，仍只认 `allowed_roots()`**——写技能文件仍会走越界确认，且 `accept_edits`
  模式不会静默改写技能目录。
- `Agent._preflight_path(raw, read_only=False)`：只读工具（`read_file` / `list_dir` / `glob` /
  `grep`）的目标落在 `SKILL_READ_ROOTS` 内时直接放行、不弹越界确认；写路径逻辑不变。
- 权限规则照旧参与：默认 `allow` 只读工具，用户可用 `deny` / `ask` 精确收紧（规则优先于白名单）。

### 8.2 其余安全语义

- `allowed-tools` 不产生任何权限效果；技能正文里的"忽略权限"话术不改变工具行为。
- 非交互 fail-closed 保持：`project="ask"` 时跳过项目技能而不是挂起；技能目录白名单
  只影响"越界确认"，`ask` / `deny` 规则照常生效。
- 技能解析阶段只读 `SKILL.md` 与列目录，不执行任何技能内代码。

## 9. 上下文、压缩与恢复

- **计量**：目录段 + 已激活段都在 `messages[0]`，计入 `context/meter.py` 的 `system` 桶，
  `/context` 与 TUI 用量卡自动反映。
- **压缩**：`compact()` 保留 `messages[0]`，所以目录与已激活正文不会被摘要；压缩后
  `sync_system()` 重建内容一致（激活集合是模块状态），不会把技能段弄丢。
- **会话恢复**（`session.load()`）是已知缺口：激活集合不随 JSON 保存，恢复后首轮
  `sync_system()` 会用空激活集合重建提示词，正文丢失。P2 方案：`sync_system` 替换旧
  system 消息前，用 `<skill name="...">` 标记从旧内容中恢复激活集合（技能仍存在时）。
  P1 文档中先声明"保存/恢复不保留激活状态，目录仍在、可重新激活"。

## 10. 模块与接口

### 10.1 新增文件

| 文件 | 内容 |
| ---- | ---- |
| `src/smithcode/skills/__init__.py` | 公共 API 汇总（`refresh` / `reset` / `render_section` / `all_skills` / `get` / `activate` / `read_roots`） |
| `src/smithcode/skills/frontmatter.py` | 宽容 frontmatter 解析器（纯函数） |
| `src/smithcode/skills/registry.py` | `Skill` 数据模型、目录扫描、优先级、信任门控、禁用过滤、诊断 |
| `src/smithcode/skills/state.py` | 会话单例（索引 + 激活集合）与生命周期 |
| `src/smithcode/skills/render.py` | 目录段 / 已激活段 / `/skills` 文案渲染与预算降级（纯函数） |
| `src/smithcode/tools/skills.py` | `use_skill` 注册 + `sync_schema()`（更新 enum、设置隐藏） |
| `src/smithcode/commands/skills.py` | `/skills`（选择框 / list / refresh）、`/skill`（直达 / 选择框） |
| `tests/test_skills_frontmatter.py` 等 | 见第 11 节 |

### 10.2 对既有模块的改动

| 文件 | 改动 |
| ---- | ---- |
| `llm/prompts.py` | `build_system_prompt` 增加 `skills_section` 动态段参数（无技能时空串，整段省略） |
| `session.py` | `sync_system()` 传入 `skills.render_section()` |
| `agent.py` | `start()` 增加 `refresh_skills()`；`new_session()` 增加 `skills.reset()`；`_run_loop` 每轮 `sync_system()`；`_chat` 改用 `visible_schemas()`；`_preflight_path` 读工具白名单短路 |
| `tools/base.py` | `HIDDEN` + `set_hidden()` + `visible_schemas()` |
| `tools/__init__.py` | 导入 `skills` 工具模块（导入即注册） |
| `tools/files.py` | `_resolve(path, write=False)`；写工具传 `write=True`（安全边界变更，需全量测试） |
| `permission/engine.py` | `DEFAULT_RULES` 增加 `("use_skill", "*", ALLOW)`（注释同 todo/goal） |
| `config.py` | `[skills]` loader（`load_skills_config()`）、`SKILL_READ_ROOTS`、`skills_trust_path()` |
| `commands/__init__.py` | 导入 `skills` 命令模块；`dispatch()` 未命中注册表时兜底技能名（`/技能名 [任务]` 直达） |
| `docs/architecture.md`、`AGENTS.md`、`CHANGELOG.md` | 新增技能章节 / 模块表行 / `[未发布]` 中文条目（按 AGENTS.md「事件同步」） |

### 10.3 公共 API（`skills/__init__.py`）

```python
def refresh() -> list: ...            # 重新发现 + 信任门控 + 同步只读白名单，返回诊断
def reset() -> None: ...              # /new：清空激活集合（发现结果与信任决定保留）
def render_section() -> str: ...      # 目录 + 已激活正文（纯读；未装载返回空串）
def all_skills() -> list: ...         # 含被禁用/仅手动者（带状态标记，/skills 用）
def model_skills() -> list: ...       # 可进目录与 use_skill enum 的技能
def get(name: str) -> Skill | None: ...
def activate(name: str, by: str = "model") -> str: ...  # 幂等；by 决定返回话术
def active_names() -> list: ...
# 技能根目录经 config.skill_roots() 暴露给权限层（发现时写入只读白名单）
```

## 11. 测试计划

| 测试文件 | 覆盖 |
| -------- | ---- |
| `tests/test_skills_frontmatter.py` | 标准字段、`>` / `|` 块标量、引号、未加引号冒号、CRLF/BOM、无/未闭合 frontmatter、缺 description |
| `tests/test_skills_registry.py` | 用 `tmp_path` 造多级目录：优先级（config > project > user、`.smithcode` > `.agents` > `.claude`）、向上到 git 根、同名遮蔽诊断、禁用通配、深度/数量上限、软链接、跳过坏技能、信任三种策略 + 非交互 fail-closed（monkeypatch 渲染器） |
| `tests/test_skills_state.py` | `refresh` / `reset` / 懒发现、`activate` 幂等与未知技能报错、激活顺序、`/new` 清空 |
| `tests/test_skills_render.py` | 目录段格式、无技能返回空串、预算三级降级、已激活段（含资源清单） |
| `tests/test_tools_skills.py` | `use_skill` 注册元数据、enum 同步、零技能隐藏、返回文案、权限默认 allow |
| `tests/test_commands_skills.py` | `/skills`（有/无/禁用）、`/skill`（激活 / 带任务 start_task / 未知）、`/skills refresh` |
| `tests/test_session.py`（扩展） | 技能段注入、字节稳定性（目录不变时不刷新）、`/new` 重置 |
| `tests/test_agent*.py`（扩展） | 激活后下一轮请求 `messages[0]` 含正文；压缩后技能段仍在 |
| `tests/test_permission*.py` / `test_tools_files.py`（扩展） | 技能目录读不弹越界、写仍 ask/deny、用户 deny 优先于白名单 |

验证命令：`pytest`、`ruff check src tests`（AGENTS.md 约定）。

## 12. 分阶段落地

| 阶段 | 内容 | 风险 |
| ---- | ---- | ---- |
| **P1 核心闭环（已完成）** | frontmatter / registry / state / render、`[skills]` 配置与信任库、目录段注入、`use_skill` + 系统提示词正文注入、只读白名单、`/skills` + `/skill`、`/new` 接线、测试与文档；扫描范围收窄为项目 `.agents/skills` + 用户 `~/.smithcode/skills` + `[skills].paths` | 中（含沙箱只读扩展，已全量回归） |
| **P2 体验与治理** | TUI 侧边栏技能卡与 `/skill` 参数补全、会话恢复激活、信任继承 git 根、兼容 `.claude/skills` 等目录与 monorepo 祖先扫描 | 低 |
| **P3 进阶（暂不承诺）** | 子代理隔离执行、组织级目录、安装/升级、`allowed-tools` 执行级白名单（需 OS 沙箱配合） | 高 |

## 13. 关键决策记录

| 决策 | 选择 | 备选 | 理由 |
| ---- | ---- | ---- | ---- |
| 激活载体 | 专用 `use_skill` 工具 | 直接 `read_file` 读 SKILL.md | 枚举防幻觉、可去重、可控制注入内容；文件读取只作为"正文已在系统提示词"的补充 |
| 正文位置 | `messages[0]`「已激活技能」段 | 工具结果 + 压缩保护位 | 零改动复用"压缩保留 messages[0]"，不易漏；代价是一次前缀缓存失效 |
| 披露位置 | 系统提示词独立段 | 工具 description（opencode 式） | 与 goal 段模式一致、`/context` 可计量、无技能时整段省略简单 |
| YAML | 自写受限解析器 | 新增 PyYAML 依赖 | 依赖纪律：标准库优先，只需少量标量 |
| 项目级信任 | `ask` 默认 + 持久化信任库 | 直接加载 / 全部询问 | 仓库是静默注入通道；一次确认兼顾安全与体验 |
| 目录预算 | 字符上限 + 三级降级 | 不设上限 | 对齐 Codex，防止多技能挤占提示词 |
| 技能目录访问 | 只读白名单 | 加入 `EXTRA_ROOTS`（可写） | 防止 accept_edits / 自动模式静默改写技能文件 |

## 14. 参考来源

- Agent Skills 规范与客户端集成指南：https://agentskills.io/specification 、
  https://agentskills.io/client-implementation/adding-skills-support
- Anthropic Agent Skills 发布博客：https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills
- Claude Code Skills / Agent SDK：https://code.claude.com/docs/en/agent-sdk/skills
- OpenAI Codex Skills：https://developers.openai.com/codex/skills 、https://developers.openai.com/codex/build-skills
- Cursor Skills：https://cursor.com/docs/context/skills
- Cline Skills：https://docs.cline.bot/customization/skills
- opencode Agent Skills：https://opencode.ai/docs/skills/
- 本项目前置调研：`SKILL_AND_MCP_RESEARCH.md`；现有架构：`docs/architecture.md`
