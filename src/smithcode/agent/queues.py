"""运行中排队：steering（插话）与 follow-up（下一轮）两条队列。

**为什么需要它**：现在任务运行期间用户再提交输入只有两种结局——被提示「上一条
任务还在运行」而丢弃，或（TUI）干脆不可输入。pi 的 `PendingMessageQueue`
（`agent.ts:140-174`）把这段输入存下来，在循环的三个抽水点取用（见 `agent.py` 的
`_drain_*`）：起点、每轮末（steering）、本要停时（follow-up）。

**与 pi 的差异（修正其缺陷）**：pi 的出队按**文本** `indexOf` 匹配
（`interactive-mode.ts:4409-4424`），同文重复时会删错项。这里每项带 `id`，
撤销与投递都精确到项。

线程模型：入队来自 UI 线程（用户按键），投递发生在事件循环线程。两侧都会改动
列表，故加锁；变更通知（`on_change`）由调用方负责转到事件循环（见
`Agent._emit_threadsafe`）。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

# 抽水策略：all = 一次全部取走；one-at-a-time = 每次只取最早一条（对齐 pi 的
# `PendingMessageQueue` mode 与 settings 里的同名选择器）。
QueueMode = Literal["all", "one-at-a-time"]

# 排队来源：steer = 当前任务运行中插话，follow_up = 等本轮跑完再送。
QueueItemKind = Literal["steer", "follow_up"]


@dataclass(frozen=True)
class QueueItem:
    """一条排队输入。

    `images` 沿用会话消息里同一形状（`type: image_url` 的 provider dict），
    与现有 `Session.add` 的图片参数一致；本阶段 UI 不产生图片，字段先留着，
    避免以后加图片时改事件与 UI 的形状。
    """

    id: str
    text: str
    kind: QueueItemKind
    images: tuple[Any, ...] | None = None
    created_at: float = 0.0


class MessageQueue:
    """单条队列。增删清投四个动作都会触发 `on_change`（内容真的变了才触发）。"""

    def __init__(
        self,
        kind: QueueItemKind,
        mode: QueueMode = "one-at-a-time",
        on_change: Callable[[], None] | None = None,
    ) -> None:
        if mode not in ("all", "one-at-a-time"):
            raise ValueError(f"未知的抽水策略: {mode!r}")
        self.kind = kind
        self.mode: QueueMode = mode
        self._on_change = on_change
        self._items: list[QueueItem] = []
        self._lock = threading.Lock()

    # ---------- 查询 ----------

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._items)

    def list(self) -> list[QueueItem]:
        """当前快照（顺序 = 入队顺序）。"""
        with self._lock:
            return list(self._items)

    # ---------- 变更 ----------

    def enqueue(self, text: str, images: tuple[Any, ...] | None = None) -> QueueItem:
        item = QueueItem(
            id=uuid.uuid4().hex,
            text=text,
            kind=self.kind,
            images=images,
            created_at=time.time(),
        )
        with self._lock:
            self._items.append(item)
        self._notify()
        return item

    def remove(self, item_id: str) -> bool:
        """按 id 撤销一条。返回是否真的删掉了（UI 的逐条撤销用）。"""
        with self._lock:
            before = len(self._items)
            self._items = [item for item in self._items if item.id != item_id]
            changed = len(self._items) != before
        if changed:
            self._notify()
        return changed

    def take(self, item_id: str) -> QueueItem | None:
        """摘出某一项并返回（UI 的「取回编辑」：要出队，但内容得还给用户）。

        与 `remove` 的区别只在于返回值：撤销只要知道成没成，编辑还要那份文本。
        """
        with self._lock:
            taken = next((item for item in self._items if item.id == item_id), None)
            if taken is not None:
                self._items = [item for item in self._items if item.id != item_id]
        # 通知必须在**放锁之后**：回调（`Agent._on_queue_changed`）会回来读同一条
        # 队列的 `list()`，而 `self._lock` 不是可重入锁——在锁内通知会当场自锁死
        # （真实 UI 上表现为点 `edit` 整个界面卡住不响应）。
        if taken is not None:
            self._notify()
        return taken

    def clear(self) -> list[QueueItem]:
        """清空并返回被清掉的项（Esc 中止时取回编辑器用）。"""
        with self._lock:
            removed, self._items = self._items, []
        if removed:
            self._notify()
        return removed

    def drain(self) -> list[QueueItem]:
        """按 `mode` 取走待投递的项（投递点调用）。

        `all` 全部取走；`one-at-a-time` 只取最早一条，剩下的留到下一个投递点。
        """
        with self._lock:
            if self.mode == "all":
                taken, self._items = self._items, []
            else:
                taken = self._items[:1]
                self._items = self._items[1:]
        if taken:
            self._notify()
        return taken

    # ---------- 内部 ----------

    def _notify(self) -> None:
        if self._on_change is not None:
            self._on_change()
