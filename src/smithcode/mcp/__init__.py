"""MCP（Model Context Protocol）子系统：外部工具服务器的接入。

对外分层（设计稿见 docs/architecture.md「MCP」节）：

- `config`：双作用域配置加载/写入（用户 config.toml + 项目 .smithcode/mcp.json），
  启停状态是服务器条目的 `enabled` 字段（写在定义它的文件里）；
- `secrets`：`${VAR}` 引用展开（进程环境 > 凭据库）、凭据存储、输出脱敏；
- `auth`：OAuth token 持久化（`mcp_auth.json`）与浏览器回调（仅交互授权）；
- `runtime` / `connection` / `factory`：基于官方 `mcp` SDK 的客户端——共享 loop
  线程 + 同步门面，按传输类型（stdio / Streamable HTTP / SSE）构造；
- `catalog`：工具命名、schema 与结果映射；
- `service`：会话级连接管理与工具注册（agent.py 生命周期挂钩）。

公共 API 从本模块汇总导出，外部只依赖这里，不直接摸内部模块。
"""
from __future__ import annotations

from .catalog import ToolSpec, build_spec, format_result, normalize_schema
from .config import (
    DEFAULT_TIMEOUT,
    LoadResult,
    ServerConfig,
    load_servers,
    project_config_path,
    remove_project_server,
    remove_user_server,
    set_enabled,
    write_project_server,
    write_user_server,
)
from .errors import McpConfigError, McpError
from .secrets import Redactor, redactor, resolve, store_secret
from .service import McpService, ServerStatus

__all__ = [
    "DEFAULT_TIMEOUT",
    "LoadResult",
    "McpConfigError",
    "McpError",
    "McpService",
    "Redactor",
    "ServerConfig",
    "ServerStatus",
    "ToolSpec",
    "build_spec",
    "format_result",
    "load_servers",
    "normalize_schema",
    "project_config_path",
    "redactor",
    "remove_project_server",
    "remove_user_server",
    "resolve",
    "set_enabled",
    "store_secret",
    "write_project_server",
    "write_user_server",
]
