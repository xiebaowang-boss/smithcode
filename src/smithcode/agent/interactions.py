"""交互（阻塞）事件：agent 等待用户输入的开始/结束配对。

**为什么不是笼统的 waiting 事件**：pi 的核心 `AgentEvent` 里没有任何 waiting
事件（`packages/agent/src/types.ts:448-463` 只有 lifecycle / message / tool 十类），
「在等用户」是扩展层一对**带判别字段**的事件
（`packages/coding-agent/src/core/extensions/types.ts:747-760`）：

    type UIPromptKind = "select" | "confirm" | "input" | "editor" | "custom";
    interface UIPromptStartEvent { type: "ui_prompt_start"; reason: "ui_prompt"; kind: UIPromptKind; title?: string; }

消费者拿 `kind` 决定怎么渲染，用 begin/end 配对判断「是否还在等」。本模块照此建模，
并加上 smithcode 需要的两件东西：

- **`id` 配对**：现有实现的 `turn_waiting_started/finished` 是**无载荷的标量信号**，
  发送方（`Relay`）包住 ask 方法发射，注释里明确写着「收发不平衡时消费方应自行用
  计数兜底——确认可能嵌套」。有了 `id`，嵌套、并发、异常路径都能精确配对，
  消费方无需计数兜底。
- **`blocking` 显式标识**：事件本身说明「此期间 agent 不会推进」，消费方不必
  按事件类型去猜。

发射点在交互端口内部（`try/finally` 保证配对），不在调用点——调用点零改动，
且前端故障也一定收到结束事件。
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

# 阻塞来源。与现有 5 个阻塞点一一对应（见 docs/rebuild-plan.md）：
# permission      工具权限确认      permission/engine.py:443
# outside_access  越界访问授权      permission/engine.py:341
# skill_trust     项目级技能信任    skills/registry.py:275
# ask_user        模型向用户提问    tools/ask.py:130
# confirm         其它 confirm_choice 调用方（如 /mcp 向导）
PromptKind = Literal["permission", "outside_access", "skill_trust", "ask_user", "confirm"]

# 用户作答的结果分类。answered 之外一律视为「没有拿到可用答案」，
# 调用方按各自既有语义兜底（拒绝 / 取消 / 跳过）。
PromptOutcome = Literal["answered", "cancelled", "denied", "error"]


@dataclass(frozen=True)
class PromptStarted:
    """开始等待用户输入。`blocking=True` 表示此事件期间 agent 不会推进。"""

    id: str
    kind: PromptKind
    title: str
    blocking: bool = True
    detail: tuple[str, ...] = ()
    options: tuple[str, ...] = ()
    # kind 专属载荷：permission 带 tool_call_id / outside_access 带路径 /
    # ask_user 带问题列表。消费方按 kind 解释，不做跨 kind 解析。
    payload: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class PromptFinished:
    """等待结束。与 `PromptStarted` 同 id 严格配对。"""

    id: str
    kind: PromptKind
    outcome: PromptOutcome
    # 用户选择（已脱敏）；denied / cancelled 为空。
    value: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class PromptRequest:
    """一次提问的声明部分（呈现所需的一切；不含「怎么问」——那是 `run` 闭包）。

    `title` / `detail` / `options` 是给**通用**订阅者看的（终端标题、状态栏、
    GUI 面板）：它们据此就能显示「在等什么」。具体怎么问仍由调用点决定
    （`ask` 的 `run` 闭包），因此各阻塞点的既有交互形态与语义逐字不变。
    """

    kind: PromptKind
    title: str
    detail: tuple[str, ...] = ()
    options: tuple[str, ...] = ()
    payload: Mapping[str, Any] | None = None


T = TypeVar("T")

PromptEventSink = Callable[[PromptStarted | PromptFinished], None]


class InteractionBridge:
    """在阻塞提问的进出两侧发成对事件（`id` 配对，`try/finally` 保证收口）。

    对齐 pi 的 `Runner.withUIPrompt`（`core/extensions/runner.ts:487-514`）的
    **形状**：进发 start、出（含异常路径）发 end、配对靠计数/depth。差异在配对
    手段——pi 用深度计数器并把嵌套合并成最外层一个区间（因为它没有 id，且它的
    扩展事件派发是 fire-and-forget，提问来源不共享调用栈）；这里每问一次发一个
    id，消费者用 `open_prompts` 自行判定「还在不在等」。这样：

    - `bool(open_prompts)` 等价于 pi 的 `depth > 0`；
    - 「嵌套时显示最外层」用 `next(iter(open_prompts.values()))` 即可（dict 保插入序）；
    - 而「是谁结束了」在事件里说得清——标量计数做不到这一点。

    `max_open` 是高水位，供测试断言「我们自己的流程从不嵌套」；真出现重叠时，
    先看到的是这个数字，而不是一个含义模糊的界面。
    """

    def __init__(self, emit: PromptEventSink) -> None:
        self._emit = emit
        self._open: dict[str, PromptStarted] = {}
        self._lock = threading.Lock()
        self.max_open = 0

    @property
    def open_prompts(self) -> Mapping[str, PromptStarted]:
        """当前未结束的提问（顺序 = 发起顺序，最外层在前）。"""
        with self._lock:
            return dict(self._open)

    def request(
        self,
        req: PromptRequest,
        run: Callable[[], T],
        outcome_of: Callable[[T], PromptOutcome] | None = None,
    ) -> T:
        """执行一次阻塞提问：进发 `PromptStarted`、出（含异常）发 `PromptFinished`。

        `run` 是既有的阻塞调用（`renderer.current().confirm_choice(...)` 之类），
        语义与改造前逐字相同；本方法只负责两侧的事件与配对。
        """
        prompt_id = uuid.uuid4().hex
        started = PromptStarted(
            id=prompt_id, kind=req.kind, title=req.title,
            detail=req.detail, options=req.options, payload=req.payload,
        )
        with self._lock:
            self._open[prompt_id] = started
            self.max_open = max(self.max_open, len(self._open))
        self._emit(started)
        try:
            value = run()
        except BaseException as exc:  # 前端故障也要配对收口，否则消费者永远在等
            self._finish(prompt_id, req.kind, "error", error=str(exc))
            raise
        self._finish(
            prompt_id, req.kind,
            outcome_of(value) if outcome_of is not None else "answered",
            value=value if isinstance(value, str) else None,
        )
        return value

    def _finish(self, prompt_id: str, kind: PromptKind, outcome: PromptOutcome,
                value: str | None = None, error: str | None = None) -> None:
        with self._lock:
            self._open.pop(prompt_id, None)
        self._emit(
            PromptFinished(id=prompt_id, kind=kind, outcome=outcome, value=value, error=error)
        )


_current: ContextVar[InteractionBridge | None] = ContextVar(
    "smithcode_interactions", default=None
)


def current() -> InteractionBridge | None:
    """当前生效的桥（无则 None：单元测试 / 非 Agent 路径直接调端口）。"""
    return _current.get()


def activate(bridge: InteractionBridge | None) -> Token:
    """挂载桥，返回复位令牌（与 `signal.activate_token` 同形）。"""
    return _current.set(bridge)


def reset(token: Token) -> None:
    """按令牌复位（与 `activate` 配对；测试隔离也用它）。"""
    _current.reset(token)


def ask(
    kind: PromptKind,
    *,
    title: str,
    run: Callable[[], T],
    detail: tuple[str, ...] = (),
    options: tuple[str, ...] = (),
    payload: Mapping[str, Any] | None = None,
    outcome_of: Callable[[T], PromptOutcome] | None = None,
) -> T:
    """阻塞提问的唯一入口：有桥就成对发事件，没有就退化为直接调用。

    退化路径是刻意的：权限引擎、技能信任等模块的单测直接调用它们，不经 Agent，
    此时不该因为「没人订阅」而改变任何行为。
    """
    bridge = _current.get()
    if bridge is None:
        return run()
    return bridge.request(
        PromptRequest(kind=kind, title=title, detail=detail, options=options, payload=payload),
        run,
        outcome_of,
    )
