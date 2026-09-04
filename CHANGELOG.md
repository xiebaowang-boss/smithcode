# Changelog

本项目的所有显著变更都记录在本文件中。

## [未发布]

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
