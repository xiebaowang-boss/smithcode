"""挂起询问登记表：`asked → replied` 跨进程的关联点。

进程内时 `AskPort.ask()` 直接 await 前端，答案出自同一个 await，不需要登记表。
跨进程（`smith serve` + 桌面端）时提问与作答走两条路：
`PromptStarted(id)` 经 SSE 下行，`reply(id, answer)` 经 HTTP 上行——
中间必须有一个按 `prompt_id` 关联两者的地方，就是这里。

设计：
- 一个 `AskPort` 一张表（serve 每会话一个端口，天然按会话隔离）；
- `future` 是等待方（agent 循环线程）的唤醒点，`reply()` 幂等（迟到/重复作答只收第一个）；
- 快照（`snapshot()`）供 `GET /sessions/{sid}/pending` 与 SSE 断线重连补发。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PendingPrompt:
    """一条等作答的提问（快照形态，可 JSON 化）。"""

    id: str
    session_id: str
    kind: str
    title: str
    created: float
    detail: tuple[str, ...] = ()
    options: tuple[str, ...] = ()
    payload: Mapping[str, Any] | None = None


class PendingRegistry:
    """`prompt_id → (PendingPrompt, Future)`。线程安全，`reply` 幂等。"""

    def __init__(self, session_id: str = "") -> None:
        self.session_id = session_id
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[PendingPrompt, asyncio.Future]] = {}

    def register(self, prompt: PendingPrompt, future: asyncio.Future) -> None:
        """登记一条挂起询问；同 id 重复登记直接报错（调用方保证 id 唯一）。"""
        with self._lock:
            if prompt.id in self._entries:
                raise ValueError(f"重复的 prompt id：{prompt.id}")
            self._entries[prompt.id] = (prompt, future)

    def reply(self, prompt_id: str, answer) -> bool:
        """远端作答：唤醒等待方。返回 True = 有人在等且已唤醒。

        迟到/重复作答（已收口或已取消）返回 False，由调用方忽略——
        幂等是跨进程必须的：SSE 重发、用户连点都会产生重复 reply。

        线程安全：`reply()` 常在 HTTP 线程被调，等待的 future 挂在事件循环上，
        必须经 `loop.call_soon_threadsafe` 唤醒——跨线程直接 `set_result` 会
        抛 `RuntimeError` 或静默弄丢唤醒。
        """
        with self._lock:
            entry = self._entries.get(prompt_id)
        if entry is None:
            return False
        _, future = entry
        if future.done():
            return False
        try:
            loop = future.get_loop()
        except (AttributeError, RuntimeError):
            loop = None
        try:
            running = None
            if loop is not None:
                try:
                    running = loop.is_running()
                except RuntimeError:
                    running = None
            if loop is not None and running:
                try:
                    current = asyncio.get_running_loop()
                except RuntimeError:
                    current = None
                if current is loop:
                    future.set_result(answer)
                else:
                    loop.call_soon_threadsafe(future.set_result, answer)
            else:
                future.set_result(answer)
        except (asyncio.InvalidStateError, RuntimeError):
            return False
        return True

    def settle(self, prompt_id: str) -> None:
        """收口：无论等到与否，移除登记（`ask()` 的 finally 调，避免泄漏）。"""
        with self._lock:
            self._entries.pop(prompt_id, None)

    def pending_ids(self) -> tuple[str, ...]:
        """当前挂起的 id（`cancel_in_flight` 用）。"""
        with self._lock:
            return tuple(self._entries)

    def snapshot(self) -> tuple[PendingPrompt, ...]:
        """挂起快照（`GET pending` / 断线重连补发；future 不出快照）。"""
        with self._lock:
            return tuple(prompt for prompt, _ in self._entries.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


def make_prompt(session_id: str, prompt_id: str, request) -> PendingPrompt:
    """`AskRequest` → 可传输的快照（payload 原样带，由接收方按 kind 解释）。"""
    return PendingPrompt(
        id=prompt_id,
        session_id=session_id,
        kind=request.kind,
        title=request.title,
        created=time.time(),
        detail=tuple(request.detail),
        options=tuple(request.options),
        payload=dict(request.payload) if request.payload else None,
    )
