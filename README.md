# CodeAgent

终端 AI 编程助手：一个教学级的 mini coding agent。让大模型通过调用工具，帮你读写文件、执行命令、完成编程任务。

CodeAgent 采用 **Agent 循环（Agentic Loop）** 架构：模型收到任务后自主决定调用哪些工具，工具的执行结果会回传给模型继续推理，如此往复直到任务完成。

## 功能特性

- **Agent 循环**：模型自主规划并多次调用工具，直到完成任务，支持配置最大迭代轮数
- **工具调用**：内置 5 个工具——读文件、写文件、精确编辑文件、列目录、执行 shell 命令
- **权限控制**：读文件 / 列目录自动放行；写文件、执行命令等敏感操作需逐个确认，或选择"本次会话总是允许"
- **沙箱约束**：所有文件操作限制在工作区内，路径越界直接拒绝；shell 命令带超时保护
- **会话管理**：多轮对话上下文，支持开启新会话、保存会话记录为 JSON
- **跨平台**：Windows / Linux / macOS 均可运行，自动适配系统编码与 shell 命令风格

## 代码结构

```
codeagent/
├── main.py                 # 程序入口，处理 Windows 控制台 UTF-8 编码后启动 CLI
├── pyproject.toml          # 项目元信息与依赖（openai、python-dotenv）
├── .env                    # 环境变量（API Key 等，不入库）
└── codeagent/              # 核心包
    ├── __init__.py         # 版本号
    ├── __main__.py         # 支持 python -m codeagent 方式启动
    ├── cli.py              # 命令行入口：参数解析、交互式 REPL、单次任务模式
    ├── agent.py            # Agent 循环：调模型 → 执行工具 → 回传结果，直至任务完成
    ├── llm.py              # LLM 客户端封装（OpenAI 兼容接口）
    ├── config.py           # 配置加载：从 .env 读取 API Key、模型名、工作区等
    ├── permission.py       # 权限控制：敏感工具调用前询问用户批准
    ├── session.py          # 会话管理：系统提示词、消息历史、会话保存与重置
    └── tools/              # 工具实现
        ├── __init__.py     # 汇总所有工具的 schema 与实现
        ├── files.py        # read_file / write_file / edit_file / list_dir（含路径越界检查）
        └── shell.py        # run_command（带超时保护）
```

核心流程一句话概括：`cli.py` 接收用户输入 → `agent.py` 把消息交给 `llm.py` 调用模型 → 模型返回工具调用 → `agent.py` 经 `permission.py` 确认后执行 `tools/` 中的工具 → 结果回传模型循环推理 → 最终回复打印给用户。

## 快速开始

### 1. 安装依赖

要求 Python >= 3.9。

```bash
pip install -e .
```

### 2. 配置环境变量

在项目根目录创建 `.env` 文件（参照下面的模板，填入你自己的信息）：

```dotenv
OPENAI_API_KEY=你的API密钥
OPENAI_API_BASE=https://api.example.com/v1
OPENAI_MODEL=你的模型名
```

> CodeAgent 使用 OpenAI 兼容接口，因此任何兼容 OpenAI 协议的服务（DeepSeek、通义、Kimi 等）都可以接入，只需修改 `OPENAI_API_BASE` 和 `OPENAI_MODEL`。

### 3. 运行

**交互模式**（进入 REPL，多轮对话）：

```bash
python main.py
```

**单次任务模式**（执行完自动退出）：

```bash
python main.py 帮我写一个斐波那契函数并测试
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

## 安全说明

- API Key 仅通过 `.env` 或环境变量加载，不会被提交到仓库
- 所有文件操作限制在工作区内，访问工作区之外的路径会被拒绝
- shell 命令默认 60 秒超时（可通过 `config.py` 中的 `COMMAND_TIMEOUT` 调整）
- 请谨慎使用 `-y` 参数，它允许 Agent 未经确认执行任意命令
