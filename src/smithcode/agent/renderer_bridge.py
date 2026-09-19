"""AgentEvent → 既有 Renderer 调用的迁移桥。

**存在理由**：阶段 3 起核心只发 `AgentEvent`，而 `ConsoleRenderer` 要到阶段 5
才改成订阅者。中间这段靠本桥把事件翻译回既有的 Renderer 方法调用，前端
（REPL / TUI）**一行不改**就能继续工作——这是让「异步循环」与「前端解耦」
能分两阶段落地的关键。

**生命周期**：阶段 5 前端改订阅后删除。

映射是**全量覆盖**的（见 `RENDERER_INPUT_METHODS` 与
`tests/agent/test_renderer_bridge.py` 的完备性断言）：Renderer 的每个输出方法
都能被某个事件触达。新增 Renderer 输出方法却没有对应事件时，完备性断言会失败。
"""

from __future__ import annotations

from ..renderer import Renderer
from .events import (
    AgentEnd,
    AgentEvent,
    MessageEnd,
    MessageStart,
    MessageUpdate,
    Notice,
    PlanUpdate,
    QueueChanged,
    TitleChanged,
    ToolEnd,
    ToolPreview,
    ToolStart,
    TurnEnd,
    TurnStart,
)
from .interactions import PromptFinished, PromptStarted
from .status import StatusChanged, StatusCleared

# 桥不消费的 Renderer 方法：这些是**输入**方法（向用户提问 / 确认），不是事件
# 落点——方向相反，由交互端口在需要用户决策时调用。完备性断言据此判定
# 「Renderer 的公开方法 = 事件覆盖的 + 这里的输入方法」。
RENDERER_INPUT_METHODS = frozenset({
    "ask_text",
    "ask_choice",
    "ask_form",
    "confirm_choice",
})

# 刻意不映射到 Renderer 的事件类型：
# - MessageStart：旧后端没有「消息开始」概念（正文靠 stream 增量、工具靠 tool_call）；
# - QueueChanged：队列面板是阶段 5 新增的 UI 部件，既有 Renderer 没有对应方法；
# - AgentEnd：流终结值，由 `EventStream.result()` 交付，不是给前端的渲染事件。
# 非 retry 的 StatusChanged / StatusCleared 也暂为无操作（kind="working" 等
# 由阶段 4 接入，替换 `turn_started` / `turn_finished`）。
UNMAPPED_EVENTS: tuple[type, ...] = (MessageStart, QueueChanged, AgentEnd)

# Notice 级别 → Renderer 方法**名**。必须按名字在实例上取方法，不能存基类的
# 未绑定函数：基类的 `success` / `warn` 默认实现是转发到 `self.info`，直接调用
# 未绑定版本会绕过子类覆写（TUI / 录制的实现收不到调用）。
_NOTICE_METHOD_NAMES: dict[str, str] = {
    "info": "info",
    "success": "success",
    "warning": "warn",
    "error": "error",
}


class RendererBridge:
    """把一个 `AgentEvent` 翻译成一次既有的 `Renderer` 调用。

    有状态：`ToolStart` 调用 `Renderer.tool_call` 拿到的**旧式整数 id** 要与模型
    给出的 `tool_call_id` 关联，后续 `ToolPreview` / `ToolEnd` 才能配对到同一行。
    配对在 `ToolEnd` 时清理，不会无限增长。
    """

    def __init__(self, renderer: Renderer) -> None:
        self._renderer = renderer
        self._legacy_ids: dict[str | None, int] = {}

    def emit(self, event: AgentEvent) -> None:
        renderer = self._renderer
        match event:
            case MessageUpdate(kind=kind, delta=delta):
                renderer.stream(kind, delta)
            case MessageEnd():
                renderer.stream_done()
            case ToolStart(tool_call_id=tool_call_id, name=name, line=line, display=display):
                self._legacy_ids[tool_call_id] = renderer.tool_call(line, display, name)
            case ToolPreview(tool_call_id=tool_call_id, detail=detail):
                renderer.tool_preview(self._legacy_ids.get(tool_call_id), detail)
            case ToolEnd(tool_call_id=tool_call_id, result=result, expand=expand):
                renderer.tool_result(
                    result, self._legacy_ids.pop(tool_call_id, None), expand=expand
                )
            case PlanUpdate(summary=summary, rendered=rendered, created=created,
                            tool_call_id=tool_call_id):
                renderer.plan(
                    summary, rendered, created=created,
                    tool_id=self._legacy_ids.get(tool_call_id),
                )
            case Notice(text=text, level=level):
                getattr(renderer, _NOTICE_METHOD_NAMES[level])(text)
            case TitleChanged(title=title):
                renderer.title_changed(title)
            case TurnStart():
                renderer.turn_started()
            case TurnEnd(status=status):
                renderer.turn_finished(status)
            case PromptStarted():
                renderer.turn_waiting_started()
            case PromptFinished():
                renderer.turn_waiting_finished()
            case StatusChanged(kind="retry", owner=owner, payload=state):
                renderer.retry_started(state, owner)
            case StatusCleared(kind="retry", owner=owner):
                renderer.retry_finished(owner)
            case _:
                # 见 UNMAPPED_EVENTS 的说明：这些事件没有对应的 Renderer 调用。
                return
