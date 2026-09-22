"""把同步的 LLM 流式生成器接到 asyncio 上。

**为什么保留同步客户端**：`LLMClient.chat_stream()`（同步生成器，yield
`(kind, payload)`）是项目真正的模型边界——60 处测试用
`monkeypatch.setattr("smithcode.agent.LLMClient", …)` 注入假客户端。若再引入
pi 那样的 `StreamFn` 协议（`Callable[[model, ctx, options], AsyncIterator]`），
就成了两套等价接缝：旧的那个仍要保留（测试与扩展在用），新的只是多一层转发。
所以这里**不引入新协议**，只提供一个把同步生成器抽到事件循环上的辅助器；
方案 §7(1) 的 `StreamFn` 抽象按此不做（见 docs/rebuild-plan.md 的偏差表）。

**逐块 `to_thread` 而不是整段下放**：整段下放要再造一个队列 + 哨兵 + 异常搬运，
而逐块抽取只需一次 `await`，且与既有的取消机制天然吻合——取消时
`llm/client.py` 登记的关流回调会让阻塞在读取上的 `next()` 立刻抛错返回，
工作线程不会滞留（这点重要：`asyncio.run` 退出时会等待默认执行器收尾，
滞留线程会让进程挂住）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import TypeVar

from .signal import AbortSignal

T = TypeVar("T")

# 生成器已耗尽的哨兵：`next(gen, sentinel)` 免去 StopIteration 的传播
_EXHAUSTED = object()


async def drain_sync_stream(
    source: Iterator[T], signal: AbortSignal | None = None
) -> AsyncIterator[T]:
    """逐块抽取同步生成器，不阻塞事件循环。

    中止时**静默结束**（不抛）：消费方看到流提前结束，按 `signal.aborted` 判定
    中断。

    检查点只在**拉取之前**：中止后不再发起新的拉取。已经在途的那一次 `next()`
    仍会产出——它的结果会被让出。这是刻意的行为等价：异步化之前，同步 `for`
    循环同样会先拿到「用户按下 Esc 那一刻已经产出」的那一块再退出，用户屏幕上
    已经显示了它；丢掉它会变成"屏幕上少了、历史里也少了"，属于本阶段不该引入的
    行为变化。真正阻止后续产出的是 `llm/client.py` 的关流回调（`_iter_cancellable`
    在取消后立刻 `return`）。
    """
    while True:
        if signal is not None and signal.aborted:
            return
        item = await asyncio.to_thread(next, source, _EXHAUSTED)
        if item is _EXHAUSTED:
            return
        yield item
