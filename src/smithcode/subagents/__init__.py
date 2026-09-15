"""子代理（Subagents）子系统：类型目录 + 执行编排。

- 定义：`SubAgentSpec` / 内置 explore、general / 用户与项目文件发现（`defs`）；
- 执行：`run_task` 由 Agent 的 task 工具调度器调用（`runner`）；
- 生命周期：`Agent.start()` 调用 `refresh()` 重新发现并同步 task 工具 schema。

子代理在独立会话与 ContextVar 渲染作用域中运行，共享父级的 LLM 客户端、
权限引擎与 MCP 服务；深度上限 1（子代理不能再派子代理）。
"""
from __future__ import annotations

from .defs import (  # noqa: F401 公共 API
    FORBIDDEN_TOOLS,
    SubAgentSpec,
    all_specs,
    diagnostics,
    get_spec,
    is_loaded,
    refresh,
    reset,
    reset_session_trust,
)
from .runner import format_report, run_task  # noqa: F401
