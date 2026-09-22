"""按服务器配置构造连接：把传输类型与连接实现的对应关系收在一处。

当前阶段（M1）仅 stdio；Streamable HTTP / SSE 在后续里程碑加入，届时本模块
按 `cfg.type` 分派即可，`service` 无需改动。
"""
from __future__ import annotations

from .connection import SdkConnection


def create_connection(cfg, resolved, runtime, *, on_tools_changed=None, on_closed=None,
                      interactive: bool = False):
    """构造一个 MCP 连接（未启动）；interactive 控制 OAuth 是否允许弹浏览器。"""
    return SdkConnection(
        cfg, resolved, runtime,
        on_tools_changed=on_tools_changed,
        on_closed=on_closed,
        interactive=interactive,
    )
