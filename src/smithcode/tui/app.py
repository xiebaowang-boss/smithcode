"""Textual 聊天界面（复刻 Claude Code 风格）：应用组装层。

- 上半：ChatView 消息区（流式回复、可折叠工具调用块）
- 右上：计划侧边栏（todo 清单实时更新，Ctrl+O 切换显示）
- 下半：多行输入框 + 状态栏 + Footer 快捷键提示

Agent 在后台线程同步运行，TuiRenderer 用 post_message（线程安全）把事件桥到
主线程；权限确认 / ask_user 通过 ModalScreen 弹窗阻塞等待。非交互模式不走
这里（cli 负责分流），仍用 ConsoleRenderer。

本文件只负责「接线」：控件在 widgets.py、弹窗面板在 panels.py、线程桥在
bridge.py、纯函数工具在 render.py。样式（CSS）集中在本文件的 SmithTUI.CSS。
"""
from __future__ import annotations

import threading
import time
from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from .. import commands, config, permission, plan, renderer, welcome
from ..llm import context
from .bridge import TuiRenderer
from .panels import (
    PermissionPanel,
    QuestionPanel,
    SelectionItem,
    SelectionPanel,
    SelectionScreen,
)
from .render import format_duration, git_branch, human_tokens
from .widgets import (
    ChatInput,
    ChatView,
    CommandMenu,
    RunningIndicator,
    Sidebar,
    ThinkingBlock,
    ToolCall,
    UiAction,
)


class SmithTUI(App):
    CSS = """
    Screen { layout: horizontal; }
    #main { width: 1fr; height: 100%; layout: horizontal; }
    #chat-col { width: 1fr; height: 100%; background: #0a0a0a; }
    #chat {
        height: 1fr;
        padding: 1 2 1 2;
        background: #0a0a0a;
        /* Claude Code 式：不画滚动条（滚动功能不受影响）。
           注意不能配 scrollbar-gutter: stable——两者同用会让 virtual_size 塌缩、滚动失效 */
        scrollbar-size-vertical: 0;
    }
    #sidebar {
        height: 100%;
        width: 46;
        padding: 1 2 1 2;
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
        max-height: 8;  /* 固定展示行数，与 widgets.MENU_VISIBLE_ITEMS 一致，超出滚动 */
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
    .assistant-stream { padding-left: 3; margin-top: 1; }

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
    /* 通用选择弹窗：居中卡片（遮罩/变暗由 SelectionScreen 的 ModalScreen 背景负责） */
    SelectionPanel {
        width: 64;
        max-width: 90%;
        height: auto;
        min-height: 16;   /* 比内容高，短列表也保持足够高度 */
        max-height: 70%;
        background: #1e1e1e;
        padding: 1 2;
    }
    SelectionPanel .selection-title {
        color: #fab283;
        text-style: bold;
        padding-left: 2;    /* 与选项文字对齐（选项行首为 2 列标记位） */
        margin-bottom: 1;   /* 标题与选项区之间留一行间隔 */
    }
    SelectionPanel .selection-body { height: auto; }
    SelectionPanel .selection-scroll { height: 1fr; }
    SelectionPanel .selection-hint { color: #808080; dock: bottom; }
    .turn-footer {
        padding-left: 3;
        margin-top: 1;
    }
    """

    BINDINGS: ClassVar = [
        Binding("ctrl+q", "quit", "退出"),
        Binding("shift+tab", "cycle_permission_mode", "权限模式", show=False),
        Binding("escape", "interrupt", "中断", show=False),
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
        self._show_welcome()
        self.ui_status()

    def _show_welcome(self) -> None:
        """在聊天区渲染欢迎横幅（启动与 /new 后复用）。

        终端放得下就用完整版（Logo），太窄降级为单行紧凑版。"""
        width = self.size.width
        self.query_one(ChatView).add_line_text(
            welcome.banner(compact=width < welcome.LOGO_WIDTH + 12)
        )

    # ----- 斜杠命令菜单 -----

    @property
    def command_menu_open(self) -> bool:
        return self.query_one(CommandMenu).open

    def move_command_menu(self, delta: int) -> None:
        self.query_one(CommandMenu).move(delta)

    def close_command_menu(self) -> None:
        self.query_one(CommandMenu).hide_menu()

    def accept_command_menu(self) -> None:
        """接受选中命令：立即执行（immediate）或填入输入框（默认），随后关菜单。"""
        cmd = self.query_one(CommandMenu).accept_command()
        if cmd is None:
            return
        self.activate_command(cmd)

    def activate_command(self, cmd) -> None:
        """菜单项被选中/点击后的统一入口，按命令的 immediate 标记分流。

        - immediate：清空输入框并立即 dispatch（如 /model 直接弹选择框）；
        - 否则：把 `/{name} ` 填入输入框，等用户补参数或回车。
        """
        self.query_one(CommandMenu).hide_menu()
        inp = self.query_one(ChatInput)
        if cmd.immediate:
            inp.clear()
            self.handle_command("/" + cmd.name)
        else:
            inp.text = f"/{cmd.name} "
            inp.move_cursor(inp.document.end)
            inp.focus()

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

    def action_interrupt(self) -> None:
        """Esc：任务运行中请求中断；空闲且无弹层时清空输入框（Claude Code 式）。

        各弹层（权限 / 提问面板、选择弹窗、命令菜单）的 Esc 各有自己的
        取消语义且焦点在内时按键先被其消费，一般走不到这里；此处守卫是
        兜底（焦点不在弹层输入上时仍不干扰其语义）。
        """
        if self._busy:
            self.agent.interrupt()
            self.ui_line("（正在停止…）", "grey50")
            return
        if (self.query(PermissionPanel) or self.query(QuestionPanel)
                or self.command_menu_open or isinstance(self.screen, ModalScreen)):
            return
        self.query_one(ChatInput).clear()

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

    # ----- 通用选择面板（居中弹窗） -----

    def show_selection(self, select) -> None:
        """按命令的选择意图弹出居中选择弹窗（如 /model 无参）。

        ModalScreen 的半透明背景让底层界面轻微变暗（而非全黑）；选中后重新
        分发 `/<command> <value>`，复用命令的参数路径做实际动作；Esc 取消
        （value 为 None）不产生任何副作用。
        """
        self.query_one(CommandMenu).hide_menu()
        items = [
            SelectionItem(
                label=choice.label,
                value=choice.value,
                description=choice.description,
                current=choice.current,
            )
            for choice in select.items
        ]
        panel = SelectionPanel(
            select.title, items, lambda value: self.screen.dismiss(value)
        )
        self.push_screen(
            SelectionScreen(panel),
            callback=lambda value: self._after_selection(select, value),
        )

    def _after_selection(self, select, value) -> None:
        if value is not None:
            self.handle_command(f"/{select.command} {value}")

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
        """「用量」卡：(标题, 正文)。标题 = Usage（有调用时附 · Calls N）；正文 = In/Out（缓存命中另起一行）。"""
        usage = self.agent.session.usage.current_session
        title = Text("Usage", style="#808080")
        if usage.calls:
            title.append(" · Calls ", style="#808080")
            title.append(str(usage.calls), style="#eeeeee")
        body = Text()
        body.append("In ", style="#808080")
        body.append(human_tokens(usage.get("prompt_tokens")), style="#eeeeee")
        body.append(" · Out ", style="#808080")
        body.append(human_tokens(usage.get("completion_tokens")), style="#eeeeee")
        cache = usage.cache_hit()
        if cache:
            body.append("\nCache ", style="#808080")
            body.append(human_tokens(cache), style="#eeeeee")
        return title, body

    def _sidebar_context(self) -> tuple[Text, Text]:
        """「上下文」卡：(标题, 正文)。标题 = Context · 占用百分比；正文 = 当前用量/预算。"""
        est, budget, pct = self._context_stats()
        title = Text("Context · ", style="#808080")
        title.append(f"{pct}%", style=self._context_color(pct))
        body = Text()
        body.append("Used ", style="#808080")
        body.append(f"{human_tokens(est)} / Budget {human_tokens(budget)}", style="#eeeeee")
        if self.agent.context.compact_count:
            body.append(f" · Compacted {self.agent.context.compact_count}", style="#808080")
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
        thinking.append(config.REASONING_EFFORT or config.DEFAULT_EFFORT, style="#e0af68")
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
        """轮次结束：在会话末尾追加 opencode 式元数据页脚「▣ 模型 · 思考强度 · 用时」。"""
        if self._turn_start is None:
            return
        elapsed = time.monotonic() - self._turn_start
        self._turn_start = None
        found = self.query(ChatView)
        if found:
            found.first().add_turn_footer(
                config.MODEL,
                config.REASONING_EFFORT or config.DEFAULT_EFFORT,
                format_duration(elapsed),
            )

    def handle_command(self, text: str) -> None:
        """斜杠命令统一走 commands.dispatch，按结果标记做 TUI 侧的收尾动作。"""
        # 对齐 opencode 的 busy 拒绝：任务运行中后台线程还在写消息历史，
        # 中途重置会撕裂进行中的轮次（工具结果落入悬空的新会话），先行拦截
        tokens = text.strip().split()
        if self._busy and tokens and tokens[0].lower() == "/new":
            self.ui_line("（任务运行中，不能开启新会话；请等待完成或先按 Esc 中断）", "yellow")
            return
        outcome = commands.dispatch(self.agent, text)
        if outcome.exit:
            self.exit()
            return
        if outcome.session_reset:
            # /new：清空聊天区本身就是全部反馈，不再追加提示文本（REPL 侧仍有文字）
            self.reset_chat()
        else:
            if outcome.text is not None:
                if outcome.kind == "block":
                    self.ui_block(outcome.text, outcome.style)
                else:
                    self.ui_line(outcome.text, outcome.style)
            if outcome.select is not None:
                self.show_selection(outcome.select)
        if outcome.session_reset:
            self.query_one(Sidebar).update_plan("", has_active=False)
        if outcome.refresh_status:
            self.ui_status()

    def reset_chat(self) -> None:
        """开新会话：清空聊天区并重新渲染欢迎横幅，屏幕回归会话起点。

        连带清掉残留的瞬时渲染状态：工具块映射（widget 已随聊天区移除，
        映射不清理会滞留旧引用）、思考块与轮次计时。"""
        self.query_one(ChatView).remove_children()
        self._tool_widgets.clear()
        self._thinking_block = None
        self._turn_start = None
        self._show_welcome()


def run_tui(agent) -> None:
    """启动 Textual 全屏聊天界面（仅交互 tty 模式）。"""
    SmithTUI(agent).run()
