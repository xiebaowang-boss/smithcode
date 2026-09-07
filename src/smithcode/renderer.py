"""渲染后端：Agent 的全部终端交互经此收口。

CLI 与 TUI 各有一个实现：ConsoleRenderer 保持原有 print / input 行为
（一次性任务、管道、CI 用）；TuiRenderer 把事件桥到 Textual 界面。
默认走 ConsoleRenderer，TUI 启动时用 set_renderer 替换全局实例。
"""
from __future__ import annotations

import time

from . import config
from .utils.terminal import flush_pending_input, prompt_choice, read_user_input

DIM = "\033[90m"  # 思考内容灰色（90m 比 dim/2m 在 Windows 终端上兼容性好）
RESET = "\033[0m"


def _is_number_list(text: str) -> bool:
    """形如 "1" / "1,3" / "1 3" 的编号串（用于 ask_choice 的选项选择）。"""
    parts = text.replace(",", " ").split()
    return bool(parts) and all(part.isdigit() for part in parts)


class Renderer:
    """Agent 与终端交互的接口，子类实现。方法都在 worker 线程被调用。"""

    def __init__(self):
        self._tool_seq = 0  # tool_call → tool_result 的配对 id（pending 态原地更新用）

    def stream(self, kind: str, chunk: str) -> None:
        """流式增量：kind 为 reasoning（思考）或 content（正文）。"""

    def stream_done(self) -> None:
        """一段流式输出结束。"""

    def tool_call(self, line: str, display: str = "inline") -> int:
        """工具调用的短摘要行：先于结果出现（pending 态），返回配对 id。
        display 为终端展示形态（inline / block），仅 TUI 使用。"""

    def tool_result(self, result: str, tool_id: int | None = None) -> None:
        """工具执行结果展示（按 tool_display 粒度决定是否展示内容）；
        tool_id 非空时按 id 配对更新对应 pending 行。"""

    def plan(self, summary: str, rendered: str) -> None:
        """任务步骤清单更新。"""

    def info(self, text: str) -> None:
        """状态/错误/上下文等杂项信息。"""

    def ask_text(self, question: str) -> str:
        """ask_user 工具：向用户提问并返回回答；失败返回空串由调用方兜底。"""

    def ask_choice(self, question: str, options: list[str], multiple: bool = False) -> str:
        """ask_user 带选项提问：返回所选 label（多选逗号拼接）或自定义文本；
        取消返回空串由调用方兜底。默认降级为纯文本提问。"""
        shown = " / ".join(options)
        suffix = "（可多选，逗号分隔编号）" if multiple else "（输编号选择，或直接输入自定义回答）"
        return self.ask_text(f"{question}\n候选: {shown}{suffix}")

    def confirm_choice(self, prompt: str, valid: str, hint: str) -> str:
        """y/n/a 类多选确认：循环直到输入合法，返回小写选择键。"""


class ConsoleRenderer(Renderer):
    """终端模式：保持原有 print / input 行为不变。"""

    def __init__(self):
        super().__init__()
        self.mode: str | None = None  # 当前流式段落类型，用于段落切换样式
        self._think_start: float | None = None  # 思考段起始时间，结束时折算耗时

    def _think_elapsed(self) -> str:
        if self._think_start is None:
            return ""
        return f" · {time.monotonic() - self._think_start:.1f}s"

    def stream(self, kind: str, chunk: str) -> None:
        if kind != self.mode:
            if self.mode == "reasoning":  # 思考段结束，追加耗时后恢复正常样式
                print(f"{DIM}{self._think_elapsed()}{RESET}", end="", flush=True)
                self._think_start = None
            print("\n助手> ", end="", flush=True)
            if kind == "reasoning":
                self._think_start = time.monotonic()
                print(f"{DIM}[Thinking] ", end="", flush=True)
            self.mode = kind
        print(chunk, end="", flush=True)

    def stream_done(self) -> None:
        if self.mode == "reasoning":  # 流在思考段中结束（如模型直接发起工具调用）
            print(f"{DIM}{self._think_elapsed()}{RESET}", end="", flush=True)
            self._think_start = None
        if self.mode is not None:
            print()
        self.mode = None

    def tool_call(self, line: str, display: str = "inline") -> int:
        self._tool_seq += 1
        print(f"  {line}")
        return self._tool_seq

    def tool_result(self, result: str, tool_id: int | None = None) -> None:
        # 失败信息无论何种模式都原样展示——失败的细节比格式化摘要更重要
        if result.startswith("错误:") or result == "用户拒绝了此操作":
            print(f"  {result}\n")
            return
        if config.load_tool_display() == "detail":
            display = result[:500] + ("..." if len(result) > 500 else "")
            print(f"  [Result] {display}\n")

    def plan(self, summary: str, rendered: str) -> None:
        print(f"\n[计划] {summary}")
        print(rendered)
        print()

    def info(self, text: str) -> None:
        print(text, flush=True)

    def ask_text(self, question: str) -> str:
        flush_pending_input()  # 丢弃缓冲区内提前键入/粘贴的内容，防止被误当成回答
        print(f"\n[提问] {question}")
        return read_user_input(prompt="回答> ").strip() or "（用户未输入内容）"

    def ask_choice(self, question: str, options: list[str], multiple: bool = False) -> str:
        """opencode 式编号选择：数字=选项，直接打字=自定义回答，空输入=取消。"""
        flush_pending_input()
        print(f"\n[提问] {question}")
        for index, opt in enumerate(options, 1):
            print(f"  {index}. {opt}")
        if multiple:
            hint = "输入编号（可多个，逗号/空格分隔），或直接输入自定义回答，回车取消"
        else:
            hint = "输入编号选择，或直接输入自定义回答，回车取消"
        print(f"  {hint}")
        answer = read_user_input(prompt="选择> ").strip()
        if not answer:
            return ""  # 用户取消
        if answer.isdigit() or _is_number_list(answer):
            indexes = [int(part) for part in answer.replace(",", " ").split()]
            picked = [options[i - 1] for i in indexes if 1 <= i <= len(options)]
            if picked:
                return ", ".join(picked)
            print("   无效编号，请重新选择")
            return self.ask_choice(question, options, multiple)
        return answer  # 非数字输入视为自定义回答

    def confirm_choice(self, prompt: str, valid: str, hint: str) -> str:
        flush_pending_input()  # 丢弃提前键入/粘贴的排队内容，防止被误当成回答
        return prompt_choice(prompt, valid, hint)


# ---------- 全局当前渲染后端（TUI 启动时替换） ----------

_current: Renderer | None = None


def current() -> Renderer:
    """当前渲染后端；未显式设置时用 ConsoleRenderer。"""
    global _current
    if _current is None:
        _current = ConsoleRenderer()
    return _current


def set_renderer(renderer: Renderer) -> None:
    """替换全局渲染后端（TUI 启动时调用；仅交互模式，一次性任务不受影响）。"""
    global _current
    _current = renderer