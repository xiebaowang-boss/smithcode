"""终端前端：事件 → 打印；询问 → 读 stdin。

这是一次性任务、REPL 与管道/CI 共用的前端。它只有两件事：

- **通知面**：作为事件订阅者（`bus.subscribe(frontend.on_event)`）把事件打印出来——
  核心不再认识任何前端，终端表现逐字保持改造前的 print 行为；
- **询问面**：实现 `Asker`（权限确认 / 越界授权 / 技能信任 / 模型提问），
  非交互 stdin 下由调用方的 `confirmations_available()` 提前挡住，
  这里不额外做 fail-closed 判断（语义与改造前逐字一致）。

`Notice` 的级别在终端**不作区分**（与改造前一致：`warn`/`error` 都降级为 `info`），
只有 `success` 带 ✓——这是既有终端文案的一部分，不要"顺手美化"。
"""

from __future__ import annotations

import threading
import time

from .. import config
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
    StepEnded,
    StepStarted,
    ToolEnd,
    ToolPreview,
    ToolStart,
    UsageChanged,
)
from ..event.envelope import Envelope
from ..utils.terminal import flush_pending_input, prompt_choice, read_user_input
from .render import DIM, RESET, diff_line_style, is_number_list

# 并行工具同时输出时防止行内交错
_PRINT_LOCK = threading.Lock()


class ConsoleFrontend:
    """终端实现：保持原有 print / input 行为不变。"""

    def __init__(self) -> None:
        self.mode: str | None = None  # 当前流式段落类型，用于段落切换样式
        self._think_start: float | None = None  # 思考段起始时间，结束时折算耗时

    # ----- 通知面：事件订阅者 -----

    def on_event(self, env: Envelope) -> None:
        """把事件渲染到终端。未列出的事件（队列/回合/标题/终结）在终端无表现。"""
        data = env.data
        match data:
            case MessageUpdate(kind=kind, delta=delta):
                self._stream(kind, delta)
            case MessageEnd():
                self._stream_done()
            case ToolStart(line=line):
                self._tool_call(line)
            case ToolPreview(detail=detail):
                self._tool_preview(detail)
            case ToolEnd(result=result, expand=expand):
                self._tool_result(result, expand)
            case PlanUpdate(summary=summary, rendered=rendered, created=created):
                self._plan(summary, rendered, created)
            case Notice(text=text, level=level):
                self._notice(text, level)
            case StatusChanged(kind="retry", text=text):
                # 重试进度在终端是一行文本（TUI 才有运行动画行）
                self._notice(text, "info")
            case (
                ExecutionStarted() | ExecutionSucceeded() | ExecutionFailed()
                | ExecutionInterrupted() | StepStarted() | StepEnded() | Idle()
                | UsageChanged() | CompactionStarted() | CompactionEnded()
                | CompactionFailed() | InboxEnqueued() | InboxDelivered()
                | InboxCancelled() | InboxCleared()
            ):
                # 执行 / 步骤边界、压缩态、排队变更：终端按"内容与工具块"呈现，
                # 排队是 TUI 的面板概念（终端里入队即排队等待，无需额外呈现）。
                # 显式列出（而不是交给 `case _`）意味着新增事件时会被这里提醒。
                return
            case _:
                return

    def _think_elapsed(self) -> str:
        if self._think_start is None:
            return ""
        return f" · {time.monotonic() - self._think_start:.1f}s"

    def _stream(self, kind: str, chunk: str) -> None:
        with _PRINT_LOCK:
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

    def _stream_done(self) -> None:
        with _PRINT_LOCK:
            if self.mode == "reasoning":  # 流在思考段中结束（如模型直接发起工具调用）
                print(f"{DIM}{self._think_elapsed()}{RESET}", end="", flush=True)
                self._think_start = None
            if self.mode is not None:
                print()  # 结束当前流式行
                self.mode = None

    def _tool_call(self, line: str) -> None:
        with _PRINT_LOCK:
            print(f"⚙ {line}", flush=True)

    def _tool_preview(self, detail: str) -> None:
        """变更预览（diff）按行着色，先于权限确认 / 执行展示。"""
        with _PRINT_LOCK:
            for line in detail.splitlines():
                color = diff_line_style(line)
                print(f"{color}{line}{RESET}" if color else line, flush=True)

    def _tool_result(self, result: str, expand: bool) -> None:
        # 失败信息无论何种模式都原样展示——失败的细节比格式化摘要更重要
        failed = result.startswith("错误:") or result == "用户拒绝了此操作"
        with _PRINT_LOCK:
            if failed:
                print(f"  {result}\n")
                return
            if config.load_tool_display() == "detail":
                display = result[:500] + ("..." if len(result) > 500 else "")
                print(f"  [Result] {display}\n")
            elif expand:
                # 写/编辑类工具的执行确认语（如「已编辑 c.txt」）在真正执行后展示；
                # 变更预览（diff）已在 tool_preview 阶段（执行前）展示过
                print(f"  {result}\n")

    def _plan(self, summary: str, rendered: str, created: bool) -> None:
        # 仅新建清单时打印整份计划；后续更新（created=False）静默刷新，避免刷屏
        if not created:
            return
        with _PRINT_LOCK:
            print(f"\n[计划] {summary}")
            print(rendered)
            print()

    def _notice(self, text: str, level: str) -> None:
        with _PRINT_LOCK:
            print(f"✓ {text}" if level == "success" else text, flush=True)

    # ----- 询问面：读终端 -----

    def ask_form(self, questions: list[dict]) -> list[str]:
        """逐题串行提问（终端天然如此），题面带 (i/n) 序号。"""
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
        if answer.isdigit() or is_number_list(answer):
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
        flush_pending_input()  # 丢弃提前键入的排队内容，防止被误当成回答
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
