"""LLM 自定义请求头测试：配置的 [provider.headers] 经 extra_headers 注入，
{$session} 占位符按当前会话 id 替换；不配置则完全不发送。不依赖真实 API。"""

from smithcode import config
from smithcode.llm import LLMClient


def _make_fake_llm(monkeypatch, headers):
    """用假 OpenAI 客户端替换 SDK，捕获每次 create 的 kwargs。返回捕获字典。"""
    monkeypatch.setattr(config, "KEY", "sk-test")
    monkeypatch.delenv("SMITHCODE_KEY", raising=False)
    monkeypatch.setattr(config, "load_provider_headers", lambda: headers)
    monkeypatch.setattr(config, "SESSION_ID", "sess-0001")

    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured["kwargs"] = kwargs
            return iter([])  # 空流：无增量事件，只收尾 yield message

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = FakeChat()

    monkeypatch.setattr("smithcode.llm.OpenAI", FakeClient)
    return captured


def _drain(client):
    """消费完一次流式请求。"""
    list(client.chat_stream([{"role": "user", "content": "hi"}]))


def test_session_placeholder_resolved_per_request(monkeypatch):
    """{$session} 每次请求都替换为当前 config.SESSION_ID（构造后 /new 也能跟上）。"""
    captured = _make_fake_llm(
        monkeypatch, {"x-opencode-session": "{$session}", "x-fixed": "v1"}
    )
    client = LLMClient()

    _drain(client)
    assert captured["kwargs"]["extra_headers"] == {
        "x-opencode-session": "sess-0001",
        "x-fixed": "v1",
    }

    # 会话轮换后下一次请求携带新 id（占位符现取现替换，而非构造时固化）
    monkeypatch.setattr(config, "SESSION_ID", "sess-0002")
    _drain(client)
    assert captured["kwargs"]["extra_headers"]["x-opencode-session"] == "sess-0002"


def test_no_headers_means_no_extra_headers(monkeypatch):
    """未配置 [provider.headers] 时请求不带 extra_headers，对老用户零影响。"""
    captured = _make_fake_llm(monkeypatch, {})
    client = LLMClient()
    _drain(client)
    assert "extra_headers" not in captured["kwargs"]
