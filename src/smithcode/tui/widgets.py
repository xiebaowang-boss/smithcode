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
from .chat import (
    LEVEL_MARK,
    LEVEL_STYLE,
    Assistant,
    Block,
    ChatItem,
    Footer,
    Notice,
    StreamDelta,
    StreamEnd,
    ThinkingDelta,
    ThinkingEnd,
    ThinkingStart,
    ToolPreview,
    ToolResult,
    ToolStart,
    User,
    Welcome,
    coerce_level,
    level_from_style,
)
from .render import (
    context_category,
    context_summary,
    format_duration,
    is_unified_diff,
    render_markdown,
    side_by_side_diff,
    split_md_blocks,
)

# ---------- 线程安全的 UI 操作投递 ----------


class UiAction(Message):
    """从 worker 线程投递的一个 UI 操作（post_message 线程安全、即发即走）。"""

    def __init__(self, action: str, *args):
        super().__init__()
        self.action = action
        self.args = args


# ---------- 消息区 ----------

# 正文渲染的回退宽度（组件尚未布局、拿不到真实宽度时）与流式重排节流间隔
_BODY_FALLBACK_WIDTH = 80
_BODY_REFRESH_INTERVAL = 0.016  # ≈ 一帧

# 带 scope（子代理来源）的对话区事件：由 ChatView 路由进对应 task 块
_SCOPED_ITEMS = (
    StreamDelta,
    StreamEnd,
    ThinkingStart,
    ThinkingDelta,
    ThinkingEnd,
    ToolStart,
    ToolPreview,
    ToolResult,
)


class MessageBody(Static):
    """消息正文承载块：按当前可用宽度渲染，窗口缩放自动重排。

    正文多为 markdown（经 rich 渲染成带样式的文本），但这里不假定具体格式——
    rich 渲染会把折行**固化进 Text**，而 Textual 只能对已有文本做软换行 / 裁切，
    无法把已折好的行并回去——正文因此不能在 update() 里一次定型。与工具正文的
    `_ToolBody` 同策略：在 render() 里按 self.size.width 实时生成，窗口缩放时
    Textual 重排即自动按新宽度重渲染。

    源文本按块增量缓存（split_md_blocks）：宽度不变时复用已渲染结果，流式开销
    与原实现一致；宽度一变则整体重排。
    """

    def __init__(self, text: str = "", **kwargs) -> None:
        kwargs.setdefault("markup", False)
        super().__init__("", **kwargs)
        self._raw = text
        self._rendered = Text()
        self._width = 0  # 最近一次渲染所用的宽度
        self._done_blocks: list[str] = []  # 已完结块原文（与 _prefix 对齐）
        self._prefix: Text | None = None  # 已完结块的渲染缓存
        self._dirty = True  # 源文本有更新、待重渲染
        self._last_refresh = 0.0  # 上次触发重排的时刻（流式节流用）

    @property
    def content(self) -> Text:
        """最近一次渲染出的文本（只读；动态渲染块不适用 Static.content）。"""
        return self._rendered

    def append(self, chunk: str) -> None:
        """追加源文本（流式）；节流重排，密集 chunk 每帧最多重排一次。"""
        self._raw += chunk
        self._dirty = True
        now = time.monotonic()
        if now - self._last_refresh < _BODY_REFRESH_INTERVAL:
            return
        self._last_refresh = now
        self.refresh(layout=True)

    def finalize(self) -> None:
        """流结束：绕过节流，确保最终态立即定型。"""
        self._dirty = True
        self._last_refresh = time.monotonic()
        self.refresh(layout=True)

    def render(self) -> Text:
        width = self.size.width or self._width or _BODY_FALLBACK_WIDTH
        if width != self._width:
            # 宽度变了：旧宽度下的折行与分块缓存全部失效，整体重排
            self._width = width
            self._done_blocks = []
            self._prefix = None
            self._dirty = True
        if self._dirty:
            self._rendered = self._render_body(width)
            self._dirty = False
        return self._rendered

    def _render_body(self, width: int) -> Text:
        """按块增量渲染正文（markdown 源）：已完结块渲染一次缓存复用，尾部块整块重渲染。

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
            piece = render_markdown("\n\n".join(done[common:]), width)
            if common == 0:
                self._prefix = piece
            else:
                self._prefix = render_markdown("\n\n".join(done[:common]), width)
                self._prefix.append("\n\n")
                self._prefix.append_text(piece)
        text = Text()
        if self._prefix is not None:
            text.append_text(self._prefix)
            if tail:
                text.append("\n\n")
        text.append_text(render_markdown(tail, width))
        return text


class ChatView(VerticalScroll):
    """聊天消息区：普通消息为独立块；流式消息单块原地更新。"""

    can_focus = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._kind: str | None = None
        self._text: Text | None = None  # reasoning 等纯文本流的缓冲
        self._block: Static | None = None  # 纯文本流承载块
        self._body: MessageBody | None = None  # 正文承载块
        self._context_group: ContextGroup | None = None  # 当前进行中的「已探索」组
        self._context_groups: dict[int, ContextGroup] = {}  # 未出结果的上下文工具 → 所属组
        self._tool_widgets: dict[int, ToolCall] = {}  # tool_id → pending 中的工具行
        self._subagent_blocks: dict[int, SubAgentBlock] = {}  # task_id → 子代理块
        self._thinking_block: ThinkingBlock | None = None  # 进行中的思考块

    # ----- 唯一打印入口 -----

    def apply(self, item: ChatItem) -> None:
        """把一条语义消息挂载 / 更新到对话区（所有内容输出的唯一入口）。

        生产者只构造 ``tui/chat.py`` 的语义消息；缩进、着色、图标、间距统一
        由此分派到各私有方法 + 集中 CSS，调用点不再各自拼字符串 / 样式。
        带 scope 的事件路由进对应子代理块（task 工具块）。"""
        scope = getattr(item, "scope", None)
        if scope is not None and isinstance(item, _SCOPED_ITEMS):
            block = self._subagent_blocks.get(scope.task_id)
            if block is not None:
                at_bottom = self._at_bottom()
                block.handle_scoped(item)
                self._follow(at_bottom)
                return
        if isinstance(item, StreamDelta):
            self.append_stream(item.kind, item.text)
        elif isinstance(item, StreamEnd):
            self.end_stream()
        elif isinstance(item, ThinkingStart):
            self._thinking_begin()
        elif isinstance(item, ThinkingDelta):
            self._thinking_tick(item.text)
        elif isinstance(item, ThinkingEnd):
            self._thinking_finish()
        elif isinstance(item, ToolStart):
            self._tool_begin(item)
        elif isinstance(item, ToolPreview):
            self._tool_detail(item.tool_id, item.detail)
        elif isinstance(item, ToolResult):
            self._tool_finish(item)
        elif isinstance(item, User):
            self._user(item.text)
        elif isinstance(item, Assistant):
            self._assistant(item.text)
        elif isinstance(item, Welcome):
            self._mk(item.text, classes="welcome")
        elif isinstance(item, Notice):
            self._notice(item.text, item.level)
        elif isinstance(item, Block):
            self._notice_block(item.text, item.level)
        elif isinstance(item, Footer):
            self._footer(item)
        else:  # 防御：新增类型忘接线时快速暴露，而不是静默丢弃
            raise TypeError(f"未知对话区消息类型: {type(item).__name__}")

    # ----- 内容项 -----

    def _user(self, text: str) -> None:
        """用户消息（opencode 式）：面板底色 + 左侧角色色竖线，无前缀。

        提交自己的消息视为回到最新位置：无条件滚到底（不受锚定约束）。"""
        self._finalize_context()
        block = Static(Text(text), classes="chat-item user-msg")
        block.can_focus = False
        self.mount(block)
        self.scroll_end(animate=False)

    def _assistant(self, text: str) -> None:
        """静态整段正文（历史回放）：复用流式按块渲染，一次定型。"""
        self.begin_stream("content")
        self.append_stream("content", text)
        self.end_stream()

    def _notice(self, text: str, level) -> None:
        """系统通知：固定 1 格级别图标，正文左起点不随级别漂移。"""
        level = coerce_level(level)
        line = Text()
        line.append(f"{LEVEL_MARK[level]} ", style=LEVEL_STYLE[level])
        line.append(text, style=LEVEL_STYLE[level])
        self._mk(line, classes="notice")

    def _notice_block(self, text: str, level) -> None:
        """多行文本块：解析内嵌 ANSI，逐行按级别着色（如 /help、计划清单）。"""
        level = coerce_level(level)
        for line in Text.from_ansi(text, style=LEVEL_STYLE[level]).split("\n"):
            self._mk(line, classes="notice")

    def _footer(self, item: Footer) -> None:
        """opencode 式轮次元数据页脚：▣ 模型 · 思考强度 · 用时。

        status 非空时追加在行尾——中断收尾时显示「· 已停止」，不再另起一行。"""
        text = Text()
        text.append("▣ ", style="#fab283")
        text.append(item.model, style="#eeeeee")
        text.append(f" · {item.effort} · {item.elapsed}", style="#808080")
        if item.status:
            text.append(f" · {item.status}", style="#f7768e")
        self._mk(text, classes="turn-footer")

    # ----- 工具 / 思考生命周期 -----

    def _tool_begin(self, item: ToolStart) -> None:
        """pending 工具行：读取/搜索/列目录类归入「已探索」汇总组，其余独立成块。

        task 工具用 SubAgentBlock：子代理的嵌套事件按 scope 路由进该块。"""
        if item.name == "task":
            block = SubAgentBlock(item.summary, classes="chat-item subagent-block")
            self._subagent_blocks[item.tool_id] = block
            self._tool_widgets[item.tool_id] = block
            self.add_widget(block)
            return
        is_context = context_category(item.name) is not None
        widget = ToolCall(
            item.summary, pending=True, display=item.display, icon=item.icon,
            running_label=item.running_label,
            classes=None if is_context else "chat-item",
        )
        self._tool_widgets[item.tool_id] = widget
        self.place_tool(item.tool_id, item.name, widget)

    def _tool_detail(self, tool_id, detail: str) -> None:
        widget = self._tool_widgets.get(tool_id) if tool_id is not None else None
        if widget is not None:
            widget.set_detail(detail)

    def _tool_finish(self, item: ToolResult) -> None:
        widget = (self._tool_widgets.pop(item.tool_id, None)
                  if item.tool_id is not None else None)
        if widget is not None:
            widget.set_result(item.result, expanded=item.expand, is_error=item.is_error)
            self.mark_tool_done(item.tool_id)
        else:  # 无配对（理论上不发生）：退化为独立块，不丢结果
            self.add_widget(ToolCall(
                "[Tool]", item.result, expanded=item.expand,
                is_error=item.is_error, classes="chat-item",
            ))

    def _thinking_begin(self) -> None:
        block = ThinkingBlock(classes="chat-item")
        self._thinking_block = block
        self.add_widget(block)

    def _thinking_tick(self, chunk: str) -> None:
        if self._thinking_block is not None:
            self._thinking_block.append(chunk)

    def _thinking_finish(self) -> None:
        if self._thinking_block is not None:
            self._thinking_block.finish()
            self._thinking_block = None

    # ----- 兼容旧调用点（内部 / 测试）：新代码请优先 apply -----

    def add_line(self, text: str, style: str | None = None) -> None:
        self.apply(Notice(text, level_from_style(style)))

    def add_line_text(self, text: Text, style: str | None = None) -> None:
        """直接挂一个已构造好的 Text（内嵌样式已就绪，不再二次解析）。"""
        self._mk(text)

    def add_user(self, text: str) -> None:
        self.apply(User(text))

    def add_block(self, text: str, style: str | None = None) -> None:
        for line in text.splitlines() or [""]:
            self._mk(Text(line, style=style))

    def add_turn_footer(self, model: str, effort: str, elapsed: str,
                        status: str | None = None) -> None:
        self.apply(Footer(model, effort, elapsed, status))

    def add_widget(self, widget) -> None:
        """挂载任意消息组件（如可折叠的工具调用块）。"""
        self._finalize_context()
        at_bottom = self._at_bottom()
        self.mount(widget)
        self._follow(at_bottom)

    def place_tool(self, tool_id: int, name: str, widget) -> None:
        """放置一个工具控件：上下文类工具归入当前「已探索」组，其余独立成块。

        组只吸收**连续**的上下文工具——遇到非上下文工具、正文/思考流、回合
        结束时封口（_finalize_context）；封口后新的上下文工具另起一组。"""
        at_bottom = self._at_bottom()
        if context_category(name) is None:
            self._finalize_context()
            self.mount(widget)
        else:
            if self._context_group is None:
                self._context_group = ContextGroup(classes="context-group chat-item")
                self.mount(self._context_group)
            self._context_group.add_tool(tool_id, name, widget)
            self._context_groups[tool_id] = self._context_group
        self._follow(at_bottom)

    def mark_tool_done(self, tool_id: int) -> None:
        """上下文工具出结果：更新所属组的计数与进行中状态。"""
        group = self._context_groups.pop(tool_id, None)
        if group is not None:
            group.mark_done(tool_id)

    def reset_context(self) -> None:
        """清空分组引用（会话重置 / 聊天区清空时调用）。"""
        self._finalize_context()
        self._context_groups.clear()

    def reset(self) -> None:
        """清空对话区与全部瞬时渲染状态（会话重置 / 回放前调用）。

        工具块映射、思考块引用随聊天区一并清理，否则会滞留已卸载 widget 的引用。"""
        self._finalize_context()
        self._context_groups.clear()
        self._tool_widgets.clear()
        self._subagent_blocks.clear()
        self._thinking_block = None
        self._kind = None
        self._text = None
        self._block = None
        self._body = None
        self.remove_children()

    def _finalize_context(self) -> None:
        """封口当前「已探索」组（幂等）；后续上下文工具将另起一组。"""
        if self._context_group is not None:
            self._context_group.finalize()
            self._context_group = None

    def begin_stream(self, kind: str) -> None:
        self._kind = kind
        if kind == "content":
            # 正文走 MessageBody：按当前宽度实时渲染，窗口缩放自动重排
            self._body = MessageBody(classes="chat-item assistant-stream")
            self._body.can_focus = False
            self._mount_block(self._body)
            return
        self._text = Text("[Thinking] ", style="grey50") if kind == "reasoning" else Text()
        # 助手正文统一缩进 3（opencode 式：用户块 2 / 助手流 3，层次差产生交叉感）
        self._block = self._mk(self._text, classes="assistant-stream")

    def append_stream(self, kind: str, chunk: str) -> None:
        if kind != self._kind:
            self.end_stream()
            self.begin_stream(kind)
        if kind == "content":
            at_bottom = self._at_bottom()
            if self._body is not None:
                self._body.append(chunk)
            self._follow(at_bottom)
            return
        at_bottom = self._at_bottom()
        self._text.append(chunk, style="grey50")
        self._block.update(self._text)
        self._follow(at_bottom)

    def end_stream(self) -> None:
        """流结束：尾部块最后一次定型，与中途渲染视觉连续（不再有"文本突然
        变漂亮"的整段跳变）。

        经 rich Console 渲染为带样式的 Text（而不是直接塞 Markdown 渲染对象），
        正文块内容仍是文本——窄终端换行交给 Textual 处理，测试也能直接读文本。
        """
        if self._kind == "content" and self._body is not None:
            self._body.finalize()
        self._kind = None
        self._text = None
        self._block = None
        self._body = None

    def _mk(self, renderable, classes: str | None = None) -> Static:
        # 所有顶层消息统一带 chat-item：缩进 / 间距的唯一来源（见 app.py 的 CSS）
        block = Static(renderable, classes="chat-item" if not classes else f"chat-item {classes}")
        block.can_focus = False
        self._mount_block(block)
        return block

    def _mount_block(self, block) -> None:
        """挂载一个顶层消息组件（统一的锚定跟随与分组封口）。"""
        self._finalize_context()
        at_bottom = self._at_bottom()
        self.mount(block)
        self._follow(at_bottom)

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
    """对话执行时的运行动画：固定在输入框上方一行，执行中可见、结束隐藏。

    opencode 式：文案带实时已用秒数，start/stop 由轮次边界驱动。
    """

    can_focus = False
    FRAMES: ClassVar = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("markup", False)
        super().__init__(*args, **kwargs)
        self._frame = 0
        self._start: float | None = None
        self._stopping = False  # Esc 中断请求后置位：行尾追加「· 正在停止…」

    def on_mount(self) -> None:
        self.set_interval(0.1, self._spin)

    def start(self) -> None:
        self._start = time.monotonic()
        self._frame = 0
        self._stopping = False
        # 立即上屏初始文案并触发一次布局（默认重排）：组件初始无内容、width:auto
        # 下宽度为 0，而 _spin 的 layout=False 不再重排——若不在首次 start 定宽，
        # 第一次显示会因零宽整轮不可见（第二次起 display 翻转强制重排才恢复）
        self.update(self._spin_text(0.0))

    def stop(self) -> None:
        self._start = None

    def mark_stopping(self) -> None:
        """请求中断后调用：行尾追加「· 正在停止…」，动画与计时继续到轮次真正结束。

        stopping 会改变文案宽度，这里用默认 update（layout=True）触发一次重排，
        否则 _spin 的 layout=False 会让新增的后缀被裁掉。"""
        if self._stopping:
            return
        self._stopping = True
        if self._start is not None:
            self.update(self._spin_text(time.monotonic() - self._start))

    def _spin(self) -> None:
        if self._start is None:
            return  # 未运行：保持静止（display 已由调用方控制）
        self._frame = (self._frame + 1) % len(self.FRAMES)
        self.update(self._spin_text(time.monotonic() - self._start), layout=False)

    def _spin_text(self, elapsed: float) -> str:
        # 左对齐定宽（覆盖到 99h 的最大形态）：秒→分→秒 逐级变长不改变组件宽度，
        # 配合 layout=False 全程免布局重排（避免输入框竖线抖动）
        text = f"{self.FRAMES[self._frame]} Working… {format_duration(elapsed):<11}"
        return text + " · 正在停止…" if self._stopping else text


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


class _ToolBody(Static):
    """工具详情正文：宽度已知时把统一 diff 渲染成左右对照，否则回退逐行样式。

    Static 默认把内容固化在 update() 里，而左右对照需要知道可用列宽（布局后
    才有、且随窗口缩放变化），因此改为在 render() 里按当前宽度实时生成。"""

    def __init__(self, owner: ToolCall, **kwargs):
        super().__init__("", **kwargs)
        self._owner = owner

    def render(self):
        return self._owner._body_renderable(self.size.width)


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
                 detail: str = "", icon: str = "", running_label: str = "", **kwargs):
        super().__init__(**kwargs)
        self._summary = summary
        self._result = result
        self._expanded = expanded
        self._pending = pending
        self._is_error = is_error
        self._display = display
        self._detail = detail
        self._icon = icon  # 非空时以静态图标替代 pending 转轮（如 plan 工具）
        self._running_label = running_label  # 非空时 pending 期显示该文案（如命令「执行中」）
        self._spin_frame = 0
        self._spin_timer = None
        self._header: Static | None = None
        self._body: Static | None = None

    def compose(self):
        # 工具摘要是模型给的自由文本（URL 等），禁止 markup 解析，防止 "[" 被当标签
        self._header = Static(self._header_text(), classes="tool-header", markup=False)
        body = _ToolBody(self, classes="tool-body", markup=False)
        # pending 且已带变更预览（diff）时直接展开：审核前改动内容就可见
        body.display = self._expanded or bool(self._pending and self._detail)
        self._body = body
        yield self._header
        yield body

    def on_mount(self) -> None:
        if self._pending and not self._icon:  # 静态图标不启用转轮
            self._spin_timer = self.set_interval(0.1, self._spin)
        self._apply_state_style()

    def _header_text(self) -> str:
        if self._pending:
            if self._icon:  # 静态图标（如 plan）：pending 期也不转轮
                return f"{self._icon} {self._summary}"
            if self._running_label:  # 命令类：显式「执行中」文案 + 转轮
                return (f"{self.SPINNER[self._spin_frame]} {self._running_label}"
                        f" · {self._summary}")
            return f"{self.SPINNER[self._spin_frame]} ⚙ {self._summary}"
        mark = "▾" if self._expanded else "▸"
        stat = ""
        if self._display == "inline" and self._result:
            stat = f" · {len(self._result.splitlines())} 行"
        return f"{mark} {self._icon or '⚙'} {self._summary}{stat}"

    def _body_renderable(self, width: int) -> Text:
        """正文渲染：diff 详情优先左右对照，窄屏或非 diff 回退逐行文本。"""
        if self._detail and is_unified_diff(self._detail) and width > 0:
            max_rows = None if self._expanded else self.BLOCK_COLLAPSE_LINES
            rendered = side_by_side_diff(self._detail, width, max_rows=max_rows)
            if rendered is not None:
                if self._result and self._show_result():
                    rendered.append("\n\n")
                    for line in self._result.splitlines():
                        rendered.append(line + "\n", style=self._line_style(line))
                return rendered
        return self._body_content()

    def _show_result(self) -> bool:
        """结果文本是否展示：带 diff 详情且执行成功时确认语冗余（审核时已看过改动），
        不再重复；失败（错误）必须展示，无 diff 的工具照常展示。"""
        return not (self._detail and not self._is_error)

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
        if self._result and self._show_result():
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
            self._body.refresh(layout=True)
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
            self._body.refresh(layout=True)
            self._body.display = expanded
        self._apply_state_style()

    def action_toggle(self) -> None:
        if self._pending and not self._detail:
            return  # 结果未到且无变更预览，没有内容可展开
        self._expanded = not self._expanded
        self.query_one(".tool-header").update(self._header_text())
        body = self.query_one(".tool-body")
        body.display = self._expanded
        body.refresh(layout=True)

    def on_click(self, event) -> None:
        event.stop()
        self.action_toggle()


# ---------- 子代理 task 块 ----------


class SubAgentBlock(ToolCall):
    """task 工具块：承载子代理的嵌套活动（子工具行、流字符计数）与最终报告。

    复用 ToolCall 的 header/body 生命周期：task 自身的 pending → result 由
    父级事件驱动；带 scope 的子代理事件经 `handle_scoped` 挂进内部活动区。
    子代理结束时仍未返回结果的子工具行统一收尾，不留 pending 转轮。
    """

    def __init__(self, summary: str, **kwargs):
        super().__init__(summary, pending=True, display="block", **kwargs)
        self._child_widgets: dict[int, ToolCall] = {}
        self._pending_children: list[ToolCall] = []
        self._activity: Vertical | None = None
        self._child_count = 0
        self._stream_chars = 0

    def compose(self):
        yield from super().compose()
        self._activity = Vertical(classes="subagent-activity")
        yield self._activity

    def on_mount(self) -> None:
        super().on_mount()
        if self._activity is not None:
            for child in self._pending_children:
                self._activity.mount(child)
        self._pending_children = []

    def handle_scoped(self, item) -> None:
        """处理一个带 scope 的子代理事件（由 ChatView 路由）。"""
        if isinstance(item, ToolStart):
            child = ToolCall(
                item.summary, pending=True, display=item.display,
                icon=item.icon, running_label=item.running_label,
                classes="subagent-tool",
            )
            self._child_widgets[item.tool_id] = child
            self._child_count += 1
            if self._activity is not None:
                self._activity.mount(child)
            else:
                self._pending_children.append(child)
        elif isinstance(item, ToolPreview):
            child = self._child_widgets.get(item.tool_id)
            if child is not None:
                child.set_detail(item.detail)
        elif isinstance(item, ToolResult):
            child = self._child_widgets.get(item.tool_id)
            if child is not None:
                child.set_result(item.result, expanded=item.expand, is_error=item.is_error)
                self._child_widgets.pop(item.tool_id, None)
        elif isinstance(item, (ThinkingDelta, StreamDelta)):
            self._stream_chars += len(item.text)
        self._refresh_header()

    def set_result(self, result: str, *, expanded: bool, is_error: bool) -> None:
        """父级 task 结果到达：先收尾仍 pending 的子工具行，再走标准结果渲染。"""
        for child in list(self._child_widgets.values()):
            if child._pending:
                child.set_result(
                    "（子任务结束时未单独返回结果）", expanded=False, is_error=False
                )
        self._child_widgets.clear()
        super().set_result(result, expanded=expanded, is_error=is_error)
        self._refresh_header()

    def _header_text(self) -> str:
        text = super()._header_text()
        if self._pending and self._child_count:
            text += f" · {self._child_count} 个子调用"
            if self._stream_chars:
                text += f" · {self._stream_chars:,} 字符"
        return text

    def _refresh_header(self) -> None:
        if self._header is not None:
            self._header.update(self._header_text())


# ---------- 上下文收集汇总块 ----------


class ContextGroup(Vertical):
    """连续的读取 / 搜索 / 列目录工具汇总块（opencode 式「已探索」）。

    头行汇总各类计数：进行中 `⠋ ⚙ 正在探索 · 3 次读取，2 次搜索`，完成后
    `▸ ⚙ 已探索 · …`；逐条明细是隐藏的子 ToolCall，展开可见。分组边界由
    ChatView 控制（遇到非上下文工具、正文/思考流、回合结束时封口）。
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
        self._items: list[tuple[str, ToolCall]] = []  # (工具名, 子控件)
        self._pending: set[int] = set()  # 未出结果的 tool_id
        self._pending_children: list[ToolCall] = []  # compose 前挂载缓冲
        self._finalized = False
        self._spin_frame = 0
        self._spin_timer = None
        self._header: Static | None = None
        self._body: Vertical | None = None

    def compose(self):
        self._header = Static(self._header_text(), classes="group-header", markup=False)
        self._body = Vertical(classes="group-body")
        self._body.display = self._expanded
        yield self._header
        yield self._body

    def on_mount(self) -> None:
        for child in self._pending_children:
            self._body.mount(child)
        self._pending_children = []
        self._sync_spinner()

    def add_tool(self, tool_id: int, name: str, widget: ToolCall) -> None:
        """纳入一个上下文工具；compose 前到达的子控件先缓冲，挂载后补挂。"""
        self._items.append((name, widget))
        self._pending.add(tool_id)
        if self._body is not None:
            self._body.mount(widget)
        else:
            self._pending_children.append(widget)
        self._sync_spinner()
        self._refresh_header()

    def mark_done(self, tool_id: int) -> None:
        """某个子工具出结果：更新计数与进行中状态。"""
        self._pending.discard(tool_id)
        self._sync_spinner()
        self._refresh_header()

    def finalize(self) -> None:
        """封口：不再接收新工具，停掉转轮（幂等）。"""
        self._finalized = True
        if self._spin_timer is not None:
            self._spin_timer.stop()
            self._spin_timer = None
        self._refresh_header()

    def _counts(self) -> dict:
        counts = {"read": 0, "search": 0}
        for name, _ in self._items:
            category = context_category(name)
            if category:
                counts[category] += 1
        return counts

    def _header_text(self) -> str:
        arrow = "▾" if self._expanded else "▸"
        summary = context_summary(self._counts())
        if self._pending and not self._finalized:
            return f"{arrow} {self.SPINNER[self._spin_frame]} ⚙ 正在探索 · {summary}"
        return f"{arrow} ⚙ 已探索 · {summary}"

    def _sync_spinner(self) -> None:
        running = self._pending and not self._finalized
        if running and self._spin_timer is None and self._header is not None:
            self._spin_timer = self.set_interval(0.1, self._spin)
        elif not running and self._spin_timer is not None:
            self._spin_timer.stop()
            self._spin_timer = None

    def _spin(self) -> None:
        self._spin_frame = (self._spin_frame + 1) % len(self.SPINNER)
        if self._header is not None:
            self._header.update(self._header_text(), layout=False)

    def _refresh_header(self) -> None:
        if self._header is not None:
            self._header.update(self._header_text())

    def action_toggle(self) -> None:
        self._expanded = not self._expanded
        if self._header is not None:
            self._header.update(self._header_text())
        if self._body is not None:
            self._body.display = self._expanded
            self._body.refresh()

    def on_click(self, event) -> None:
        event.stop()
        self.action_toggle()


# ---------- 常驻侧边栏 ----------


class Sidebar(Vertical):
    """右侧常驻面板：两张小卡片「用量」「上下文」，中间计划，底部版本号。"""

    can_focus = False
    DEFAULT_CSS = """
    Sidebar .section-title { color: #808080; text-style: bold; }
    Sidebar .sidebar-title {
        color: #7dcfff;
        text-style: bold;
        height: 1;
        margin-bottom: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
        display: none;
    }
    Sidebar #sidebar-top { height: 1fr; }
    Sidebar #sidebar-goal-section { height: auto; display: none; margin-bottom: 1; }
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
        self._goal_title = Static("目标", classes="section-title")
        self._goal = Static("", classes="goal-body")
        self._plan = Static("（暂无任务计划）", classes="plan-body")
        self._title = Static("", classes="sidebar-title")

    def compose(self):
        # 会话标题置顶（/rename 或后台自动生成；无内容/新会话时整段隐藏）
        yield self._title
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
            # 目标区：持久目标（/goal）存在时展示，位于计划区上方；无目标整块隐藏
            with Vertical(id="sidebar-goal-section"):
                yield self._goal_title
                yield self._goal
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

    def update_goal(self, snapshot) -> None:
        """更新目标区：snapshot 为 (标题 Text, 正文) 或 None（无目标则整块隐藏）。"""
        section = self.query_one("#sidebar-goal-section")
        if snapshot is None:
            section.display = False
            return
        title, body = snapshot
        self._goal_title.update(title)
        self._goal.update(body)
        section.display = True

    def update_title(self, title: Text) -> None:
        """更新侧边栏顶部的会话标题；空内容整段隐藏。"""
        self._title.update(title)
        self._title.display = bool(str(title))


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
