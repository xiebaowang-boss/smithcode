"""协作式取消的对外入口（兼容别名层）。

实现已移入 `agent/`：

- `agent/signal.py`：`AbortSignal` + `Cancelled` + ContextVar 传播
  （`current_token` / `activate_token`）；
- `agent/result.py`：`RunResult`。

**为什么要搬**：本模块将长期作为「包外调用点的入口」，而 `agent/` 内部
（`agent.py` / `events.py`）也需要这些类型。若实现留在 `cancel.py`，就会形成
`cancel → agent.signal`（本模块转出）与 `agent.* → cancel`（内部取用）的导入环：
`agent` 包初始化时会先执行 `agent/agent.py`，此时 `cancel` 尚未定义出任何名字。
把实现移进 `agent/`、由本模块单向转出即可消除环，且包外调用点一行不改。

**过渡别名**：`CancellationToken` 是 `AbortSignal` 的旧名（阶段 3 起包外调用点
统一切到 `AbortSignal`，届时删除这个别名）。
"""

from __future__ import annotations

from .agent.result import RunResult
from .agent.signal import (
    DEFAULT_ABORT_REASON,
    AbortSignal,
    Cancelled,
    activate_token,
    current_token,
)

# 旧名：既有调用点与测试写的是 CancellationToken，语义与 AbortSignal 完全一致
CancellationToken = AbortSignal

__all__ = [
    "DEFAULT_ABORT_REASON",
    "AbortSignal",
    "CancellationToken",
    "Cancelled",
    "RunResult",
    "activate_token",
    "current_token",
]
