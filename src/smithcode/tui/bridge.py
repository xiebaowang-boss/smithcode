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
    """Agent 与 SmithTUI 之间唯一的线程侧通道（renderer 基类的 TUI 实现）。"""

    def __init__(self, app: SmithTUI):
        self.app = app
        self._tool_seq = 0  # tool_call → tool_result 的配对 id（renderer 基类约定）
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
        self._tool_seq += 1
        self._post("tool_start", self._tool_seq, line, display, name)
        return self._tool_seq

    def tool_preview(self, tool_id: int | None, detail: str) -> None:
        """执行前的变更预览（diff）：推给对应的 pending 工具块，审核时已可见。"""
        self._post("tool_preview", tool_id, detail)

    def tool_result(self, result: str, tool_id: int | None = None,
                    expand: bool = False) -> None:
        is_error = result.startswith("错误:") or result == "用户拒绝了此操作"
        expanded = is_error or expand or config.load_tool_display() == "detail"
        self._post("tool_result", tool_id, result, expanded, is_error)

    def plan(self, summary: str, rendered: str) -> None:
        self._post("block", f"[计划] {summary}\n{rendered}", "magenta")
        # 侧边栏只展示标题（紧凑渲染），聊天 [计划] 块保持全量（标题+描述+reason）
        self._post("plan_sidebar", plan.render_titles(color=True))

    def info(self, text: str) -> None:
        self._post("line", text, "grey50")

    def ask_text(self, question: str) -> str:
        result, evt = {}, threading.Event()
        self.app.call_from_thread(self.app.show_question_panel, question, [], False, result, evt)
        evt.wait()
        return result.get("value") or "（用户未输入内容）"

    def ask_choice(self, question: str, options: list[str], multiple: bool = False) -> str:
        result, evt = {}, threading.Event()
        self.app.call_from_thread(
            self.app.show_question_panel, question, options, multiple, result, evt
        )
        evt.wait()
        return result.get("value", "")  # 空串 = 用户取消

    def confirm_choice(self, prompt: str, valid: str, hint: str) -> str:
        result, evt = {}, threading.Event()
        self.app.call_from_thread(
            self.app.show_permission_panel, prompt, valid, hint, result, evt
        )
        evt.wait()
        return result.get("value", "n")  # 异常兜底按拒绝处理
