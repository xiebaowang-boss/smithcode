"""TUI 渲染后端：把 Agent 的终端交互桥接到 Textual 界面。

**线程不变量（承重，改动前先读）**：Agent 的任务本身跑在 Textual 的事件循环
上（`app.run_worker`），但**所有会弹窗的调用都来自 Agent 下放的 worker 线程**——
预检（权限确认 / 越界授权）与工具执行（`ask_user` / 技能信任）都经
`asyncio.to_thread` 执行（见 `agent/tools_run.py`）。因此：

- 即发即走的更新用 `post_message`：线程安全，从循环线程或 worker 线程都可以；
- 需要结果的弹窗用 `call_from_thread` + `Event`：**只能从 worker 线程调用**
  （Textual 在同一个线程上调用它会直接抛 `RuntimeError`，这也是上一条不变量的
  自动保护）。

若将来把预检/工具执行搬回循环线程，这两处会立刻炸——那正是我们想要的信号：
那时必须改成 `push_screen_wait`（异步等待），而不是让 `Event.wait()` 冻住循环。
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from .. import config, plan, renderer
from .widgets import UiAction

if TYPE_CHECKING:
    from .app import SmithTUI


class TuiRenderer(renderer.Renderer):
    """Agent 与 SmithTUI 之间唯一的线程侧通道（renderer 基类的 TUI 实现）。

    Agent 事件经 post_message 投递到宿主，由主线程消费渲染。"""

    def __init__(self, app: SmithTUI):
        super().__init__()
        self.app = app
        self._thinking: int | None = None  # 正在思考时累计的字符数
        # 挂起中的弹窗等待：{就绪事件: 结果槽}。唤醒口见 abandon_pending
        self._pending: dict[threading.Event, dict] = {}
        self._pending_lock = threading.Lock()
        self._closed = False  # 界面已收尾：此后的询问直接走默认值，不进等待

    def _post(self, action: str, *args) -> None:
        self.app.post_message(UiAction(action, *args))

    def stream(self, kind: str, chunk: str) -> None:
        if kind == "reasoning":
            if self._thinking is None:
                self._thinking = 0
                self._post("thinking_start")
            self._thinking += len(chunk)
            self._post("thinking_tick", chunk)
        else:
            if self._thinking is not None:
                self._post("thinking_done")
                self._thinking = None
            self._post("stream", kind, chunk)

    def stream_done(self) -> None:
        if self._thinking is not None:
            self._post("thinking_done")
            self._thinking = None
        else:
            self._post("stream_done")

    def tool_call(self, line: str, display: str = "inline", name: str = "") -> int:
        """opencode 式 pending 行：摘要先上屏转轮，结果到了原地更新。

        name 供 TUI 判定是否归入「已探索」上下文汇总块（读取/搜索/列目录）。"""
        tool_id = self._next_tool_id()
        self._post("tool_start", tool_id, line, display, name)
        return tool_id

    def tool_preview(self, tool_id: int | None, detail: str) -> None:
        """执行前的变更预览（diff）：推给对应的 pending 工具块，审核时已可见。"""
        self._post("tool_preview", tool_id, detail)

    def tool_result(self, result: str, tool_id: int | None = None,
                    expand: bool = False) -> None:
        is_error = result.startswith("错误:") or result == "用户拒绝了此操作"
        expanded = is_error or expand or config.load_tool_display() == "detail"
        self._post("tool_result", tool_id, result, expanded, is_error)

    def plan(self, summary: str, rendered: str, *, created: bool = False,
             tool_id: int | None = None) -> None:
        # 侧边栏始终刷新；聊天区仅新建清单时展示一次详情——复用 plan 工具块，
        # 可展开/收起、默认展开；后续每步更新不再往对话区重复打印进度
        self._post("plan_sidebar", plan.render_titles(color=True))
        if created:
            self._post("tool_result", tool_id, plan.render_current(), True, False)

    def info(self, text: str) -> None:
        self._post("notice", text, "info")

    def success(self, text: str) -> None:
        self._post("notice", text, "success")

    def warn(self, text: str) -> None:
        self._post("notice", text, "warning")

    def error(self, text: str) -> None:
        self._post("notice", text, "error")

    def title_changed(self, title: str) -> None:
        """会话标题变化（/rename 或后台自动标题）：通知主线程刷新底栏。"""
        self._post("title", title)

    def retry_started(self, state, owner=None) -> None:
        """模型请求失败即将重试：宿主在运行动画行显示「正在重试 N/M · Xs 后」。

        不落对话区（对齐 opencode：重试进度属于状态行，不是对话内容）；已上屏的
        那一段正文由宿主标记为中断，避免与重试后的正文看起来一模一样。
        owner 随事件带上，宿主据此只清自己那条重试态（后台标题可能同时在重试）。"""
        self._post("retry_start", state, owner)

    def retry_finished(self, owner=None) -> None:
        """重试过程结束（成功或放弃）：清除运行动画行的重试态。"""
        self._post("retry_end", owner)

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
