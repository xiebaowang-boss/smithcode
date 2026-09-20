"""前端适配层（L3）：事件订阅者 + 询问端口。

**两个方向，两套语义**（这是本层的全部内容）：

| 方向 | 机制 | 说明 |
|---|---|---|
| 通知面（下行） | `event.Bus.subscribe()` | 核心只 `publish`，前端按事件渲染；无返回值、可丢 |
| 询问面（请求-应答） | 本模块的 `Asker` | 必须拿到答案：权限确认、越界授权、技能信任、模型提问 |

实现者：`frontend/console.py`（REPL / 单次任务 / 管道）、`tui/frontend.py`（Textual 界面）。
两者都只做同一件事的不同呈现，不含任何判定逻辑。

**为什么询问面还没有做成事件**：阶段 A 只做「单一出口」，把进程级的
`renderer.current()` 换成这里的**上下文级**端口（并发会话各拿各的）；把询问本身
改成事件化请求-应答（`asked → replied`，带 id、可跨进程）是阶段 C 的事——那时
`Asker` 的方法会换成 `async def`，本模块变成 `event/asks.py` 之上的薄封装。

**上下文语义**：`activate()` 挂载当前前端的询问端口，与总线一样是 ContextVar——
所以「谁在问」由运行上下文决定，而不是进程里最后一个装配的前端。裸线程不继承
上下文（见 `event/bus.py` 的说明），需要在其中提问的路径必须显式传入端口。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..event import activate as activate_bus
from ..event import reset as reset_bus
from ..event.asks import AskAnswer, AskRequest


@runtime_checkable
class Asker(Protocol):
    """询问端口：前端只实现**一个异步方法**。

    `AskRequest.payload` 带各 kind 的专属数据、`options` 带选项键；**怎么问**
    （编号选择还是方向键、单面板还是逐题）完全由前端决定——所以终端交互的差异
    不会散落到权限引擎这类调用点里。

    返回值语义与改造前逐字一致：没作答（`outcome != "answered"`）一律按
    fail-closed 收尾——权限确认即拒绝、提问即取消。
    """

    async def ask(self, request: AskRequest) -> AskAnswer: ...


class _UnavailableAsker:
    """显式「不要提问」时的兜底：一律拒绝 / 取消，绝不挂起，也不读终端。

    需要「问谁都别问」的路径（非交互、测试里断言 fail-closed）可以
    `activate(_UNAVAILABLE)` 显式挂上——它与「未挂载时的终端兜底」是两回事。
    """

    async def ask(self, request: AskRequest) -> AskAnswer:
        return AskAnswer(outcome="cancelled")


_UNAVAILABLE = _UnavailableAsker()
_current: ContextVar[Asker | None] = ContextVar("smithcode_asker", default=None)
_default: Asker | None = None  # 终端兜底实例（懒建，进程内一个）


def activate(asker: Asker | None) -> Token:
    """挂载当前前端的询问端口，返回复位令牌（与 `event.activate` 配对使用）。"""
    return _current.set(asker)


def reset(token: Token) -> None:
    """按令牌复位（与 `activate` 配对；测试隔离也用它）。"""
    _current.reset(token)


def current() -> Asker:
    """当前前端的询问端口；未挂载时用**终端前端**兜底。

    兜底与改造前的 `renderer.current()` 一致：没有宿主装配时（裸 Agent、命令层、
    测试）提问落在终端上。**fail-closed 由调用点保证**——权限引擎与 ask_user 在
    提问前先看 `confirmations_available()`，非交互 stdin 一律拒绝，所以这里不会
    把管道 / CI 挂住。
    """
    asker = _current.get()
    if asker is not None:
        return asker
    global _default
    if _default is None:
        from .console import ConsoleFrontend  # 懒导入：避免包初始化期成环

        _default = ConsoleFrontend()
    return _default


@dataclass
class Attached:
    """一次装配的两枚复位令牌（`detach()` 按逆序复位）。"""

    bus_token: object
    asker_token: object

    def detach(self) -> None:
        reset(self.asker_token)
        reset_bus(self.bus_token)


def attach(bus, frontend_obj, *, extra_subscribers: tuple = ()) -> Attached:
    """宿主装配前端的**唯一入口**：订阅事件 + 挂上询问端口。

    - 事件：`frontend_obj.on_event` 与 `extra_subscribers`（如终端标题呈现器）
      都订阅**同一个**总线——事件只有一条路，不存在第二条通道；
    - 询问：端口挂到当前上下文，因此并发会话各拿各的前端，而不是"进程里最后
      装配的那个"。

    返回两枚令牌，收尾时 `Attached.detach()` 复位（测试隔离也依赖它）。
    """

    bus.subscribe(frontend_obj.on_event)
    for subscriber in extra_subscribers:
        bus.subscribe(subscriber)
    return Attached(activate_bus(bus), activate(frontend_obj))
