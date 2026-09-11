# Skill 与 MCP：成熟 Agent 的两种扩展机制实现调研

> 调研目标：弄清成熟 AI Agent 产品中 **Agent Skills** 与 **MCP（Model Context Protocol）**
> 两个模块分别是"是什么、怎么定义、客户端怎么实现"的，并对照 SmithCode 现有架构给出落地建议。
>
> 调研时间锚点：MCP 规范最新版为 `2026-07-28`；Agent Skills 已由 Anthropic 在 2025-12-18
> 发布为开放标准（agentskills.io）。文末附全部信息来源。

---

## 0. 一页结论（TL;DR）

- **MCP 是"协议层"的扩展**：一套基于 JSON-RPC 2.0 的开放标准，用 client/server 架构把
  **外部工具、资源、提示词**接进 Agent。它解决"Agent 能连到什么"的问题，能力由服务端提供、
  运行在独立进程或远端。客户端要做的是一套 **MCP Host/Client**：连接管理、能力协商、
  工具发现与包装、审批与认证、生命周期与重连。
- **Skill 是"知识层"的扩展**：一个含 `SKILL.md` 的文件夹（可选带脚本/参考资料/模板），
  用 YAML frontmatter 声明 `name` + `description`，正文写"怎么做"。它解决"Agent 知道怎么做"的
  问题，能力是**纯文本指令 + 可选脚本**，随文件走。核心设计是 **渐进式披露（progressive
  disclosure）**：启动只加载 name/description（约 50–100 token），命中任务才读全文，
  需要时才读引用文件——因此上下文成本近乎恒定。
- **两者互补、不替代**：Anthropic 的官方定位是 "MCP 提供连接，Skill 教 Agent 完成更复杂、
  带外部工具的工作流"。实践中常见组合是：Skill 里写"先调哪个 MCP 工具、按什么顺序、出错怎么办"，
  MCP 提供那个工具本体。
- **对客户端（如 SmithCode）而言，Skill 的接入成本远低于 MCP**：Skill 只需文件扫描 + YAML
  解析 + 往系统提示词塞一段"技能目录" + 一个激活工具（甚至复用 `read_file`）；MCP 则要引入
  子进程/HTTP 通信、JSON-RPC 会话、协议版本与能力协商、OAuth、进程树管理等一整套基础设施。

---

## 1. 背景：两种扩展机制各解决什么问题

通用 Agent（Claude Code、Cursor、Cline、Gemini CLI 等）有两个绕不开的扩展需求：

| 需求 | 典型问题 | 对应机制 |
| ---- | -------- | -------- |
| **连外部世界** | 读日历、查数据库、操作 Figma/Notion、调内部 API | **MCP**（协议 + 服务端进程） |
| **装领域知识/流程** | 公司的发布流程、PDF 表单填写规范、审查清单、代码规范 | **Agent Skills**（文件夹 + 指令） |

在 Skills 出现前，"装知识"的主流做法是往系统提示词/规则文件里堆内容，代价是**每轮对话都占上下文、
且随规则数量线性增长**。Skills 的贡献是把知识"按需加载"——把常驻上下文压到每条技能约 100 token
的目录条目，正文与附属文件都在真正用到时才进入上下文。

---

## 2. MCP（Model Context Protocol）

### 2.1 定位与参与角色

MCP 是**连接 AI 应用与外部系统**的开放标准（官方比喻：AI 应用的 USB-C 口）。它只规定"上下文
如何交换"，不规定 Agent 如何使用 LLM、也不管理上下文本身。

参与者分为三种（来自官方架构文档）：

- **MCP Host**：AI 应用本体（Claude Code、Claude Desktop、VS Code、Cursor…），负责协调、管理
  一个或多个 MCP Client，并把从服务端拿到的上下文交给模型。
- **MCP Client**：协议级组件，**每个 Client 对应一个 Server**，维护一条专属连接。Host 连 N 个
  Server 就实例化 N 个 Client。
- **MCP Server**：提供上下文的程序，可本地（stdio 子进程）也可远端（HTTP）。

关键点：**"Server" 指提供上下文的程序，与它跑在哪里无关**。本地 stdio 服务通常服务单个 Client，
远端 Streamable HTTP 服务通常服务多个 Client。

### 2.2 两层架构：数据层 + 传输层

- **数据层（内层）**：基于 **JSON-RPC 2.0** 的交换协议，定义能力/版本发现、核心原语
  （tools/resources/prompts）、通知。
- **传输层（外层）**：通信通道与鉴权。抽象掉通信细节，让上层统一用 JSON-RPC 消息格式。

**传输方式**（官方两种 + 兼容历史）：

| 传输 | 机制 | 适用 |
| ---- | ---- | ---- |
| **stdio** | 通过标准输入输出与本地子进程通信，无网络开销 | 本地工具（需直连系统/自定义脚本） |
| **Streamable HTTP** | HTTP POST 传客户端→服务端消息，可选 SSE 流式；支持 Bearer/API Key/自定义头，推荐 OAuth 取 token | 远端/云服务、多客户端共享 |
| *SSE（HTTP+SSE）* | 已废弃（deprecated），但多数客户端仍兼容（Claude Code 会先试 HTTP 再自动回落 SSE） | 仅剩 SSE 端点的旧服务 |

### 2.3 协议原语：服务端三个 + 客户端若干

MCP 的核心是**原语（primitives）**——定义"客户端与服务端能互相提供什么"。

**服务端提供的三类原语**（官方 server-concepts，明确标注控制方）：

| 原语 | 说明 | 控制方 | 典型操作 |
| ---- | ---- | ------ | -------- |
| **Tools** | 模型主动调用的可执行函数，带 JSON Schema 输入输出 | **模型**（Model-controlled） | `tools/list`、`tools/call` |
| **Resources** | 只读上下文数据源，各带唯一 URI + MIME 类型；支持固定资源与**资源模板**（参数化 URI） | **应用**（Application-controlled） | `resources/list`、`resources/templates/list`、`resources/read`、`subscriptions/listen` |
| **Prompts** | 预置的指令模板 | **用户**（User-controlled） | `prompts/list`、`prompts/get` |

工具发现是**动态**的：客户端先 `tools/list` 拿到带 schema 的定义，再按需 `tools/call`。
原语都有 `*/list`（发现）与 `*/get`（读取），工具额外有 `*/call`（执行）。服务端可发
`notifications/.../list_changed` 通知列表变化。

**客户端提供的原语**（让服务端能反向交互，来自 client-concepts）：

| 特性 | 说明 | 状态 |
| ---- | ---- | ---- |
| **Elicitation** | 服务端在交互中向用户索取结构化输入。两种模式：**form**（客户端按 schema 渲染表单并校验）、**URL**（给用户一个链接，数据不经过客户端，适合凭据/第三方 OAuth）。走 MRTR（多轮往返）模式 | 现行 |
| **Roots** | 客户端告诉服务端"可操作哪些目录"（`file://` URI 列表），传达意图边界（**不强制安全**，安全要靠 OS 权限/沙箱） | **2026-07-28 起废弃** |
| **Sampling** | 服务端借客户端的 LLM 能力做补全（客户端掌握权限与安全），可带 tools 数组做工具调用 | **2026-07-28 起废弃** |

> 注意：Roots 与 Sampling 在最新协议里已标记 deprecated/scheduled for removal，新的实现应改走
> "通过工具参数/资源 URI/服务端配置传路径"与"直接对接 LLM 供应商 API"。Elicitation 仍在用。

### 2.4 生命周期与"无状态化"

MCP 定义严格的三阶段生命周期（官方 lifecycle 规范）：

1. **初始化（Initialization）**：客户端发 `initialize`（含协议版本、客户端能力、客户端信息），
   服务端回自身能力与信息；客户端再发 `notifications/initialized` 表示就绪。此阶段协商
   **协议版本**与**能力**（roots/sampling/elicitation、prompts/resources/tools/logging/completions 等）。
2. **运行（Operation）**：按协商结果交换消息。
3. **关闭（Shutdown）**：无特定消息，靠传输层——stdio 关闭输入流 / 等待退出 / SIGTERM / SIGKILL；
   HTTP 关闭连接。

**版本协商**：客户端在 `initialize` 报自己支持的最新版本；服务端支持则回同版本，否则回自己支持的
最新版本；客户端不支持服务端回的版本应断开。HTTP 下后续请求须带 `MCP-Protocol-Version` 头。

**无状态与发现（新版重点）**：最新规范把 MCP 描述为**无状态协议**——每个请求在 `_meta` 里携带
协议版本与本请求相关能力，服务端可独立处理每个请求。服务端通过一个**必选的 `server/discover`**
请求发布支持的版本与能力，客户端可在任何其他请求之前发送它。

> 生态过渡期的典型实现（OpenAI Agents SDK 文档）：装了 MCP Python SDK v2 时，客户端以
> `mode="auto"` 先发 `server/discover` 探测；现代服务端应答即采用结果，旧服务端不支持则回落
> 到经典的 `initialize` 握手并用其中协商的版本。**装新版 SDK 并不强制所有连接都用最新协议版本。**

### 2.5 认证与安全

- **远端鉴权**：推荐 OAuth（Bearer token / API Key / 自定义头）。Cline/Cursor 等在配置里支持静态
  OAuth 客户端凭据（当服务端不支持动态客户端注册时）；Cursor 有固定回调地址
  （`https://www.cursor.com/agents/mcp/oauth/callback` 与桌面 `http://localhost:8787/callback`）。
- **本地进程环境脱敏**：Gemini CLI 在 spawn stdio 服务前会**自动从继承环境里抹掉敏感变量**
  （`GEMINI_API_KEY`/`GOOGLE_API_KEY`，以及匹配 `*TOKEN*`/`*SECRET*`/`*PASSWORD*`/`*KEY*`/`*AUTH*`/
  `*CREDENTIAL*` 的变量、证书私钥模式），只有用户在 `env` 里**显式声明**的才放行——"显式配置=知情同意"。
- **审批控制**：工具调用普遍支持"逐个确认 / 预批准安全操作 / 关闭确认（trust）"。MCP 官方在
  tools 的用户交互模型里列了几种手段：UI 里展示可用工具、单次执行审批对话框、预批准权限设置、
  带结果的执行日志。

### 2.6 成熟客户端怎么接入 MCP（实现对比）

| 客户端 | 配置位置 | 传输 | 工具转接模型 | 审批/安全要点 |
| ------ | -------- | ---- | ------------ | ------------- |
| **Claude Code** | `claude mcp add`（CLI）、`.mcp.json`（项目级，可提交）、`~/.claude.json`（local/user scope）、`claude mcp add-json` | stdio / http（`streamable-http` 别名）/ sse / **ws** | 将 MCP 工具并入自身工具集 | 项目级 `.mcp.json` 需批准；stdio 服务被注入 `CLAUDE_PROJECT_DIR`；用 `roots/list` 应答会话目录（`--add-dir` 追加）；`mcp_server_errors` 上报加载失败 |
| **Cursor** | Customize 页一键装、`mcp.json`、团队 marketplace | stdio / SSE / Streamable HTTP | 并入 Agent | 支持 static OAuth（`auth.CLIENT_ID`）+ 固定回调；支持 Tools/Prompts/Resources/Roots/Elicitation 及 **MCP Apps 扩展**（工具可返回内联交互 UI） |
| **Cline** | CLI `~/.cline/mcp.json`；IDE 面板 Configure MCP Servers | stdio / Streamable HTTP / SSE | 并入内置工具集 | 每服务 `disabled` / `autoApprove`（工具白名单）；可启用/禁用、重启、设超时 |
| **Continue** | `config.yaml` 的 `mcpServers`，或 `.continue/mcpServers/*.yaml`（可直接放别的工具的 JSON） | stdio / sse / streamable-http | 并入 agent 模式工具（**仅 agent 模式可用**） | `env` 支持 `${{ secrets.XXX }}` 注入密钥 |
| **Gemini CLI** | `settings.json` 的 `mcpServers` + `mcp`（全局 `allowed`/`excluded` 白/黑名单） | stdio / SSE / Streamable HTTP | 包装为 `DiscoveredMCPTool`：schema 清洗校验后注册进全局工具表，冲突解决；自动发现 resources | 每服务 `trust`（true=跳过全部确认）/ `includeTools` / `excludeTools` / `timeout`；**env 脱敏**；OAuth 自动发现 |
| **VS Code + Copilot** | 扩展市场 `@mcp` 一键装；`.vscode/mcp.json`（工作区）/ 用户 profile；Agent Host 读 `.mcp.json` 或 `~/.copilot/mcp-config.json` | 多种 | 并入 chat 工具 | 首次使用需确认信任；可用 input 变量/环境文件避免硬编码密钥 |
| **OpenAI Agents SDK（库）** | 代码里配置 `mcp_servers=[...]` | `MCPServerStdio` / `MCPServerSse` / `MCPServerStreamableHttp`；另有 `HostedMCPTool`（把整趟往返交给 Responses API 托底） | 工具转成 SDK 的 FunctionTool，可用 `include_server_in_tool_names` 防重名；派生前可 `convert_schemas_to_strict` | `failure_error_function` 决定失败呈报；Streamable HTTP 支持逐次审批策略与 per-call `_meta`；工具可过滤/缓存 |

**共性提炼（一份 MCP 客户端要做的事）**：

1. **配置**：支持 stdio（command+args+env）与远端（url+headers/OAuth）两类条目；作用域
   （项目级可提交 / 用户级 / 本地）。
2. **连接**：为每个 Server 建一条连接（子进程或 HTTP），做 `initialize`（或新版 `server/discover`）
   完成版本与能力协商。
3. **发现**：`tools/list` 拿工具 schema，清洗/校验后注册进 Agent 工具表；同样可发现 resources/prompts。
   处理列表变更通知。
4. **调用**：把模型发起的工具调用路由到 `tools/call`；结果按截断规则回传。
5. **治理**：审批（逐次/白名单/trust）、凭据管理（env 脱敏、OAuth）、超时与重连、进程树清理、
   名称冲突处理（服务名前缀）。

### 2.7 生态

MCP 官方文档站列出的配套生态：**MCP Registry**（服务发现）、各语言 **SDK**、**MCP Inspector**
（交互式调试工具）、**MCP Apps**（在聊天里渲染交互式 UI 的扩展）、**MCP Bundles (MCPB)**
（把本地 stdio 服务 + 运行时打包成 `.mcpb`，用户无需装 Node/Python 即可安装）。

---

## 3. Agent Skills（SKILL.md）

### 3.1 定位与核心概念

Agent Skills 是**轻量、开放的扩展格式**：一个技能就是一个**文件夹**，至少含一个 `SKILL.md`。
`SKILL.md` 含元数据（至少 `name` + `description`）与指令正文；还能打包**脚本、参考资料、模板**等资源。

```
my-skill/
├── SKILL.md        # 必需：元数据 + 指令
├── scripts/        # 可选：可执行代码
├── references/     # 可选：文档（按需读）
├── assets/         # 可选：模板、资源
└── ...             # 其他任意文件/目录
```

官方给它的三个好处：**领域专长**（把"公司/团队/个人的专门知识"打包成可移植、可版本控制的文件夹）、
**可复现工作流**（多步任务变成一致、可审计的流程）、**跨产品复用**（一次编写、任意支持该标准的
Agent 通用）。

它由 Anthropic 于 2025-10-16 发布，2025-12-18 发布为开放标准，现由 agentskills.io 维护，已被
一批 Agent 产品采用（见 3.5）。

### 3.2 规范：`SKILL.md` 的 frontmatter 与目录约定

`SKILL.md` = **YAML frontmatter**（`---` 包裹）+ **Markdown 正文**。字段约束（来自官方 specification）：

| 字段 | 必需 | 约束 |
| ---- | ---- | ---- |
| `name` | ✅ | ≤64 字符；仅小写字母/数字/连字符；不以连字符开头或结尾；**不得含连续连字符**；**必须与父目录名一致** |
| `description` | ✅ | 1–1024 字符；非空；**要同时说明"做什么"与"何时用"**（这是模型判断是否触发的唯一依据） |
| `license` | ❌ | 许可证名或指向随附许可证文件 |
| `compatibility` | ❌ | ≤500 字符；环境要求（目标产品、系统依赖、网络需求等） |
| `metadata` | ❌ | 任意 string→string 的键值映射（供客户端存扩展属性） |
| `allowed-tools` | ❌ | 空格分隔的"预批准工具"字符串（**实验性**，各实现支持度不一） |

最小示例：

```markdown
---
name: pdf-processing
description: Extract PDF text, fill forms, merge files. Use when handling PDFs.
---

## 步骤
1. ...
```

正文无格式限制，推荐包含：分步指令、输入/输出示例、常见边界情况。**正文全文在技能被激活时会
整体进上下文**，所以官方建议 `SKILL.md` 正文 **< 500 行 / < 5000 token**，超出的内容拆分到
`references/`、`scripts/`、`assets/`，用**相对技能根目录的一级相对路径**引用（避免深层嵌套链）。

参考库 `skills-ref` 提供校验：`skills-ref validate ./my-skill`。

### 3.3 渐进式披露（progressive disclosure）——Skill 的灵魂

官方把加载分成三层：

| 层 | 加载内容 | 时机 | token 成本 |
| -- | -------- | ---- | ---------- |
| **1. Catalog（目录）** | 每个技能的 `name` + `description` | 会话启动 | 约 50–100 token/技能 |
| **2. Instructions** | `SKILL.md` 正文全文 | 技能被激活时 | 建议 < 5000 token |
| **3. Resources** | `scripts/`、`references/`、`assets/` 里的文件 | 真正需要时 | 实质无上限 |

因此**可以一次性挂很多技能而几乎不占上下文**：启动只把一长串"名字+描述"塞进系统提示词，
模型按描述判断相关性，命中才逐层读取。这也意味着**技能能打包的上下文规模实际上是无界的**——
因为不需要一次性读进上下文窗口。

### 3.4 客户端集成生命周期（五步）

agentskills.io 的"如何给 Agent 加 Skills 支持"给出了标准流程（对任何客户端都适用）：

**Step 1｜发现（Discover）**

- 扫描**子目录中名为 `SKILL.md` 的文件**（`README.md` 之类忽略）。
- 约定位置：客户端原生目录 + **跨客户端互通目录 `.agents/skills/`**（项目级与用户级各一份），
  不少实现还兼容 `.claude/skills/`。其他可扩展位置：向上到 git 根（monorepo）、XDG 目录、用户配置路径。
- 扫描要设边界（跳过 `.git/`、`node_modules/`，可选尊重 `.gitignore`，限深度 4–6 层 / 最多约 2000 目录）。
- **同名冲突**：通行规则是 **项目级覆盖用户级**（同层级内取先/后发现的都可，但要有日志提示被遮蔽）。
- **信任考量**：项目级技能来自"可能不可信的仓库"，建议在用户标记该目录受信任前**门控加载**
  （防止恶意仓库静默往上下文注入指令）。

**Step 2｜解析（Parse）**

- 定位首个 `---` 与随后 `---`，解析其间 YAML 取 `name`/`description`（必需）及可选字段，其后正文即 body。
- **宽容解析**：对别的客户端写的"技术上非法但能被解析"的 YAML（如未加引号、含冒号的 description）
  做兜底（加引号/转块标量后重试）；name 不匹配目录名、超长等只告警仍加载；**description 缺失或
  完全无法解析才跳过该技能**。
- 每个技能至少存：`name`、`description`、`location`（`SKILL.md` 绝对路径）；可另存 body。
  以 `name` 建内存索引。

**Step 3｜披露（Disclose）**

- 构造**技能目录**（`name` + `description`，可选 `location`），放进**系统提示词的一个段落**，
  或**塞进激活工具的 description**（两种都行，前者更通用、后者更干净）。
- 配一段简短行为指令，告诉模型"技能存在、如何加载"（用文件读取激活 vs 用专门工具激活，措辞不同）。
- **过滤**：被用户禁用/权限拒绝/显式 opt-out（如 `disable-model-invocation`）的技能应当**整条从目录里
  隐藏**，而不是列出来再在激活时拦——否则模型会浪费轮次去尝试加载。
- **没有技能时**：连目录和指令都不加，不要放一个空的说明块。

**Step 4｜激活（Activate）**

- **模型驱动激活**（主流）：靠模型自己读目录后判断，客户端不做关键词匹配。
  - *文件读取激活*：模型直接用它已有的 `read_file` 读 `SKILL.md`（最简单，无需新基础设施）。
  - *专用工具激活*：注册一个 `activate_skill(name)` 工具，好处是能控制返回内容（去/留 frontmatter）、
    用结构化标签包裹、顺带列出 `scripts/`/`references/` 等捆绑资源、执行权限校验、记录埋点。
    （用专用工具时把 `name` 参数约束为**合法技能名的枚举**，防止模型幻觉出不存在的名字。）
- **用户显式激活**：最常见是 `/skill-name` 斜杠命令或 `$skill-name` 提及语法，由宿主拦截并注入内容，
  模型无需自己触发；可配自动补全。
- **模型收到什么**：整份文件（含 frontmatter）或仅正文（去 frontmatter）都可；有专用激活工具的实现
  多数返回**去掉 frontmatter 的正文**。
- **结构化包裹**：用带标识的标签（如 `<skill name=...>` … `</skill>`）包住内容，便于上下文管理与压缩时识别。
- **权限白名单**：若 Agent 有文件访问审批，**把技能目录加白**，否则每次读捆绑脚本都会弹确认框。
- **列出捆绑资源**但**不预读**：激活工具可以枚举技能目录里的支持文件，具体文件仍由模型按需读取。

**Step 5｜长期管理上下文**

- **保护技能内容不被压缩剪掉**：技能指令是持久行为准则，被摘要掉会**静默降级** Agent 表现。
  做法：把技能工具输出标记为"受保护"，或用结构化标签在压缩时识别并保留。
- **去重激活**：记录本会话已激活的技能，避免同一指令重复注入。
- **子代理委派（可选，高级）**：把技能放进独立子会话执行，再把结果摘要回主会话。

### 3.5 成熟客户端的 Skills 实现对比

| 客户端 | 技能目录位置 | 激活方式 | 特别之处 |
| ------ | ------------ | -------- | -------- |
| **Claude Code** | `~/.claude/skills/`（个人）、`.claude/skills/`（项目，可提交）、企业托管目录、`--add-dir` 目录、插件 `skills/`、claude.ai 同步 | 自动（按 description）+ `/skill-name` | 支持**动态上下文注入**（`` !`git diff HEAD` `` 会在模型看到前先执行并内联输出）、内置一批 bundled skills（`/doctor`、`/code-review`、`/verify` 等）、`disableBundledSkills` 开关、monorepo 的嵌套/父目录技能加载规则 |
| **Cursor** | `.cursor/skills/`、`.agents/skills/`（项目级）、`~/.cursor/skills/`、`~/.agents/skills/`（用户级）；兼容 `.claude/skills/`、`.codex/skills/` | 自动 + `/skill-name` | 支持**递归嵌套**分组与 monorepo 就地放置（嵌套技能自动 scope 到该目录）；额外 frontmatter：`paths`（按 glob 限定生效文件）、`disable-model-invocation`、`icon`/`color`；内置技能（`/create-skill`、`/migrate-to-skills` 等）；可作为 Custom Mode 常开 |
| **Cline** | `.cline/skills/`、`.clinerules/skills/`、`.claude/skills/`（项目）；`~/.cline/skills/`（全局） | 自动 + `/skill-name` | 用专门的 **`use_skill` 工具**激活；每个技能可**单独开关**（默认启用）；同项目 vs 全局同名时全局优先；官方建议正文 **< 5k token**、重要信息前置 |
| **Gemini CLI** | 见客户端清单（skills 支持） | 见文档 | 在 Agent Skills 客户端清单中列为支持 |
| **VS Code / GitHub Copilot** | 见各自文档（`agent-skills`） | 见文档 | 在客户端清单中列为支持 |

**已采用 Agent Skills 标准的客户端**（agentskills.io "Client Showcase" 摘录，按官方列出的名字）：
Claude Code、Claude、ChatGPT & Codex、Cursor、VS Code、GitHub Copilot、Gemini CLI、OpenCode、
OpenHands、Goose（Block）、Amp、Letta、Mux、Junie（JetBrains）、Firebender、Autohand Code CLI、
ZeroClaw 等。

### 3.6 Skills 与 MCP 的互补

Anthropic 在发布博客中明确表示：期待探索 **Skills 如何补充 MCP**——"教 Agent 完成涉及外部工具与
软件的更复杂工作流"。MCP 官方文档也把这条落地成了实践：`mcp-server-dev` 插件用**一组 Skills**
（`build-mcp-server` / `build-mcp-app` / `build-mcpb`）来指导 Agent 设计并脚手架化一个 MCP 服务端，
每个技能都是标准 `SKILL.md` + `references/`，用任何支持标准的 Agent 都能装。

一句话分工：**MCP 负责"连接"（能力供给），Skill 负责"知识与编排"（怎么用这些能力完成任务）。**

---

## 4. Skill 与 MCP 横向对比

| 维度 | MCP | Agent Skills |
| ---- | --- | ------------ |
| **本质** | 跨进程/跨网络的**协议**（JSON-RPC 2.0） | 磁盘上的**文件夹 + Markdown 指令**（开放格式） |
| **解决的问题** | Agent 能**连到什么**（工具/资源/提示词） | Agent **知道怎么做**（流程/规范/经验） |
| **能力载体** | 独立 Server 进程或远端服务 | `SKILL.md` 正文 + 可选脚本/参考资料/模板 |
| **控制方** | 工具=模型、资源=应用、提示词=用户 | 主要靠模型判断何时激活；也可用户显式调用 |
| **发现方式** | 运行时协议握手：`initialize`/`server/discover` + `tools/list` | 启动时扫盘，把 name+description 装进目录 |
| **加载/上下文成本** | 工具 schema 进上下文（技能多时可能较大，需按需暴露/过滤） | **渐进式披露**：目录 ~100 token → 正文 <5k → 资源按需，近乎恒定 |
| **扩展新能力** | 写一个 MCP Server（任意语言） | 写一个技能文件夹（无需写代码，可选带脚本） |
| **可移植性** | 高（跨客户端、跨语言；有 Registry） | 高（跨客户端；`.agents/skills/` 已成通用互通位置） |
| **信任/安全模型** | 运行第三方代码（本地进程/远端），需审批、env 脱敏、OAuth、沙箱 | 注入文本指令+脚本，**建议对项目级技能做信任门控**，脚本执行仍走既有权限 |
| **典型用例** | GitHub/DB/Slack/日历/浏览器自动化等外部系统 | 发布流程、审查清单、代码规范、领域分析流水线 |
| **失效/降级** | 连不上/超时/认证失败，需重连与错误呈报 | 解析失败跳过该技能；正文被压缩掉会静默降级（需保护） |
| **与对方关系** | 提供能力 | 编排能力（可规定"先调哪个 MCP 工具、按什么顺序"） |

**选型直觉**：要"接一个新系统/账号/API" → MCP；要"教会 Agent 一套做法/规范/流程" → Skill；
两者叠加 → 用 Skill 编排多个 MCP 工具完成复杂任务。

---

## 5. 对 SmithCode 的落地建议

SmithCode 的现状（据 `docs/architecture.md`、`src/smithcode/tools/base.py`、`llm/prompts.py`）：

- 工具通过 `tools/base.py` 的 **`@register(schema)`** 声明注册：`SCHEMAS`（发给 LLM 的 function
  schema）与 `FUNCTIONS`（实现）分离，另有 `PATTERN_ARGS`（权限匹配参数）、`PATTERN_FAMILIES`、
  `PATHS_EXTRACTORS`、`DESCRIBERS`、`PREVIEWS`、`DISPLAY`、`SERIAL` 等元数据；`tools/__init__.py`
  导入即注册。
- 系统提示词由 `llm/prompts.py` 的 **`build_system_prompt(goal_section)`** 拼装（`_SECTIONS` 常量段 +
  动态 goal 段），`Session.sync_system()` 在目标变更时刷新 `messages[0]`。
- 有权限引擎（`permission/`）、上下文计量与压缩（`context/`）、协作式取消（`cancel.py`）、
  外部命令唯一出口（`process.py`）。

### 5.1 先做 Skills（成本低、收益直观）

Skills 的接入几乎完全契合 SmithCode 现有机制，**推荐优先做**：

1. **扫描发现**：新增 `skills.py`（会话级），启动时扫描 `.agents/skills/`、`.claude/skills/`
   （项目级）+ `~/.agents/skills/`、`~/.claude/skills/`（用户级），找**含 `SKILL.md` 的子目录**；
   跳过 `.git/`、`node_modules/`，限深度；**项目级覆盖用户级**并记冲突日志；对项目级做**信任门控**
   （复用既有工作区信任/权限语义）。
2. **解析**：解析 frontmatter（`name`/`description` 必需，沿用列表其余可选字段），宽容处理非法 YAML；
   `description` 缺失才跳过。以 `name` 建索引，存 `name`/`description`/`location`。
3. **披露**：在 `prompts.py` 的 `build_system_prompt` 里新增一段"可用技能目录"（`name`+`description`+
   `location`）+ 简短行为指令。**注意提示前缀缓存**：目录内容应在技能集合未变时逐字节稳定；技能集合变化
   时一次性重建。可参照现有 goal 段的"稳定+动态"注入方式。
4. **激活**：两种都行——**最省事的是复用 `read_file`**（目录里给 `location`，指令里告诉模型"命中就用
   读取工具加载该 `SKILL.md`"），零新工具；若要更可控（去 frontmatter、列捆绑资源、埋点），
   新增 `activate_skill` 工具用 `@register` 注册（`name` 用枚举约束），并把它设为 `allow` 权限族。
   建议同时支持 **`/skill-name`** 斜杠命令（SmithCode 已有 `commands/` 注册表，天然契合）。
5. **上下文保护与去重**：在 `context/compact.py` 里把技能内容（激活结果）标为**受保护、压缩时不剪**；
   记录本会话已激活技能避免重复注入。**把技能目录加进权限白名单**，避免读捆绑脚本反复弹确认。
6. **提示词同步**：按项目约定（AGENTS.md「事件同步」），新增技能能力时要更新 `prompts.py` 中对 Agent
   的描述，并在 `CHANGELOG.md` `[未发布]` 记一条中文条目。

### 5.2 再做 MCP（工作量大，但价值高）

建议把 MCP 客户端做成**独立子系统 + 动态工具源**，尽量不动现有 `tools/` 契约：

1. **新增 `mcp/` 包**：`client.py`（一条连接 = 一个 Server）、`transport.py`（stdio 子进程 / Streamable
   HTTP；SSE 作兼容）、`session.py`（`initialize`/`server/discover` 握手、能力协商、`tools/list` 缓存与
   `list_changed`）、`registry.py`（把 MCP 工具转成 SmithCode 工具注册项）。
2. **动态注册进现有注册表**：MCP 工具在运行时才能知道 schema，需在 `tools/base.py` 之上加一层
   "**动态工具源**"——启动/连接成功后把 MCP 工具**追加**到 `SCHEMAS`/`FUNCTIONS`（或扩展现有注册表
   支持运行时增删）。工具名建议加 **server 前缀**（如 `mcp__github__create_issue`）防重名，并借用
   既有 `describe`/`serial`/`pattern_arg` 元数据：只读 MCP 工具可并行、有副作用者设 `serial=True`。
3. **权限映射**：MCP 工具默认走 `ask`，可在 `config.toml` 的 `[permissions]` 用通配符按工具名配置
   `allow`/`ask`/`deny`；本地 stdio 服务 spawn 前做 **env 脱敏**（复用 Gemini CLI 的思路）；远端走
   OAuth/Header。复用 `process.py` 统一管理子进程的**超时、取消、进程树终止**（stdio 服务天然是子进程）。
4. **配置与作用域**：在 `~/.smithcode/config.toml` 增加 `[mcpServers]` 段（兼容 `mcpServers` JSON 形状，
   以便直接搬用别家配置），支持项目级 `.smithcode/mcp.json`（可提交）与用户级；提供
   `smithcode mcp add/list/get/remove` 之类 CLI（参照 Claude Code `claude mcp add`）。
5. **生命周期**：连接失败/超时要有**降级与错误呈报**（不能让某个 Server 拖垮整个 Agent）；
   支持 `tools/list` 变化的重新注册；关闭时清理子进程。
6. **协议版本策略**：以 `initialize` 握手为基准，能识别新版 `server/discover`；对旧服务端回落经典
   `initialize`。把"协议版本"作为连接元数据记录，不假设所有 Server 同版本。
7. **可选进阶**：支持 **Resources**（把 `resources/list` 的结果暴露成一个 `read_resource` 工具或
   `@` 提及）、**MCP Apps**、**Registry** 一键安装。

### 5.3 分阶段路线（建议）

| 阶段 | 内容 | 依赖/风险 |
| ---- | ---- | --------- |
| **P1** | Skills：发现+解析+披露+激活（`read_file` 或 `activate_skill`）+ `/skill-name`；补测试与文档 | 低风险，几乎零新依赖 |
| **P2** | Skills：上下文压缩保护、权限白名单、去重；`/doctor` 式诊断命令 | 低 |
| **P3** | MCP：配置 + stdio 客户端 + 工具动态注册 + 权限映射（先本地 stdio） | 中；子进程管理复用 `process.py` |
| **P4** | MCP：Streamable HTTP + OAuth + env 脱敏 + 重连/错误降级 | 中高（网络与鉴权） |
| **P5** | 进阶：Resources、MCP Apps、Registry 一键安装、用 Skill 编排多 MCP 工具 | 视需求 |

> 兼容性红线（AGENTS.md）：SmithCode 坚持 **Python 3.9**、标准库优先。MCP 客户端如果要引第三方
  SDK（`mcp` 包）需评估——但**手写 JSON-RPC over stdio/HTTP 用标准库完全可行**（与 `webfetch` 零依赖
  的思路一致），可先手写最小实现。

---

## 6. 关键结论速记

1. **MCP = 协议**：Host/Client/Server 三角，数据层（JSON-RPC 2.0 原语）+ 传输层（stdio / Streamable
   HTTP）；三个服务端原语（tools=模型控、resources=应用控、prompts=用户控），客户端原语里
   **Roots/Sampling 已废弃**、**Elicitation 仍在**；新版强调**无状态 + `server/discover`**。
2. **Skill = 文件夹**：`SKILL.md`（frontmatter 的 `name`/`description` + 正文）+ 可选 `scripts/references/
   assets`；核心是**渐进式披露**（目录 ~100 token → 正文 <5k → 资源按需），故可挂很多技能而上下文近恒定。
3. **客户端要做的五步**：发现（扫 `SKILL.md`，项目覆盖用户、信任门控）→ 解析（宽容、缺 description 才跳过）
   → 披露（目录进系统提示词或工具描述、过滤掉不可用的）→ 激活（文件读取或专用工具、结构化包裹、权限白名单）
   → 管理（压缩保护、去重、可选子代理）。
4. **两者互补**：MCP 供能力，Skill 供编排；可用 Skill 教 Agent 如何组合使用多个 MCP 工具。
5. **对 SmithCode**：Skills 接入成本低、优先做，能直接复用 `@register` 工具注册表、`prompts.py` 注入、
   `commands/` 斜杠命令与 `context/compact` 保护；MCP 建议做成独立子系统 + 动态工具源，复用
   `process.py`/`permission/`，先 stdio 后 HTTP。

---

## 7. 信息来源

**MCP 官方（modelcontextprotocol.io，规范版本 2026-07-28 / 2025-06-18）**

- What is MCP：https://modelcontextprotocol.io/docs/getting-started/intro
- Architecture overview（Host/Client/Server、两层、原语、无状态与发现）：https://modelcontextprotocol.io/docs/learn/architecture
- Understanding MCP servers（Tools/Resources/Prompts 与控制方、资源模板、参数补全）：https://modelcontextprotocol.io/docs/learn/server-concepts
- Understanding MCP clients（Elicitation/Roots/Sampling）：https://modelcontextprotocol.io/docs/learn/client-concepts
- Specification — Lifecycle（初始化/版本协商/能力协商/关闭/超时/错误）：https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle
- Build with Agent Skills（`mcp-server-dev` 插件）：https://modelcontextprotocol.io/docs/develop/build-with-agent-skills

**Agent Skills 官方（agentskills.io）**

- Overview：https://agentskills.io/
- Specification（frontmatter 字段与约束、目录约定、渐进式披露）：https://agentskills.io/specification
- Client Showcase（支持的客户端清单）：https://agentskills.io/clients
- How to add skills support to your agent（五步集成指南）：https://agentskills.io/client-implementation/adding-skills-support

**Anthropic**

- Equipping agents for the real world with Agent Skills（发布博客，2025-10-16，2025-12-18 更新为开放标准）：https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills

**各客户端文档**

- Claude Code — MCP：https://docs.claude.com/en/docs/claude-code/mcp
- Claude Code — Skills：https://docs.claude.com/en/docs/claude-code/skills
- Cursor — MCP：https://cursor.com/docs/context/mcp
- Cursor — Agent Skills：https://cursor.com/docs/context/skills
- Cline — MCP：https://docs.cline.bot/mcp/mcp-overview
- Cline — Skills：https://docs.cline.bot/customization/skills
- Continue — MCP：https://docs.continue.dev/customize/deep-dives/mcp
- Gemini CLI — MCP servers：https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md
- VS Code — MCP servers：https://code.visualstudio.com/docs/copilot/customization/mcp-servers
- OpenAI Agents SDK — MCP：https://openai.github.io/openai-agents-python/mcp/

**本项目内部参考**

- `docs/architecture.md`（模块划分与 Agent 循环）
- `src/smithcode/tools/base.py`（`@register` 工具注册表）
- `src/smithcode/llm/prompts.py`（`build_system_prompt` 系统提示词装配）
- `AGENTS.md`（兼容性红线、事件同步约定、文档风格）
