"""Agent 钩子：四个决策点（对齐 pi 的 `AgentLoopConfig`）。

pi 把「工具执行前后」与「回合结束时怎么办」做成了配置项
（`packages/agent/src/types.ts:285,300,415`），smithcode 把它们内联在
`Agent._preflight` / `_collect` / `_run_loop` 与会话层的 `run_with_goal` 里。本模块只把
边界**外露**：不设钩子时行为与改造前逐字相同（默认实现就是现状）。

三条刻意的取舍（与方案的差异见 docs/rebuild-plan.md）：

1. **权限与路径检查留在核心**。方案写「`_preflight()` 的权限/路径检查成为
   `before_tool_call` 的默认实现」，实际做成「`before_tool_call` 是预检**之前**
   的异步决策点，权限与路径检查仍走核心的默认路径」。理由：安全边界不外包给
   钩子（钩子没配、配错、抛异常都不该让沙箱失效）；而且预检要跑在 worker
   线程里（阻塞式弹窗不能占住事件循环），异步钩子跑不了那儿。
2. **`prepare_next_turn` 是附加调用点**，不替换默认准备（`sync_system` +
   按需压缩）。提示段的字节级稳定与压缩是正确性所需，不该因为「钩子没写」而
   丢掉。钩子在默认准备**之后**被 await。
3. **不提供 `is_error` 覆盖**：smithcode 的工具结果就是一段文本（错误以
   `错误: …` 形式呈现），消息里没有 `is_error` 字段，覆盖它无处可落。

`terminate` 对齐 pi 的批次语义（`agent-loop.ts:644` 的 `shouldTerminateToolBatch`）：
**整批结果都为 True** 才提前结束本次 run，否则继续下一轮模型调用。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .signal import AbortSignal

Message = dict[str, Any]


@dataclass(frozen=True)
class BeforeToolCallContext:
    """`before_tool_call` 的入参：本批助手消息 + 待执行的调用 + 已解析的参数。"""

    assistant_message: Message
    tool_call: Message
    args: dict[str, Any]


@dataclass(frozen=True)
class BeforeToolCallResult:
    """`block=True` 阻止本次调用（其余调用照常），`reason` 作为结果文本回传模型。

    `terminate` 参与批次提前结束规则（见模块 docstring）。
    """

    block: bool = False
    reason: str = ""
    terminate: bool = False


@dataclass(frozen=True)
class AfterToolCallContext:
    """`after_tool_call` 的入参：再加上刚跑完的结果。"""

    assistant_message: Message
    tool_call: Message
    args: dict[str, Any]
    result: str


@dataclass(frozen=True)
class AfterToolCallResult:
    """`content` 非空时替换结果文本（收集与展示都用替换后的值）。"""

    content: str | None = None
    terminate: bool | None = None


@dataclass(frozen=True)
class TurnContext:
    """一轮的描述，供 `prepare_next_turn` / `should_stop_after_turn` 裁决。

    `message` 是本轮模型给的助手消息（首轮为 `{}`），`tool_results` 是本轮工具
    结果文本（按提交序）。
    """

    message: Message = field(default_factory=dict)
    tool_results: tuple[str, ...] = ()
    tools_used: tuple[str, ...] = ()
    iteration: int = 0


BeforeToolCall = Callable[[BeforeToolCallContext, AbortSignal], Awaitable[BeforeToolCallResult | None]]
AfterToolCall = Callable[[AfterToolCallContext, AbortSignal], Awaitable[AfterToolCallResult | None]]
PrepareNextTurn = Callable[[TurnContext, AbortSignal], Awaitable[None]]
ShouldStopAfterTurn = Callable[[TurnContext, AbortSignal], Awaitable[bool]]


@dataclass(frozen=True)
class AgentHooks:
    """四个可选钩子；全部为 None 时 `Agent` 的行为与没有本模块时完全一致。"""

    before_tool_call: BeforeToolCall | None = None
    after_tool_call: AfterToolCall | None = None
    prepare_next_turn: PrepareNextTurn | None = None
    should_stop_after_turn: ShouldStopAfterTurn | None = None
