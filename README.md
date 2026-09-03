# SmithCode

终端 AI 编程助手：让大模型调用工具，帮你读写文件、执行命令、完成编程任务。

你用自然语言描述任务，SmithCode 自主规划步骤、调用工具、根据结果继续推理，直到任务完成——全程流式输出，敏感操作逐个向你确认。

> **Harbormaster:** Hold up there, you. It's a shilling to tie up your boat at the dock... and I shall need to know your name.
>
> **Jack Sparrow:** What do you say to three shillings and we forget the name?
>
> **Harbormaster:** *Welcome to Port Royal, Mr. Smith.*

## 功能特性

### 智能体

- **9 个内置工具**：读 / 写 / 精确编辑文件、apply_patch 批量原子改文件、列目录、glob 文件名搜索、grep 内容搜索、执行 shell 命令（60 秒超时）、任务中途向用户提问（ask_user）
- **自主多步执行**：模型自主决定调用哪些工具、调用几次，直到完成任务；可配置单次任务最大迭代轮数
- **流式输出**：回复与思考内容实时逐字显示，工具调用打印一行短摘要（如 `read src/agent.py`）

### 上下文管理

- **占用可视化**：`/context` 按角色分桶查看当前上下文的 token 占用与压缩阈值距离
- **自动摘要压缩**：上下文越过阈值时自动把早期历史压缩为结构化摘要（任务目标、关键决策、已完成、下一步），保留近期对话，任务不中断
- **手动压缩**：`/compact` 随时主动释放上下文空间
- **溢出自愈**：服务商返回上下文超限错误时，自动压缩后重试，无需人工干预
- **用量统计**：`/usage` 查看 token 消耗（按"应用启动以来 / 当前会话"两个口径），每轮任务结束显示本轮用量速览

### 权限与安全

- **三级权限规则**：`allow` / `ask` / `deny`，按工具与参数通配符匹配，通过工作区 `smithcode.json` 自定义
- **保护路径**：`.env` 禁止读写、`.git` 目录只读，防止密钥泄露与仓库破坏
- **工作区沙箱**：文件操作默认限制在工作区内，越界访问需逐次确认
- **非交互安全**：管道 / CI 环境下无法弹确认时，所有需确认的操作一律拒绝（fail-closed），不会崩溃

### 使用体验

- **会话管理**：多轮对话、`/new` 开新会话、`/save` 保存会话记录
- **多行输入**：粘贴多行文本自动合并为一条消息
- **跨平台**：Windows / Linux / macOS，自动适配系统编码与 shell 风格（Windows 下提醒模型用 cmd 语法）
- **模型无关**：任何 OpenAI 兼容接口均可接入（DeepSeek、通义、Kimi 等）

## 快速开始

### 1. 安装

要求 Python >= 3.9。

```bash
pip install -e .
```

### 2. 配置

复制 `.env.example` 为 `.env`，填入密钥：

```dotenv
OPENAI_API_KEY=你的API密钥
OPENAI_API_BASE=https://api.deepseek.com/v1
OPENAI_MODEL=deepseek-chat
```

可选配置（同样写在 `.env`）：

| 变量 | 默认 | 说明 |
| ---- | ---- | ---- |
| `ROOT` | 当前目录 | 工作区根目录 |
| `CONTEXT_BUDGET` | 65536 | 上下文预算（token），建议设为模型窗口大小 |
| `COMPACT_TRIGGER` | 0.8 | 占预算的比例，越过即触发自动压缩 |
| `COMPACT_KEEP_TOKENS` | 15000 | 压缩时尾部原样保留的 token 数 |

### 3. 运行

```bash
smithcode                        # 交互模式（REPL，多轮对话）
smithcode 帮我写个斐波那契函数    # 单次任务模式，完成即退出
python -m smithcode              # 等价的另一种启动方式
```

## 使用方法

### 交互模式命令

| 命令 | 说明 |
| ---- | ---- |
| `/help` | 显示帮助 |
| `/new` | 开启新会话 |
| `/save` | 保存会话记录到 `sessions/` 目录 |
| `/usage` | 查看 token 用量统计 |
| `/context` | 查看上下文占用分布与压缩次数 |
| `/compact` | 手动压缩上下文 |
| `/exit` | 退出程序 |

### 命令行参数

| 参数 | 说明 |
| ---- | ---- |
| `task` | 一次性任务描述；留空则进入交互模式 |
| `-w, --workspace` | 指定工作区目录（默认当前目录） |
| `--add DIR` | 追加授权目录（可重复传入），跨项目访问用 |
| `-m, --model` | 指定模型名（覆盖 `.env` 配置） |
| `-y, --yes` | 自动批准所有确认（deny 规则依然生效），慎用 |
| `--max-iterations N` | 单次任务最大迭代轮数（默认 30） |
| `-V, --version` | 显示版本号 |

### 权限确认

默认情况下：读文件、列目录自动放行；写文件、编辑、执行命令需要你确认：

```
⚠️  Agent 请求执行: run_command
   模式: git push origin main
   允许? [y]本次 / [n]拒绝 / [a]总是允许该模式:
```

选 `a` 后该模式在本会话内静默放行，`/new` 或退出后清零。

### 自定义权限规则（smithcode.json）

在工作区根目录创建 `smithcode.json`。动作支持 `allow` / `ask` / `deny`，通配符匹配（文件工具匹配路径、`run_command` 匹配命令串），**写在前面的先生效，精确规则请放在宽泛规则之后**：

```json
{
  "permissions": {
    "read_file": "allow",
    "write_file": {"*": "ask", "*.env": "deny"},
    "run_command": {
      "*": "ask",
      "git *": "allow",
      "rm -rf*": "deny"
    }
  },
  "tool_display": "summary"
}
```

上例含义：文件读取放行；写文件需确认、写 `.env` 直接拒绝；git 命令放行、`rm -rf` 直接拒绝、其余命令需确认。

`tool_display` 控制工具调用的终端展示粒度：`summary`（默认）只显示短摘要行，`detail` 追加结果内容（前 500 字符）。

### 典型用法

```bash
# 修 bug：在当前项目里描述现象即可
smithcode 运行 pytest 里有 3 个失败，帮我修掉

# 跨项目操作：主工作区之外再授权一个目录
smithcode --add ../frontend 重构前端里所有调 /api/v1 的地方，改成 /api/v2

# CI / 脚本中无人值守运行（需确认的操作会被拒绝而非挂起）
smithcode -y 跑一遍测试并总结失败原因
```

## 安全说明

- API Key 仅从 `.env` 或环境变量加载，不会被提交到仓库
- 所有需确认的操作在非交互环境（管道 / CI）下一律拒绝，不挂起、不崩溃
- `-y` 跳过所有 `ask` 确认（含工作区外路径访问），但显式 `deny` 规则依然生效——仅在信任任务时使用
- shell 命令 60 秒超时；LLM 请求 120 秒超时，限流 / 断网自动重试
- 单次工具输出超长时自动头尾截断，防止撑爆上下文

## 开发

```bash
pip install -e ".[dev]"   # 安装测试与 lint 工具
pytest                    # 运行测试（不依赖真实 API）
ruff check src tests      # 代码检查
```

## 文档

- [架构说明](docs/architecture.md)：模块划分、安全边界设计、如何新增一个工具
- [更新日志](CHANGELOG.md)
