"""取消原语测试：令牌语义与 LLM 流式截停，不依赖真实 API。"""

import threading
import time
from types import SimpleNamespace

from smithcode.cancel import CancellationToken, activate_token, current_token
from smithcode.llm import LLMClient


def test_token_cancel_idempotent():
    """cancel 幂等：理由与监听回调都只生效一次；任意线程查询 cancelled。"""
    token = CancellationToken()
    fired = []
    token.subscribe(lambda: fired.append(1))
    token.cancel("测试中断")
    token.cancel("再次取消")
    assert token.cancelled
    assert token.reason == "测试中断"
    assert fired == [1]


def test_token_cancel_thread_safe():
    """跨线程 cancel 可见（TUI/REPL 主线程发起、Agent 线程检查的场景）。"""
    token = CancellationToken()
    threading.Thread(target=token.cancel, daemon=True).start()
    while not token.cancelled:
        pass  # 等待另一线程的取消生效
    assert token.reason == "用户中断"


def test_activate_token_scopes_current_token():
    """activate_token 设定/复位当前令牌；无任务时 current_token 为 None。"""
    assert current_token() is None
    token = CancellationToken()
    reset = activate_token(token)
    try:
        assert current_token() is token
    finally:
        reset()
    assert current_token() is None


def _content_chunk(text):
    delta = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=delta)])


def test_stream_stops_and_closes_on_cancel():
    """取消后流不再产出事件、HTTP 流被立即关闭（message/usage 一并放弃）。"""
    token = CancellationToken()
    reset = activate_token(token)
    closed = []

    class FakeStream:
        def __iter__(self):
            yield _content_chunk("你好")
            token.cancel()  # 模拟用户按 Esc
            yield _content_chunk("世界")  # 取消后到达的块：应被丢弃

        def close(self):
            closed.append(True)

    llm = object.__new__(LLMClient)  # 绕过 __init__（无需真实 API 配置）
    llm._open_stream = lambda kwargs: FakeStream()

    events = list(llm._stream_once({}))
    reset()

    assert events == [("content", "你好")]
    assert closed  # 订阅回调与 with closing 至少关闭一次
    assert not any(kind == "message" for kind, _ in events)


def test_cancel_unblocks_waiting_stream():
    """取消线程直接关流：阻塞在等下一块数据时被立即解除，无需等块到达。"""
    token = CancellationToken()
    reset = activate_token(token)

    class BlockingStream:
        """模拟网络静默：迭代时阻塞，直到流被 close() 唤醒。"""

        def __init__(self):
            self.closed = threading.Event()

        def __iter__(self):
            self.closed.wait(5)  # 没有数据块到达，靠 close() 解除
            return iter(())

        def close(self):
            self.closed.set()

    stream = BlockingStream()
    llm = object.__new__(LLMClient)
    llm._open_stream = lambda kwargs: stream

    threading.Thread(target=lambda: (time.sleep(0.1), token.cancel()), daemon=True).start()
    start = time.monotonic()
    events = list(llm._stream_once({}))  # 主线程消费，取消来自另一线程
    elapsed = time.monotonic() - start
    reset()

    assert events == []  # 取消后不产出任何事件（含 message）
    assert stream.closed.is_set()
    assert elapsed < 2  # 远小于 BlockingStream 的 5s 兜底等待，证明阻塞被解除


def test_stream_skips_open_when_already_cancelled():
    """已取消时不发起新请求：打开流之前就返回。"""
    token = CancellationToken()
    reset = activate_token(token)
    token.cancel()
    opened = []

    llm = object.__new__(LLMClient)
    llm._open_stream = lambda kwargs: opened.append(True)
    events = list(llm._stream_once({}))
    reset()

    assert events == []
    assert opened == []  # 未调用 _open_stream


def test_stream_completes_normally_without_cancel():
    """无取消时行为不变：增量之后产出完整 message。"""

    class FakeStream:
        def __iter__(self):
            yield _content_chunk("你好")
            yield _content_chunk("世界")

        def close(self):
            pass

    llm = object.__new__(LLMClient)
    llm._open_stream = lambda kwargs: FakeStream()

    events = list(llm._stream_once({}))
    kinds = [kind for kind, _ in events]
    assert kinds[-1] == "message"
    assert events[-1][1]["content"] == "你好世界"
