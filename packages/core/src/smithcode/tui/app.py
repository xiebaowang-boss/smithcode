"""Textual 聊天界面（复刻 Claude Code 风格）：应用组装层。

- 上半：ChatView 消息区（流式回复、可折叠工具调用块）
- 右上：侧边栏（终端够宽时显示：会话标题、用量、目标卡片、计划清单）
- 下半：多行输入框 + 状态栏 + Footer 快捷键提示

Agent 在后台线程同步运行，TuiRenderer 用 post_message（线程安全）把事件桥到
主线程；权限确认 / ask_user 通过 ModalScreen 弹窗阻塞等待。非交互模式不走
这里（cli 负责分流），仍用 ConsoleRenderer。

本文件只负责「接线」：控件在 widgets.py、弹窗面板在 panels.py、线程桥在
bridge.py、纯函数工具在 render.py。样式集中在本目录的 app.tcss（经 SmithTUI.CSS_PATH
加载），本文件不再内嵌 CSS。
"""
from __future__ import annotations

import time
from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from .. import (
    commands,
    config,
    context,
    goal,
    permission,
    plan,
    skills,
    title,
    welcome,
)
from ..agent import format_stream_interrupted
from ..frontend import attach as attach_frontend
from ..mcp.errors import McpConfigError
from ..mcp.wizard import McpWizard, apply_plan
from . import clipboard
from .chat import (
    Assistant,
    Block,
    Footer,
    Level,
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
from .frontend import TuiFrontend
from .panels import (
    McpWizardPanel,
    McpWizardScreen,
    PermissionPanel,
    QuestionPanel,
    SelectionItem,
    SelectionPanel,
    SelectionScreen,
)
from .render import footer_suffix, format_duration, git_branch, human_tokens
from .widgets import (
    ChatInput,
    ChatView,
    CommandMenu,
    QueuePanel,
    RunningIndicator,
    Sidebar,
    UiAction,
)

# 侧边栏目标卡片标题的着色：进行中橙、暂停黄、完成绿、受阻/预算红
_GOAL_STATUS_COLORS = {
    "active": "#fab283",
    "paused": "#e0af68",
    "complete": "#23d18b",
    "blocked": "#f7768e",
    "budget_limited": "#f7768e",
}

# 计划工具（todo_write）用静态清单图标，不用 pending 转轮——它的执行是瞬时的，
# 转轮既无意义又容易被误认为"卡住/一直转"
_PLAN_ICON = "☰"


def _message_text(content) -> str:
    """历史消息 content 的纯文本（兼容多模态列表）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return ""


class _InboxView:
    """排队面板的视图状态：由 `Inbox*` 事件维护（增 / 投递 / 撤销 / 清空）。

    面板需要的是"当前排着什么"，而事件是"发生了什么"——这一层就是两者的转换点
    （事件溯源里的投影）。放在 app 侧而不是 frontend：它服务于这一个界面。
    """

    def __init__(self) -> None:
        self._by_id: dict = {}

    def add(self, item) -> None:
        self._by_id[item.id] = item

    def pop(self, item_id: str) -> None:
        self._by_id.pop(item_id, None)

    def clear(self) -> None:
        self._by_id.clear()

    @property
    def steering(self) -> list:
        return [i for i in self._by_id.values() if i.kind == "steer"]

    @property
    def follow_up(self) -> list:
        return [i for i in self._by_id.values() if i.kind == "follow_up"]


class SmithTUI(App):
    # 全部样式集中在同目录的 app.tcss（Textual 的 CSS_PATH，相对本文件解析），
    # 本文件只管布局接线。
    CSS_PATH: ClassVar[str] = "app.tcss"

    BINDINGS: ClassVar = [
        Binding("ctrl+q", "quit", "退出"),
        Binding("shift+tab", "cycle_permission_mode", "权限模式", show=False),
        Binding("escape", "interrupt", "中断", show=False),
    ]
    SIDEBAR_BREAKPOINT: ClassVar[int] = 120
    """终端宽度 >= 此值才显示侧边栏（40 列侧边栏 + 约 80 列聊天区）。"""

    def __init__(self, agent):
        super().__init__()
        self.agent = agent
        self._busy = False
        self._turn_start: float | None = None
        self._turn_reason = ""  # 本轮 stream_error 的失败原因（页脚带出，便于排障）
        # 本界面的前端（on_mount 装配后填入）：界面收尾时要靠它唤醒挂起的
        # 弹窗等待方，见 TuiFrontend.abandon_pending
        self._frontend: TuiFrontend | None = None
        self._attached = None  # frontend.attach 的两枚复位令牌（收尾用）
        self._usage = None  # 最新一条 UsageChanged（侧边栏只从事件渲染）
        # 排队面板的视图：由 Inbox* 事件维护（前端不再收快照、自己算差异）
        self._inbox = _InboxView()
        self._open_panel = None  # 当前打开的提问/权限面板（收尾时要关掉）
        # 选择面板的层级栈：[(父级 CommandSelect, 进入下级时选中的值)]，
        # Esc 未选中时逐级返回（锚点让光标落回原行），执行动作后清空
        self._select_stack: list = []

    @property
    def _tool_widgets(self):
        """当前 pending 工具块映射（代理到 ChatView；瞬时状态收归消息区）。"""
        return self.query_one(ChatView)._tool_widgets

    def _chat(self) -> ChatView:
        return self.query_one(ChatView)

    def _notice(self, text: str, level=Level.INFO) -> None:
        """对话区系统通知的统一出口（信息 / 警告 / 错误）。"""
        self._chat().apply(Notice(text, level))

    def copy_to_clipboard(self, text: str) -> None:
        """复制到系统剪贴板：优先系统工具，失败退回 Textual 的 OSC 52。

        Textual 默认只写 OSC 52 序列，VTE 系终端（GNOME Terminal / Console /
        Tilix / xfce4-terminal）不支持该序列，复制会静默失败；改用 wl-copy /
        xclip / xsel 等直接写系统剪贴板（见 tui/clipboard）。`_clipboard` 同时
        更新，保持应用内粘贴语义与 Textual 一致。
        """
        self._clipboard = text
        if clipboard.copy_to_system(text):
            return
        super().copy_to_clipboard(text)

    def compose(self) -> ComposeResult:
        # opencode 式布局：侧边栏通高居右；对话列（消息区 + 输入框 + 状态行）居左
        with Horizontal(id="main"):
            with Vertical(id="chat-col"):
                yield ChatView(id="chat")
                # 输入框（自身带左侧竖线，框内上方为运行动画）+ 底行（最左：权限模式·模型·思考·目标，最右：git/上下文）
                with Vertical(id="input-wrap"):
                    yield RunningIndicator(id="running")
                    # 排队面板：运行动画之下、输入框之上（计划 §5(4)）；空则隐藏
                    yield QueuePanel(id="queued")
                    yield ChatInput(id="input")
                with Horizontal(id="bottom"):
                    yield Static(id="composer-mode")
                    yield Static(id="composer-model")
                    yield Static(id="composer-thinking")
                    yield Static(id="composer-goal")
                    yield Static(id="status")
                # 斜杠命令菜单：绝对定位悬浮层（锚在输入框正上方），不挤压聊天区布局
                yield CommandMenu(id="command-menu")
            yield Sidebar(id="sidebar")

    def on_mount(self) -> None:
        # 装配前端：事件只有一条路（订阅 agent 的事件总线），询问端口挂在当前
        # 上下文。窗口标题的 sink 换成 Textual 的写入队列（整条序列由 writer
        # 线程落盘，与帧输出不交错）；cli.main 那边装的是终端前端，TUI 起来后
        # 由这里接管（同一进程只会有一个接受询问的前端）。
        driver = self._driver
        presenter = title.enable_title(sink=driver.write if driver is not None else None)
        self._frontend = TuiFrontend(self)
        self._attached = attach_frontend(
            self.agent.events,
            self._frontend,
            extra_subscribers=(presenter.on_agent_event,),
        )
        self.query_one("#queued").display = False  # 排队面板：空则隐藏
        self.query_one(ChatInput).focus()
        self.query_one("#running").display = False  # 运行动画默认隐藏
        self.query_one(CommandMenu).hide_menu()  # 命令菜单默认隐藏
        self._show_welcome()
        if getattr(self.agent.session, "messages", None):
            self._replay_history()  # 启动时恢复的会话：回放历史
        self.ui_status()

    def on_unmount(self) -> None:
        """界面下线（Ctrl+Q / `/exit` / 测试收尾）：取消本会话所有挂起询问。

        面板已不可能再被作答；取消让等待方按 fail-closed 兜底收口（权限确认即
        拒绝），否则那一轮会永远停在"在等"上——退出卡死那个故障的根因就是它
        （见 `AskPort.cancel_in_flight`）。
        """
        if self._frontend is not None:
            self._frontend.abandon_pending()
        self.close_open_panel()
        if self.agent is not None:
            self.agent.cancel_pending_asks()

    def _replay_history(self) -> None:
        """恢复会话后回放历史：**按事件重建界面**（文本 / 工具行 / 每轮页脚）。

        为什么不从 `session.messages` 渲染：消息只是事件的一种投影，工具调用行
        （`ToolStart` / `ToolEnd` / `PlanUpdate`）与每轮页脚（模型 / 思考强度 /
        用时）只有事件才有——只走消息就会把这些信息丢掉（"日志里存了、界面不显示"
        正是这个原因）。事件里没有的（易失的流式增量）本就不需要：历史只要结果。

        没有事件可放（临时会话 / 测试直接塞的消息）时退回按消息渲染，保证那块
        路径不至于空白。
        """
        events = self.agent.replayable_events() if self.agent is not None else []
        chat = self.query_one(ChatView)
        with self.batch_update():
            if events:
                self._replay_events(chat, events)
            else:
                self._replay_messages(chat)

    def _replay_events(self, chat: ChatView, events: list) -> None:
        """按事件重放：一次自成一体的投影（每轮的时间戳由事件带来）。"""
        # 事件用**模块别名**引用：事件与聊天项同名（`ToolStart` / `ToolEnd`），
        # 直接 import 会把模块级的聊天项遮蔽掉——那正是"工具行画不出来"的坑
        from ..event import catalog as ev

        model, effort = "", ""  # 页脚用：跟随最近一条 ModelSelected
        turn_start: float | None = None  # 页脚用时：Execution 起止的时间戳之差
        texts_seen: set[str] = set()  # 去重（同一条消息可能有多个 MessageEnd）
        for env in events:
            data = env.data
            if isinstance(data, ev.MessageEnd):
                message = data.message
                role = message.get("role")
                # system 不进日志；tool 结果由工具行承载，不再单列一条消息
                if role not in ("user", "assistant"):
                    continue
                text = _message_text(message.get("content"))
                if role == "user":
                    skill_name = skills.render.payload_skill_name(text)
                    if skill_name:
                        chat.apply(Notice(f"已加载技能 {skill_name}"))
                    elif text:
                        chat.apply(User(text))
                elif text.strip() and text not in texts_seen:
                    texts_seen.add(text)
                    chat.apply(Assistant(text))
            elif isinstance(data, ev.ToolStart):
                # 静态行（历史没有 pending / 转轮语义）
                icon = _PLAN_ICON if data.name == "todo_write" else ""
                chat.apply(ToolStart(data.tool_call_id, data.line, data.display,
                                     data.name, icon, ""))
            elif isinstance(data, ev.ToolEnd):
                chat.apply(ToolResult(data.tool_call_id, data.result, data.expand,
                                      data.is_error))
            elif isinstance(data, ev.PlanUpdate) and data.created:
                # 新建清单时按既有形态展示一次详情（后续更新只刷侧边栏）
                chat.apply(ToolResult(data.tool_call_id, data.rendered, True, False))
            elif isinstance(data, ev.ModelSelected):
                model, effort = data.model, data.effort
            elif isinstance(data, ev.ExecutionStarted):
                turn_start = env.created
            elif isinstance(data, (ev.ExecutionSucceeded, ev.ExecutionFailed,
                                   ev.ExecutionInterrupted)):
                if turn_start is None:
                    continue
                # 与实时路径同一个口径（含流断开的失败原因）
                status = "interrupted" if isinstance(data, ev.ExecutionInterrupted) else data.status
                reason = "" if isinstance(data, ev.ExecutionInterrupted) else getattr(data, "reason", "")
                suffix = footer_suffix(status, format_stream_interrupted(reason))
                chat.apply(Footer(
                    model or config.MODEL,
                    effort or (config.REASONING_EFFORT or config.DEFAULT_EFFORT),
                    format_duration(max(0.0, env.created - turn_start)),
                    suffix,
                ))
                turn_start = None

    def _replay_messages(self, chat: ChatView) -> None:
        """没有日志事件时的退化路径：按消息渲染 user / assistant 文本。

        技能载荷消息（技能正文）折叠为一行提示，避免整段正文铺满聊天区。
        """
        for message in getattr(self.agent.session, "messages", None) or []:
            role = message.get("role")
            text = _message_text(message.get("content"))
            if role == "user" and text:
                skill_name = skills.render.payload_skill_name(text)
                if skill_name:
                    chat.apply(Notice(f"已加载技能 {skill_name}"))
                else:
                    chat.apply(User(text))
            elif role == "assistant" and text.strip():
                chat.apply(Assistant(text))

    def _show_welcome(self) -> None:
        """在聊天区渲染欢迎横幅（启动与 /new 后复用）。

        终端放得下就用完整版（Logo），太窄降级为单行紧凑版。"""
        width = self.size.width
        self.query_one(ChatView).apply(
            Welcome(welcome.banner(compact=width < welcome.LOGO_WIDTH + 12))
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
        """Shift+Tab 循环权限模式，只刷新底栏模式段。权限/提问面板弹出期间不响应（瞬时态）。

        模式只展示在底栏 `#composer-mode`，此处不调全量 `ui_status()`——后者会
        无条件重写侧边栏标题等控件（`Static.update` 恒触发重排），空闲时按一次
        就让整栏闪一下，看起来像标题"跟着变化"。"""
        if self.query(PermissionPanel) or self.query(QuestionPanel):
            return
        self.agent.permission.cycle_mode()
        mode, _, _ = self._composer_status()
        self.query_one("#composer-mode").update(mode)

    def action_interrupt(self) -> None:
        """Esc：任务运行中请求中断；空闲且无弹层时清空输入框（Claude Code 式）。

        任务运行时不再往对话区打「正在停止…」行，改为让底部运行动画行尾追加
        「· 正在停止…」（动态、随动画刷新），任务真正收尾时由轮次页脚补「· 已停止」。

        各弹层（权限 / 提问面板、选择弹窗、命令菜单）的 Esc 各有自己的
        取消语义且焦点在内时按键先被其消费，一般走不到这里；此处守卫是
        兜底（焦点不在弹层输入上时仍不干扰其语义）。
        """
        if self._busy:
            self.agent.interrupt()
            self.ui_running_stopping()
            self._recall_queue()  # 中止即把排队内容取回输入框（计划 §5(3)）
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
            self.ui_notice(f"（未知 UI 动作: {message.action}）", "error")
            return
        handler(*message.args)

    def ui_notice(self, text: str, level: str = "info") -> None:
        """系统通知（信息 / 警告 / 错误）落对话区，级别决定颜色与图标。"""
        self._chat().apply(Notice(text, coerce_level(level)))

    def ui_line(self, text: str, style: str | None = None) -> None:
        """兼容旧调用点：style 字符串映射为语义级别（新代码请用 ui_notice）。"""
        self._chat().apply(Notice(text, level_from_style(style)))

    def ui_title(self, title: str) -> None:
        """后台自动标题生成完成（Renderer.title_changed）：刷新底栏标题。"""
        self.ui_status()

    def ui_retry_start(self, state, owner=None) -> None:
        """模型请求失败即将重试（Renderer.retry_started）：进度落在输入框上方那一行。

        同时把已经上屏的这一段正文标记为「已中断」——重试会把完整正文重写一遍，
        不标的话屏幕上会出现两段几乎相同的内容（上游同样会重放，但至少这里说清楚
        哪段是残缺的）。组件已卸载（应用退出）时忽略。
        """
        found = self.query("#running")
        if found:
            found.first().set_retry(state, owner)
        chat = self.query(ChatView)
        if chat:
            chat.first().mark_stream_interrupted()

    def ui_retry_end(self, owner=None) -> None:
        """重试过程结束（成功或放弃）：清掉运行动画行的重试后缀。"""
        found = self.query("#running")
        if found:
            found.first().clear_retry(owner)

    def ui_block(self, text: str, style: str | None = None) -> None:
        """多行文本块；解析内嵌 ANSI 转义（如计划清单的颜色码），避免转义符作为
        字面字符进入渲染流（真实终端会打花整个界面）。"""
        self._chat().apply(Block(text, level_from_style(style)))

    def ui_stream(self, kind: str, chunk: str) -> None:
        self._chat().apply(StreamDelta(kind, chunk))

    def ui_stream_done(self) -> None:
        self._chat().apply(StreamEnd())

    def ui_tool_start(self, tool_id: int, summary: str, display: str = "inline",
                      name: str = "") -> None:
        """pending 工具行：转轮摘要先上屏；读取/搜索/列目录类归入「已探索」汇总组。

        命令工具耗时不确定，pending 期显式标「执行中」；完成后统一转静态行。"""
        icon = _PLAN_ICON if name == "todo_write" else ""
        running = "执行中" if name == "run_command" else ""
        self._chat().apply(ToolStart(tool_id, summary, display, name, icon, running))

    def ui_tool_preview(self, tool_id: int | None, detail: str) -> None:
        """执行前的变更预览（diff）：更新对应 pending 工具块，审核时改动已可见。"""
        self._chat().apply(ToolPreview(tool_id, detail))

    def ui_tool_result(self, tool_id: int | None, result: str, expanded: bool,
                       is_error: bool) -> None:
        self._chat().apply(ToolResult(tool_id, result, expanded, is_error))

    def ui_thinking_start(self) -> None:
        self._chat().apply(ThinkingStart())

    def ui_thinking_tick(self, chunk: str) -> None:
        self._chat().apply(ThinkingDelta(chunk))

    def ui_thinking_done(self) -> None:
        self._chat().apply(ThinkingEnd())

    def ui_plan_sidebar(self, rendered: str) -> None:
        self.query_one(Sidebar).update_plan(rendered, plan.has_active())

    def ui_focus_input(self) -> None:
        self.query_one(ChatInput).focus()

    def show_ask_panel(self, request, result: dict, on_done) -> None:
        """挂提问/权限面板（原地替换输入框，含框内状态行），答完由面板回调收尾。

        一个入口承载所有 kind：面板形状按 `request.kind` 与 `payload` 决定，
        调用点（权限引擎 / ask_user / 技能信任）不需要知道终端怎么问。
        """
        self.query_one("#input-wrap").display = False
        payload = dict(request.payload or {})
        if request.kind == "ask_user":
            questions = list(payload.get("questions") or [])
            panel = QuestionPanel(questions, result, on_done)
        else:
            panel = PermissionPanel(
                request.title,
                "".join(request.options),
                str(payload.get("hint") or ""),
                result,
                on_done,
                list(request.detail or []),
                dict(payload.get("descriptions") or {}),
                payload.get("content"),
            )
        self._open_panel = panel
        self.mount(panel, before=self.query_one("#input-wrap"))

    def close_open_panel(self) -> None:
        """收掉当前打开的面板（会话/界面收尾时调用）：面板不在，等待方已被取消。"""
        panel = getattr(self, "_open_panel", None)
        if panel is not None and panel.is_attached:
            self.close_composer_panel(panel)
        self._open_panel = None

    def close_composer_panel(self, panel: Vertical) -> None:
        """关闭提问/权限面板，恢复输入框（含框内状态行）并聚焦（composer 位三态的归位动作）。"""
        if getattr(self, "_open_panel", None) is panel:
            self._open_panel = None
        panel.remove()
        self.query_one("#input-wrap").display = True
        chat_input = self.query_one(ChatInput)
        chat_input.focus()

    # ----- 通用选择面板（居中弹窗，支持逐级返回） -----

    def show_selection(self, select) -> None:
        """按命令的选择意图弹出居中选择弹窗（如 /model 无参）。

        选择层级由 `self._select_stack` 维护：进入下级时把父级压栈，Esc 未
        选中值时逐级返回上一级（带锚点恢复光标），栈空（根级）则关闭。
        ModalScreen 的半透明背景让底层界面轻微变暗；选中后重新分发
        `/<command> <value>`，若结果仍是选择意图则继续下钻。
        readonly 意图仅展示：面板禁用 Enter 确认，只留 ↑↓ 查看与 Esc 关闭。
        """
        self._select_stack.clear()
        self._present_select(select)

    def _present_select(self, select, anchor: str | None = None) -> None:
        """挂载一张选择面板；anchor 为返回上一级时要落回的光标位置。

        anchor 只影响初始光标（走 SelectionPanel 的 initial 参数），不改变
        条目的「(当前)」语义——返回上一级时不会给原选项加上「(当前)」。
        """
        self.query_one(CommandMenu).hide_menu()
        items = [
            SelectionItem(
                label=choice.label,
                value=choice.value,
                description=choice.description,
                current=choice.current,
                trailing=choice.trailing,
                trailing_style=choice.trailing_style,
                separator=choice.separator,
            )
            for choice in select.items
        ]
        panel = SelectionPanel(
            select.title, items, lambda value: self.screen.dismiss(value),
            size=select.size,  # 宽度档位由命令声明，宿主不测量内容
            initial=anchor,
            readonly=select.readonly,  # 只读展示：禁用 Enter 确认
        )
        self.push_screen(
            SelectionScreen(panel),
            callback=lambda value: self._after_selection(select, value),
        )

    def _after_selection(self, select, value) -> None:
        """选择面板收尾：Esc 返回上一级；选中则分发，若仍是选择意图则下钻。"""
        if value is None:
            self._select_back()
            return
        # command 留空 = value 本身即命令名（技能名直达，见 commands/skills.py）
        command = f"/{select.command} {value}" if select.command else f"/{value}"
        outcome = self._dispatch_command(command)
        if outcome is None:  # busy 守卫拦截：清栈，面板已关
            self._select_stack.clear()
            return
        if outcome.select is not None:
            self._select_stack.append((select, value))
            self._apply_outcome(outcome, command, nested=True)
            return
        self._select_stack.clear()
        self._apply_outcome(outcome, command)

    def _select_back(self) -> None:
        """Esc 逐级返回：有父级则带锚点重开；栈空（根级）不额外动作。"""
        if not self._select_stack:
            return
        parent, anchor = self._select_stack.pop()
        self._present_select(parent, anchor=anchor)

    # ----- MCP 添加向导 -----

    def show_mcp_wizard(self, wizard_intent) -> None:
        """按命令的向导意图弹出 MCP 添加面板；完成回调落盘并触发连接。"""
        if getattr(wizard_intent, "name", "") != "mcp.add":
            self.ui_notice(f"不支持的向导: {getattr(wizard_intent, 'name', '?')}", "warning")
            return
        wizard = McpWizard(workspace=config.WORKSPACE_ROOT)
        panel = McpWizardPanel(wizard, lambda plan: self.screen.dismiss(plan))
        self.push_screen(McpWizardScreen(panel), callback=self._after_mcp_wizard)

    def _after_mcp_wizard(self, plan) -> None:
        if plan is None:
            self.ui_notice("已取消添加 MCP 服务器。", "info")
            return
        try:
            apply_plan(self.agent.mcp, plan)
        except McpConfigError as e:
            self.ui_notice(f"添加 MCP 服务器失败: {e}", "error")
            return
        self.ui_notice(f"已保存 MCP 服务器 {plan.config.name}，正在连接…", "info")

    def ui_status(self) -> None:
        usage_title, usage_body = self._sidebar_usage()
        ctx_title, ctx_body = self._sidebar_context()
        sidebar = self.query_one(Sidebar)
        sidebar.update_usage(usage_title, usage_body, ctx_title, ctx_body)
        snapshot = goal.sidebar()  # 侧边栏目标卡片（计划区上方）；无目标整块隐藏
        if snapshot is None:
            sidebar.update_goal(None)
        else:
            title, body = snapshot
            color = _GOAL_STATUS_COLORS.get(goal.current().status, "#808080")
            sidebar.update_goal((Text(title, style=color), body))
        mode, model, thinking = self._composer_status()
        self.query_one("#composer-mode").update(mode)
        self.query_one("#composer-model").update(model)
        self.query_one("#composer-thinking").update(thinking)
        sidebar.update_title(self._session_title())
        marker = goal.marker()  # 「◎ 目标 3/50」；无目标时隐藏该段
        goal_status = self.query_one("#composer-goal")
        goal_status.update(marker)
        goal_status.display = bool(marker)
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

    def _context_text(self, est: int, pct: int) -> Text:
        """底栏上下文占用（文字形式）：`12.3K(10%)`，颜色随底栏默认灰，不做着色。"""
        return Text(f"{human_tokens(est)}({pct}%)")

    def ui_usage(self, usage) -> None:
        """收到用量事件（`UsageChanged`）：存下最新口径并刷新侧边栏。

        界面**只从事件渲染**：不读会话内部状态，所以多客户端/远程接入时
        这一块天然可用（事件带 session_id，谁订阅谁看得到）。
        """
        self._usage = usage
        self.ui_status()

    def _sidebar_usage(self) -> tuple[Text, Text]:
        """「用量」卡：(标题, 正文)。标题 = Usage（有调用时附 · Calls N）；正文 = In/Out + Cache。"""
        usage = self._usage
        title = Text("Usage", style="#808080")
        calls = usage.calls if usage is not None else 0
        if calls:
            title.append(" · Calls ", style="#808080")
            title.append(str(calls), style="#eeeeee")
        body = Text()
        body.append("In ", style="#808080")
        body.append(human_tokens(usage.total_input if usage else 0), style="#eeeeee")
        body.append(" · Out ", style="#808080")
        body.append(human_tokens(usage.total_output if usage else 0), style="#eeeeee")
        cache = usage.cached_tokens if usage is not None else 0
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
        body.append(f"{human_tokens(est)} / {human_tokens(budget)}", style="#eeeeee")
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

    def _session_title(self) -> Text:
        """侧边栏顶部的会话标题：标题优先，未生成时回退首轮 user 消息截断。

        无任何可展示内容时返回空 Text，由 Sidebar 整段隐藏。标题可能来自
        `/rename`（用户）或首轮后的后台自动生成，两个入口都经 `ui_status` 刷新。"""
        session = self.agent.session
        title = str(getattr(session, "title", "") or "").strip()
        if not title:
            for message in session.messages:
                if message.get("role") == "user":
                    title = _message_text(message.get("content")).strip()
                    if title:
                        break
        title = " ".join(title.split())
        if not title:
            return Text("")
        if len(title) > 30:
            title = title[:29] + "…"
        return Text(title, style="#7dcfff")

    def _status_text(self) -> Text:
        """底部状态栏：git 分支 / 上下文占用文字（模型与思考强度已移入输入框内）。"""
        pieces = []
        branch = git_branch(config.WORKSPACE_ROOT)
        if branch:
            pieces.append(Text(branch))
        est, _, pct = self._context_stats()
        pieces.append(self._context_text(est, pct))
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
            return
        if self._busy:
            # 运行中提交 = 入队（默认 follow）：**不回显到对话区**——它还没被投递，
            # 只属于排队面板；投递时由 QueuedPromptDelivered 落到对话区（本轮结束
            # 或工具批之间）。回显在这里会让用户以为已经发出去了，而模型还没看到。
            self.start_task(text)
            return
        self._chat().apply(User(text))  # 非运行态：回显用户消息，避免"发出去没反应"
        self.start_task(text)

    def edit_queued(self, item_id: str) -> None:
        """把某条排队输入取回输入框去改（面板行的 `edit` 按钮）。

        与 Esc 的区别是**不碰当前任务**：Esc 会先中断正在跑的任务，而改一条排队
        里的错别字不该付这个代价。取回后这条出队、文本拼到现有草稿之后、光标到末尾，
        改完按 Enter 重新排队。
        """
        item = self.agent.take_queued(item_id)
        if item is None:
            return
        found = self.query("#input")
        if not found:
            return
        editor = found.first()
        joiner = "\n" if editor.text.strip() else ""
        editor.text = joiner.join([editor.text, item.text]).lstrip("\n")
        editor.move_cursor(editor.document.end)
        editor.focus()

    def _recall_queue(self) -> None:
        """把排队内容取回输入框并清空队列（既有 Esc 路径，不新增按键）。

        与编辑器里已键入的内容拼接（换行分隔），对齐 pi 的「queued + currentText」。
        """
        steering, follow_up = self.agent.clear_queue()
        texts = steering + follow_up
        if not texts:
            return
        found = self.query("#input")
        if not found:
            return
        editor = found.first()
        joiner = "\n" if editor.text.strip() else ""
        editor.text = joiner.join([editor.text, *texts]).lstrip("\n")
        editor.move_cursor(editor.document.end)
        editor.focus()

    def ui_inbox_add(self, item) -> None:
        """入队：面板多一行（它还没进对话区）。"""
        self._inbox.add(item)
        self._refresh_queue_panel()

    def ui_inbox_deliver(self, item) -> None:
        """投递：从面板移除 + 补上对话区的用户消息（入队时只在面板里）。"""
        self._inbox.pop(item.id)
        self._refresh_queue_panel()
        self._chat().apply(User(item.text))

    def ui_inbox_cancel(self, item_id: str) -> None:
        """撤销 / 取回编辑：从面板移除。"""
        self._inbox.pop(item_id)
        self._refresh_queue_panel()

    def ui_inbox_clear(self) -> None:
        """全部清空（Esc 中止时取回编辑器）。"""
        self._inbox.clear()
        self._refresh_queue_panel()

    def _refresh_queue_panel(self) -> None:
        """按当前排队状态重建面板（空队列整块隐藏，锚点随高度重算）。"""
        found = self.query("#queued")
        if not found:
            return
        found.first().show_items(
            tuple(self._inbox.steering),
            tuple(self._inbox.follow_up),
            on_cancel=self.agent.cancel_queued,
            on_edit=self.edit_queued,
        )
        self.call_after_refresh(self.anchor_command_menu)

    def start_task(self, text: str) -> None:
        if self._busy:
            # 运行中提交 = 入队（默认 follow，见 [queue].delivery）；不新增按键。
            # 不在这里打 notice：面板本身就是反馈，而 notice 会把原文重复印进
            # 对话区，看起来像"已经发出去了"。
            self.agent.enqueue(text)
            return
        self._busy = True
        self._turn_start = time.monotonic()
        running = self.query_one("#running", RunningIndicator)
        running.display = True
        running.start()
        self._sync_command_menu_anchor(running=True)
        # 任务本身跑在事件循环上（Textual worker），不再另起线程：
        # 阻塞型工作（LLM 流 / 预检 / 工具执行）由 Agent 侧下放到 to_thread，
        # 所以循环始终能处理按键与重绘。见 tui/bridge.py 的线程不变量说明。
        self.run_worker(self._run_task(text), name="agent-task", group="agent")

    async def _run_task(self, text: str) -> None:
        status = "ok"
        result = None
        try:
            result = await self.agent.session_owner.run_with_goal(text)  # 无目标时等价 run
            status = result.status
        except Exception as e:  # noqa: BLE001
            self.post_message(
                UiAction("notice", f"{type(e).__name__}: {e}", "error")
            )
            status = "error"
        finally:
            self._turn_reason = getattr(result, "reason", "") if result is not None else ""
            self._busy = False
            self.post_message(UiAction("focus_input"))
            self.post_message(UiAction("status"))
            self.post_message(UiAction("running_off"))
            self.post_message(UiAction("turn_end", status))

    def start_compact(self) -> None:
        """手动 /compact：后台线程压缩，主线程只负责即时反馈，避免 UI 卡住。

        压缩要发摘要请求（数秒到数十秒），同步跑会冻结界面；这里复用任务的
        忙守卫与运行动画：立即提示「正在压缩」，完成后由后台线程投递结果。
        """
        if self._busy:
            self.ui_notice("（上一条任务还在运行，请等待完成或先按 Esc 中断）", "warning")
            return
        self._busy = True
        self.ui_notice(commands.COMPACT_RUNNING, "info")
        running = self.query_one("#running", RunningIndicator)
        running.display = True
        running.start()
        self._sync_command_menu_anchor(running=True)
        self.run_worker(self._run_compact(), name="agent-compact", group="agent")

    async def _run_compact(self) -> None:
        """执行一次手动压缩（与任务同一条事件循环），结果经 UiAction 回渲染。"""
        try:
            status = await self.agent.compact_manual()
        except Exception as e:  # noqa: BLE001
            self.post_message(UiAction("notice", f"压缩失败：{type(e).__name__}: {e}", "error"))
        else:
            text, style = commands.compact_report(status)
            self.post_message(UiAction("notice", text, level_from_style(style)))
        finally:
            self._busy = False
            self.post_message(UiAction("focus_input"))
            self.post_message(UiAction("status"))
            self.post_message(UiAction("running_off"))

    def ui_running_off(self) -> None:
        # 应用退出时组件可能已卸载，消息晚到会导致 NoMatches——查不到就忽略
        found = self.query("#running")
        if not found:
            return
        found.first().stop()
        found.first().display = False
        self._sync_command_menu_anchor(running=False)

    def _sync_command_menu_anchor(self, running: bool) -> None:
        """运行动画可见性变化时更新命令菜单锚点：动画占一行则整体上移一行。

        切换 CSS 类（见 `#command-menu.running`）并在下一帧按实时几何重算 offset——
        动画行会让输入区高一格，而输入框自身尺寸没变（不触发 resize），须显式重算。
        应用退出时组件可能已卸载，查不到菜单就忽略。"""
        found = self.query(CommandMenu)
        if found:
            found.first().set_class(running, "running")
        self.call_after_refresh(self.anchor_command_menu)

    def anchor_command_menu(self) -> None:
        """按输入区实时几何重新锚定命令菜单：底部贴住输入区顶部。

        输入框高度随内容自适应（1-12 行），写死行数的 offset 会随高度变化错位，
        故每次输入区几何变化都重算（含上方运行动画行与下方底行的高度）。
        应用退出 / 组件未挂载时查不到容器就忽略。"""
        menu = self.query(CommandMenu)
        wrap = self.query("#input-wrap")
        bottom = self.query("#bottom")
        if not (menu and wrap and bottom):
            return
        offset_y = -(wrap.first().region.height + bottom.first().region.height)
        menu.first().styles.offset = (0, offset_y)

    def ui_running_stopping(self) -> None:
        """Esc 后让运行动画行尾显示「· 正在停止…」（组件已卸载则忽略）。"""
        found = self.query("#running")
        if found:
            found.first().mark_stopping()

    def ui_turn_end(self, status: str = "ok") -> None:
        """轮次结束：在会话末尾追加 opencode 式元数据页脚「▣ 模型 · 思考强度 · 用时」。

        模型与思考强度读本轮 pin 住的快照（实际发出的值）：轮内切配置不影响
        本轮，页脚不再误报切换后的值；快照缺失（假 LLM 路径）回退全局配置。

        中断收尾（status == "interrupted"）时在页脚行尾补「· 已停止」，替代此前
        对话区单独一行的中断提示；响应流断开（status == "stream_error"）补
        「· 输出中断」——正文是残缺的，页脚要能一眼看出来。"""
        if self._turn_start is None:
            return
        elapsed = time.monotonic() - self._turn_start
        self._turn_start = None
        # 后缀口径与重放共用（见 render.footer_suffix）：中断 / 流断开才有后缀
        suffix = footer_suffix(status, format_stream_interrupted(self._turn_reason))
        turn = self.agent.last_turn
        model = turn.model if turn is not None else config.MODEL
        effort = turn.effort if turn is not None else (
            config.REASONING_EFFORT or config.DEFAULT_EFFORT)
        found = self.query(ChatView)
        if found:
            # 轮次已结束，收尾汇总组里没等到结果的子工具（兜底，防转轮永转）
            found.first().drain_context_groups()
            found.first().apply(Footer(
                model,
                effort,
                format_duration(elapsed),
                suffix,
            ))

    def handle_command(self, text: str) -> None:
        """斜杠命令统一入口：busy 守卫 → 分发 → 结果收尾。"""
        outcome = self._dispatch_command(text)
        if outcome is None:
            return
        self._apply_outcome(outcome, text)

    def _dispatch_command(self, text: str):
        """busy 守卫 + commands.dispatch；被拦截时提示并返回 None。

        对齐 opencode 的 busy 拒绝：任务运行中后台线程还在写消息历史，
        中途重置 / 切换 / 压缩 / 改 MCP 配置会撕裂进行中的轮次，先行拦截。
        技能名直达同样拦截：加载在分发期就登记进集合，正文要等宿主投递
        （busy 时被跳过），放行会留下「已加载但正文没进对话」的坏状态。
        """
        tokens = text.strip().split()
        mutating_session = bool(tokens) and tokens[0].lower() in ("/new", "/sessions", "/compact")
        mutating_mcp = (
            bool(tokens) and tokens[0].lower() == "/mcp" and len(tokens) > 1
            and tokens[1].lower() in ("add", "remove", "enable", "disable", "reconnect", "auth")
        )
        if self._busy and (
            mutating_session or mutating_mcp or commands.is_skill_command(text)
        ):
            self.ui_notice(
                "（任务运行中，不能切换会话、压缩上下文、加载技能或修改 MCP 配置；"
                "请等待完成或先按 Esc 中断）",
                "warning",
            )
            return None
        return commands.dispatch(self.agent, text)

    def _apply_outcome(self, outcome, text: str = "", nested: bool = False) -> None:
        """把 CommandResult 落到界面：会话重置 / 切换、文本、选择、向导、任务。

        `nested=True`（选择面板下钻）表示父级已由调用方压栈，此处只展示新
        一层选择、不重置层级栈。
        """
        if outcome.exit:
            self.exit()
            return
        if outcome.session_reset:
            # /new：清空聊天区本身就是全部反馈，不再追加提示文本（REPL 侧仍有文字）
            self.reset_chat()
        elif outcome.session_resume:
            # /sessions <id|序号>（或选择框选中）：清屏后回放切换后的历史
            self.reset_chat()
            self._replay_history()
            self.query_one(Sidebar).update_plan(
                plan.render_titles(color=True), plan.has_active()
            )
        else:
            if outcome.text is not None:
                if outcome.kind == "block":
                    self.ui_block(outcome.text, outcome.style)
                else:
                    self.ui_line(outcome.text, outcome.style)
            if outcome.select is not None:
                if nested:
                    self._present_select(outcome.select)
                else:
                    self.show_selection(outcome.select)
            if outcome.wizard is not None:
                self.show_mcp_wizard(outcome.wizard)
        if outcome.session_reset:
            self.query_one(Sidebar).update_plan("", has_active=False)
        if outcome.refresh_status:
            self.ui_status()
        if outcome.inject_history and not self._busy:
            # 技能载荷等注入：必须在 run() 之前落库；busy 时与 start_task 一起跳过，
            # 不留没有任务的孤儿消息
            for role, content in outcome.inject_history:
                self.agent.session.add(role, content)
        if outcome.start_task is not None:
            # /goal 设定/恢复后立即开跑；任务运行中则只提示——正在跑的续跑循环
            # 会在当前轮结束后读到新目标状态并自动接续
            if self._busy:
                if outcome.echo_input:  # 技能手动加载：不排队，提示用户等待/中断
                    self.ui_notice("（上一条任务还在运行，请等待完成或先按 Esc 中断）", "warning")
                else:
                    self.ui_notice("（目标已记录，当前任务结束后自动接续）", "info")
            else:
                if outcome.echo_input:  # 技能手动加载带任务：用户输入原文整体回显
                    self._chat().apply(User(text))
                self.start_task(outcome.start_task)
        if outcome.start_compact:
            self.start_compact()  # 压缩在后台线程执行，界面保持可响应

    def reset_chat(self) -> None:
        """开新会话：清空聊天区并重新渲染欢迎横幅，屏幕回归会话起点。

        连带清掉残留的瞬时渲染状态（工具块映射、思考块、轮次计时收归 ChatView）。"""
        self.query_one(ChatView).reset()
        self._turn_start = None
        self._show_welcome()


def run_tui(agent) -> None:
    """启动 Textual 全屏聊天界面（仅交互 tty 模式）。"""
    SmithTUI(agent).run()
