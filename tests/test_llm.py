"""LLM 客户端测试：`GET /models` 拉取的容错与去重。"""

from types import SimpleNamespace

from smithcode.llm import LLMClient


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
