"""TUI 前端：事件订阅者 + 询问端口。

与 `frontend/console.py` 的关系：同一个契约（订阅事件 + 实现 `Asker`）、同一批判定
逻辑，只有「呈现」不同——本类把事件投成 `UiAction` 交给 Textual 主线程，把询问
交给面板。

**线程不变量（承重，改动前先读）**：Agent 的任务跑在 Textual 的事件循环上
（`app.run_worker`），但**所有会弹窗的调用都来自 worker 线程**——预检
（权限确认 / 越界授权）与工具执行（`ask_user` / 技能信任）都经 `asyncio.to_thread`
执行（见 `agent/tools_run.py`）。因此：

- 即发即走的通知用 `post_message`：线程安全，从循环线程或 worker 线程都可以；
- 需要结果的弹窗用 `call_from_thread` + `Event`：**只能从 worker 线程调用**
  （Textual 在同一个线程上调用它会直接抛 `RuntimeError`，这也是上一条不变量的
  自动保护）。

阶段 C 会把询问改成事件化的请求-应答（`asked → replied`），届时这里换成
`push_screen_wait`，`call_from_thread` 与 `Event` 一起退场。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from .. import config
from ..event.catalog import (
    MessageEnd,
    MessageUpdate,
    Notice,
    PlanUpdate,
    QueueChanged,
    QueuedPromptDelivered,
    StatusChanged,
    StatusCleared,
    TitleChanged,
    ToolEnd,
    ToolPreview,
    ToolStart,
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
        # 挂起中的弹窗等待：{就绪事件: 结果槽}。唤醒口见 abandon_pending
        self._pending: dict[threading.Event, dict] = {}
        self._pending_lock = threading.Lock()
        self._closed = False  # 界面已收尾：此后的询问直接走默认值，不进等待

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
            case QueueChanged():
                self.app.post_message(UiAction("queue", data))
            case QueuedPromptDelivered(text=text):
                self.app.post_message(UiAction("queued_delivered", text))
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

    def ask_form(self, questions: list[dict]) -> list[str]:
        """一次提交 1-N 个问题：单个面板承载，可手动切题，答完一次性回传。"""
        result = self._await_panel(
            lambda slot, evt: self.app.call_from_thread(
                self.app.show_question_panel, questions, slot, evt
            )
        )
        values = result.get("values")
        if not values:
            return [""] * len(questions)  # 空串 = 用户取消（含未作答）
        return [str(value) for value in values]

    def ask_text(self, question: str) -> str:
        answer = self.ask_form([{"question": question}])
        return answer[0] or "（用户未输入内容）"

    def ask_choice(self, question: str, options: list[str], multiple: bool = False,
                   descriptions: list[str] | None = None) -> str:
        answer = self.ask_form([{
            "question": question,
            "options": options,
            "descriptions": descriptions or [],
            "multiple": multiple,
        }])
        return answer[0]  # 空串 = 用户取消

    def confirm_choice(self, prompt: str, valid: str, hint: str,
                       detail: list[str] | None = None,
                       descriptions: dict[str, str] | None = None,
                       content: str | None = None) -> str:
        result = self._await_panel(
            lambda slot, evt: self.app.call_from_thread(
                self.app.show_permission_panel, prompt, valid, hint, slot, evt,
                detail or [], descriptions or {}, content,
            )
        )
        return result.get("value", "n")  # 未作答 / 异常兜底按拒绝处理

    def _await_panel(self, mount: Callable[[dict, threading.Event], None]) -> dict:
        """挂面板并等作答，返回结果槽；槽为空表示没拿到答案（调用方走默认值）。

        等待期间登记在 `_pending`：界面收尾时由 `abandon_pending` 唤醒。不登记的
        话，等待线程会永远停在 `Event.wait()` 上——那不只是泄漏一个线程，见
        `abandon_pending` 的说明。
        """
        result: dict = {}
        evt = threading.Event()
        with self._pending_lock:
            if self._closed:
                return result  # 界面已收尾：不进等待，直接走默认值
            self._pending[evt] = result
        try:
            mount(result, evt)
            evt.wait()
        finally:
            with self._pending_lock:
                self._pending.pop(evt, None)
        return result

    def abandon_pending(self) -> None:
        """界面收尾（`SmithTUI.on_unmount`）：唤醒全部挂起的弹窗等待方。

        结果槽一律留空，各调用方按既有 fail-closed 语义兜底（权限确认 → "n"，
        提问 → 空串即取消），此后新到的询问也不再进入等待。

        为什么必须唤醒：这些等待线程跑在 `asyncio.to_thread` 的默认线程池里，
        而默认池是非 daemon 的，收尾时会被 join——
        - `asyncio.run` 收尾：`shutdown_default_executor`，上限 `THREAD_JOIN_TIMEOUT`
          （300s），期间界面已卸载、终端已还原，用户看到的是「退了但 shell 不回来」；
        - 解释器退出：`concurrent.futures` 的 atexit join **没有上限**，进程直接卡死。
        实测（最小 Textual 应用 + 默认池里永久阻塞的线程）：`app.run()` 12s 内不返回；
        把线程放行后立即返回。

        先置位、后清表：等待方被唤醒后会自己在 finally 里摘掉登记，这里清表是为了
        此后新到的询问能被 `_await_panel` 的 `_closed` 分支挡住（不再进等待）。
        """
        with self._pending_lock:
            self._closed = True
            pending = list(self._pending)
        for evt in pending:
            evt.set()
