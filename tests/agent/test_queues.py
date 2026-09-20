"""排队（agent/queues.py）：增删清投四个动作的语义 + 变更通知。"""

from __future__ import annotations

import threading

import pytest

from smithcode.agent.queues import MessageQueue, QueueItem


def _queue(mode="one-at-a-time", kind="follow_up", notify=None):
    return MessageQueue(kind, mode=mode, on_change=notify)


def test_enqueue_builds_item_with_identity_and_kind():
    queue = _queue(kind="steer")
    item = queue.enqueue("帮我看下日志")

    assert isinstance(item, QueueItem)
    assert item.text == "帮我看下日志"
    assert item.kind == "steer"
    assert item.id and len(item.id) == 32
    assert item.created_at > 0


def test_items_keep_enqueue_order():
    queue = _queue()
    first = queue.enqueue("一")
    second = queue.enqueue("二")

    assert [item.id for item in queue.list()] == [first.id, second.id]
    assert queue.count == 2


def test_list_returns_a_copy():
    queue = _queue()
    queue.enqueue("一")

    queue.list().clear()

    assert queue.count == 1


def test_remove_targets_the_id_not_the_text():
    """同文重复时按 id 撤销，不能删错项（pi 按文本 indexOf 的缺陷）。"""
    queue = _queue()
    first = queue.enqueue("重复的话")
    second = queue.enqueue("重复的话")

    assert queue.remove(first.id) is True
    assert [item.id for item in queue.list()] == [second.id]


def test_remove_unknown_id_reports_false_without_notifying():
    seen: list[int] = []
    queue = _queue(notify=lambda: seen.append(1))

    assert queue.remove("不存在") is False
    assert seen == []


def test_clear_returns_removed_items():
    queue = _queue()
    queue.enqueue("一")
    queue.enqueue("二")

    removed = queue.clear()

    assert [item.text for item in removed] == ["一", "二"]
    assert queue.count == 0


def test_clear_on_empty_queue_does_not_notify():
    seen: list[int] = []
    queue = _queue(notify=lambda: seen.append(1))

    assert queue.clear() == []
    assert seen == []


def test_drain_one_at_a_time_takes_the_earliest():
    queue = _queue(mode="one-at-a-time")
    queue.enqueue("一")
    queue.enqueue("二")

    assert [item.text for item in queue.drain()] == ["一"]
    assert [item.text for item in queue.list()] == ["二"]


def test_drain_all_takes_everything():
    queue = _queue(mode="all")
    queue.enqueue("一")
    queue.enqueue("二")

    assert [item.text for item in queue.drain()] == ["一", "二"]
    assert queue.count == 0


def test_drain_on_empty_queue_does_not_notify():
    seen: list[int] = []
    queue = _queue(notify=lambda: seen.append(1))

    assert queue.drain() == []
    assert seen == []


def test_every_mutation_notifies_once():
    """增 / 删 / 清三个动作各发一次变更通知，且带**动作名**与项。

    投递（`drain`）刻意不发通知：只有调用方知道这次取走是"投递"（要进历史、要
    上屏），所以投递事件由调用方发（见 `Agent._deliver`）——在这里发会让同一次
    投递被通知两遍（面板与对话区各多一条）。
    """
    calls: list[tuple] = []
    queue = _queue(notify=lambda action, item: calls.append((action, item)))
    item = queue.enqueue("一")  # 1 增
    queue.enqueue("二")  # 2 增
    queue.remove(item.id)  # 3 删
    queue.clear()  # 4 清（此时还有「二」，内容确实变了）
    queue.enqueue("三")  # 5 增
    queue.drain()  # 投：不发通知（由调用方发）

    assert [action for action, _ in calls] == [
        "enqueued", "enqueued", "cancelled", "cleared", "enqueued",
    ]
    assert calls[0][1].text == "一"
    assert calls[2][1].id == item.id


def test_mode_is_switchable_and_validated():
    queue = _queue(mode="one-at-a-time")
    queue.enqueue("一")
    queue.enqueue("二")
    queue.mode = "all"

    assert len(queue.drain()) == 2

    with pytest.raises(ValueError):
        _queue(mode="unknown")


def test_images_are_carried_through():
    queue = _queue()
    image = {"type": "image_url", "image_url": {"url": "data:x"}}

    item = queue.enqueue("看图", images=(image,))

    assert item.images == (image,)


def test_concurrent_enqueue_from_ui_thread_does_not_lose_items():
    """入队来自 UI 线程、投递来自循环线程：并发下不能丢项或重项。"""
    queue = _queue(mode="all")

    def enqueue_many(prefix: str) -> None:
        for index in range(50):
            queue.enqueue(f"{prefix}-{index}")

    threads = [
        threading.Thread(target=enqueue_many, args=(name,)) for name in ("a", "b", "c")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    items = queue.list()
    assert len(items) == 150
    assert len({item.id for item in items}) == 150
