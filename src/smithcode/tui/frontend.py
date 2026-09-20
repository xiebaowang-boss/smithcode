"""TUI 前端：事件订阅者 + 询问端口。

与 `frontend/console.py` 的关系：同一个契约（订阅事件 + 实现 `Asker`）、同一批判定
逻辑，只有「呈现」不同——本类把事件投成 `UiAction` 交给 Textual 主线程，把询问
交给面板。

**线程不变量**：通知是即发即走的 `post_message`（线程安全，从循环线程或 worker
线程都可以）；询问是 `await` 一个 Future（面板挂在事件循环上，答案从面板回来），
**不再有 `call_from_thread` + `Event`**——提问方与面板在同一线程上，天然不会挂死。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from .. import config
from ..event.asks import AskAnswer, AskRequest
from ..event.catalog import (
    CompactionEnded,
    CompactionFailed,
    CompactionStarted,
    ExecutionFailed,
    ExecutionInterrupted,
    ExecutionStarted,
    ExecutionSucceeded,
    Idle,
    InboxCancelled,
    InboxCleared,
    InboxDelivered,
    InboxEnqueued,
    MessageEnd,
    MessageUpdate,
    Notice,
    PlanUpdate,
    StatusChanged,
    StatusCleared,
    StepEnded,
    StepStarted,
    TitleChanged,
    ToolEnd,
    ToolPreview,
    ToolStart,
    UsageChanged,
)
from ..event.envelope import Envelope
from .widgets import UiAction

if TYPE_CHECKING:
    from .app import SmithTUI


class TuiFrontend:
    """SmithTUI 的前端实现：唯一与 Agent 交互的一侧。"""

    def __init__(self, app: SmithTUI):
        self.app = app
        self._thinking: int | None = None  # 正在思考时累计的字符数
        self._closed = False  # 界面已收尾：此后的询问不再挂面板（走 fail-closed 兜底）

    # ----- 通知面：事件订阅者 -----

    def on_event(self, env: Envelope) -> None:
        """事件 → UiAction（主线程消费）。未列出的事件在 TUI 无额外表现。"""
        data = env.data
        match data:
            case MessageUpdate(kind="reasoning", delta=delta):
                self._thinking_tick(delta)
            case MessageUpdate(kind=kind, delta=delta):
                self._thinking_done()
                self._post("stream", kind, delta)
            case MessageEnd():
                self._thinking_done()
                self._post("stream_done")
            case ToolStart(tool_call_id=tool_call_id, line=line, display=display, name=name):
                # id 由模型给出（agent 侧生成），前端只做查表——不再自行编号
                self._post("tool_start", tool_call_id, line, display, name)
            case ToolPreview(tool_call_id=tool_call_id, detail=detail):
                self._post("tool_preview", tool_call_id, detail)
            case ToolEnd(tool_call_id=tool_call_id, result=result, is_error=is_error,
                         expand=expand):
                expanded = is_error or expand or config.load_tool_display() == "detail"
                self._post("tool_result", tool_call_id, result, expanded, is_error)
            case PlanUpdate(tool_call_id=tool_call_id, created=created,
                            rendered=rendered, titles=titles):
                # 两种渲染形态都由载荷带来（前端不读会话状态）；侧边栏始终刷新，
                # 聊天区仅新建清单时展示一次详情——复用 plan 工具块，可展开/收起、
                # 默认展开；后续每步更新不再往对话区重复打印进度
                self._post("plan_sidebar", titles)
                if created:
                    self._post("tool_result", tool_call_id, rendered, True, False)
            case Notice(text=text, level=level):
                self._post("notice", text, level)
            case TitleChanged(title=title):
                self._post("title", title)
            case StatusChanged(kind="retry", owner=owner, payload=state):
                self._post("retry_start", state, owner)
            case StatusCleared(kind="retry", owner=owner):
                self._post("retry_end", owner)
            case (
                ExecutionStarted() | ExecutionSucceeded() | ExecutionFailed()
                | ExecutionInterrupted() | StepStarted() | StepEnded() | Idle()
                | CompactionStarted() | CompactionEnded() | CompactionFailed()
            ):
                # 执行 / 步骤边界与压缩态：界面按内容、工具块与忙闲行呈现，
                # 这些事件本身不额外呈现。显式列出以便新增事件时被提醒。
                return
            case UsageChanged():
                # 用量变化：侧边栏据此刷新（界面不再去读会话内部状态）
                self._post("usage", data)
            case InboxEnqueued(item=item) | InboxDelivered(item=item):
                self._post("inbox_add" if isinstance(data, InboxEnqueued) else "inbox_deliver",
                           item)
            case InboxCancelled(item_id=item_id):
                self._post("inbox_cancel", item_id)
            case InboxCleared():
                self._post("inbox_clear")
            case _:
                return

    def _post(self, action: str, *args) -> None:
        self.app.post_message(UiAction(action, *args))

    def _thinking_tick(self, chunk: str) -> None:
        if self._thinking is None:
            self._thinking = 0
            self._post("thinking_start")
        self._thinking += len(chunk)
        self._post("thinking_tick", chunk)

    def _thinking_done(self) -> None:
        if self._thinking is not None:
            self._post("thinking_done")
            self._thinking = None

    # ----- 询问面：面板 -----

    async def ask(self, request: AskRequest) -> AskAnswer:
        """把一次提问交给面板，等用户作答（**在事件循环上** await）。

        不再用 `call_from_thread` + `threading.Event`：提问本身是异步的，等的是
        一个 Future——所以面板在哪个线程挂、答案从哪个线程回来都不需要手工搬运
        （阶段 C 的目标）。
        """
        if self._closed:
            # 界面已收尾：不再挂面板（挂上去也没人能答），按 fail-closed 收口
            return AskAnswer(outcome="cancelled")
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        slot: dict = {}
        self.app.show_ask_panel(request, slot, lambda: _finish(future, slot))
        try:
            result = await future
        except asyncio.CancelledError:
            self.app.close_open_panel()  # 收尾：面板也要收掉（会话取消 / 退出）
            raise
        return _answer_of(request, result)

    def abandon_pending(self) -> None:
        """界面收尾（`SmithTUI.on_unmount`）：标记"此后不再挂面板"。

        真正的取消在 `AskPort.cancel_in_flight`（`SmithTUI.on_unmount` 会调它）：
        等待方由那里统一放行，所以这里只置位——此后新到的询问直接走 fail-closed
        兜底，不会再去 mount 一个已经卸载的界面。
        """
        self._closed = True


def _finish(future: asyncio.Future, slot: dict) -> None:
    """面板答完：把结果交给等待中的 `ask()`（同线程，直接 set_result）。"""
    if not future.done():
        future.set_result(slot)


def _answer_of(request: AskRequest, result: dict) -> AskAnswer:
    """面板结果 → `AskAnswer`（各 kind 的形状差异只在这里解释一次）。"""
    if request.kind == "ask_user":
        values = result.get("values")
        if not values:
            return AskAnswer(outcome="cancelled")
        answers = tuple(str(value) for value in values)
        # 整组全空 = 用户取消了这次提问（每题空串表示该题取消）
        outcome = "cancelled" if all(not value for value in answers) else "answered"
        return AskAnswer(outcome=outcome, values=answers)
    value = result.get("value")
    if not value:
        return AskAnswer(outcome="cancelled")
    return AskAnswer(outcome="answered", value=str(value))
