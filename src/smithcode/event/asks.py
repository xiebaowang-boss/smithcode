"""阻塞询问的事件对：进发 `PromptStarted`、出（含异常路径）发 `PromptFinished`。

对齐 pi 的 `Runner.withUIPrompt`（`core/extensions/runner.ts:487-514`）与 opencode 的
`permission.asked → permission.replied`：**用事件表达「在等什么」与「等到了什么」**，
消费者（终端标题、面板、将来的远程前端）自己按 `id` 配对判定「还在不在等」。

差异（刻意）：
- pi 用深度计数器把嵌套合并成最外层一个区间（它没有 id，事件派发是 fire-and-forget）；
  这里每次提问一个 `id`，「是谁结束了」在事件里说得清。
- 本模块**不持有任何状态**：配对信息全在事件里，消费方按 id 自建映射（如
  `title.py` 的 `open_prompts`）。曾经这里有个 `InteractionBridge` 跟踪表，
  运行期已无消费方，故删除——不为无人读的状态留代码。

`run` 是既有的**阻塞**调用（`renderer → 前端` 的询问方法），语义与改造前逐字相同；
把询问本身改成异步请求-应答（`AskPort`）是阶段 C 的事。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

from .bus import publish
from .catalog import PromptFinished, PromptKind, PromptOutcome, PromptStarted
from .envelope import new_id

T = TypeVar("T")


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
    """阻塞提问的唯一入口：`run()` 两侧发成对事件（`try/finally` 保证收口）。

    无总线时事件被静默丢弃——权限引擎、技能信任等模块的单测直接调用它们、
    不经 Agent，此时不该因为「没人订阅」而改变任何行为。

    `run` 抛异常也要发 `PromptFinished(outcome="error")`：否则消费者会永远停在
    「在等」上（这不是理论问题：前端故障就是这样表现）。
    """
    prompt_id = new_id()
    publish(PromptStarted(
        id=prompt_id, kind=kind, title=title, detail=detail, options=options,
        payload=payload,
    ))
    try:
        value = run()
    except BaseException as exc:  # 前端故障也要配对收口
        publish(PromptFinished(id=prompt_id, kind=kind, outcome="error", error=str(exc)))
        raise
    publish(PromptFinished(
        id=prompt_id,
        kind=kind,
        outcome=outcome_of(value) if outcome_of is not None else "answered",
        value=value if isinstance(value, str) else None,
    ))
    return value
