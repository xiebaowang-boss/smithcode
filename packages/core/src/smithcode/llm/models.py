"""候选模型目录：候选模型的唯一来源与启动期装载。

设计（面向对象、来源可替换）：
- `ModelSource` 抽象一种候选来源（外部配置 / 磁盘缓存 / 远端接口）；
- `ModelCatalog` 按优先级组合来源，线程安全地维护当前列表；
- 装载策略：显式配置优先（存在即不联网）；否则先读磁盘缓存立即可用，
  再由 `Agent.start()` 触发后台拉取远端 `/models` 并回写缓存。

命令层只依赖 `Agent.models.list()`，不关心来源与装载时机。
"""
from __future__ import annotations

import json
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

from .. import config


class ModelSource(ABC):
    """候选模型来源：返回列表表示命中，None 表示无可用数据（交给下一来源）。"""

    @abstractmethod
    def load(self) -> list[str] | None:
        raise NotImplementedError


class ConfiguredModelSource(ModelSource):
    """外部配置来源：config.toml 的 [provider].models。"""

    def load(self) -> list[str] | None:
        return config.read_configured_models()


class ModelCache:
    """`~/.smithcode/models.json` 磁盘缓存：按接口地址存储上次拉取的模型列表。

    接口地址不匹配（换了 provider）视为无缓存，避免串用其他服务的模型名。
    读写失败一律静默降级——缓存只是优化，绝不影响对话。
    """

    def __init__(self, path: Path | None = None):
        self._path = path or config.models_cache_path()

    def read(self) -> list[str] | None:
        if not self._path.is_file():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or data.get("url") != config.URL:
            return None
        models = data.get("models")
        if not isinstance(models, list):
            return None
        cleaned = [m for m in models if isinstance(m, str) and m]
        return cleaned or None

    def write(self, models: list[str]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"url": config.URL, "models": list(models)}
            self._path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass  # 写失败下次启动再试，不影响本次会话


class CachedModelSource(ModelSource):
    """磁盘缓存来源。"""

    def __init__(self, cache: ModelCache):
        self._cache = cache

    def load(self) -> list[str] | None:
        return self._cache.read()


class RemoteModelSource(ModelSource):
    """远端来源：OpenAI 兼容 `GET /models`；成功后回写缓存，失败返回 None。

    `list_models` 为空（测试替身等无该能力的 LLM）时恒返回 None——目录退化为
    "当前模型兜底"，不影响对话。
    """

    def __init__(self, list_models: Callable[[], list[str] | None] | None,
                 cache: ModelCache):
        self._list_models = list_models
        self._cache = cache

    def load(self) -> list[str] | None:
        if not callable(self._list_models):
            return None
        try:
            models = self._list_models()
        except Exception:  # noqa: BLE001 网络/接口异常一律视为无数据
            return None
        if not models:
            return None
        self._cache.write(models)
        return models


class ModelCatalog:
    """线程安全的候选模型目录。

    装载优先级：显式配置 > 磁盘缓存 > 远端刷新 > 当前模型兜底。
    - `bootstrap()`：同步装载配置/缓存；配置缺失时按需后台刷新远端（幂等）。
    - `refresh()`：同步拉取远端并更新，返回是否有更新（后台线程 / 测试用）。
    - `list()`：当前候选，始终包含当前模型。
    """

    def __init__(self, configured: ModelSource, cached: ModelSource,
                 remote: ModelSource, current_model: Callable[[], str]):
        self._configured = configured
        self._cached = cached
        self._remote = remote
        self._current_model = current_model
        self._models: list[str] = []
        self._lock = threading.RLock()
        self._bootstrapped = False

    def bootstrap(self, *, refresh_remote: bool = True) -> None:
        """启动期同步装载；外部未配置时后台刷新远端（幂等）。"""
        with self._lock:
            if self._bootstrapped:
                return
            self._bootstrapped = True

        configured = self._configured.load()
        if configured:
            self._set(configured)
            return  # 外部已配置：不再联网

        cached = self._cached.load()
        if cached:
            self._set(cached)

        if refresh_remote:
            threading.Thread(
                target=self.refresh, name="model-catalog-refresh", daemon=True
            ).start()

    def refresh(self) -> bool:
        """同步拉取远端并更新；返回是否有更新。"""
        models = self._remote.load()
        if not models:
            return False
        self._set(models)
        return True

    def list(self) -> list[str]:
        """当前候选模型；始终包含当前模型（兜底）。"""
        with self._lock:
            models = list(self._models)
        current = self._current_model()
        if current and current not in models:
            models.append(current)
        return models

    def _set(self, models: list[str]) -> None:
        with self._lock:
            self._models = list(models)


# 思考强度（reasoning_effort）候选档位：本地默认维护，不从远端获取。
# 取值即 OpenAI 官方 `reasoning.effort` 的完整支持范围（模型相关，
# 也覆盖 Codex / Claude Code 的档位语义）；服务商不支持某档位时换用其他档即可。
DEFAULT_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
