"""TUI 消息区控件：消息区、运行动画、思考/工具折叠块、侧边栏、命令菜单、输入框。

所有控件都是自包含的：不反向调用 App 方法（经 `self.app` 的运行期属性或
post_message 向上通信），可独立测试；样式集中在 app.py 的 SmithTUI.CSS 统一管理。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Static, TextArea

from .. import __version__, config, renderer
from .render import format_duration, render_markdown, split_md_blocks

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
        self._raw = ""  # 正文段落纯文本累积，按块增量转 markdown 渲染
        self._block: Static | None = None
        self._prefix: Text | None = None  # 已渲染完结块的缓存（与 _done_blocks 对齐）
        self._done_blocks: list[str] = []  # 已渲染完结块的原文，内容对齐防计数漂移
        self._last_render = 0.0  # 上次渲染时刻（尾部块重渲染节流用）

    def add_line(self, text: str, style: str | None = None) -> None:
        self._mk(Text.from_ansi(text, style=style))

    def add_line_text(self, text: Text, style: str | None = None) -> None:
        """直接挂一个已构造好的 Text（内嵌样式已就绪，不再二次解析）。"""
        self._mk(text)

    def add_user(self, text: str) -> None:
        """用户消息（opencode 式）：面板底色 + 左侧角色色竖线，无前缀。

        提交自己的消息视为回到最新位置：无条件滚到底（不受锚定约束）。"""
        block = Static(Text(text), classes="user-msg")
        block.can_focus = False
        self.mount(block)
        self.scroll_end(animate=False)

    def add_block(self, text: str, style: str | None = None) -> None:
        for line in text.splitlines() or [""]:
            self._mk(Text(line, style=style))

    def add_turn_footer(self, model: str, effort: str, elapsed: str) -> None:
        """opencode 式轮次元数据页脚：▣ 模型 · 思考强度 · 用时（▣ 用强调色，缩进 3 格）。"""
        at_bottom = self._at_bottom()
        text = Text()
        text.append("▣ ", style="#fab283")
        text.append(model, style="#eeeeee")
        text.append(f" · {effort} · {elapsed}", style="#808080")
        block = Static(text, classes="turn-footer")
        block.can_focus = False
        self.mount(block)
        self._follow(at_bottom)

    def add_widget(self, widget) -> None:
        """挂载任意消息组件（如可折叠的工具调用块）。"""
        at_bottom = self._at_bottom()
        self.mount(widget)
        self._follow(at_bottom)

    def begin_stream(self, kind: str) -> None:
        self._kind = kind
        self._text = Text()
        self._raw = ""
        self._prefix = None
        self._done_blocks = []
        self._last_render = 0.0
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
            self._refresh_markdown()
            return
        at_bottom = self._at_bottom()
        self._text.append(chunk, style="grey50")
        self._block.update(self._text)
        self._follow(at_bottom)

    def _refresh_markdown(self) -> None:
        """按块增量渲染（Claude Code / opencode 式）：已完结块渲染一次缓存
        复用，尾部未完结块整块重渲染并节流（16ms ≈ 一帧）。

        完结块对齐按**内容**而非数量：流式 chunk 可能把"  "前导空格先送来
        被误判成空行（块提前完结），下个 chunk 又让它缩回未完结——数量对齐
        会漏渲后续块（内容丢失），逐块内容比对则能自动发现并重建前缀。
        """
        done, tail = split_md_blocks(self._raw)
        # 与已渲染前缀逐块比对：找公共前缀长度；不同即从该块起重建
        common = 0
        for a, b in zip(self._done_blocks, done):
            if a != b:
                break
            common += 1
        if common < len(done) or common < len(self._done_blocks):
            self._done_blocks = done[:common]
            piece = render_markdown("\n\n".join(done[common:]), self._block.size.width or 80)
            if common == 0:
                self._prefix = piece
            else:
                self._prefix = render_markdown(
                    "\n\n".join(done[:common]), self._block.size.width or 80
                )
                self._prefix.append("\n\n")
                self._prefix.append_text(piece)
            self._last_render = 0.0  # 块完结不受节流约束
        if self._last_render and time.monotonic() - self._last_render < 0.016:
            return
        self._last_render = time.monotonic()
        at_bottom = self._at_bottom()
        self._text = Text()
        if self._prefix is not None:
            self._text.append_text(self._prefix)
            if tail:
                self._text.append("\n\n")
        self._text.append_text(render_markdown(tail, self._block.size.width or 80))
        self._block.update(self._text)
        self._follow(at_bottom)

    def end_stream(self) -> None:
        """流结束：走与流式期间相同的按块增量渲染路径，最后只剩尾部块的
        一次定型，与中途渲染视觉连续（不再有"文本突然变漂亮"的整段跳变）。

        经 rich Console 渲染为带样式的 Text（而不是直接塞 Markdown 渲染对象），
        Static 内容仍是文本——窄终端换行交给 Textual 处理，测试也能直接读文本。
        """
        if self._kind == "content" and self._block is not None and self._raw.strip():
            self._last_render = 0.0  # 结束时绕过节流，确保最终态立即定型
            self._refresh_markdown()
        self._kind = None
        self._text = None
        self._raw = ""
        self._prefix = None
        self._done_blocks = []
        self._block = None

    def _mk(self, renderable, classes: str | None = None) -> Static:
        at_bottom = self._at_bottom()
        block = Static(renderable, classes=classes)
        block.can_focus = False
        self.mount(block)
        self._follow(at_bottom)
        return block

    # ----- 底部锚定跟随 -----

    def _at_bottom(self) -> bool:
        """当前是否贴在底部（留 1 行容差）。必须在挂载/更新内容**之前**取值——
        新内容一进来 virtual_size 就涨，贴底判断会被误判成"用户已翻走"。"""
        return self.scroll_offset.y >= self.max_scroll_y - 1

    def _follow(self, at_bottom: bool) -> None:
        """底部锚定跟随：用户贴底时才滚到底；翻看历史时保持位置不打断。"""
        if at_bottom:
            self.scroll_end(animate=False)


# ---------- 运行中动画 ----------


class RunningIndicator(Static):
    """对话执行时的运行动画：显示在输入框下方，执行中可见、结束隐藏。

    opencode 式：文案带实时已用秒数，start/stop 由轮次边界驱动。
    """

    can_focus = False
    FRAMES: ClassVar = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("markup", False)
        super().__init__(*args, **kwargs)
        self._frame = 0
        self._start: float | None = None

    def on_mount(self) -> None:
        self.set_interval(0.1, self._spin)

    def start(self) -> None:
        self._start = time.monotonic()
        self._frame = 0
        # 立即上屏初始文案并触发一次布局（默认重排）：组件初始无内容、width:auto
        # 下宽度为 0，而 _spin 的 layout=False 不再重排——若不在首次 start 定宽，
        # 第一次显示会因零宽整轮不可见（第二次起 display 翻转强制重排才恢复）
        self.update(self._spin_text(0.0))

    def stop(self) -> None:
        self._start = None

    def _spin(self) -> None:
        if self._start is None:
            return  # 未运行：保持静止（display 已由调用方控制）
        self._frame = (self._frame + 1) % len(self.FRAMES)
        self.update(self._spin_text(time.monotonic() - self._start), layout=False)

    def _spin_text(self, elapsed: float) -> str:
        # 左对齐定宽（覆盖到 99h 的最大形态）：秒→分→秒 逐级变长不改变组件宽度，
        # 配合 layout=False 全程免布局重排（避免输入框竖线抖动）
        return f"{self.FRAMES[self._frame]} Working… {format_duration(elapsed):<11}"


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
        self._header = Static(self._running_text(), classes="think-header", markup=False)
        self._body = Static("", classes="think-body", markup=False)
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
        # 工具摘要是模型给的自由文本（URL 等），禁止 markup 解析，防止 "[" 被当标签
        self._header = Static(self._header_text(), classes="tool-header", markup=False)
        body = Static(self._body_content(), classes="tool-body", markup=False)
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
        margin-bottom: 1;
    }
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._usage_title = Static("Usage", classes="section-title")
        self._usage = Static("", classes="usage-body")
        self._context_title = Static("Context", classes="section-title")
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


# 命令菜单固定展示的行数：候选超出即出现滚动条（与 app.py 里 #command-menu 的
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

    def on_click(self, event) -> None:
        """鼠标点击菜单项：与 Enter/Tab 接受选中项走同一入口。"""
        event.stop()
        self.app.activate_command(self.cmd)


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

    def accept_command(self):
        """返回当前选中的 Command 对象（无候选时 None）。"""
        return self._candidates[self._selected] if self._candidates else None


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
