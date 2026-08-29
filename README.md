# CodeAgent

终端 AI 编程助手：一个教学级的 mini coding agent。让大模型通过调用工具，帮你读写文件、执行命令、完成编程任务。

CodeAgent 采用 **Agent 循环（Agentic Loop）** 架构：模型收到任务后自主决定调用哪些工具，工具的执行结果会回传给模型继续推理，如此往复直到任务完成。

## 功能特性

- **Agent 循环**：模型自主规划并多次调用工具，直到完成任务，支持配置最大迭代轮数
- **工具调用**：内置 5 个工具——读文件、写文件、精确编辑文件、列目录、执行 shell 命令；新增工具只需一个 `@register` 装饰器
- **权限控制**：读文件 / 列目录自动放行；写文件、执行命令等敏感操作需逐个确认，或选择"本次会话总是允许"
- **沙箱约束**：所有文件操作限制在工作区内，路径越界直接拒绝；shell 命令带超时保护
- **会话管理**：多轮对话上下文，支持开启新会话、保存会话记录为 JSON
- **跨平台**：Windows / Linux / macOS 均可运行，自动适配系统编码与 shell 命令风格

## 代码结构

采用 src 布局：`src/` 是源码，根目录是项目配置，测试与文档独立成目录。

```
code_agent/                        # 仓库根目录
├── src/
│   └── codeagent/                 # Python 主包（pip 安装的就是它）
│       ├── __init__.py            # 版本号
│       ├── __main__.py            # python -m codeagent 方式启动
│       ├── cli.py                 # 命令行解析、交互式 REPL、单次任务模式
│       ├── agent.py               # Agent 循环：调模型 → 执行工具 → 回传结果
│       ├── llm.py                 # LLM 客户端封装（OpenAI 兼容接口）
│       ├── config.py              # 配置加载：从 .env 读取密钥、模型名、工作区
│       ├── permission.py          # 权限控制：敏感工具调用前询问用户批准
│       ├── prompts.py             # 系统提示词（人设与行为规则）
│       ├── session.py             # 会话管理：消息历史、保存与重置
│       ├── tools/                 # 工具子系统
│       │   ├── base.py            # 工具注册表（@register 装饰器）
│       │   ├── files.py           # read_file / write_file / edit_file / list_dir
│       │   └── shell.py           # run_command（带超时保护）
│       └── utils/
│           └── terminal.py        # 终端 UTF-8 编码处理
├── tests/                         # pytest 测试，与源码模块一一对应
├── docs/
│   └── architecture.md            # 架构说明与"如何新增工具"指南
├── examples/                      # 示例任务
├── pyproject.toml                 # 项目元信息、依赖、构建配置
├── .env.example                   # 环境变量模板（复制为 .env 使用）
├── CHANGELOG.md                   # 版本变更记录
└── LICENSE                        # MIT
```

核心流程：`cli.py` 接收用户输入 → `agent.py` 把消息交给 `llm.py` 调用模型 → 模型返回工具调用 → 经 `permission.py` 确认后由 `tools/` 执行 → 结果回传模型循环推理 → 最终回复打印给用户。详见 [docs/architecture.md](docs/architecture.md)。

## 快速开始

### 1. 安装

要求 Python >= 3.9。

```bash
pip install -e .
```

### 2. 配置环境变量

复制 `.env.example` 为 `.env`，填入你自己的配置：

```bash
cp .env.example .env
```

```dotenv
OPENAI_API_KEY=你的API密钥
OPENAI_API_BASE=https://api.example.com/v1
OPENAI_MODEL=你的模型名
```

> CodeAgent 使用 OpenAI 兼容接口，因此任何兼容 OpenAI 协议的服务（DeepSeek、通义、Kimi 等）都可以接入，只需修改 `OPENAI_API_BASE` 和 `OPENAI_MODEL`。

### 3. 运行

```bash
codeagent                     # 交互模式（REPL，多轮对话）
python -m codeagent           # 等价的另一种启动方式
codeagent 帮我写个斐波那契函数  # 单次任务模式，执行完自动退出
```

## 使用方法

### 交互模式内置命令

| 命令   | 说明             |
| ------ | ---------------- |
| `/help` | 显示帮助         |
| `/new`  | 开启新会话       |
| `/save` | 保存会话记录到 `sessions/` 目录（JSON 格式） |
| `/exit` | 退出程序         |

### 命令行参数

```
usage: codeagent [-h] [-w WORKSPACE] [-m MODEL] [-y] [--max-iterations N] [-V] [task ...]
```

| 参数                 | 说明                                           |
| -------------------- | ---------------------------------------------- |
| `task`               | 一次性任务描述；留空则进入交互模式             |
| `-w, --workspace`    | 指定工作区目录（默认当前目录）                 |
| `-m, --model`        | 指定模型名（覆盖 `.env` 中的配置）             |
| `-y, --yes`          | 自动批准所有工具调用，不再逐个询问             |
| `--max-iterations N` | 单次任务最大迭代轮数（默认 30）                |
| `-V, --version`      | 显示版本号                                     |

### 权限确认

写文件、编辑文件、执行命令属于敏感操作，默认会弹出确认：

```
⚠️  Agent 请求执行: write_file
   允许? [y]是 / [n]否 / [a]本次会话总是允许:
```

- `y`：本次允许
- `n`：拒绝，Agent 会收到"用户拒绝了此操作"
- `a`：本次会话内该工具不再询问
- `-y` 参数可跳过全部确认（信任环境下使用）

## 开发

```bash
pip install -e ".[dev]"   # 安装测试与 lint 工具
pytest                    # 运行测试（不依赖真实 API）
ruff check src tests      # 代码检查
```

## 安全说明

- API Key 仅通过 `.env` 或环境变量加载，不会被提交到仓库
- 所有文件操作限制在工作区内，访问工作区之外的路径会被拒绝
- shell 命令默认 60 秒超时（可通过 `config.py` 中的 `COMMAND_TIMEOUT` 调整）
- 请谨慎使用 `-y` 参数，它允许 Agent 未经确认执行任意命令
