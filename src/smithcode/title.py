"""终端窗口标题：由 agent 事件驱动，前端只提供写入通道。

分层（前端无关）：

- Agent 经渲染后端发事件——`title_changed`（会话标题变化）/ `turn_started` /
  `turn_finished`（忙闲）；
- 阻塞（等用户确认/回答）订阅 `PromptStarted` / `PromptFinished` 事件对：
  **按 id 配对**而不是计数——`open_prompts` 非空即「在等」，先结束的那一个不会
  把还在等的那一个抹掉（旧实现用标量计数规避这一点，代价是消费者不知道是谁
  结束了）。等待态优先于运行态，标题前缀由 `◐` 换成 `!`——切到别的窗口再回来，
  一眼能看出 agent 是卡在等自己，而不是还在跑；
- `TerminalTitlePresenter` **订阅 Agent 事件**（`on_agent_event`），合成品牌化标题
  （`Smith · 重构会话管理`，运行中加 `◐` 前缀、等待确认时加 `!` 前缀），并管理窗口
  标题栈的压栈 / 出栈生命周期；
- 装配入口只有 `attach()`：接管标题 + 订阅事件，并提供 sink 决定往哪写。
  （原来还有一个渲染后端装饰器 `Relay`：它在 ask 方法进出时广播等待信号，并把
  标题/忙闲事件转给呈现器。事件层补齐后两件事都有正式通道——等待是
  `PromptStarted/Finished`、标题与忙闲是 `TitleChanged` / `TurnStart` / `TurnEnd`，
  于是 `Relay` 与 `bus()` 一并删除。）

退出恢复用终端的窗口标题栈（xterm XTWINOPS：`CSI 22;2t` 压栈、`CSI 23;2t`
出栈），不读回原标题——读回要抢 stdin 解析终端应答，而 stdin 归
prompt_toolkit / Textual 独占，代价远大于收益。
"""
from __future__ import annotations

import atexit
import os
import re
import signal
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from . import config
from .agent.events import TitleChanged, TurnEnd, TurnStart
from .agent.interactions import PromptFinished, PromptStarted
from .utils.terminal import stdout_is_tty, write_terminal_control

if TYPE_CHECKING:  # 仅用于 attach 的类型注解（Relay 删除后不再继承 Renderer）
    from .renderer import Renderer

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
    """标题状态（纯数据）：会话标题 + 忙闲计数 + 未结束的提问 + 品牌 / 回退名。

    忙闲用计数而非布尔：`run_with_goal` 的多回合里，内层 `run()` 结束会发
    `turn_finished`，而外层仍在推进——计数 >0 期间保持忙碌，回合切换不闪烁。

    等待改用**id 配对**：`open_prompts` 里还有条目就是「在等」。旧实现用标量
    计数，为的是「内层结束不代表外层结束」；id 配对既能保证这一点，又能回答
    「是谁结束了」——嵌套或并发提问时，先结束的那个不会误清另一个。
    """

    def __init__(self, brand: str = BRAND, workspace: str = "") -> None:
        self.brand = brand
        self.workspace = workspace
        self.title = ""
        self.busy = 0
        self.open_prompts: dict[str, PromptStarted] = {}

    @property
    def waiting(self) -> int:
        """未结束的提问数（0 = 没在等用户）；顺序 = 发起顺序，最外层在前。"""
        return len(self.open_prompts)

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

    def on_agent_event(self, event) -> None:
        """Agent 事件订阅入口：标题 / 忙闲 / 等待态全部从这一条通道来。

        （`Relay` 删除后，原先经渲染后端转发的 `title_changed` / `turn_started` /
        `turn_finished` 改由事件驱动；`turn_waiting_*` 是无载荷信号、本来就无法
        配对，改由带 id 的提问事件对承担。）
        """
        if isinstance(event, PromptStarted):
            self.on_prompt_started(event)
        elif isinstance(event, PromptFinished):
            self.on_prompt_finished(event)
        elif isinstance(event, TitleChanged):
            self.on_title_changed(event.title)
        elif isinstance(event, TurnStart):
            self.on_turn_started()
        elif isinstance(event, TurnEnd):
            self.on_turn_finished(event.status)

    def on_prompt_started(self, event: PromptStarted) -> None:
        """开始等待用户输入（权限确认 / 技能信任 / 提问面板弹出）。"""
        with self._lock:
            self._state.open_prompts[event.id] = event
            self._flush()

    def on_prompt_finished(self, event: PromptFinished) -> None:
        """该次提问结束（作答 / 取消 / 前端故障都成对收口）。"""
        with self._lock:
            self._state.open_prompts.pop(event.id, None)
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


def attach(inner: Renderer, sink=None, workspace: str = "", agent=None) -> Renderer:
    """终端宿主的组合根装配：接管窗口标题 + 订阅 Agent 事件，返回**原样**的后端。

    曾经这里会包一层 `Relay` 装饰器来转发标题/等待事件；事件层补齐后不需要中间
    层了（见模块 docstring），因此现在只做两件事：`enable_title()` 与
    `agent.subscribe(...)`，后端保持原对象——调用方拿到的仍是自己的渲染后端。

    `agent` 不传时只装配标题（命令层自建宿主、测试等场景）。
    """
    target = enable_title(sink, workspace)
    if agent is not None:
        agent.subscribe(target.on_agent_event)
    return inner
