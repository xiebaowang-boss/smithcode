# AGENTS.md — AI 编程助手工作指南

本文件供 AI 编程助手（Claude Code、Codex、Cursor、SmithCode 等）使用：进入项目后先读这里，按约定干活。

## 项目概览

SmithCode 是一个终端 AI 编程助手（mini coding agent）：用户用自然语言描述任务，Agent 自主规划步骤、调用工具、根据结果继续推理，直到任务完成。核心是 **Agentic Loop**——模型要么返回纯文本（任务完成），要么返回工具调用，经权限确认后执行、结果回传，循环直至结束。

- 语言：纯 Python，**>= 3.9**（无类型标注依赖、不用 3.10+ 语法糖）
- 布局：src 布局，包在 `src/smithcode/`，测试在 `tests/`，一一对应（如 `test_agent.py` 测 `agent.py`）
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
| `tui/` | Textual 全屏聊天界面（`app.py`），仅交互终端加载 |
| `commands/` | 斜杠命令框架：注册表（`@register`）+ 统一 `dispatch()`，REPL/TUI 共用；新命令一个文件接入，`/help` 自动生成 |
| `agent.py` | Agent 循环编排（工具调用分发、todo 专用路径 `_execute_todo`） |
| `llm.py` | OpenAI 兼容接口封装（流式、超时、指数退避重试） |
| `prompts.py` | 系统提示词（Agent 的行为规则，改行为先看这里） |
| `session.py` | 消息历史的增删存取 |
| `plan.py` | todo_write 的会话级步骤清单（状态机 + 渲染） |
| `context/` | 上下文计量（`meter`）、压缩逻辑（`compact`）、压缩提示词（`prompts`） |
| `permission.py` | 敏感操作的用户确认，规则引擎 |
| `config.py` | 配置中心，优先级：内置默认 < `config.toml` < 环境变量 < CLI 参数 |
| `tools/base.py` | 工具注册表（`@register` 装饰器） |
| `tools/*.py` | 各工具实现（files / search / shell / patch / web / ask / todo） |

## 关键约定

### 安全边界（不要破坏）

- **路径沙箱**：所有文件操作经 `_resolve()` 检查，解析后的真实路径必须在授权目录内；绕过沙箱的"捷径"一律不加
- **保护路径**：`.env` 禁止读写，`.git` 只读（内置 deny 规则）
- **权限规则**：工具通过 schema 的 `pattern_arg` 声明权限模式来源；权限语义与既有工具一致时用 `family` 继承（如 `apply_patch` 继承 `edit_file`）；多路径工具用 `paths_from` 逐路径求值聚合
- **非交互 fail-closed**：管道 / CI 下无法弹确认时，所有 `ask` 一律拒绝而非挂起；改动确认流程时保持此语义
- **输出截断**：工具返回超过 `MAX_TOOL_OUTPUT`（默认 2 万字符）时保留头尾省略中间，防止撑爆上下文

### 新增工具的流程

1. 在 `tools/` 下新建文件，用 `@register` 声明 schema（`tools/__init__.py` 无需修改，注册表自动发现）
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

- Python 3.9 兼容：不用 `match` 语句、不用 `X | Y` 类型联合写法、不用仅 3.10+ 的标准库特性
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
