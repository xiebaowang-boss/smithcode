"""TUI 渲染后端：把 Agent 的终端交互桥接到 Textual 界面（从 worker 线程调用）。

流式/工具/信息类更新用 post_message 即发即走（不阻塞 worker、异常不会被
吞）；弹窗类（confirm / ask）需要结果，仍用 call_from_thread + Event 阻塞。
"""
from __future__ import annotations

import threading
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
        result, evt = {}, threading.Event()
        self.app.call_from_thread(self.app.show_question_panel, questions, result, evt)
        evt.wait()
        values = result.get("values")
        if not values:
            return [""] * len(questions)  # 空串 = 用户取消
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
        result, evt = {}, threading.Event()
        self.app.call_from_thread(
            self.app.show_permission_panel, prompt, valid, hint, result, evt,
            detail or [], descriptions or {}, content,
        )
        evt.wait()
        return result.get("value", "n")  # 异常兜底按拒绝处理
