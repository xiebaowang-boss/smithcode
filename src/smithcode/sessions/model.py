"""会话数据类：列举摘要（SessionSummary）与加载结果（LoadedSession）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SessionSummary:
    """列表 / picker 用的一条会话摘要（由文件头尾派生，不读全文）。"""

    id: str
    path: Path
    cwd: str = ""
    created: float = 0.0
    updated: float = 0.0
    model: str = ""
    title: str = ""
    title_source: str = ""
    first_prompt: str = ""
    oneshot: bool = False
    size: int = 0

    @property
    def short_id(self) -> str:
        return self.id[:8]

    @property
    def display_name(self) -> str:
        """展示名：标题优先，其次首轮 prompt 截断（不落冗余字段）。"""
        return self.title or self.first_prompt or "（无标题）"


@dataclass
class LoadedSession:
    """从转录恢复出的完整会话数据。"""

    path: Path
    meta: dict
    messages: list = field(default_factory=list)
    state: dict | None = None
    title: str = ""
    title_source: str = ""
    compact_count: int = 0
    model: str = ""  # 最后一条 model 记录（无记录时回退 meta 的创建时模型）
    effort: str = ""
    bad_lines: int = 0
    repair: str = "none"  # none / appended / truncated（崩溃修复结果）
    repaired: list = field(default_factory=list)  # appended 时补的占位消息

    @property
    def id(self) -> str:
        return str(self.meta.get("id") or self.path.stem)

    @property
    def cwd(self) -> str:
        return str(self.meta.get("cwd") or "")

    @property
    def oneshot(self) -> bool:
        return bool(self.meta.get("oneshot"))
