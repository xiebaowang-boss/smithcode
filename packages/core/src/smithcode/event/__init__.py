"""事件层（L0）：信封、目录、总线、事件流。

**这一层是核心与前端之间唯一的契约面**，也是「唯一真相源」的搬运层：

- 事件类只在 `catalog.py` 定义并声明（类型名 / 版本 / 持久性 / 聚合根）；
- 发布只有 `publish()` 一个口，订阅只有 `Bus.subscribe*` 一个口；
- 每条事件都带信封（id / created / session_id），会话标识由总线注入；
- 本层不 import `smithcode` 的其他任何包——上层可以是核心、会话持久化或前端。

用法（核心侧）：

    from ..event import catalog, publish

    publish(catalog.Notice("已连接", level="success"))

用法（前端侧）：

    bus.subscribe(frontend.on_event)        # 全部事件
    bus.subscribe_type("session.notice", fn)  # 只订某类
"""

from __future__ import annotations

from .bus import Bus, activate, current, publish, reset
from .envelope import Envelope, new_id, payload_to_dict, wrap
from .stream import EventStream, agent_event_stream

__all__ = [
    "Bus",
    "Envelope",
    "EventStream",
    "activate",
    "agent_event_stream",
    "current",
    "new_id",
    "payload_to_dict",
    "publish",
    "reset",
    "wrap",
]
