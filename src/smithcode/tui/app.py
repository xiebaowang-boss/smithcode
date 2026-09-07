"""Textual 聊天界面（复刻 Claude Code 风格）。

- 上半：ChatView 消息区（流式回复、可折叠工具调用块）
- 右上：计划侧边栏（todo 清单实时更新，Ctrl+O 切换显示）
- 下半：多行输入框 + 状态栏 + Footer 快捷键提示

Agent 在后台线程同步运行，TuiRenderer 用 post_message（线程安全）把事件桥到
主线程；权限确认 / ask_user 通过 ModalScreen 弹窗阻塞等待。非交互模式不走
这里（cli 负责分流），仍用 ConsoleRenderer。
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import ClassVar

from rich.console import Console
from rich.markdown import Markdown as RichMarkdown
from rich.text import Text


def render_markdown(text: str, width: int) -> Text:
    """把 markdown 文本渲染成带样式的 Text（标题/粗体/代码块着色）。

    直接消费 rich 的 render_lines 段（pad=False，无整行填充），样式随段
    附加；宽度用组件实际宽度，由 rich 负责换行。
    """
    console = Console(width=width, force_terminal=True, color_system="standard")
    result = Text()
    for index, line in enumerate(console.render_lines(RichMarkdown(text), options=console.options, pad=False)):
        if index:
            result.append("\n")
        for seg in line:
            if seg.text:
                result.append(seg.text, style=seg.style or None)
    return result
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Input, Static, TextArea

from .. import __version__, config, context, plan, renderer
from ..cli import HELP


def git_branch(workspace: str) -> str | None:
    """当前工作区的 git 分支名；非 git 仓库或读取失败返回 None。

    直接读 .git/HEAD（"ref: refs/heads/main" → main），不调 git 命令，
    免依赖、速度快。子模块/worktree 的 .git 是指向实际 gitdir 的文本文件。
    """
    root = Path(workspace)
    git = root / ".git"
    try:
        if git.is_dir():
            head = git / "HEAD"
        elif git.is_file():
            gitdir = git.read_text(encoding="utf-8").strip()
            if not gitdir.startswith("gitdir:"):
                return None
            head = (root / gitdir[7:].strip() / "HEAD").resolve()
        else:
            return None
        text = head.read_text(encoding="utf-8").strip()
        if text.startswith("ref: "):
            return text[5:].split("/")[-1]
        return text[:7]  # detached HEAD：显示短提交号
    except OSError:
        return None

# ---------- 线程安全的 UI 操作投递 ----------


class UiAction(Message):
    """从 worker 线程投递的一个 UI 操作（post_message 线程安全、即发即走）。"""

    def __init__(self, action: str, *args):
        super().__init__()
        self.action = action
        self.args = args


# ---------- 消息区 ----------


class ChatView(VerticalScroll):
    """聊天消息区：普通消息为独立块；流式消息单块原地更新。"""

    can_focus = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._kind: str | None = None
        self._text: Text | None = None
        self._raw = ""  # 正文段落纯文本累积，流结束后转 markdown 渲染
        self._block: Static | None = None

    def add_line(self, text: str, style: str | None = None) -> None:
        self._mk(Text.from_ansi(text, style=style))

    def add_line_text(self, text: Text, style: str | None = None) -> None:
        """直接挂一个已构造好的 Text（内嵌样式已就绪，不再二次解析）。"""
        self._mk(text)

    def add_user(self, text: str) -> None:
        """用户消息（opencode 式）：面板底色 + 左侧角色色竖线，无前缀。"""
        block = Static(Text(text), classes="user-msg")
        block.can_focus = False
        self.mount(block)
        self.scroll_end(animate=False)

    def add_block(self, text: str, style: str | None = None) -> None:
        for line in text.splitlines() or [""]:
            self._mk(Text(line, style=style))

    def add_turn_footer(self, model: str, elapsed: str) -> None:
        """opencode 式轮次元数据页脚：▣ 模型 · 用时（▣ 用强调色，缩进 3 格）。"""
        text = Text()
        text.append("▣ ", style="#fab283")
        text.append(model, style="#eeeeee")
        text.append(f" · 用时 {elapsed}", style="#808080")
        block = Static(text, classes="turn-footer")
        block.can_focus = False
        self.mount(block)
        self.scroll_end(animate=False)

    def add_widget(self, widget) -> None:
        """挂载任意消息组件（如可折叠的工具调用块）。"""
        self.mount(widget)
        self.scroll_end(animate=False)

    def begin_stream(self, kind: str) -> None:
        self._kind = kind
        self._text = Text()
        self._raw = ""
        if kind == "reasoning":
            self._text.append("[Thinking] ", style="grey50")
        # 助手正文统一缩进 3（opencode 式：用户块 2 / 助手流 3，层次差产生交叉感）
        self._block = self._mk(self._text, classes="assistant-stream")

    def append_stream(self, kind: str, chunk: str) -> None:
        if kind != self._kind:
            self.end_stream()
            self.begin_stream(kind)
        if kind == "content":
            self._raw += chunk
        self._text.append(chunk, style="grey50" if kind == "reasoning" else None)
        self._block.update(self._text)
        self.scroll_end(animate=False)

    def end_stream(self) -> None:
        """流结束：正文段落原地转 markdown 渲染（流式中仍是纯文本，稳定不闪）。

        经 rich Console 渲染为带样式的 Text（而不是直接塞 Markdown 渲染对象），
        Static 内容仍是文本——窄终端换行交给 Textual 处理，测试也能直接读文本。
        """
        if self._kind == "content" and self._block is not None and self._raw.strip():
            width = self._block.size.width or 80
            self._block.update(render_markdown(self._raw, width))
        self._kind = None
        self._text = None
        self._raw = ""
        self._block = None

    def _mk(self, renderable, classes: str | None = None) -> Static:
        block = Static(renderable, classes=classes)
        block.can_focus = False
        self.mount(block)
        self.scroll_end(animate=False)
        return block


# ---------- 运行中动画 ----------


class RunningIndicator(Static):
    """对话执行时的运行动画：显示在输入框下方，执行中可见、结束隐藏。

    opencode 式：文案带实时已用秒数，start/stop 由轮次边界驱动。
    """

    can_focus = False
    FRAMES: ClassVar = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def on_mount(self) -> None:
        self._frame = 0
        self._start: float | None = None
        self.set_interval(0.1, self._spin)

    def start(self) -> None:
        self._start = time.monotonic()

    def stop(self) -> None:
        self._start = None

    def _spin(self) -> None:
        if self._start is None:
            return  # 未运行：保持静止（display 已由调用方控制）
        elapsed = time.monotonic() - self._start
        self._frame = (self._frame + 1) % len(self.FRAMES)
        if elapsed < 60:
            secs = f"{elapsed:.0f}s"
        else:
            secs = f"{int(elapsed // 60)}m{int(elapsed % 60):02d}s"
        self.update(f"{self.FRAMES[self._frame]} 运行中… {secs}")


# ---------- 思考折叠块 ----------


class ThinkingBlock(Vertical):
    """可折叠的思考过程：流式时只更新行首计数，不刷屏；展开可见全文。

    挂载是异步的，tick 可能先于 compose 到达，先用 _pending 暂存，
    on_mount 时一次性补上。
    """

    can_focus = True
    BINDINGS: ClassVar = [
        Binding("enter", "toggle", "展开/收起"),
        Binding("space", "toggle", "展开/收起"),
    ]
    SPINNER: ClassVar = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._expanded = False
        self._chars = 0
        self._text = Text()
        self._pending: list[str] = []
        self._finished = False
        self._start = time.monotonic()  # 思考段起始时间，结束时折算耗时
        self._elapsed: float | None = None  # finish 后定格，展开/收起不丢
        self._spin_frame = 0
        self._spin_timer = None
        self._header: Static | None = None
        self._body: Static | None = None

    def compose(self):
        self._header = Static(self._running_text(), classes="think-header")
        self._body = Static("", classes="think-body")
        self._body.display = False
        yield self._header
        yield self._body

    def on_mount(self) -> None:
        for chunk in self._pending:
            self._apply(chunk)
        self._pending = []
        if self._finished:
            self._finish_header()
        else:
            self._spin_timer = self.set_interval(0.1, self._spin)

    def _spin(self) -> None:
        self._spin_frame = (self._spin_frame + 1) % len(self.SPINNER)
        if self._header is not None:
            self._header.update(self._running_text())

    def _running_text(self) -> str:
        return f"{self.SPINNER[self._spin_frame]} Thinking…（{self._chars:,} 字符）"

    def append(self, chunk: str) -> None:
        self._chars += len(chunk)
        self._text.append(chunk)
        if self._body is None:
            self._pending.append(chunk)
        else:
            self._apply(chunk)

    def finish(self) -> None:
        self._finished = True
        self._elapsed = time.monotonic() - self._start
        if self._spin_timer is not None:  # 停掉思考动画
            self._spin_timer.stop()
            self._spin_timer = None
        if self._header is not None:
            self._finish_header()

    def _apply(self, chunk: str) -> None:
        self._body.update(self._text)
        self._header.update(self._running_text())

    def _elapsed_text(self) -> str:
        return "" if self._elapsed is None else f" · {self._elapsed:.1f}s"

    def _finish_header(self) -> None:
        self._header.update(
            f"▸ Thinking {self._chars:,} 字符{self._elapsed_text()}（点击 / Enter 展开）"
        )

    def action_toggle(self) -> None:
        self._expanded = not self._expanded
        arrow = "▾" if self._expanded else "▸"
        self._header.update(f"{arrow} Thinking {self._chars:,} 字符{self._elapsed_text()}")
        self._body.display = self._expanded
        self._body.refresh()

    def on_click(self, event) -> None:
        event.stop()
        self.action_toggle()


# ---------- 工具调用折叠块 ----------


class ToolCall(Vertical):
    """opencode 式工具行：pending（spinner 摘要）→ 完成 / 失败原地更新。

    两种展示形态（opencode 的 InlineTool / BlockTool）：
    - inline：一行摘要 + 结果计数（如 `▸ ⚙ read x · 23 行`），结果默认收起
    - block：结果按行截断预览（默认 10 行），展开看全文
    失败信息 / detail 模式默认展开；pending 阶段只有转轮摘要行。
    """

    can_focus = True
    BINDINGS: ClassVar = [
        Binding("enter", "toggle", "展开/收起"),
        Binding("space", "toggle", "展开/收起"),
    ]
    SPINNER: ClassVar = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    BLOCK_COLLAPSE_LINES: ClassVar = 10

    def __init__(self, summary: str, result: str = "", *, expanded: bool = False,
                 pending: bool = False, is_error: bool = False, display: str = "inline",
                 **kwargs):
        super().__init__(**kwargs)
        self._summary = summary
        self._result = result
        self._expanded = expanded
        self._pending = pending
        self._is_error = is_error
        self._display = display
        self._spin_frame = 0
        self._spin_timer = None
        self._header: Static | None = None
        self._body: Static | None = None

    def compose(self):
        self._header = Static(self._header_text(), classes="tool-header")
        body = Static(self._body_content(), classes="tool-body")
        body.display = self._expanded
        self._body = body
        yield self._header
        yield body

    def on_mount(self) -> None:
        if self._pending:
            self._spin_timer = self.set_interval(0.1, self._spin)
        self._apply_state_style()

    def _header_text(self) -> str:
        if self._pending:
            return f"{self.SPINNER[self._spin_frame]} ⚙ {self._summary}"
        mark = "▾" if self._expanded else "▸"
        stat = ""
        if self._display == "inline" and self._result:
            stat = f" · {len(self._result.splitlines())} 行"
        return f"{mark} ⚙ {self._summary}{stat}"

    def _body_content(self) -> str:
        """block 形态未展开时按行截断预览；其余展示全文。"""
        if self._display == "block" and not self._expanded:
            lines = self._result.splitlines()
            if len(lines) > self.BLOCK_COLLAPSE_LINES:
                preview = "\n".join(lines[: self.BLOCK_COLLAPSE_LINES])
                return (
                    f"{preview}\n…（+{len(lines) - self.BLOCK_COLLAPSE_LINES} 行，"
                    "Enter / 点击展开）"
                )
        return self._result

    def _spin(self) -> None:
        self._spin_frame = (self._spin_frame + 1) % len(self.SPINNER)
        if self._header is not None:
            self._header.update(self._header_text())

    def _apply_state_style(self) -> None:
        """按成功 / 失败着色：失败行红色，成功行灰色（opencode 的状态色语义）。"""
        if self._header is None:
            return
        if self._is_error:
            self._header.add_class("tool-error")
            self._body.add_class("tool-error")
        else:
            self._header.remove_class("tool-error")
            self._body.remove_class("tool-error")

    def set_result(self, result: str, *, expanded: bool, is_error: bool) -> None:
        """结果到达：停掉 pending spinner，原地更新为可折叠块。

        挂载是异步的，结果可能先于 on_mount 到达——此时只改状态字段，
        compose 时自然会渲染成完成态。
        """
        self._pending = False
        self._result = result
        self._expanded = expanded
        self._is_error = is_error
        if self._spin_timer is not None:
            self._spin_timer.stop()
            self._spin_timer = None
        if self._header is not None:
            self._header.update(self._header_text())
        if self._body is not None:
            self._body.update(self._body_content())
            self._body.display = expanded
        self._apply_state_style()

    def action_toggle(self) -> None:
        if self._pending:
            return  # 结果未到，没有内容可展开
        self._expanded = not self._expanded
        self.query_one(".tool-header").update(self._header_text())
        body = self.query_one(".tool-body")
        body.update(self._body_content())
        body.display = self._expanded
        body.refresh()

    def on_click(self, event) -> None:
        event.stop()
        self.action_toggle()


# ---------- 常驻侧边栏 ----------


class Sidebar(Vertical):
    """右侧常驻面板：上半用量/上下文，中间计划，底部版本号。"""

    can_focus = False
    DEFAULT_CSS = """
    Sidebar .section-title { color: #808080; text-style: bold; }
    Sidebar #sidebar-plan { height: 1fr; }
    Sidebar .sidebar-version { color: #808080; height: 1; }
    Sidebar .sidebar-workspace {
        color: #808080;
        height: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._usage = Static("", classes="usage-body")
        self._plan = Static("（暂无任务计划）", classes="plan-body")

    def compose(self):
        yield Static("用量 / 上下文", classes="section-title")
        yield self._usage
        yield Static("计划", classes="section-title")
        with VerticalScroll(id="sidebar-plan"):
            yield self._plan
        yield Static(f"v{__version__}", classes="sidebar-version")
        yield Static(str(Path(config.WORKSPACE_ROOT)), classes="sidebar-workspace")

    def update_usage(self, text: str) -> None:
        self._usage.update(text)

    def update_plan(self, rendered: str, has_items: bool) -> None:
        if has_items:
            self._plan.update(Text.from_ansi(rendered))
        else:
            self._plan.update("（暂无任务计划）")


# ---------- 输入区 ----------


class ChatInput(TextArea):
    """多行输入：Enter 发送，Shift+Enter / Ctrl+J 换行。"""

    class Submitted(Message):
        def __init__(self, value: str):
            super().__init__()
            self.value = value

    def on_key(self, event) -> None:
        if event.key in ("enter", "ctrl+enter"):
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self.text.rstrip("\n")))
        elif event.key in ("shift+enter", "ctrl+j"):
            event.stop()
            self.insert("\n")


# ---------- 弹窗 ----------


def _parse_choice_options(prompt: str, valid: str) -> list[tuple[str, str]]:
    """从确认提示串解析 (键, 标签) 列表，如 "[y]仅本次 / [a]总是 / [n]拒绝"。

    解析不到标签时退化为单字符标签，保证任何 valid 串都能渲染成选项列表。
    """
    labels = {
        m.group(1).lower(): m.group(2).strip().rstrip(":").strip()
        for m in re.finditer(r"\[(\w+)\]([^/\[:\]]*)", prompt)
    }
    options = [(key, labels.get(key) or key) for key in valid if key in labels]
    return options or [(key, key) for key in valid]


class PermissionPanel(Vertical):
    """opencode 式权限申请面板：申请时原地替换输入框，按键即答。

    视觉对齐 opencode permission.tsx：左侧 warning 竖线 + 「△ 需要授权」标题，
    选项为底部横排按钮块（选中项 accent 底色），提示仅 ⇆ / enter / esc。
    字母键（y/n/a）与数字键为隐藏快捷键，不在提示里展示。
    """

    can_focus = True
    BINDINGS: ClassVar = [
        Binding("left", "move_prev", "上一项", show=False),
        Binding("right", "move_next", "下一项", show=False),
        Binding("h", "move_prev", "上一项", show=False),
        Binding("l", "move_next", "下一项", show=False),
        Binding("up", "move_prev", "上一项", show=False),
        Binding("down", "move_next", "下一项", show=False),
        Binding("k", "move_prev", "上一项", show=False),
        Binding("j", "move_next", "下一项", show=False),
        Binding("enter", "confirm", "确认", show=False),
        Binding("escape", "cancel", "拒绝", show=False),
    ]

    def __init__(self, prompt: str, valid: str, hint: str, result: dict, evt: threading.Event, **kwargs):
        super().__init__(**kwargs)
        self._valid = valid
        self._options = _parse_choice_options(prompt, valid)
        self._title = re.split(r"\[", prompt, maxsplit=1)[0].strip() or "允许?"
        self._selected = 0
        self._result, self._evt = result, evt
        self._body: Static | None = None
        self._footer: Static | None = None

    def compose(self):
        yield Static(f"△ 需要授权：{self._title}", classes="perm-title")
        self._body = Static(self._render_options())
        yield self._body
        self._footer = Static(self._hints(), classes="ask-hint")
        yield self._footer

    def on_mount(self) -> None:
        self.focus()  # 挂载不会自动聚焦（原弹窗 push_screen 时代会），不聚焦按键会落进隐藏输入框

    def _hints(self) -> str:
        return "⇆ 选择  ·  enter 确认  ·  esc 拒绝"

    def _render_options(self) -> Text:
        """横排按钮块（opencode 的 chip 式选项）：选中项 accent 底色。"""
        text = Text()
        for i, (_, label) in enumerate(self._options):
            if i:
                text.append("  ")
            if i == self._selected:
                text.append(f" {label} ", style="bold black on #fab283")
            else:
                text.append(f" {label} ", style="#a9b1d6")
        return text

    def _refresh(self) -> None:
        if self._body is not None:
            self._body.update(self._render_options())

    def action_move_prev(self) -> None:
        self._selected = (self._selected - 1) % len(self._options)
        self._refresh()

    def action_move_next(self) -> None:
        self._selected = (self._selected + 1) % len(self._options)
        self._refresh()

    def action_confirm(self) -> None:
        key, _ = self._options[self._selected]
        self._finish(key)

    def action_cancel(self) -> None:
        # Esc=拒绝：有 n 选 n（两处调用点均为 y/n/a 语义），否则按最后一个选项
        deny = "n" if "n" in self._valid else self._valid[-1]
        self._finish(deny)

    def on_key(self, event) -> None:
        """字母键直选（y/n/a 按键即答）与数字键快选（隐藏快捷键）。

        注意 event.character 对方向键等特殊键为 None——必须先判空，
        否则 "" in "yna" 恒为 True，按方向键会被当成空回答直接拒绝。
        """
        char = event.character or ""
        if char and char in self._valid:
            event.stop()
            event.prevent_default()
            self._finish(char)
            return
        if char and char.isdigit():
            n = int(char)
            if 1 <= n <= len(self._options):
                event.stop()
                event.prevent_default()
                self._selected = n - 1
                self._refresh()
                self.action_confirm()

    def _finish(self, value: str) -> None:
        self._result["value"] = value
        self._evt.set()
        self.app.close_composer_panel(self)


class QuestionPanel(Vertical):
    """opencode 式提问面板：提问时原地替换输入框，答完换回。

    带选项时显示编号列表（↑↓/j/k/数字键选择），最后一项固定"输入自定义
    回答"，选中后原地展开输入框；无选项时直接进入输入态。单选 Enter 即答；
    多选 空格 勾选、Enter 提交全部勾选项；Esc 取消。
    """

    can_focus = True
    BINDINGS: ClassVar = [
        Binding("up", "move_up", "上移", show=False),
        Binding("down", "move_down", "下移", show=False),
        Binding("k", "move_up", "上移", show=False),
        Binding("j", "move_down", "下移", show=False),
        Binding("space", "toggle", "勾选", show=False),
        Binding("enter", "confirm", "确认", show=False),
        Binding("escape", "cancel", "取消", show=False),
    ]

    def __init__(self, question: str, options: list[str], multiple: bool,
                 result: dict, evt: threading.Event, **kwargs):
        super().__init__(**kwargs)
        self._question = question
        self._options = options or []
        self._multiple = multiple
        self._selected = 0
        self._checked: set[int] = set()
        self._editing = not self._options  # 无选项：直接进入输入态
        self._result, self._evt = result, evt
        self._body: Static | None = None
        self._footer: Static | None = None
        self._input: Input | None = None

    @property
    def _custom_index(self) -> int:
        return len(self._options)  # 最后一项固定为自定义输入

    def compose(self):
        yield Static(f"[提问] {self._question}", classes="ask-title")
        self._input = Input(placeholder="输入回答（Enter 提交，Esc 取消）")
        if self._options:
            self._body = Static(self._render_options())
            yield self._body
            self._input.display = False
        yield self._input
        self._footer = Static(self._hints(), classes="ask-hint")
        yield self._footer

    def on_mount(self) -> None:
        if self._editing:
            self._input.focus()
        else:
            self.focus()

    # ----- 状态渲染 -----

    def _hints(self) -> str:
        if self._editing:
            return "enter 提交回答 · esc 返回选项" if self._options else "enter 提交 · esc 取消"
        if self._multiple:
            return "↑↓ 选择 · 空格 勾选 · enter 提交 · esc 取消"
        return "↑↓ 选择 · enter 确认 · esc 取消"

    def _render_options(self) -> Text:
        """opencode question.tsx 式选项行：编号 + 标签，选中行暗色底，已选绿 ✓。"""
        text = Text()
        for i, opt in enumerate(self._options):
            picked = i in self._checked
            mark = f"[{'✓' if picked else ' '}] " if self._multiple else ""
            suffix = " ✓" if picked and not self._multiple else ""
            if i == self._selected:
                text.append(f" {i + 1}. {mark}{opt}{suffix} \n", style="on #292e42")
            else:
                text.append(f" {i + 1}. {mark}{opt}{suffix}\n", style="#a9b1d6" if picked else "")
        cursor_style = "on #292e42" if self._selected == self._custom_index else ""
        text.append(f" {self._custom_index + 1}. 输入自定义回答… ", style=cursor_style)
        return text

    def _refresh(self) -> None:
        if self._body is not None:
            self._body.update(self._render_options())
        if self._footer is not None:
            self._footer.update(self._hints())

    # ----- 交互 -----

    def action_move_up(self) -> None:
        if self._editing or not self._options:
            return
        self._selected = (self._selected - 1) % (len(self._options) + 1)
        self._refresh()

    def action_move_down(self) -> None:
        if self._editing or not self._options:
            return
        self._selected = (self._selected + 1) % (len(self._options) + 1)
        self._refresh()

    def action_toggle(self) -> None:
        if self._editing or not self._multiple:
            return
        if self._selected < self._custom_index:
            self._checked.symmetric_difference_update({self._selected})
            self._refresh()

    def action_confirm(self) -> None:
        if self._editing:
            text = self._input.value.strip() if self._input is not None else ""
            if text:
                self._finish(text)
            return
        if self._selected == self._custom_index:
            self._begin_editing()
            return
        if self._multiple:
            if self._checked:
                self._finish(", ".join(self._options[i] for i in sorted(self._checked)))
        else:
            self._finish(self._options[self._selected])

    def action_cancel(self) -> None:
        if self._editing and self._options:
            self._end_editing()
            return
        self._finish("")  # 空串 = 用户取消，由调用方兜底

    def on_key(self, event) -> None:
        """数字键快选（opencode 的 1-9 直接选）；输入态时不抢输入框的按键。"""
        if self._editing or not event.character or not event.character.isdigit():
            return
        n = int(event.character)
        if 1 <= n <= self._custom_index + 1:
            event.stop()
            event.prevent_default()
            self._selected = n - 1
            self._refresh()
            self.action_confirm()

    def on_input_submitted(self, event) -> None:
        text = event.value.strip()
        if text:
            self._finish(text)

    # ----- 编辑态 -----

    def _begin_editing(self) -> None:
        self._editing = True
        if self._input is not None:
            self._input.display = True
            self._input.focus()
        self._refresh()

    def _end_editing(self) -> None:
        self._editing = False
        if self._input is not None:
            self._input.display = False
        self.focus()
        self._refresh()

    def _finish(self, value: str) -> None:
        self._result["value"] = value
        self._evt.set()
        self.app.close_composer_panel(self)


# ---------- 渲染后端 ----------


class TuiRenderer(renderer.Renderer):
    """把 Agent 的终端交互桥到 Textual 界面（从 worker 线程调用）。

    流式/工具/信息类更新用 post_message 即发即走（不阻塞 worker、异常不会被
    吞）；弹窗类（confirm / ask）需要结果，仍用 call_from_thread + Event 阻塞。
    """

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

    def tool_call(self, line: str, display: str = "inline") -> int:
        """opencode 式 pending 行：摘要先上屏转轮，结果到了原地更新。"""
        self._tool_seq += 1
        self._post("tool_start", self._tool_seq, line, display)
        return self._tool_seq

    def tool_result(self, result: str, tool_id: int | None = None) -> None:
        is_error = result.startswith("错误:") or result == "用户拒绝了此操作"
        expanded = is_error or config.load_tool_display() == "detail"
        self._post("tool_result", tool_id, result, expanded, is_error)

    def plan(self, summary: str, rendered: str) -> None:
        self._post("block", f"[计划] {summary}\n{rendered}", "magenta")
        self._post("plan_sidebar", rendered)

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


# ---------- 应用 ----------


class SmithTUI(App):
    CSS = """
    Screen { layout: horizontal; }
    #main { width: 1fr; height: 100%; layout: horizontal; }
    #chat-col { width: 1fr; height: 100%; background: #0a0a0a; }
    #chat {
        height: 1fr;
        padding: 1 2 1 2;
        background: #0a0a0a;
    }
    #sidebar {
        height: 100%;
        width: 46;
        padding: 1 2 0 2;
        background: #141414;
    }
    #input-wrap {
        height: auto;
        margin: 0 2;
        border-left: solid #23d18b;
    }
    #input {
        height: 4;
        padding: 1 2 0 2;
        border: none;
        background: #1e1e1e;
    }
    #input:focus {
        border: none;
    }
    #input .text-area--cursor-line {
        background: transparent;
    }
    #bottom {
        height: 1;
        layout: horizontal;
    }
    #running {
        color: #fab283;
        width: auto;
        padding: 0 2;
    }
    #status {
        width: 1fr;
        color: #808080;
        content-align: right middle;
        padding: 0 2;
    }

    ToolCall { height: auto; padding-left: 3; }
    ToolCall .tool-header { color: #808080; }
    ToolCall .tool-header.tool-error { color: #f7768e; }
    ToolCall .tool-body { color: #808080; margin-left: 2; }
    ToolCall .tool-body.tool-error { color: #f7768e; }
    ThinkingBlock { height: auto; padding-left: 3; margin-top: 1; margin-bottom: 1; }
    ThinkingBlock .think-header { color: #808080; }
    ThinkingBlock .think-body { color: #808080; margin-left: 2; }
    .assistant-stream { padding-left: 3; }

    .user-msg {
        background: #141414;
        border-left: solid #23d18b;
        padding: 1 1 1 2;
        margin-top: 1;
    }
    QuestionPanel {
        height: auto;
        padding: 1 2 1 2;
        background: #141414;
        border-left: solid #fab283;
    }
    QuestionPanel .ask-title { color: #fab283; }
    QuestionPanel .ask-hint { color: #808080; }
    /* opencode 式自定义回答：单行、无边框，嵌在选项列表末尾 */
    QuestionPanel Input {
        border: none;
        height: 1;
        padding: 0 0 0 1;
        background: #1e1e2e;
    }
    PermissionPanel {
        height: auto;
        padding: 1 2 1 2;
        background: #141414;
        border-left: solid #fab283;
    }
    PermissionPanel .perm-title { color: #fab283; }
    PermissionPanel .ask-hint { color: #808080; }
    .turn-footer {
        padding-left: 3;
        margin-top: 1;
    }
    """

    BINDINGS: ClassVar = [
        Binding("ctrl+q", "quit", "退出"),
    ]

    def __init__(self, agent):
        super().__init__()
        self.agent = agent
        self._busy = False
        self._thinking_block: ThinkingBlock | None = None
        self._turn_start: float | None = None
        self._tool_widgets: dict[int, ToolCall] = {}  # tool_id → pending 中的工具行

    def compose(self) -> ComposeResult:
        # opencode 式布局：侧边栏通高居右；对话列（消息区 + 输入框 + 状态行）居左
        with Horizontal(id="main"):
            with Vertical(id="chat-col"):
                yield ChatView(id="chat")
                with Horizontal(id="input-wrap"):
                    yield ChatInput(id="input")
                with Horizontal(id="bottom"):
                    yield RunningIndicator(id="running")
                    yield Static(id="status")
            yield Sidebar(id="sidebar")

    def on_mount(self) -> None:
        renderer.set_renderer(TuiRenderer(self))
        self.query_one(ChatInput).focus()
        self.query_one("#running").display = False  # 运行动画默认隐藏
        self.ui_line("SmithCode TUI（Enter 发送，Shift+Enter 换行，/help 查看命令）", "bold")
        self.ui_status()

    # ----- 消息路由（主线程，供 TuiRenderer 经 UiAction 投递） -----

    def on_ui_action(self, message: UiAction) -> None:
        handler = getattr(self, f"ui_{message.action}", None)
        if handler is None:
            self.ui_line(f"（未知 UI 动作: {message.action}）", "red")
            return
        handler(*message.args)

    def ui_line(self, text: str, style: str | None = None) -> None:
        self.query_one(ChatView).add_line(text, style)

    def ui_block(self, text: str, style: str | None = None) -> None:
        """多行文本块；from_ansi 解析内嵌 ANSI 转义（如 [计划] 清单的颜色码），
        避免转义符作为字面字符进入渲染流（真实终端会打花整个界面）。"""
        for line in Text.from_ansi(text, style=style).split("\n"):
            self.query_one(ChatView).add_line_text(line, style)

    def ui_stream(self, kind: str, chunk: str) -> None:
        self.query_one(ChatView).append_stream(kind, chunk)

    def ui_stream_done(self) -> None:
        self.query_one(ChatView).end_stream()

    def ui_tool_start(self, tool_id: int, summary: str, display: str = "inline") -> None:
        """pending 工具行：转轮摘要先上屏，结果到达后原地更新（opencode 式）。"""
        widget = ToolCall(summary, pending=True, display=display)
        self._tool_widgets[tool_id] = widget
        self.query_one(ChatView).add_widget(widget)

    def ui_tool_result(self, tool_id: int | None, result: str, expanded: bool, is_error: bool) -> None:
        widget = self._tool_widgets.pop(tool_id, None) if tool_id is not None else None
        if widget is not None:
            widget.set_result(result, expanded=expanded, is_error=is_error)
        else:  # 无配对（理论上不发生）：退化为独立块，不丢结果
            self.query_one(ChatView).add_widget(
                ToolCall("[Tool]", result, expanded=expanded, is_error=is_error)
            )

    def ui_thinking_start(self) -> None:
        block = ThinkingBlock()
        self._thinking_block = block
        self.query_one(ChatView).add_widget(block)

    def ui_thinking_tick(self, chunk: str) -> None:
        if self._thinking_block is not None:
            self._thinking_block.append(chunk)

    def ui_thinking_done(self) -> None:
        if self._thinking_block is not None:
            self._thinking_block.finish()
            self._thinking_block = None

    def ui_plan_sidebar(self, rendered: str) -> None:
        self.query_one(Sidebar).update_plan(rendered, bool(plan.current().items))

    def ui_focus_input(self) -> None:
        self.query_one(ChatInput).focus()

    def show_question_panel(
        self, question: str, options: list[str], multiple: bool, result: dict, evt: threading.Event
    ) -> None:
        """提问面板原地替换输入框，答完由 close_composer_panel 换回。"""
        self.query_one(ChatInput).display = False
        self.mount(
            QuestionPanel(question, options, multiple, result, evt),
            before=self.query_one("#input-wrap"),
        )

    def show_permission_panel(self, prompt: str, valid: str, hint: str, result: dict, evt: threading.Event) -> None:
        """权限申请面板原地替换输入框，答完由 close_composer_panel 换回。"""
        self.query_one(ChatInput).display = False
        self.mount(
            PermissionPanel(prompt, valid, hint, result, evt),
            before=self.query_one("#input-wrap"),
        )

    def close_composer_panel(self, panel: Vertical) -> None:
        """关闭提问/权限面板，恢复输入框并聚焦（composer 位三态的归位动作）。"""
        panel.remove()
        chat_input = self.query_one(ChatInput)
        chat_input.display = True
        chat_input.focus()

    def ui_status(self) -> None:
        self.query_one(Sidebar).update_usage(self._usage_text())
        self.query_one("#status").update(self._status_text())

    def _usage_text(self) -> str:
        """侧边栏上半：会话 token 用量与上下文占用。"""
        usage = self.agent.session.usage.current_session
        est = context.total_tokens(self.agent.session.messages)
        budget = config.CONTEXT_TOKEN_BUDGET
        pct = min(100, int(est / budget * 100)) if budget else 0
        lines = [usage.humanize(), f"上下文 {est:,}/{budget:,} ({pct}%)"]
        if self.agent.context.compact_count:
            lines.append(f"已压缩 {self.agent.context.compact_count}")
        return "\n".join(lines)

    def _status_text(self) -> str:
        """底部状态栏：模型 / 思考强度 / 项目名 / git 分支（如有）。"""
        parts = [config.MODEL, f"思考 {config.REASONING_EFFORT or '默认'}"]
        parts.append(f"项目 {Path(config.WORKSPACE_ROOT).name}")
        branch = git_branch(config.WORKSPACE_ROOT)
        if branch:
            parts.append(f"git {branch}")
        return "  |  ".join(parts)

    # ----- 输入与命令 -----

    def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        self.query_one(ChatInput).clear()
        if text.startswith("/"):
            self.handle_command(text)
        else:
            self.query_one(ChatView).add_user(text)  # 回显用户消息，避免"发出去没反应"
            self.start_task(text)

    def start_task(self, text: str) -> None:
        if self._busy:
            self.ui_line("（上一条任务还在运行，请等待）", "yellow")
            return
        self._busy = True
        self._turn_start = time.monotonic()
        running = self.query_one("#running", RunningIndicator)
        running.display = True
        running.start()
        threading.Thread(target=self._run_task, args=(text,), daemon=True).start()

    def _run_task(self, text: str) -> None:
        try:
            self.agent.run(text)
        except Exception as e:  # noqa: BLE001
            self.post_message(UiAction("line", f"[错误] {type(e).__name__}: {e}", "red"))
        finally:
            self._busy = False
            self.post_message(UiAction("focus_input"))
            self.post_message(UiAction("status"))
            self.post_message(UiAction("running_off"))
            self.post_message(UiAction("turn_end"))

    def ui_running_off(self) -> None:
        # 应用退出时组件可能已卸载，消息晚到会导致 NoMatches——查不到就忽略
        found = self.query("#running")
        if not found:
            return
        found.first().stop()
        found.first().display = False

    def ui_turn_end(self) -> None:
        """轮次结束：在会话末尾追加 opencode 式元数据页脚「▣ 模型 · 用时」。"""
        if self._turn_start is None:
            return
        elapsed = time.monotonic() - self._turn_start
        self._turn_start = None
        found = self.query(ChatView)
        if found:
            found.first().add_turn_footer(config.MODEL, self._format_elapsed(elapsed))

    @staticmethod
    def _format_elapsed(secs: float) -> str:
        if secs < 60:
            return f"{secs:.1f}s"
        return f"{int(secs // 60)}m {int(secs % 60)}s"

    def handle_command(self, text: str) -> None:
        agent = self.agent
        if text == "/exit":
            self.exit()
        elif text == "/new":
            agent.session.reset()
            agent.permission.session_rules.clear()
            config.SESSION_EXTRA_ROOTS.clear()
            agent.context.compact_count = 0
            plan.reset()
            self.query_one(Sidebar).update_plan("", has_items=False)
            self.ui_line("已开启新会话。", "yellow")
            self.ui_status()
        elif text == "/plan":
            self.ui_block(f"[计划] {plan.summary()}\n{plan.render_current(color=True)}")
        elif text == "/save":
            path = agent.session.save()
            self.ui_line(f"会话已保存到 {path}", "green")
        elif text == "/usage":
            self.ui_block(agent.session.usage.summary())
            self.ui_status()
        elif text == "/context":
            self.ui_block(
                context.report(
                    agent.session.messages,
                    config.CONTEXT_TOKEN_BUDGET,
                    config.COMPACT_TRIGGER,
                    agent.context.last_actual,
                    agent.context.compact_count,
                )
            )
            self.ui_status()
        elif text == "/compact":
            if agent.compact():
                self.ui_line("已压缩上下文。", "green")
            else:
                self.ui_line("没有可压缩的上下文（历史太短或摘要未生成）。", "yellow")
            self.ui_status()
        elif text == "/help":
            self.ui_block(HELP)
        else:
            self.ui_line(f"未知命令: {text}（/help 查看）", "red")


def run_tui(agent) -> None:
    """启动 Textual 全屏聊天界面（仅交互 tty 模式）。"""
    SmithTUI(agent).run()