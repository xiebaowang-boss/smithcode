"""对话区消息数据模型：语义类型 + 级别。

Textual 无关的纯数据（可单测）：所有写入对话区的内容先构造成 ``ChatItem``，
再由 ``ChatView.apply`` 统一挂载 / 更新。视觉（缩进、着色、图标、间距）由
渲染层与集中 CSS 决定，生产者只表达语义 —— 这样新增消息类型不需要再散落
地写打印代码，格式也不会各处漂移。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from rich.text import Text


class Level(str, Enum):
    """消息级别：跨渲染后端统一的语义色。"""

    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    RETRY = "retry"


LEVEL_STYLE = {
    Level.INFO: "#808080",
    Level.SUCCESS: "#23d18b",
    Level.WARNING: "#e0af68",
    Level.ERROR: "#f7768e",
    Level.RETRY: "#7aa2f7",
}

# 固定 1 格的级别图标：所有通知正文的左起点因此恒定（不随级别漂移）
LEVEL_MARK = {
    Level.INFO: "·",
    Level.SUCCESS: "✓",
    Level.WARNING: "!",
    Level.ERROR: "✗",
    Level.RETRY: "↻",
}

# 兼容命令层的旧 style 字符串 → 级别（命令零改动即可着色）
_STYLE_LEVELS = {
    "red": Level.ERROR,
    "yellow": Level.WARNING,
    "green": Level.SUCCESS,
    "success": Level.SUCCESS,
    "error": Level.ERROR,
    "warning": Level.WARNING,
    "retry": Level.RETRY,
}


def coerce_level(level) -> Level:
    """把字符串 / Level 归一到 Level；未知值回退 INFO。"""
    if isinstance(level, Level):
        return level
    return _STYLE_LEVELS.get(str(level).lower(), Level.INFO)


def level_from_style(style: str | None) -> Level:
    """命令 / 旧调用点的 style 字符串 → 级别（映射集中在此一处）。"""
    return coerce_level(style or Level.INFO)


class ChatItem:
    """对话区消息标记基类：只用于 ``isinstance`` 分派与类型标注。"""


# ---------- 会话内容 ----------


@dataclass
class User(ChatItem):
    text: str


@dataclass
class Assistant(ChatItem):
    """已定稿的助手正文（历史回放等静态整段）。"""

    text: str


@dataclass
class Welcome(ChatItem):
    text: Text


@dataclass
class Notice(ChatItem):
    """系统通知（信息 / 警告 / 错误）：单行，级别只改颜色与图标。"""

    text: str
    level: Level = Level.INFO


@dataclass
class Block(ChatItem):
    """多行文本块（帮助 / 计划 / 列表）：逐行按级别着色，可含 ANSI。"""

    text: str
    level: Level = Level.INFO


@dataclass
class Footer(ChatItem):
    """轮次元数据页脚：▣ 模型 · 思考强度 · 用时。"""

    model: str
    effort: str
    elapsed: str
    status: str | None = None


# ---------- 助手流（增量事件） ----------


@dataclass
class StreamDelta(ChatItem):
    """流式增量：kind 为 content（正文）或 reasoning（思考）。"""

    kind: str
    text: str


@dataclass
class StreamEnd(ChatItem):
    """一段流式输出结束。"""



# ---------- 思考折叠块（生命周期事件） ----------


@dataclass
class ThinkingStart(ChatItem):
    """思考开始（挂载折叠块）。"""



@dataclass
class ThinkingDelta(ChatItem):
    text: str


@dataclass
class ThinkingEnd(ChatItem):
    """思考结束（停转轮、定格耗时）。"""



# ---------- 工具调用折叠块（生命周期事件） ----------


@dataclass
class ToolStart(ChatItem):
    tool_id: int
    summary: str
    display: str = "inline"
    name: str = ""
    icon: str = ""
    running_label: str = ""


@dataclass
class ToolPreview(ChatItem):
    tool_id: int | None
    detail: str


@dataclass
class ToolResult(ChatItem):
    tool_id: int | None
    result: str
    expand: bool = False
    is_error: bool = False
