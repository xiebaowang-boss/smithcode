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

from .. import __version__, commands, config, context, permission, plan, renderer


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
        # 分段格式化保证任意时长 ≤ 5 列：配合右对齐定宽，转轮全程免布局重排
        if elapsed < 60:
            secs = f"{elapsed:.0f}s"  # 59s
        elif elapsed < 3600:
            secs = f"{elapsed / 60:.1f}m"  # 5.3m
        else:
            secs = f"{elapsed / 3600:.1f}h"  # 1.2h
        self.update(f"{self.FRAMES[self._frame]} Working… {secs:>5}", layout=False)


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
            # tick 间字符计数不变、宽度恒定，免布局防输入框竖线抖动
            self._header.update(self._running_text(), layout=False)

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
    失败信息 / detail 模式默认展开；文件读写类工具带 expand 标记默认展开
    （内容直接可见，可手动收起）；write/edit 附带变更预览（diff，按行着色）。
    pending 阶段只有转轮摘要行。
    """

    can_focus = True
    BINDINGS: ClassVar = [
        Binding("enter", "toggle", "展开/收起"),
        Binding("space", "toggle", "展开/收起"),
    ]
    SPINNER: ClassVar = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    BLOCK_COLLAPSE_LINES: ClassVar = 10

    # diff 行配色：增行绿、删行红、位置头琥珀、文件头蓝（opencode 式语义色）
    DIFF_STYLES: ClassVar = {
        "add": "#23d18b",
        "del": "#f44747",
        "hunk": "#e0af68",
        "head": "#7aa2f7",
    }

    def __init__(self, summary: str, result: str = "", *, expanded: bool = False,
                 pending: bool = False, is_error: bool = False, display: str = "inline",
                 detail: str = "", **kwargs):
        super().__init__(**kwargs)
        self._summary = summary
        self._result = result
        self._expanded = expanded
        self._pending = pending
        self._is_error = is_error
        self._display = display
        self._detail = detail
        self._spin_frame = 0
        self._spin_timer = None
        self._header: Static | None = None
        self._body: Static | None = None

    def compose(self):
        self._header = Static(self._header_text(), classes="tool-header")
        body = Static(self._body_content(), classes="tool-body")
        # pending 且已带变更预览（diff）时直接展开：审核前改动内容就可见
        body.display = self._expanded or bool(self._pending and self._detail)
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

    def _body_content(self):
        """详情内容：结果文本 + 变更预览（diff，按行着色）；block 形态未展开时按行截断。"""
        lines = self._body_lines()
        if self._display == "block" and not self._expanded:
            hidden = len(lines) - self.BLOCK_COLLAPSE_LINES
            if hidden > 0:
                lines = lines[: self.BLOCK_COLLAPSE_LINES]
                lines.append(f"…（+{hidden} 行，Enter / 点击展开）")
        text = Text()
        for line in lines:
            text.append(line + "\n", style=self._line_style(line))
        return text

    def _body_lines(self) -> list[str]:
        """完整详情行：变更预览（diff）在前、执行结果确认语在后，供截断计数。"""
        lines = []
        if self._detail:
            lines.extend(self._detail.splitlines())
        if self._result:
            if lines:
                lines.append("")
            lines.extend(self._result.splitlines())
        return lines

    def _line_style(self, line: str) -> str | None:
        """diff 行着色；普通行不着色（继承 tool-body 样式）。"""
        kind = renderer.diff_line_kind(line)
        return self.DIFF_STYLES.get(kind) if kind else None

    def _spin(self) -> None:
        self._spin_frame = (self._spin_frame + 1) % len(self.SPINNER)
        if self._header is not None:
            # pending 期间文案宽度恒定（frame 单宽、summary 不变），免布局防输入框竖线抖动
            self._header.update(self._header_text(), layout=False)

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

    def set_detail(self, detail: str) -> None:
        """执行前推送的变更预览（diff）：pending 态就地展开展示，审核前可见。

        挂载是异步的，可能先于 compose 到达——此时只存字段，compose 时
        自然渲染（见 compose 的 pending+detail 展开逻辑）。"""
        self._detail = detail
        if self._body is not None:
            self._body.update(self._body_content())
            self._body.display = True

    def set_result(self, result: str, *, expanded: bool, is_error: bool) -> None:
        """结果到达：停掉 pending spinner，原地更新为可折叠块。

        挂载是异步的，结果可能先于 on_mount 到达——此时只改状态字段，
        compose 时自然会渲染成完成态。预览（_detail）保留在详情里一起展示。
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
        if self._pending and not self._detail:
            return  # 结果未到且无变更预览，没有内容可展开
        self._expanded = not self._expanded
        self.query_one(".tool-header").update(self._header_text())
        body = self.query_one(".tool-body")
        body.update(self._body_content())
        body.display = self._expanded
        body.refresh()

    def on_click(self, event) -> None:
        event.stop()
        self.action_toggle()


def human_tokens(n: int) -> str:
    """token 数的人性化缩写：980 → 980，12345 → 12.3K，234567 → 235K，1234567 → 1.2M。"""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        value = n / 1_000
        return f"{value:.0f}K" if value >= 100 else f"{value:.1f}K"
    return str(n)


# ---------- 常驻侧边栏 ----------


class Sidebar(Vertical):
    """右侧常驻面板：两张小卡片「用量」「上下文」，中间计划，底部版本号。"""

    can_focus = False
    DEFAULT_CSS = """
    Sidebar .section-title { color: #808080; text-style: bold; }
    Sidebar #sidebar-top { height: 1fr; }
    Sidebar #sidebar-plan-section { height: 1fr; display: none; }
    Sidebar #sidebar-plan { height: 1fr; scrollbar-gutter: stable; }
    Sidebar .sidebar-version { color: #808080; height: 1; }
    Sidebar .sidebar-workspace {
        color: #808080;
        height: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    Sidebar .side-card {
        width: 100%;
        height: auto;
        background: #1b1b1b;
        padding: 1 1;
        margin-bottom: 1;
    }
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._usage_title = Static("用量", classes="section-title")
        self._usage = Static("", classes="usage-body")
        self._context_title = Static("上下文", classes="section-title")
        self._context_body = Static("", classes="context-body")
        self._plan = Static("（暂无任务计划）", classes="plan-body")

    def compose(self):
        # 「用量」「上下文」各自成卡（浅色底小标题分组），下方计划清单沿用原样。
        # 卡片 + 计划区包进 #sidebar-top（1fr）：计划区隐藏时仍有弹性占位，
        # 保证底部版本号 / 工作区路径始终钉在侧边栏底部。
        with Vertical(id="sidebar-top"):
            with Vertical(classes="side-card usage-card"):
                yield self._usage_title
                yield self._usage
            with Vertical(classes="side-card context-card"):
                yield self._context_title
                yield self._context_body
            # 任务区：仅在有未完结步骤（pending / in_progress）时展示，opencode 式——
            # 无任务或全部完成 / 取消时整块隐藏（CSS 默认 display:none，update_plan 切换）
            with Vertical(id="sidebar-plan-section"):
                yield Static("计划", classes="section-title")
                with VerticalScroll(id="sidebar-plan"):
                    yield self._plan
        yield Static(f"v{__version__}", classes="sidebar-version")
        # 项目名 | 完整路径：路径过长被省略号截断时，名字保证可见（换行到版本号下方）
        root = Path(config.WORKSPACE_ROOT)
        yield Static(f"{root.name} | {root}", classes="sidebar-workspace")

    def update_usage(
        self,
        usage_title: Text | str,
        usage_body: Text | str,
        context_title: Text | str,
        context_body: Text | str,
    ) -> None:
        self._usage_title.update(usage_title)
        self._usage.update(usage_body)
        self._context_title.update(context_title)
        self._context_body.update(context_body)

    def update_plan(self, rendered: str, has_active: bool) -> None:
        """更新任务区：有未完结步骤才展示（含标题），否则整块隐藏。"""
        section = self.query_one("#sidebar-plan-section")
        section.display = has_active
        if has_active:
            self._plan.update(Text.from_ansi(rendered))


# ---------- 输入区 ----------


# 命令菜单固定展示的行数：候选超出即出现滚动条（与 CSS 里 #command-menu 的
# max-height 保持一致）
MENU_VISIBLE_ITEMS = 8


class CommandMenuItem(Static):
    """命令菜单里的一行（/命令名 + 中文描述）。"""

    def __init__(self, cmd, selected: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.cmd = cmd
        self.set_selected(selected)

    def set_selected(self, selected: bool) -> None:
        self.set_class(selected, "selected")
        mark = "› " if selected else "  "
        text = Text()
        if selected:  # 选中项整行 accent 底色（对齐权限面板的 chip 高亮）
            text.append(f"{mark}/{self.cmd.name}", style="bold black on #fab283")
            text.append(f"  {self.cmd.description}", style="black on #fab283")
        else:
            text.append(f"{mark}/{self.cmd.name}", style="#a9b1d6")
            text.append(f"  {self.cmd.description}", style="#565f89")
        self.update(text)


class CommandMenu(VerticalScroll):
    """输入框正上方的斜杠命令菜单（opencode 式）：输入 / 即弹出，
    实时前缀过滤，↑↓ 选择、Enter/Tab 补全、Esc 关闭。
    候选统一来自 commands.complete_commands()，新命令自动进菜单。
    高度按候选数自适应、封顶 MENU_VISIBLE_ITEMS 行，超出后滚动查看。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._candidates: list = []
        self._selected = 0

    @property
    def open(self) -> bool:
        return bool(self._candidates) and bool(self.display)

    def show_candidates(self, candidates) -> None:
        """候选变化（用户每敲一个字符）时重建列表，选中项回到第一个。"""
        self._candidates = list(candidates)
        self._selected = 0
        if not self._candidates:
            self.hide_menu()
            return
        self.display = True
        self.remove_children()
        self.mount_all(
            CommandMenuItem(cmd, i == self._selected, classes="menu-item")
            for i, cmd in enumerate(self._candidates)
        )
        self.scroll_to(y=0, animate=False)  # 列表重建后从顶部开始

    def hide_menu(self) -> None:
        self._candidates = []
        self.display = False

    def move(self, delta: int) -> None:
        if not self._candidates:
            return
        self._selected = (self._selected + delta) % len(self._candidates)
        items = list(self.query(CommandMenuItem))
        for i, item in enumerate(items):
            item.set_selected(i == self._selected)
        if items:
            items[self._selected].scroll_visible(animate=False)  # 选中项滚进可视区

    def accept(self) -> str | None:
        """返回当前选中项的命令名（无候选时 None）。"""
        return self._candidates[self._selected].name if self._candidates else None


class ChatInput(TextArea):
    """多行输入：Enter 发送，Shift+Enter / Ctrl+J 换行。

    命令菜单弹出期间按键让位菜单：↑↓ 移动高亮、Enter/Tab 接受补全
    （填入命令名，不发送）、Esc 关菜单；菜单关闭时行为不变。
    """

    class Submitted(Message):
        def __init__(self, value: str):
            super().__init__()
            self.value = value

    def on_key(self, event) -> None:
        menu_open = self.app.command_menu_open
        if menu_open and event.key in ("up", "down"):
            event.stop()
            event.prevent_default()
            self.app.move_command_menu(-1 if event.key == "up" else 1)
        elif menu_open and event.key in ("enter", "tab"):
            event.stop()
            event.prevent_default()
            self.app.accept_command_menu()
        elif menu_open and event.key == "escape":
            event.stop()
            event.prevent_default()
            self.app.close_command_menu()
        elif event.key in ("enter", "ctrl+enter"):
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self.text.rstrip("\n")))
        elif event.key in ("shift+enter", "ctrl+j"):
            event.stop()
            self.insert("\n")
        elif event.key == "shift+tab":
            # TextArea 会吞 tab 系按键，这里拦下交给 App 切换权限模式
            event.stop()
            event.prevent_default()
            self.app.action_cycle_permission_mode()


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
        scrollbar-gutter: stable;
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
    #command-menu {
        /* 悬浮层：dock 到聊天列底部再上移 5 行（#bottom 2 + #input-wrap 3），
           锚在输入框正上方、向上展开盖住聊天区底部，弹出/收起不改变输入框与聊天区大小 */
        dock: bottom;
        offset: 0 -5;
        layer: command-menu;
        margin: 0 2;
        height: auto;
        max-height: 8;  /* 固定展示行数，与 MENU_VISIBLE_ITEMS 一致，超出滚动 */
        padding: 0 2;
        background: #1e1e1e;
        overflow: hidden auto;
    }
    #command-menu .menu-item {
        width: 1fr;  /* 拉满整行，选中项的高亮底色才贯通 */
        height: 1;
    }
    #input {
        height: 3;
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
        height: 2;
        layout: horizontal;
        padding: 0 0 1 0;
        background: transparent;
    }
    #composer-mode, #composer-model, #composer-thinking {
        width: auto;
        padding: 0 0 0 2;
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
        Binding("shift+tab", "cycle_permission_mode", "权限模式", show=False),
    ]
    SIDEBAR_BREAKPOINT: ClassVar[int] = 120
    """终端宽度 >= 此值才显示侧边栏（46 列侧边栏 + 约 74 列聊天区）。"""
    CONTEXT_BAR_CELLS: ClassVar[int] = 10
    """底栏上下文占用条的格数：█ 填充 + ░ 空位，一格约 10%。"""

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
                # 输入框（左侧竖线）+ 底行（最左：权限模式·模型·思考·运行提示，最右：git/上下文）
                with Vertical(id="input-wrap"):
                    yield ChatInput(id="input")
                with Horizontal(id="bottom"):
                    yield Static(id="composer-mode")
                    yield Static(id="composer-model")
                    yield Static(id="composer-thinking")
                    yield RunningIndicator(id="running")
                    yield Static(id="status")
                # 斜杠命令菜单：绝对定位悬浮层（锚在输入框正上方），不挤压聊天区布局
                yield CommandMenu(id="command-menu")
            yield Sidebar(id="sidebar")

    def on_mount(self) -> None:
        renderer.set_renderer(TuiRenderer(self))
        self.query_one(ChatInput).focus()
        self.query_one("#running").display = False  # 运行动画默认隐藏
        self.query_one(CommandMenu).hide_menu()  # 命令菜单默认隐藏
        self.ui_line("SmithCode TUI（Enter 发送，Shift+Enter 换行，/help 查看命令）", "bold")
        self.ui_status()

    # ----- 斜杠命令菜单 -----

    @property
    def command_menu_open(self) -> bool:
        return self.query_one(CommandMenu).open

    def move_command_menu(self, delta: int) -> None:
        self.query_one(CommandMenu).move(delta)

    def close_command_menu(self) -> None:
        self.query_one(CommandMenu).hide_menu()

    def accept_command_menu(self) -> None:
        """接受选中命令：填入输入框（带尾随空格）并关菜单，不直接发送。"""
        name = self.query_one(CommandMenu).accept()
        if not name:
            return
        self.query_one(CommandMenu).hide_menu()
        inp = self.query_one(ChatInput)
        inp.text = f"/{name} "
        inp.move_cursor(inp.document.end)

    def on_text_area_changed(self, event) -> None:
        """输入变化实时刷新命令菜单：/ 前缀且光标仍在首 token 内才弹出。"""
        menu = self.query_one(CommandMenu)
        text = event.text_area.text
        if not text.startswith("/") or " " in text:
            menu.hide_menu()
        else:
            menu.show_candidates(commands.complete_commands(text[1:]))

    def action_cycle_permission_mode(self) -> None:
        """Shift+Tab 循环权限模式并刷新底栏。权限/提问面板弹出期间不响应（瞬时态）。"""
        if self.query(PermissionPanel) or self.query(QuestionPanel):
            return
        self.agent.permission.cycle_mode()
        self.ui_status()

    def on_resize(self, event) -> None:
        """响应式侧边栏：窗口不够宽就隐藏，够宽再显示（与 App 内部重排并存）。"""
        sidebar = self.query("#sidebar")
        if not sidebar:
            return
        show = event.size.width >= self.SIDEBAR_BREAKPOINT
        if sidebar.first().display != show:
            sidebar.first().display = show

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

    def ui_tool_preview(self, tool_id: int | None, detail: str) -> None:
        """执行前的变更预览（diff）：更新对应 pending 工具块，审核时改动已可见。"""
        widget = self._tool_widgets.get(tool_id) if tool_id is not None else None
        if widget is not None:
            widget.set_detail(detail)

    def ui_tool_result(self, tool_id: int | None, result: str, expanded: bool,
                       is_error: bool) -> None:
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
        self.query_one(Sidebar).update_plan(rendered, plan.has_active())

    def ui_focus_input(self) -> None:
        self.query_one(ChatInput).focus()

    def show_question_panel(
        self, question: str, options: list[str], multiple: bool, result: dict, evt: threading.Event
    ) -> None:
        """提问面板原地替换输入框（含框内状态行），答完由 close_composer_panel 换回。"""
        self.query_one("#input-wrap").display = False
        self.mount(
            QuestionPanel(question, options, multiple, result, evt),
            before=self.query_one("#input-wrap"),
        )

    def show_permission_panel(self, prompt: str, valid: str, hint: str, result: dict, evt: threading.Event) -> None:
        """权限申请面板原地替换输入框（含框内状态行），答完由 close_composer_panel 换回。"""
        self.query_one("#input-wrap").display = False
        self.mount(
            PermissionPanel(prompt, valid, hint, result, evt),
            before=self.query_one("#input-wrap"),
        )

    def close_composer_panel(self, panel: Vertical) -> None:
        """关闭提问/权限面板，恢复输入框（含框内状态行）并聚焦（composer 位三态的归位动作）。"""
        panel.remove()
        self.query_one("#input-wrap").display = True
        chat_input = self.query_one(ChatInput)
        chat_input.focus()

    def ui_status(self) -> None:
        usage_title, usage_body = self._sidebar_usage()
        ctx_title, ctx_body = self._sidebar_context()
        self.query_one(Sidebar).update_usage(usage_title, usage_body, ctx_title, ctx_body)
        mode, model, thinking = self._composer_status()
        self.query_one("#composer-mode").update(mode)
        self.query_one("#composer-model").update(model)
        self.query_one("#composer-thinking").update(thinking)
        self.query_one("#status").update(self._status_text())

    def _context_stats(self) -> tuple[int, int, int]:
        """当前会话的 (估算 token, 预算, 占用百分比)，侧边栏与底栏共用一份口径。"""
        est = context.total_tokens(self.agent.session.messages)
        budget = config.CONTEXT_TOKEN_BUDGET
        pct = min(100, int(est / budget * 100)) if budget else 0
        return est, budget, pct

    @staticmethod
    def _context_color(pct: int) -> str:
        """占用率对应颜色：绿（健康）→ 琥珀（偏高）→ 红（接近压缩阈值）。"""
        return "#f7768e" if pct >= 90 else "#fab283" if pct >= 70 else "#23d18b"

    def _context_bar(self, pct: int) -> Text:
        """底栏上下文占用条：█ 填充 + ░ 空位，占用率越高颜色 绿→琥珀→红。"""
        bar = Text()
        filled = round(pct / 100 * self.CONTEXT_BAR_CELLS)
        bar.append("█" * filled, style=self._context_color(pct))
        bar.append("░" * (self.CONTEXT_BAR_CELLS - filled), style="#333333")
        bar.append(f" {pct}%")
        return bar

    def _sidebar_usage(self) -> tuple[Text, Text]:
        """「用量」卡：(标题, 正文)。标题 = 用量 · 调用 N；正文 = 输入/输出（缓存命中另起一行）。"""
        usage = self.agent.session.usage.current_session
        title = Text("用量", style="#808080")
        body = Text()
        if usage.calls:
            title.append(" · 调用 ", style="#808080")
            title.append(str(usage.calls), style="#eeeeee")
            body.append("输入 ", style="#808080")
            body.append(human_tokens(usage.get("prompt_tokens")), style="#eeeeee")
            body.append(" · 输出 ", style="#808080")
            body.append(human_tokens(usage.get("completion_tokens")), style="#eeeeee")
            cache = usage.cache_hit()
            if cache:
                body.append("\n缓存命中 ", style="#808080")
                body.append(human_tokens(cache), style="#eeeeee")
        else:
            body.append("尚无调用", style="#808080")
        return title, body

    def _sidebar_context(self) -> tuple[Text, Text]:
        """「上下文」卡：(标题, 正文)。标题 = 上下文 · 占用百分比；正文 = 当前用量/预算。"""
        est, budget, pct = self._context_stats()
        title = Text("上下文 · ", style="#808080")
        title.append(f"{pct}%", style=self._context_color(pct))
        body = Text()
        body.append("当前 ", style="#808080")
        body.append(f"{human_tokens(est)} / 预算 {human_tokens(budget)}", style="#eeeeee")
        if self.agent.context.compact_count:
            body.append(f" · 已压缩 {self.agent.context.compact_count}", style="#808080")
        return title, body

    def _composer_status(self) -> tuple[Text, Text, Text]:
        """底行左侧状态：权限模式 · 模型（蓝） · 思考强度（黄）。

        后两段以灰色 `·` 前缀作分隔（与底栏右侧 / 侧边栏一致）；模式着色按风险
        递进：Smith 灰（中性）、Accept Edits 黄、Auto 橙（提示免确认范围最大）。"""
        mode_style = {"smith": "#808080", "accept_edits": "#e0af68", "auto": "#fab283"}
        mode = Text(permission.MODE_LABELS[self.agent.permission.mode], style=mode_style[self.agent.permission.mode])
        model = Text("· ", style="#808080")
        model.append(config.MODEL, style="#7aa2f7")
        thinking = Text("· ", style="#808080")
        thinking.append(config.REASONING_EFFORT or "默认", style="#e0af68")
        return mode, model, thinking

    def _status_text(self) -> Text:
        """底部状态栏：git 分支 / 上下文占用条（模型与思考强度已移入输入框内）。"""
        pieces = []
        branch = git_branch(config.WORKSPACE_ROOT)
        if branch:
            pieces.append(Text(branch))
        _, _, pct = self._context_stats()
        pieces.append(self._context_bar(pct))
        result = Text()
        for piece in pieces:
            if result:
                result.append(" · ")
            result.append_text(piece)
        return result

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
        """斜杠命令统一走 commands.dispatch，按结果标记做 TUI 侧的收尾动作。"""
        outcome = commands.dispatch(self.agent, text)
        if outcome.exit:
            self.exit()
            return
        if outcome.text is not None:
            if outcome.kind == "block":
                self.ui_block(outcome.text, outcome.style)
            else:
                self.ui_line(outcome.text, outcome.style)
        if outcome.session_reset:
            self.query_one(Sidebar).update_plan("", has_active=False)
        if outcome.refresh_status:
            self.ui_status()


def run_tui(agent) -> None:
    """启动 Textual 全屏聊天界面（仅交互 tty 模式）。"""
    SmithTUI(agent).run()