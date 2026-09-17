"""终端窗口标题：由 agent 事件驱动，前端只提供写入通道。

分层（前端无关）：

- Agent 经渲染后端发事件——`title_changed`（会话标题变化）/ `turn_started` /
  `turn_finished`（忙闲）；
- 阻塞（等用户确认/回答）由 `Relay` 在 ask 类方法进出时上报，与忙闲分开计数：
  等待态优先于运行态，标题前缀由 `◐` 换成 `!`——切到别的窗口再回来，一眼能
  看出 agent 是卡在等自己，而不是还在跑；
- `TerminalTitlePresenter` 消费事件，合成品牌化标题（`Smith · 重构会话管理`，
  运行中加 `◐` 前缀、等待确认时加 `!` 前缀），并管理窗口标题栈的压栈 / 出栈生命周期；
- 装配入口分两个：终端宿主用 `attach()`（总线 + 接管标题，再提供 sink 往哪写），
  只想要事件的 GUI 前端用 `bus()`（纯总线，不碰终端标题）。见 docs/architecture.md。

退出恢复用终端的窗口标题栈（xterm XTWINOPS：`CSI 22;2t` 压栈、`CSI 23;2t`
出栈），不读回原标题——读回要抢 stdin 解析终端应答，而 stdin 归
prompt_toolkit / Textual 独占，代价远大于收益。
"""
from __future__ import annotations

import atexit
import contextlib
import os
import re
import signal
import threading
from pathlib import Path

from . import config
from .renderer import Renderer
from .utils.terminal import stdout_is_tty, write_terminal_control

BRAND = "Smith"
"""标题里的品牌词：对齐 welcome.LOGO 与权限模式名。"""

BUSY_MARK = "◐"
"""运行中前缀（LLM 请求 / 工具执行）。"""

WAITING_MARK = "!"
"""等待用户输入的前缀（权限确认 / 提问面板）：纯 ASCII，不挑字体，且优先级高于忙碌。"""

TITLE_MAX = 60
"""标题最大字符数（超出截断，防止长标题挤占标签栏）。"""

_OSC_TITLE = "\x1b]0;{title}\x07"
"""OSC 0：设置窗口标题 + 图标名。BEL 结尾比 ST 兼容性更好。"""

_STACK_PUSH = "\x1b[22;2t"
_STACK_POP = "\x1b[23;2t"
"""XTWINOPS 压栈 / 出栈窗口标题（第二参数 2 = 窗口标题，1 = 图标名，0 = 两者）。"""

_UNSAFE = re.compile(r"[\x00-\x1f\x7f]")
"""控制字符（含 ESC / BEL）：标题可能来自模型生成，必须剥离后再写。"""


def sanitize_title(text: str) -> str:
    """标题净化：控制字符替换为空格、去首尾空白、限长。"""
    return _UNSAFE.sub(" ", text).strip()[:TITLE_MAX]


def default_workspace() -> str:
    """无会话标题时的回退名：工作区目录名。"""
    try:
        return Path(config.WORKSPACE_ROOT).name
    except (TypeError, ValueError):  # 路径异常时不至于连累启动
        return ""


class TitleState:
    """标题状态（纯数据）：会话标题 + 忙闲计数 + 等待计数 + 品牌 / 回退名。

    忙闲用计数而非布尔：`run_with_goal` 的多回合里，内层 `run()` 结束会发
    `turn_finished`，而外层仍在推进——计数 >0 期间保持忙碌，回合切换不闪烁。
    等待同样用计数：ask 可能嵌套（内层结束不代表外层结束），用布尔会在先
    结束的那次把等待态提前抹掉。
    """

    def __init__(self, brand: str = BRAND, workspace: str = "") -> None:
        self.brand = brand
        self.workspace = workspace
        self.title = ""
        self.busy = 0
        self.waiting = 0

    def base(self) -> str:
        """标题主体：会话标题 > 工作区目录名 > 品牌词。"""
        return self.title.strip() or self.workspace.strip() or self.brand

    def compose(self) -> str:
        """合成最终标题：`Smith` / `Smith · X` / `◐ Smith · X` / `! Smith · X`。

        等待用户输入优先于运行中——等确认时任务本就是停住的，先让人看到要处理。
        """
        if self.waiting > 0:
            text = f"{WAITING_MARK} {self.brand}"
        elif self.busy > 0:
            text = f"{BUSY_MARK} {self.brand}"
        else:
            text = self.brand
        base = self.base()
        return text if base == self.brand else f"{text} · {base}"


class TerminalTitlePresenter:
    """消费 agent 标题事件并写入终端。

    线程安全：事件来自 Agent 所在的工作线程（TUI `_run_task` / REPL
    `_run_agent_task`），生命周期调用来自宿主主线程（on_mount / repl），
    统一加锁。运行期只写 OSC 0，绝不重复压栈。
    """

    def __init__(self, workspace: str | None = None) -> None:
        self._lock = threading.RLock()
        self._state = TitleState(
            workspace=default_workspace() if workspace is None else workspace
        )
        self._sink = None  # None = 写真实终端（utils.terminal）
        self._last: str | None = None
        self._enabled = False
        self._pushed = False
        self._released = False

    # ---------- 事件入口（agent → 标题） ----------

    def on_title_changed(self, title: str) -> None:
        """会话标题变化：后台自动标题 / `/rename` / 新会话清空（空串）。"""
        with self._lock:
            self._state.title = str(title or "")
            self._flush()

    def on_turn_started(self) -> None:
        """一轮任务开始（LLM 请求 / 工具执行）。"""
        with self._lock:
            self._state.busy += 1
            self._flush()

    def on_turn_finished(self, status: str = "ok") -> None:
        """一轮任务结束，status 取 RunResult.status（供未来前端细分展示）。"""
        with self._lock:
            self._state.busy = max(0, self._state.busy - 1)
            self._flush()

    def on_waiting_started(self) -> None:
        """开始等待用户输入（权限确认 / 提问面板弹出）。"""
        with self._lock:
            self._state.waiting += 1
            self._flush()

    def on_waiting_finished(self) -> None:
        """等待结束（用户作答、取消或确认框异常退出）。"""
        with self._lock:
            self._state.waiting = max(0, self._state.waiting - 1)
            self._flush()

    # ---------- 生命周期（宿主主线程） ----------

    def bind_sink(self, sink) -> None:
        """注入写入通道：TUI 传 `driver.write`（Textual 的写入队列），
        None / 未注入时写真实终端。"""
        with self._lock:
            self._sink = sink

    def enable(self, workspace: str = "") -> None:
        """接管标题：解析开关、装退出钩子、压栈并写出当前标题。

        必须在主线程调用（signal.signal 限制）。非 tty 或配置关闭时整体退化
        为空操作——一个字节都不写。幂等：重复调用（组合根 + 宿主各接一次）
        不会重复压栈。

        事件早于本调用时（`--name` / `-c`/`--resume` 发生在宿主启动之前）
        状态已累积，这里统一 flush 出真实标题。
        """
        with self._lock:
            if self._enabled or self._released:
                return
            if not (config.load_terminal_title() and stdout_is_tty()):
                return
            if workspace:
                self._state.workspace = workspace
            self._enabled = True
            _install_exit_hooks()
            self._emit(_STACK_PUSH)
            self._pushed = True
            self._flush()

    def release(self) -> None:
        """退出收尾：出栈恢复原标题（幂等；未压栈则空操作）。

        不写空标题——那会把原标题抹掉，正是要避免的"退出后啥也没有"。
        """
        with self._lock:
            if self._released:
                return
            self._released = True
            if not self._pushed:
                return
            self._pushed = False
            self._emit(_STACK_POP, raw=True)

    # ---------- 内部 ----------

    def _flush(self) -> None:
        """状态 → 标题：去重后写出（相同标题不重复写终端）。"""
        if not self._enabled:
            return
        text = sanitize_title(self._state.compose())
        if text == self._last:
            return
        self._last = text
        self._emit(_OSC_TITLE.format(title=text))

    def _emit(self, seq: str, *, raw: bool = False) -> None:
        """写一条控制序列；失败静默，绝不影响主流程。

        raw=True 强制走真实终端：atexit 阶段 Textual 已 teardown、driver 的
        写线程已停，注入的 sink 不再可用。
        """
        sink = None if raw else self._sink
        try:
            if sink is None:
                write_terminal_control(seq)
            else:
                sink(seq)
        except Exception:  # noqa: BLE001, S110 - 标题写入失败不影响主流程
            pass


# ---------- 退出钩子 ----------

_hooks_installed = False


def _install_exit_hooks() -> None:
    """装统一的标题恢复钩子（只装一次，主线程调用）。

    atexit 覆盖正常退出 / `/exit` / EOF / 第二次 Ctrl+C 的 SystemExit(130) /
    未捕获异常；SIGTERM / SIGHUP 覆盖被 kill。

    刻意不注册 SIGINT：cli._wait_for_task 用它做「第一次取消任务、第二次退出」，
    抢占会破坏中断语义；而第二次退出走 SystemExit，atexit 照样触发。
    """
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True
    atexit.register(_release_at_exit)
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:  # Windows 无 SIGHUP
            continue
        try:
            previous = signal.getsignal(sig)
            signal.signal(sig, _chain_handler(sig, previous))
        except (OSError, RuntimeError, ValueError):
            continue  # 非主线程 / 平台不支持：至少保住 atexit


def _chain_handler(sig, previous):
    """恢复标题后把控制权交回原处理器，保持退出码与语义不变。"""

    def _handler(signum, frame):
        _release_at_exit()
        if previous == signal.SIG_IGN:
            return
        if callable(previous):
            previous(signum, frame)
            return
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    return _handler


def _release_at_exit() -> None:
    """退出收尾：只出栈，不打印、不抛异常（异常会改变退出码）。"""
    try:
        presenter().release()
    except Exception:  # noqa: BLE001, S110 - 退出期不得再抛异常
        pass


# ---------- 进程级单例与组合根装配 ----------

_presenter: TerminalTitlePresenter | None = None


def presenter() -> TerminalTitlePresenter:
    """当前标题呈现器（进程内一块终端标题，故为单例）。"""
    global _presenter
    if _presenter is None:
        _presenter = TerminalTitlePresenter()
    return _presenter


def reset() -> None:
    """复位单例与钩子标记：仅供测试隔离（进程内正常只装配一次）。"""
    global _presenter, _hooks_installed
    _presenter = None
    _hooks_installed = False


def bus(inner: Renderer) -> Renderer:
    """只建事件总线，不接管终端标题：GUI 前端（desktop / web）的装配入口。

    事件照常送进内层后端，但既不创建标题呈现器、也不装退出钩子、更不写任何
    控制序列——前端覆盖 `turn_waiting_*` 等方法即可自行消费。需要终端标题的
    宿主用 `attach()`。
    """
    return Relay(inner)


def enable_title(sink=None, workspace: str = "") -> TerminalTitlePresenter:
    """接管终端标题：注入写入通道、装退出钩子、压栈并写出当前标题（幂等）。

    sink 为 None 时写真实终端（REPL）；TUI 传 `driver.write`——它本身就是
    写入队列（textual WriterThread），整条序列由单线程落盘，不会与帧输出交错。
    """
    target = presenter()
    if sink is not None:
        target.bind_sink(sink)
    target.enable(workspace)
    return target


def attach(inner: Renderer, sink=None, workspace: str = "") -> Renderer:
    """终端宿主的组合根装配：`bus()` + `enable_title()`，返回包装后的渲染后端。"""
    return Relay(inner, enable_title(sink, workspace))


class Relay(Renderer):
    """渲染后端装饰器：透传全部事件，并充当标题事件的订阅转发层。

    订阅者可省略（`target=None`）——此时它就是一条**纯事件总线**，只把事件
    交给内层后端，不碰终端标题（`bus()` 即此形态）；终端宿主用 `attach()`，
    总线与标题接管一起装。

    标题三类事件总是上报给终端宿主（title_changed / turn_started /
    turn_finished）；纯总线形态（`bus()`）不接管终端标题，只转发事件。

    另有一处拦截：ask 类方法（确认 / 提问）前后广播总线事件
    `turn_waiting_started` / `turn_waiting_finished`。这些方法本就同步阻塞等用户，
    包住它们等于覆盖了全部阻塞入口——权限确认、越界授权、技能信任确认、
    ask_user 提问，调用点无需改动。事件同时喂给呈现器与内层后端，故任何前端
    （TUI / 未来 desktop、web）覆盖 `turn_waiting_*` 即可当消费者。
    """

    def __init__(self, inner: Renderer,
                 target: TerminalTitlePresenter | None = None) -> None:
        super().__init__()
        self._inner = inner
        self._presenter = target

    # ----- 标题相关事件（拦截后转发） -----

    def _notify(self, event: str, *args) -> None:
        """把事件喂给标题订阅者；纯总线模式（无订阅者）下静默跳过。

        订阅者只需实现自己关心的事件回调：基类事件是前端可订阅的总线，新增事件
        不能让既有订阅者（如只关心标题的 `TerminalTitlePresenter`）直接抛
        `AttributeError`——那会被 Agent 的流异常处理误判成"流中断"。没有对应
        回调即视为不关心。
        """
        if self._presenter is None:
            return
        handler = getattr(self._presenter, event, None)
        if handler is not None:
            handler(*args)

    def title_changed(self, title: str) -> None:
        self._notify("on_title_changed", title)
        self._inner.title_changed(title)  # TUI 侧边栏标题等仍照常刷新

    def turn_started(self) -> None:
        self._notify("on_turn_started")
        self._inner.turn_started()

    def turn_finished(self, status: str = "ok") -> None:
        self._notify("on_turn_finished", status)
        self._inner.turn_finished(status)

    def retry_started(self, state, owner: object | None = None) -> None:
        """重试开始：纯透传给内层（终端标题不关心重试进度）与订阅者。"""
        self._notify("on_retry_started", state, owner)
        self._inner.retry_started(state, owner)

    def retry_finished(self, owner: object | None = None) -> None:
        """重试结束：纯透传（与 retry_started 成对、owner 相同）。"""
        self._notify("on_retry_finished", owner)
        self._inner.retry_finished(owner)

    # ----- 等待用户输入（总线：呈现器与内层后端都收） -----

    def turn_waiting_started(self) -> None:
        self._notify("on_waiting_started")
        self._inner.turn_waiting_started()

    def turn_waiting_finished(self) -> None:
        self._notify("on_waiting_finished")
        self._inner.turn_waiting_finished()

    @contextlib.contextmanager
    def _waiting(self):
        """ask 类方法的公共包装：进出发总线事件，异常路径同样收尾。"""
        self.turn_waiting_started()
        try:
            yield
        finally:
            self.turn_waiting_finished()

    # ----- 其余事件（透传） -----

    def stream(self, kind: str, chunk: str) -> None:
        self._inner.stream(kind, chunk)

    def stream_done(self) -> None:
        self._inner.stream_done()

    def tool_call(self, line: str, display: str = "inline", name: str = "") -> int:
        return self._inner.tool_call(line, display, name)

    def tool_preview(self, tool_id: int | None, detail: str) -> None:
        self._inner.tool_preview(tool_id, detail)

    def tool_result(self, result: str, tool_id: int | None = None,
                    expand: bool = False) -> None:
        self._inner.tool_result(result, tool_id, expand)

    def plan(self, summary: str, rendered: str, *, created: bool = False,
             tool_id: int | None = None) -> None:
        self._inner.plan(summary, rendered, created=created, tool_id=tool_id)

    def info(self, text: str) -> None:
        self._inner.info(text)

    def success(self, text: str) -> None:
        self._inner.success(text)

    def warn(self, text: str) -> None:
        self._inner.warn(text)

    def error(self, text: str) -> None:
        self._inner.error(text)

    def ask_text(self, question: str) -> str:
        with self._waiting():
            return self._inner.ask_text(question)

    def ask_choice(self, question: str, options: list[str], multiple: bool = False,
                   descriptions: list[str] | None = None) -> str:
        with self._waiting():
            return self._inner.ask_choice(question, options, multiple, descriptions)

    def ask_form(self, questions: list[dict]) -> list[str]:
        with self._waiting():
            return self._inner.ask_form(questions)

    def confirm_choice(self, prompt: str, valid: str, hint: str,
                       detail: list[str] | None = None,
                       descriptions: dict[str, str] | None = None,
                       content: str | None = None) -> str:
        with self._waiting():
            return self._inner.confirm_choice(
                prompt, valid, hint, detail=detail, descriptions=descriptions,
                content=content,
            )
