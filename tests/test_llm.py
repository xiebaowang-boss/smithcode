"""LLM 客户端测试：`GET /models` 拉取的容错与去重；流中断线自动重试。"""

from types import SimpleNamespace

import httpx2
import pytest

import smithcode.llm.client as client_mod
from smithcode.llm import LLMClient

DROP = "peer closed connection without sending complete message body (incomplete chunked read)"


class _FakeModels:
    def __init__(self, items=None, error=None):
        self._items = items or []
        self._error = error

    def list(self):
        if self._error:
            raise self._error
        return self._items


def _client(items=None, error=None):
    # 跳过 __init__：不校验 key、不建真实连接，只替换 client.models
    client = object.__new__(LLMClient)
    client.client = SimpleNamespace(models=_FakeModels(items, error))
    return client


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


def _streaming_client(stream_once):
    """绕过 __init__ 的流式客户端：只替换 _stream_once，固定请求头为空。"""
    llm = object.__new__(LLMClient)
    llm._custom_headers = {}
    llm._stream_once = stream_once
    return llm


def _patch_retry(monkeypatch, warnings: list[str], retries: int = 2):
    monkeypatch.setattr(client_mod.config, "MAX_RETRIES", retries)
    monkeypatch.setattr(client_mod.time, "sleep", lambda _: None)  # 不真的退避等待
    monkeypatch.setattr(
        "smithcode.renderer.current",
        lambda: SimpleNamespace(warn=warnings.append),
    )


def test_chat_stream_retries_incomplete_stream(monkeypatch):
    """思考中流被掐断：自动重试，重试前把错误打印出来。"""
    calls = []

    def fake_stream(kwargs):
        calls.append(1)
        if len(calls) == 1:
            yield ("reasoning", "先想一半…")
            raise httpx2.RemoteProtocolError(DROP)  # 已输出的思考不阻断重试
        yield ("content", "答案")
        yield ("message", {"role": "assistant", "content": "答案"})

    warnings: list[str] = []
    _patch_retry(monkeypatch, warnings)

    events = list(_streaming_client(fake_stream).chat_stream(
        [{"role": "user", "content": "问题"}]
    ))

    assert len(calls) == 2  # 首次失败 + 一次重试成功
    assert [kind for kind, _ in events] == ["reasoning", "content", "message"]
    assert len(warnings) == 1
    assert "RemoteProtocolError" in warnings[0] and "重试" in warnings[0]


def test_chat_stream_no_retry_after_content(monkeypatch):
    """已输出正文后断流：不重试（重放会重复打印），错误照常抛出。"""
    calls = []

    def fake_stream(kwargs):
        calls.append(1)
        yield ("content", "半句")
        raise httpx2.RemoteProtocolError(DROP)

    warnings: list[str] = []
    _patch_retry(monkeypatch, warnings, retries=3)

    with pytest.raises(httpx2.RemoteProtocolError):
        list(_streaming_client(fake_stream).chat_stream(
            [{"role": "user", "content": "问题"}]
        ))

    assert len(calls) == 1
    assert warnings == []


def test_chat_stream_raises_after_retries_exhausted(monkeypatch):
    """重试次数用尽仍失败：抛原错误，每次重试前的错误提示都已打印。"""
    calls = []

    def fake_stream(kwargs):
        calls.append(1)
        yield from ()  # 保持生成器语义
        raise httpx2.RemoteProtocolError(DROP)

    warnings: list[str] = []
    _patch_retry(monkeypatch, warnings)

    with pytest.raises(httpx2.RemoteProtocolError):
        list(_streaming_client(fake_stream).chat_stream(
            [{"role": "user", "content": "问题"}]
        ))

    assert len(calls) == 3  # 初次 + 2 次重试
    assert len(warnings) == 2
