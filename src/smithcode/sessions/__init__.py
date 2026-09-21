"""会话持久化子系统：**事件日志**的写路径、投影与项目级查询。

对外只暴露本模块的公共 API；其余实现模块（paths / format / model / store /
project / journal）不直接被外部导入。

日志只有事件（见 `format.py` 的模块说明），会话状态由折叠得出（`project.py`），
落盘与折叠的装配在 `journal.py`。
"""
from .format import CRASH_PLACEHOLDER, LOG_VERSION
from .model import LoadedSession, SessionSummary
from .store import (
    SessionStore,
    StoreError,
    delete,
    find,
    find_last,
    list_sessions,
    load,
    rename,
    summary_from_path,
    sweep,
)
from .title import build_title_request, clean_title, should_generate

__all__ = [
    "CRASH_PLACEHOLDER",
    "LOG_VERSION",
    "LoadedSession",
    "SessionStore",
    "SessionSummary",
    "StoreError",
    "build_title_request",
    "clean_title",
    "delete",
    "find",
    "find_last",
    "list_sessions",
    "load",
    "rename",
    "should_generate",
    "summary_from_path",
    "sweep",
]
