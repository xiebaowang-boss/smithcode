# 架构说明

CodeAgent 是一个教学级的 mini coding agent，核心是 **Agent 循环（Agentic Loop）**。

## 核心循环

```
用户输入
   │
   ▼
┌─────────────────────────────────────────┐
│  Agent.run()                            │
│                                         │
│  ┌────────┐    消息列表    ┌─────────┐  │
│  │  LLM   │──────────────▶│ Session │  │
│  │(llm.py)│◀──────────────│(session)│  │
│  └────┬───┘               └─────────┘  │
│       │ 返回 tool_calls？               │
│       │ 是                              │
│       ▼                                │
│  ┌─────────────┐   ┌──────────────┐   │
│  │ Permission  │──▶│ tools/*.py   │   │
│  │ (权限确认)   │   │ (执行工具)    │   │
│  └─────────────┘   └──────┬───────┘   │
│                           │ 结果回传    │
│                           ▼            │
│                 下一轮循环（受 max_iterations 限制）
└─────────────────────────────────────────┘
   │ 否（纯文本回复）
   ▼
打印给用户，结束
```

1. `cli.py` 接收用户输入，交给 `Agent.run()`。
2. `Agent` 把消息列表（含系统提示词）发给 LLM。
3. 模型要么返回纯文本（任务完成，循环结束），要么返回工具调用。
4. 工具调用先经 `Permission` 确认（读文件/列目录免确认），再由 `tools/` 执行。
5. 执行结果以 `role: tool` 消息回传给模型，进入下一轮循环。
6. 循环超过 `MAX_ITERATIONS` 次则强制终止，防止失控。

## 模块职责

| 模块 | 职责 |
| ---- | ---- |
| `cli.py` | 参数解析、交互式 REPL、单次任务模式 |
| `agent.py` | Agent 循环编排 |
| `llm.py` | OpenAI 兼容接口封装 |
| `prompts.py` | 系统提示词（行为规则） |
| `session.py` | 消息历史的增删存取 |
| `permission.py` | 敏感操作的用户确认 |
| `config.py` | `.env` 配置加载 |
| `tools/base.py` | 工具注册表（`@register` 装饰器） |
| `tools/files.py` | 文件读写，含路径越界检查 |
| `tools/shell.py` | 命令执行，含超时保护 |

## 安全边界

- **路径沙箱**：所有文件操作经 `_resolve()` 检查，解析后必须位于工作区内。
- **权限门控**：写文件 / 编辑 / 执行命令默认需要用户确认。
- **超时保护**：shell 命令默认 60 秒超时。
- **迭代上限**：默认 30 轮，防止 Agent 无限循环消耗 token。

## 如何新增一个工具

在 `tools/` 下新建文件，用 `@register` 声明 schema 即可，`tools/__init__.py` 无需修改：

```python
from .base import register

@register({
    "name": "search_code",
    "description": "在工作区内搜索代码片段",
    "parameters": {
        "type": "object",
        "properties": {"pattern": {"type": "string"}},
        "required": ["pattern"],
    },
})
def search_code(pattern: str) -> str:
    ...
```

若该工具属于敏感操作，还需把工具名加入 `permission.py` 的确认逻辑（`SAFE_TOOLS` 之外的工具默认都需要确认）。
