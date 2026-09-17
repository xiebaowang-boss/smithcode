"""LLM 客户端测试：`GET /models` 拉取的容错、去重与翻页；流中断线自动重试。"""

from types import SimpleNamespace

import httpx2
import pytest

import smithcode.llm.client as client_mod
from smithcode.llm import LLMClient

DROP = "peer closed connection without sending complete message body (incomplete chunked read)"


def _fake_client_factory(models=None, error=None):
    """构造 OpenAI SDK 替身的工厂：只实现 models.list，供 from_config 注入。"""

    class _FakeModels:
        def __init__(self):
            self._items = models or []
            self._error = error

        def list(self):
            if self._error:
                raise self._error
            return self._items

    def factory(**kwargs):
        return SimpleNamespace(models=_FakeModels())

    return factory


def _client(items=None, error=None):
    # 显式构造：不校验 key、不建真实连接，只替换 models.list
    return LLMClient(
        api_key="test", base_url=None, timeout=1.0, default_model="m",
        client_factory=_fake_client_factory(items, error),
    )


def test_list_models_returns_unique_ids():
    items = [SimpleNamespace(id="a"), SimpleNamespace(id="b"), SimpleNamespace(id="a")]
    assert _client(items).list_models() == ["a", "b"]


def test_list_models_empty_returns_none():
    assert _client([]).list_models() is None


def test_list_models_error_returns_none():
    assert _client(error=RuntimeError("不支持 /models")).list_models() is None


def test_list_models_skips_items_without_id():
    items = [SimpleNamespace(id="a"), SimpleNamespace(), SimpleNamespace(id="")]
    assert _client(items).list_models() == ["a"]


# ---------- 流中断线自动重试 ----------


class _FakeView:
    """只记录重试相关调用的渲染后端（其余事件忽略）。"""

    def __init__(self):
        self.retries = []
        self.finished = 0
        self.warns = []

    def retry_started(self, state, owner=None):
        self.retries.append(state)

    def retry_finished(self, owner=None):
        self.finished += 1

    def warn(self, text):
        self.warns.append(text)


def _streaming_client(stream_once):
    """显式构造的流式客户端：只替换 _stream_once，请求头为空。"""
    llm = LLMClient(
        api_key="test", base_url=None, timeout=1.0, default_model="m",
        client_factory=lambda **kwargs: SimpleNamespace(),
    )
    llm._stream_once = stream_once
    return llm


def _patch_retry(monkeypatch, view: _FakeView, retries: int = 2):
    monkeypatch.setattr(client_mod.config, "MAX_RETRIES", retries)
    monkeypatch.setattr(client_mod.retry_mod, "wait", lambda state: None)  # 不真的退避
    monkeypatch.setattr("smithcode.renderer.current", lambda: view)


def test_chat_stream_retries_incomplete_stream(monkeypatch):
    """思考中流被掐断：自动重试，退避前上报重试状态（含尝试序号与原因）。"""
    calls = []

    def fake_stream(kwargs):
        calls.append(1)
        if len(calls) == 1:
            yield ("reasoning", "先想一半…")
            raise httpx2.RemoteProtocolError(DROP)  # 已输出的思考不阻断重试
        yield ("content", "答案")
        yield ("message", {"role": "assistant", "content": ""})

    view = _FakeView()
    _patch_retry(monkeypatch, view)

    events = list(_streaming_client(fake_stream).chat_stream(
        [{"role": "user", "content": "问题"}]
    ))

    assert len(calls) == 2  # 首次失败 + 一次重试成功
    assert [kind for kind, _ in events] == ["reasoning", "content", "message"]
    assert len(view.retries) == 1
    state = view.retries[0]
    assert state.attempt == 2 and state.total == 3
    assert state.reason == "连接中断"
    assert "正在重试 2/3" in state.text()
    assert view.finished == 1  # 过程结束一定收口


def test_chat_stream_retries_after_content(monkeypatch):
    """**已输出正文后断流同样重试**（对齐 opencode / Codex）。

    旧行为是"已打印正文就放弃"，导致一次读完超时报废整轮；现改为整请求重发，
    两次尝试的正文都会实时上屏（由 Agent 按尝试累积进同一条消息）。
    """
    calls = []

    def fake_stream(kwargs):
        calls.append(1)
        if len(calls) == 1:
            yield ("content", "半句总结：改了 ")
            raise httpx2.ReadTimeout("The read operation timed out")
        yield ("content", "半句总结：改了 commands/base.py，Enter 已屏蔽。")
        yield ("message", {"role": "assistant", "content": ""})

    view = _FakeView()
    _patch_retry(monkeypatch, view, retries=3)

    events = list(_streaming_client(fake_stream).chat_stream(
        [{"role": "user", "content": "问题"}]
    ))

    assert len(calls) == 2  # 关键在于：正文已上屏仍然重试了
    contents = [payload for kind, payload in events if kind == "content"]
    assert contents == ["半句总结：改了 ", "半句总结：改了 commands/base.py，Enter 已屏蔽。"]
    # message 只交付 tool_calls 与归属（content 刻意留空），失败尝试的不污染会话
    messages = [payload for kind, payload in events if kind == "message"]
    assert messages == [{"role": "assistant", "content": ""}]
    assert view.retries[0].reason == "读取超时"


def test_chat_stream_raises_after_retries_exhausted(monkeypatch):
    """重试次数用尽仍失败：抛原错误，每次退避前都已上报重试状态。"""
    calls = []

    def fake_stream(kwargs):
        calls.append(1)
        yield from ()  # 保持生成器语义
        raise httpx2.RemoteProtocolError(DROP)

    view = _FakeView()
    _patch_retry(monkeypatch, view)

    with pytest.raises(httpx2.RemoteProtocolError):
        list(_streaming_client(fake_stream).chat_stream(
            [{"role": "user", "content": "问题"}]
        ))

    assert len(calls) == 3  # 初次 + 2 次重试
    assert [s.attempt for s in view.retries] == [2, 3]
    assert view.finished == 1


def test_chat_stream_does_not_retry_non_transient(monkeypatch):
    """不可重试的错误（如 400 参数错误）一次就抛，不浪费预算。"""
    from openai import BadRequestError

    calls = []

    def fake_stream(kwargs):
        calls.append(1)
        yield from ()
        response = httpx2.Response(400, request=httpx2.Request("POST", "http://x"))
        raise BadRequestError("bad", response=response, body=None)

    view = _FakeView()
    _patch_retry(monkeypatch, view, retries=3)

    with pytest.raises(BadRequestError):
        list(_streaming_client(fake_stream).chat_stream(
            [{"role": "user", "content": "问题"}]
        ))

    assert len(calls) == 1
    assert view.retries == []
    assert view.finished == 1  # 即便没有重试也要收口，避免状态行挂在"重试中"


def test_chat_stream_retry_finished_on_success_without_retry(monkeypatch):
    """一次成功也要发 retry_finished：消费方据此清除可能残留的重试态。"""
    def fake_stream(kwargs):
        yield ("message", {"role": "assistant", "content": ""})

    view = _FakeView()
    _patch_retry(monkeypatch, view)

    events = list(_streaming_client(fake_stream).chat_stream(
        [{"role": "user", "content": "问题"}]
    ))

    assert [kind for kind, _ in events] == ["message"]
    assert view.retries == []
    assert view.finished == 1


def test_list_models_follows_next_page():
    """多页模型列表按 has_next_page/get_next_page 翻页，去重后返回。"""
    first = [SimpleNamespace(id="a"), SimpleNamespace(id="b")]
    second = [SimpleNamespace(id="b"), SimpleNamespace(id="c")]

    class _Page(list):
        def __init__(self, items, nxt=None):
            super().__init__(items)
            self._next = nxt

        def has_next_page(self):
            return self._next is not None

        def get_next_page(self):
            return self._next

    llm = LLMClient(
        api_key="test", base_url=None, timeout=1.0, default_model="m",
        client_factory=lambda **kwargs: SimpleNamespace(
            models=SimpleNamespace(list=lambda: _Page(first, _Page(second)))),
    )
    assert llm.list_models() == ["a", "b", "c"]


def test_list_models_page_failure_keeps_received():
    """翻页失败不丢已收到的部分：用第一页结果返回。"""
    first = [SimpleNamespace(id="a")]

    class _BrokenPage(list):
        def has_next_page(self):
            return True

        def get_next_page(self):
            raise RuntimeError("翻页失败")

    llm = LLMClient(
        api_key="test", base_url=None, timeout=1.0, default_model="m",
        client_factory=lambda **kwargs: SimpleNamespace(
            models=SimpleNamespace(list=lambda: _BrokenPage(first))),
    )
    assert llm.list_models() == ["a"]
