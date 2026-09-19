"""Agent 核心事件：类型化事件联合 + 有序事件流。

替换现有 `Renderer` 基类方法即事件总线的做法（`renderer.py` 的 `stream` /
`tool_call` / `tool_result` / `turn_started` / `retry_started` …）。目标是把
「事件产生」与「事件呈现」分开：Agent 只发类型化事件，前端只做订阅者，
一个前端不必再实现基类的全部方法。

事件集合对齐 pi 的 `AgentEvent`（`packages/agent/src/types.ts:448-463` 的十类
lifecycle / message / tool），另加 smithcode 特有的三类（`Notice` / `PlanUpdate` /
`AgentEnd`，分别对应现有 `Renderer.info|success|warn|error`、`Renderer.plan`、
`RunResult`）。交互（阻塞）事件与运行状态事件分居 `interactions.py` / `status.py`。

**不在核心事件里放 waiting**：pi 的核心同样没有（见 `interactions.py` 的说明）。
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Generic, TypeAlias, TypeVar

from .interactions import PromptFinished, PromptStarted
from .queues import QueueItem
from .result import RunResult
from .status import StatusChanged, StatusCleared

# 消息/增量/级别的类型别名见 `types.py`（本模块只用不定义，保证单一来源）
from .types import AgentMessage, NoticeLevel, StreamKind

# --------------------------------------------------------------------------
# 消息生命周期
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MessageStart:
    """一条消息开始（system / user / assistant / tool）。"""

    message: AgentMessage


@dataclass(frozen=True)
class MessageUpdate:
    """assistant 流式增量（对齐 pi 的 `message_update`，仅 assistant 会发）。"""

    message: AgentMessage
    delta: str
    kind: StreamKind


@dataclass(frozen=True)
class MessageEnd:
    """一条消息完成。"""

    message: AgentMessage


# --------------------------------------------------------------------------
# 工具执行生命周期
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolStart:
    """工具调用开始（摘要行先上屏，结果为 pending 态）。"""

    tool_call_id: str | None
    name: str
    line: str
    display: str = "inline"


@dataclass(frozen=True)
class ToolPreview:
    """执行前的变更预览（diff）。必须在真正执行前发出，否则文件已变更。"""

    tool_call_id: str | None
    detail: str


@dataclass(frozen=True)
class ToolEnd:
    """工具执行结束。expand 标记写/编辑类工具（前端默认展开详情）。"""

    tool_call_id: str | None
    result: str
    is_error: bool = False
    expand: bool = False


# --------------------------------------------------------------------------
# 任务计划与提示
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanUpdate:
    """步骤清单更新。created=True 表示本次是新建清单（只有此时前端展示整份详情）。"""

    summary: str
    rendered: str
    created: bool = False
    tool_call_id: str | None = None


@dataclass(frozen=True)
class Notice:
    """面向用户的状态文本，按级别呈现。"""

    text: str
    level: NoticeLevel = "info"


@dataclass(frozen=True)
class QueuedPromptDelivered:
    """排队输入**被投递**：已作为 user 消息进入会话历史。

    区分它和 `QueueChanged` 的必要性：入队时这条文本只存在于排队面板里，**不在**
    对话区；投递（本轮跑完 / 工具批之间抽水）后才成为历史的一部分。前端要在这个
    时刻把它落到对话区，否则用户看到自己排队的消息从面板消失、对话区却没有出现，
    而模型已经开始回应一条"看不见的"用户消息。
    """

    text: str
    steering: bool = False


@dataclass(frozen=True)
class TitleChanged:
    """会话标题变化（自动生成 / `/rename` / 新会话清空）。空串表示回退默认标题。"""

    title: str


# --------------------------------------------------------------------------
# 排队
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class QueueChanged:
    """排队内容变化：增 / 删 / 清 / 投递四个动作各发一次。

    带完整项（id + 文本）而不是纯文本列表：UI 的行尾「✕」要按 id 撤销，
    同文重复时不能靠文本匹配（见 `queues.py` 对 pi 该处缺陷的说明）。
    两个队列一起发，UI 一次刷新即可，不必维护两条独立订阅。
    """

    steering: tuple[QueueItem, ...] = ()
    follow_up: tuple[QueueItem, ...] = ()


# --------------------------------------------------------------------------
# 回合与终结
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnStart:
    """一轮开始（一次模型调用 + 其工具执行）。"""


@dataclass(frozen=True)
class TurnEnd:
    """一轮结束，status 取 `RunResult.status` 的取值。"""

    status: str


@dataclass(frozen=True)
class AgentEnd:
    """终结事件：本次 run 的结果。流到此结束，终结值即 `result`。"""

    result: RunResult


# --------------------------------------------------------------------------
# 事件联合
# --------------------------------------------------------------------------

AgentEvent: TypeAlias = (
    MessageStart
    | MessageUpdate
    | MessageEnd
    | ToolStart
    | ToolPreview
    | ToolEnd
    | PlanUpdate
    | Notice
    | TitleChanged
    | QueueChanged
    | TurnStart
    | TurnEnd
    | StatusChanged
    | StatusCleared
    | PromptStarted
    | PromptFinished
    | AgentEnd
)

# 全部具体事件类型，供完备性断言（见 tests/agent/test_events.py）遍历。
AGENT_EVENT_TYPES: tuple[type, ...] = (
    MessageStart,
    MessageUpdate,
    MessageEnd,
    ToolStart,
    ToolPreview,
    ToolEnd,
    PlanUpdate,
    Notice,
    TitleChanged,
    QueueChanged,
    TurnStart,
    TurnEnd,
    StatusChanged,
    StatusCleared,
    PromptStarted,
    PromptFinished,
    AgentEnd,
)


# --------------------------------------------------------------------------
# 事件流
# --------------------------------------------------------------------------

E = TypeVar("E")
R = TypeVar("R")


class EventStream(Generic[E, R]):
    """有序事件流：`async for` 逐事件消费，`await result()` 取终结值。

    对齐 pi 的 `EventStream`（`packages/ai/src/utils/event-stream.ts`）：消费者可以
    慢于生产者（事件入队），也可以快于生产者（挂起等待）；终结事件由 `is_complete`
    判定、其值由 `extract` 取出。

    与 pi 的差异（刻意）：**不在构造时创建 `asyncio.Future`**。`__init__` 可能在没有
    运行中事件循环的地方被调用，而 `asyncio.get_event_loop()` 在新版本 Python 上已
    不可依赖；这里改为在 `result()` / 迭代内部（必然处于协程中）按需创建。

    生产者异常：`run` 侧在 `finally` 里调用 `end(error)`，`result()` 与迭代都会
    立即收尾，异常原样抛给调用方——不静默截断。
    """

    def __init__(self, is_complete: Callable[[E], bool], extract: Callable[[E], R]) -> None:
        self._is_complete = is_complete
        self._extract = extract
        self._queue: deque[E] = deque()
        self._waiters: deque[asyncio.Future] = deque()
        self._result_waiters: deque[asyncio.Future] = deque()
        self._done = False
        self._has_result = False
        self._result: R | None = None
        self._error: BaseException | None = None

    @property
    def done(self) -> bool:
        """流是否已收尾（终结事件已发或生产者已 `end()`）。"""
        return self._done

    def push(self, event: E) -> None:
        """生产一个事件。终结事件会同时解析终结值；收尾后的事件被忽略。"""
        if self._done:
            return
        if self._is_complete(event):
            self._result = self._extract(event)
            self._has_result = True
            self._done = True
            self._wake_result_waiters()
        waiter = self._waiters.popleft() if self._waiters else None
        if waiter is not None:
            waiter.set_result(event)
        else:
            self._queue.append(event)

    def end(self, error: BaseException | None = None) -> None:
        """生产者收尾：唤醒全部等待者。error 非空表示生产者异常中断。"""
        self._done = True
        if error is not None:
            self._error = error
        while self._waiters:
            self._waiters.popleft().set_result(None)
        self._wake_result_waiters()

    def __aiter__(self) -> AsyncIterator[E]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[E]:
        while True:
            if self._queue:
                yield self._queue.popleft()
            elif self._done:
                return
            else:
                waiter: asyncio.Future = asyncio.get_running_loop().create_future()
                self._waiters.append(waiter)
                event = await waiter
                if event is None:  # end() 唤醒：不再有事件
                    return
                yield event

    async def result(self) -> R:
        """终结值。生产者异常中断时原样抛出该异常。"""
        if not self._has_result and not self._done:
            waiter = asyncio.get_running_loop().create_future()
            self._result_waiters.append(waiter)
            await waiter
        if self._has_result:
            return self._result  # type: ignore[return-value]
        if self._error is not None:
            raise self._error
        raise RuntimeError("事件流已结束，但没有产生终结事件")

    def _wake_result_waiters(self) -> None:
        while self._result_waiters:
            self._result_waiters.popleft().set_result(None)


def agent_event_stream() -> EventStream[AgentEvent, RunResult]:
    """Agent run 的标准事件流：以 `AgentEnd` 终结，终结值为 `RunResult`。

    对齐 pi `agent-loop.ts:152` 的
    `new EventStream(e => e.type === "agent_end", e => e.messages)`——
    只是终结值换成 smithcode 信息更全的 `RunResult`。
    """
    return EventStream(
        lambda event: isinstance(event, AgentEnd),
        lambda event: event.result,
    )
