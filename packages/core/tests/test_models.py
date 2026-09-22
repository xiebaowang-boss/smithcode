"""模型目录测试：来源优先级、缓存读写、当前模型兜底、后台装载。"""

import threading

from smithcode import config
from smithcode.llm.models import (
    CachedModelSource,
    ModelCache,
    ModelCatalog,
    ModelSource,
)


class _Source(ModelSource):
    """可观测的假来源：记录调用次数，按需返回固定列表。"""

    def __init__(self, models=None):
        self.models = models
        self.calls = 0

    def load(self):
        self.calls += 1
        return self.models


def _catalog(configured=None, cached=None, remote=None, current="cur"):
    return ModelCatalog(
        configured=_Source(configured),
        cached=_Source(cached),
        remote=_Source(remote),
        current_model=lambda: current,
    )


# ---------- 来源优先级 ----------

def test_configured_wins_and_skips_remote():
    catalog = _catalog(configured=["a", "b"], cached=["c"], remote=["x"])
    catalog.bootstrap()  # 配置存在：不应联网
    assert catalog.list() == ["a", "b", "cur"]
    assert catalog._remote.calls == 0


def test_cache_used_when_unconfigured():
    catalog = _catalog(cached=["c", "d"], remote=["x"])
    catalog.bootstrap(refresh_remote=False)
    assert catalog.list() == ["c", "d", "cur"]


def test_remote_refresh_populates():
    catalog = _catalog(remote=["r1", "r2"])
    catalog.bootstrap(refresh_remote=False)
    assert catalog.list() == ["cur"]  # 尚未刷新：只有当前模型兜底
    assert catalog.refresh() is True
    assert catalog.list() == ["r1", "r2", "cur"]


def test_refresh_without_data_keeps_existing():
    catalog = _catalog(configured=["a"], remote=None)
    catalog.bootstrap(refresh_remote=False)
    assert catalog.refresh() is False
    assert catalog.list() == ["a", "cur"]


def test_list_always_includes_current():
    catalog = _catalog(configured=["a"])
    catalog.bootstrap(refresh_remote=False)
    assert catalog.list() == ["a", "cur"]


def test_bootstrap_is_idempotent():
    catalog = _catalog(configured=["a"])
    catalog.bootstrap(refresh_remote=False)
    catalog.bootstrap(refresh_remote=False)
    assert catalog._configured.calls == 1


def test_bootstrap_starts_background_refresh_when_unconfigured():
    class _SignalingSource(ModelSource):
        def __init__(self):
            self.done = threading.Event()

        def load(self):
            self.done.set()
            return ["r"]

    remote = _SignalingSource()
    catalog = ModelCatalog(
        configured=_Source(None),
        cached=_Source(None),
        remote=remote,
        current_model=lambda: "cur",
    )
    catalog.bootstrap()
    assert remote.done.wait(2)  # 后台线程已触发远端拉取
    assert catalog.list() == ["r", "cur"]


# ---------- 磁盘缓存 ----------

def test_model_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "URL", "http://example/v1")
    cache = ModelCache(tmp_path / "models.json")
    cache.write(["a", "b"])
    assert cache.read() == ["a", "b"]


def test_model_cache_url_mismatch_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "URL", "http://one/v1")
    cache = ModelCache(tmp_path / "models.json")
    cache.write(["a"])
    monkeypatch.setattr(config, "URL", "http://two/v1")
    assert cache.read() is None


def test_model_cache_broken_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "URL", "http://example/v1")
    path = tmp_path / "models.json"
    path.write_text("{ not json", encoding="utf-8")
    assert ModelCache(path).read() is None


def test_cached_source_delegates_to_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "URL", "http://example/v1")
    cache = ModelCache(tmp_path / "models.json")
    cache.write(["x"])
    assert CachedModelSource(cache).load() == ["x"]
