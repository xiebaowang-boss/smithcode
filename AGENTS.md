# AGENTS.md — AI 编程助手工作指南

本文件供 AI 编程助手（Claude Code、Codex、Cursor、SmithCode 等）使用：进入项目后先读这里，按约定干活。

## 项目概览

SmithCode 是一个终端 AI 编程助手（mini coding agent）：用户用自然语言描述任务，Agent 自主规划步骤、调用工具、根据结果继续推理，直到任务完成。核心是 **Agentic Loop**——模型要么返回纯文本（任务完成），要么返回工具调用，经权限确认后执行、结果回传，循环直至结束。

- 语言：纯 Python，**>= 3.9**（不引入额外类型依赖，仅用标准库 typing；不用 3.10+ 语法糖）
- 布局：src 布局，包在 `src/smithcode/`，测试在 `tests/`，大体对应（如 `test_agent.py` 测 `agent.py`；集成类测试如 `test_display.py` / `test_agent_parallel.py` 不一一对端）
- 接口：任何 OpenAI 兼容接口均可接入；LLM 请求带流式输出与自动重试
- 跨平台：Windows / Linux / macOS 都要正常工作

## 常用命令

```bash
pip install -e ".[dev]"   # 安装含测试与 lint 工具
pytest                    # 运行全部测试（不依赖真实 API，可放心跑）
pytest tests/test_agent.py  # 只跑一个测试文件
ruff check src tests      # 代码检查
smithcode setup           # 初始化配置（用户机器上才需要）
```

本项目用 uv 管理（有 `uv.lock`），也可用 `uv run pytest` / `uv run ruff check src tests`。

## 改动后必须验证

- 改了 `src/` 或 `tests/` 下任何代码：跑 `pytest`
- 改了用户可见行为（新功能、命令、配置项、提示词调整）：同步更新 `CHANGELOG.md` 的 `[未发布]` 段落，中文条目，风格参照现有内容
- 改了工具实现或权限语义：对照 `docs/architecture.md` 确认描述仍然准确

## 架构与模块职责

模块详情见 [docs/architecture.md](docs/architecture.md)。速查：

| 模块 | 职责 |
| ---- | ---- |
| `cli.py` | 参数解析、交互式 REPL、单次任务模式 |
| `tui/` | Textual 全屏聊天界面，仅交互终端加载：`app.py` 组装层（布局接线 + 集中 CSS）、`widgets.py` 自包含控件、`panels.py` 弹窗面板、`bridge.py` 线程桥（`TuiRenderer`）、`render.py` 纯函数工具 |
| `commands/` | 斜杠命令框架：注册表（`@register`）+ 统一 `dispatch()`，REPL/TUI 共用；新命令一个文件接入，`/help` 自动生成 |
| `agent.py` | Agent 循环编排（`_BatchScheduler` 流式调度：边预检边执行、并行波次 + 串行屏障、结果按提交序；todo 专用路径：`todo_write` 以 serial 计划独占主线程、`display_result=False`）；`run_with_goal()` 是 `/goal` 的续跑驱动器 |
| `cancel.py` | 协作式取消原语：`CancellationToken` + ContextVar 传播 + `RunResult`；Esc / Ctrl+C 中断的唯一通道 |
| `process.py` | 外部命令执行的唯一出口：超时、取消与跨平台进程树终止（`taskkill` / `killpg`），工具层只做文案映射 |
| `renderer.py` | 渲染后端抽象（`Renderer` 基类 + `ConsoleRenderer` + `current()` / `set_renderer()`）：Agent 全部终端交互经此收口，TUI 启动时替换后端 |
| `llm/` | 模型交互子系统：`client.py` OpenAI 兼容接口封装（流式、重试、自定义请求头、`/models` 拉取）、`models.py` 候选模型目录 `ModelCatalog`、`usage.py` token 用量、`prompts.py` 系统提示词（Agent 行为规则，改行为先看这里）；`__init__.py` 汇总公共 API |
| `session.py` | 消息历史的增删存取（系统提示词装配、会话保存/恢复） |
| `plan.py` | todo_write 的会话级步骤清单（状态机 + 渲染） |
| `goal.py` | 持久目标（`/goal`）的会话级状态机与提示词：生命周期、回合预算、完成/阻碍审计、续跑注入；`/new` 时重置 |
| `skills/` | 技能子系统：`SKILL.md` 宽容解析（无第三方 YAML）、扫描发现与优先级、项目级信任门控、会话级激活集合、目录/已激活段渲染（设计见 `docs/skills-architecture.md`）；扫描范围暂为项目 `.agents/skills` + 用户 `~/.smithcode/skills` + `[skills].paths` |
| `context/` | 上下文计量（`meter`）、压缩逻辑（`compact`）、压缩提示词（`prompts`） |
| `permission/` | 权限子系统：`engine.py` 规则引擎与确认流程、`shell_policy.py` Shell 命令静态分析（只读判定 `is_safe_command` + 前缀推导 `command_key` / `derive_prefix`，命令规范表 `COMMANDS`），`__init__.py` 汇总公共 API |
| `config.py` | 配置中心，优先级：内置默认 < `config.toml` < 环境变量 < CLI 参数 |
| `wizard.py` / `welcome.py` | `setup` 初始化向导 / 启动欢迎横幅 |
| `utils/terminal.py` | 终端交互底层（输入读取、确认可用性判断） |
| `tools/base.py` | 工具注册表（`@register` 装饰器） |
| `tools/*.py` | 各工具实现（files / search / shell / patch / web / ask / todo / goal / skills） |

## 关键约定

### 安全边界（不要破坏）

- **路径沙箱**：所有文件操作经 `_resolve()` 检查，解析后的真实路径必须在授权目录内；绕过沙箱的"捷径"一律不加
- **保护路径**：`.env` 的内容不在变更预览/确认框中回显（防密钥泄露），读写仍按普通规则（读默认放行、写默认确认）；`.git` 只读（内置 deny 规则只拦写入/编辑，读取放行）
- **技能目录只读**：技能根目录经 `config.read_roots()` 对读工具放行（免越界确认），写工具/`apply_patch` 只认授权目录（`_resolve(write=True)`）；技能 frontmatter 的 `allowed-tools` 不产生授权效果
- **权限规则**：工具通过 schema 的 `pattern_arg` 声明权限模式来源；权限语义与既有工具一致时用 `family` 继承（如 `apply_patch` 继承 `edit_file`）；多路径工具用 `paths_from` 逐路径求值聚合
- **安全命令免确认**：内置只读命令集（`ls` / `cat` / `git status` 等，POSIX 与 cmd.exe 各一套）在内置默认 `ask` 下自动放行，且**仅在无任何用户/会话规则命中时生效**——用户可用精确 `ask`/`deny` 收紧，宽泛 `ask` 即整体关闭。开发工具链仅放行版本查询/只读枚举/静态检查（`python --version`、`pip list`、`ruff check`），**真正运行代码的用法（`pytest`、`python x.py`、`npm run`、`uv run`、`cargo test`）不放行**。判定为纯函数 `permission/shell_policy.is_safe_command`，拿不准（解析失败、inline 环境变量前缀、路径限定 argv[0]、重定向、命令替换、危险标志、未加引号 glob）一律回退确认
- **命令前缀记忆**：`run_command` 的"总是允许"记 argv 前缀（`permission/shell_policy.derive_prefix`），不记整串；匹配用 `command_key` token 前缀比较，文件/参数变化仍命中。拿不准（未登记命令、标志截断、`python -c`、`bash -c`、含危险标志）一律退回精确记忆；`BANNED_PREFIXES` 兜底；`cd`+`git` 守卫不提供"总是允许"
- **非交互 fail-closed**：管道 / CI 下无法弹确认时，所有 `ask` 一律拒绝而非挂起；改动确认流程时保持此语义
- **输出截断**：工具返回超过 `MAX_TOOL_OUTPUT`（默认 2 万字符）时保留头尾省略中间，防止撑爆上下文

### 新增工具的流程

1. 在 `tools/` 下新建文件，用 `@register` 声明 schema，并把模块名加入 `tools/__init__.py` 的导入列表（导入即注册自动发现）
2. 敏感操作用 `pattern_arg` 声明权限模式来源，并在默认规则中给出合理的初始动作
3. 用 `describe` 声明终端短摘要（格式「短名 + 目标」，如 `read src/a.py`）
4. 新建 `tests/test_tools_<名字>.py` 补测试
5. 系统提示词 `prompts.py` 中补充该工具的使用时机与注意事项（agent 不会读你的代码，只读提示词）

### 代码风格

- 中文注释与中文文档字符串（项目惯例），用户可见文案一律中文
- 错误信息用中文、面向用户友好（如「文件不存在」而非裸异常 traceback）
- 工具返回字符串而非抛异常给模型看；对模型的报错要可操作（提示下一步怎么改参数）
- 不引入重量级依赖；能标准库就标准库（webfetch 即标准库实现，无新依赖）
- Windows 下 shell 是 cmd.exe：涉及 shell 语法示例时用 Windows 兼容写法

### 兼容性红线

- Python 3.9 兼容：不用 `match` 语句、不用仅 3.10+ 的标准库特性。**注解**里可用 `X | Y` 联合写法——凡用到它的模块都带 `from __future__ import annotations`（延迟求值，3.9 不会报错）；但**运行期即时求值**处禁用（如 `isinstance(x, int | str)`、模块级类型别名求值），那些地方改用 `typing.Union`
- TOML 读取走 `tomli`（3.11+ 才有内置 `tomllib`），写走 `tomlkit`（保留用户注释）
- 交互层依赖（prompt_toolkit / textual）仅交互模式加载，非交互 stdin 退回普通 `input()`，保证管道 / CI 可用

### 事件同步

- 修改工具、权限、配置、TUI 行为时，同步更新 `prompts.py` 中对 agent 的描述——两处不一致会让 agent 行为错乱（如系统提示词承诺的能力实际已被删除）
- `prompts.py` 的提示词是行为规则的核心：新增工具后不更新提示词 = agent 基本不会用这个工具

## 不要做

- 不要提交（commit / push），除非用户明确要求
- 不要动 `uv.lock`（由 uv 管理）
- 不要在 `permission.py` / `tools/files.py` 的沙箱逻辑上"顺手优化"——安全边界改动需明确说明并跑全量测试
- 不要跳过测试直接交付：改坏已有行为比不实现更糟
- 不要在非交互路径上引入对 TUI / prompt_toolkit 的硬依赖
