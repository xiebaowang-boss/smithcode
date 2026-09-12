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
GREEN = "\033[32m"  # diff 增行
RED = "\033[31m"  # diff 删行
CYAN = "\033[36m"  # diff 位置头（@@）


def diff_line_kind(line: str) -> str | None:
    """diff 行的语义分类：add 增 / del 删 / hunk 位置头 / head 文件头 / None 普通。
    渲染后端按各自配色方案映射（REPL 用 ANSI，TUI 用主题色）。"""
    if line.startswith(("+++", "---")):
        return "head"
    if line.startswith("+"):
        return "add"
    if line.startswith("-"):
        return "del"
    if line.startswith("@@"):
        return "hunk"
    return None


def diff_line_style(line: str) -> str | None:
    """diff 行的 ANSI 颜色（REPL 用）：+/++ 绿、-/-- 红、@@ 与文件头青。"""
    return {"add": GREEN, "del": RED, "hunk": CYAN, "head": CYAN}.get(diff_line_kind(line))


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

    def tool_call(self, line: str, display: str = "inline", name: str = "") -> int:
        """工具调用的短摘要行：先于结果出现（pending 态），返回配对 id。
        display 为终端展示形态（inline / block），仅 TUI 使用。
        name 为工具名，供 TUI 判定是否归入「已探索」上下文汇总块。"""

    def tool_preview(self, tool_id: int | None, detail: str) -> None:
        """执行前推送变更预览（diff）到工具调用块：审核前就能看到改动。
        REPL 直接打印；TUI 更新对应的 pending 工具块。默认无操作。"""

    def tool_result(self, result: str, tool_id: int | None = None,
                    expand: bool = False) -> None:
        """工具执行结果展示（按 tool_display 粒度决定是否展示内容）；
        tool_id 非空时按 id 配对更新对应 pending 行。
        expand 标记写/编辑类工具：REPL 在执行后展示结果确认语，TUI 详情默认展开。"""

    def plan(self, summary: str, rendered: str, *, created: bool = False,
             tool_id: int | None = None) -> None:
        """任务步骤清单更新。

        created=True 表示本次是**新建**清单（此前无未完结步骤）：仅此时在
        对话区展示整份计划详情，后续每步更新只静默刷新常驻区域（侧边栏），
        避免把完成进度反复打印到对话区。tool_id 为对应 plan 工具块（新建时
        用于承载可展开 / 收起的详情）。"""

    def info(self, text: str) -> None:
        """状态/上下文等普通信息。"""

    def warn(self, text: str) -> None:
        """警告级信息（重试、降级等）；默认降级为 info，子类可覆盖着色。"""
        self.info(text)

    def error(self, text: str) -> None:
        """错误级信息（拒绝、失败等）；默认降级为 info，子类可覆盖着色。"""
        self.info(text)

    def title_changed(self, title: str) -> None:
        """会话标题变化（后台自动标题 / /rename）：宿主可刷新状态栏。默认忽略。"""

    def ask_text(self, question: str) -> str:
        """ask_user 工具：向用户提问并返回回答；失败返回空串由调用方兜底。"""

    def ask_choice(self, question: str, options: list[str], multiple: bool = False,
                   descriptions: list[str] | None = None) -> str:
        """ask_user 带选项提问：返回所选 label（多选逗号拼接）或自定义文本；
        取消返回空串由调用方兜底。descriptions 为与 options 对齐的选项说明
        （展示在选项下方的小字，可空）。默认降级为纯文本提问。"""
        shown = " / ".join(options)
        suffix = "（可多选，逗号分隔编号）" if multiple else "（输编号选择，或直接输入自定义回答）"
        return self.ask_text(f"{question}\n候选: {shown}{suffix}")

    def ask_form(self, questions: list[dict]) -> list[str]:
        """ask_user 工具入口：一次提交 1-N 个问题，返回与 questions 对齐的答案
        列表（空串 = 该题取消）。questions 每项为已归一化的
        {question, options, descriptions, multiple}。默认实现逐题串行提问
        （CLI 自然如此）；TUI 覆盖为单面板承载全部问题、可手动切题。"""
        total = len(questions)
        answers: list[str] = []
        for index, item in enumerate(questions, 1):
            question = item["question"]
            if total > 1:
                question = f"（{index}/{total}）{question}"
            options = item.get("options") or []
            if options:
                answers.append(self.ask_choice(
                    question, options, item.get("multiple", False),
                    descriptions=item.get("descriptions"),
                ))
            else:
                answers.append(self.ask_text(question))
        return answers

    def confirm_choice(self, prompt: str, valid: str, hint: str,
                       detail: list[str] | None = None,
                       descriptions: dict[str, str] | None = None,
                       content: str | None = None) -> str:
        """y/n/a 类多选确认：循环直到输入合法，返回小写选择键。

        detail 为确认框上方的说明行；descriptions 为按选项键索引的小字说明
        （如 `{"a": "本会话将记住 …"}`）；content 为紧跟在标题后的内容（工具摘要），
        TUI 与标题同排展示，REPL 在提示前打印。默认无操作，保证旧调用点不受影响。"""


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

    def tool_call(self, line: str, display: str = "inline", name: str = "") -> int:
        self._tool_seq += 1
        print(f"  {line}")
        return self._tool_seq

    def tool_preview(self, tool_id: int | None, detail: str) -> None:
        # 变更预览（diff）按行着色，先于权限确认 / 执行展示
        for line in detail.splitlines():
            color = diff_line_style(line)
            print(f"{color}{line}{RESET}" if color else line, flush=True)

    def tool_result(self, result: str, tool_id: int | None = None,
                    expand: bool = False) -> None:
        # 失败信息无论何种模式都原样展示——失败的细节比格式化摘要更重要
        if result.startswith("错误:") or result == "用户拒绝了此操作":
            print(f"  {result}\n")
            return
        if config.load_tool_display() == "detail":
            display = result[:500] + ("..." if len(result) > 500 else "")
            print(f"  [Result] {display}\n")
        elif expand:
            # 写/编辑类工具的执行确认语（如「已编辑 c.txt」）在真正执行后展示；
            # 变更预览（diff）已在 tool_preview 阶段（执行前）展示过
            print(f"  {result}\n")

    def plan(self, summary: str, rendered: str, *, created: bool = False,
             tool_id: int | None = None) -> None:
        # 仅新建清单时打印整份计划；后续更新（created=False）静默刷新，避免刷屏
        if not created:
            return
        print(f"\n[计划] {summary}")
        print(rendered)
        print()

    def info(self, text: str) -> None:
        print(text, flush=True)

    def ask_text(self, question: str) -> str:
        flush_pending_input()  # 丢弃缓冲区内提前键入/粘贴的内容，防止被误当成回答
        print(f"\n[提问] {question}")
        return read_user_input(prompt="回答> ").strip() or "（用户未输入内容）"

    def ask_choice(self, question: str, options: list[str], multiple: bool = False,
                   descriptions: list[str] | None = None) -> str:
        """opencode 式编号选择：数字=选项，直接打字=自定义回答，空输入=取消。"""
        flush_pending_input()
        print(f"\n[提问] {question}")
        for index, opt in enumerate(options, 1):
            print(f"  {index}. {opt}")
            desc = descriptions[index - 1] if descriptions and index <= len(descriptions) else ""
            if desc:
                print(f"     {desc}")
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
            return self.ask_choice(question, options, multiple, descriptions)
        return answer  # 非数字输入视为自定义回答

    def confirm_choice(self, prompt: str, valid: str, hint: str,
                       detail: list[str] | None = None,
                       descriptions: dict[str, str] | None = None,
                       content: str | None = None) -> str:
        flush_pending_input()  # 丢弃提前键入/粘贴的排队内容，防止被误当成回答
        lines = list(detail or [])
        if content:
            lines.append(content)
        for key, desc in (descriptions or {}).items():
            if desc:
                lines.append(f"[{key}] {desc}")
        if lines:
            print()
            for line in lines:
                print(f"   {line}", flush=True)
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