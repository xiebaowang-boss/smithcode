"""会话持久化子系统：JSONL 转录的写路径与项目级查询。

对外只暴露本模块的公共 API；其余实现模块（paths / format / model / store）
不直接被外部导入。设计文档见 docs/session-architecture.md。
"""
from .format import CRASH_PLACEHOLDER, FORMAT_VERSION
from .model import LoadedSession, SessionSummary
from .store import (
    SessionStore,
    StoreError,
    delete,
    find,
    find_last,
    import_json,
    list_sessions,
    load,
    rename,
    summary_from_path,
    sweep,
)
from .title import build_title_request, clean_title, should_generate

__all__ = [
    "CRASH_PLACEHOLDER",
    "FORMAT_VERSION",
    "LoadedSession",
    "SessionStore",
    "SessionSummary",
    "StoreError",
    "build_title_request",
    "clean_title",
    "delete",
    "find",
    "find_last",
    "import_json",
    "list_sessions",
    "load",
    "rename",
    "should_generate",
    "summary_from_path",
    "sweep",
]
