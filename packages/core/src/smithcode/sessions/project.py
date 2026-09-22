"""会话投影：**事件 → 会话视图**（纯折叠，可直接单测与重放）。

事件是事实，视图是折叠出来的结果。这份折叠规则只有一个入口 `apply`，所以
"在线跑出来的视图"与"重放日志得到的视图"必然一致——它们走的是同一个函数
（`tests/test_event_sourcing.py` 的等价性断言就在锁这一点）。

折叠哪些事件：

| 事件 | 对视图的作用 |
|---|---|
| `MessageEnd` | 消息进入历史（system / user / assistant / tool 都走它） |
| `HistoryCompacted` | 消息换成 `summary + tail`（此后重放的新基线） |
| `TitleChanged` | 标题与来源 |
| `ModelSelected` | 本轮模型与思考强度 |
| `UsageChanged` | 会话用量账本（累计口径） |
| `StepEnded` | 记下该步的 `usage.input_tokens`（= 那次请求的**真实 prompt_tokens**，恢复时的上下文锚点） |
| `SessionCheckpoint` | 会话状态快照（goal / plan / skills 等，见 `SessionCheckpoint` 的说明） |
| `SessionCreated` | 创建时间等元数据 |

其余事件（流式增量、通知、工具行程、忙碌态）**不改视图**：它们要么是过程，
要么是呈现。折叠函数对不认识的事件静默跳过——新增事件不会让重放炸掉。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..event.catalog import (
    HistoryCompacted,
    MessageEnd,
    ModelSelected,
    SessionCheckpoint,
    SessionCreated,
    StepEnded,
    TitleChanged,
    UsageChanged,
)
from ..event.envelope import Envelope


@dataclass
class SessionView:
    """折叠结果：会话的可序列化视图。"""

    messages: list[dict] = field(default_factory=list)
    title: str = ""
    title_source: str = ""
    model: str = ""
    effort: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    #: 会话状态快照（goal / plan / skills 的 `snapshot()` 结果），来自检查点事件
    state: dict[str, Any] = field(default_factory=dict)
    #: 最后一条 `SessionCreated` 的载荷（cwd / app / oneshot 等元数据）
    meta: dict[str, Any] = field(default_factory=dict)
    #: 折叠到的事件条数（重放进度，也用于断言）
    applied: int = 0
    #: 压缩次数（`/context` 与恢复报告要它）
    compactions: int = 0
    #: 最后一次模型请求的真实 prompt_tokens（上下文估算的锚点；0 = 日志里没有）
    last_input_tokens: int = 0


def apply(view: SessionView, env: Envelope) -> SessionView:
    """把一个事件折进视图（返回同一个 view，便于链式使用）。"""
    view.applied += 1
    data = env.data
    if isinstance(data, MessageEnd):
        view.messages.append(dict(data.message))
    elif isinstance(data, HistoryCompacted):
        # 压缩：system 段保留（提示词规则不随压缩丢失），其余换成 summary + tail。
        # **就地赋值**（`[:]`）而不是重新绑定：视图的消息列表与 `Session` 共享同一个
        # 对象（见 journal.py），重绑定会让会话看不到这次替换。
        head = view.messages[:1] if view.messages and view.messages[0].get("role") == "system" else []
        view.messages[:] = head + [dict(data.summary), *(dict(m) for m in data.tail)]
        view.compactions += 1
    elif isinstance(data, TitleChanged):
        view.title = data.title
        if data.source:
            view.title_source = data.source
        if not data.title:
            view.title_source = ""  # 清空标题时来源一并清掉（与 /new 语义一致）
    elif isinstance(data, ModelSelected):
        view.model = data.model
        view.effort = data.effort
    elif isinstance(data, UsageChanged):
        view.usage = {
            "calls": data.calls,
            "prompt_tokens": data.total_input,
            "completion_tokens": data.total_output,
            "total_tokens": data.total_tokens,
            "cached_tokens": data.cached_tokens,
        }
    elif isinstance(data, StepEnded):
        # 只认带真实用量的步：0 表示这次调用没回报用量（别把锚点冲成 0）
        if data.usage.input_tokens:
            view.last_input_tokens = data.usage.input_tokens
    elif isinstance(data, SessionCheckpoint):
        if data.state:
            view.state = dict(data.state)
    elif isinstance(data, SessionCreated):
        view.meta = {
            "created": env.created,
            "cwd": data.cwd,
            "model": data.model,
            "effort": data.effort,
            "app": data.app,
            "oneshot": data.oneshot,
        }
        if data.model:
            view.model = data.model
            view.effort = data.effort
    return view


def fold(events) -> SessionView:
    """按顺序折叠一批事件（重放与在线折叠共用）。"""
    view = SessionView()
    for env in events:
        apply(view, env)
    return view
