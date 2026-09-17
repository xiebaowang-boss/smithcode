"""LLM 模型交互子系统：客户端、模型目录、用量、提示词。

公共 API 在此汇总，外部统一 `from smithcode.llm import ...`；各子模块如何
拆分是对外不可见的实现细节。

- client：OpenAI 兼容客户端（流式、自定义请求头、`/models` 拉取）
- request：请求值对象（`ChatRequest` + 纯函数 `build_kwargs`）
- stream：流式响应解析（`parse_stream`，纯函数、无 IO）
- retry：重试策略与状态机（分类 / 预算 / 退避 / 状态文案 + 流式执行的唯一权威）
- models：候选模型目录（显式配置 / 磁盘缓存 / 远端接口按优先级组合）
- usage：token 用量统计（解析服务商返回）
- prompts：系统提示词（Agent 行为规则）
"""
from .client import LLMClient
from .models import (
    DEFAULT_EFFORTS,
    CachedModelSource,
    ConfiguredModelSource,
    ModelCache,
    ModelCatalog,
    ModelSource,
    RemoteModelSource,
)
from .prompts import build_system_prompt
from .request import ChatRequest, build_kwargs
from .retry import RetryPolicy, RetryState, build_retry_state, stream_with_retry
from .stream import parse_stream, usage_to_dict
from .usage import UsageAccumulator, UsageTracker, format_call

__all__ = [
    "DEFAULT_EFFORTS",
    "CachedModelSource",
    "ChatRequest",
    "ConfiguredModelSource",
    "LLMClient",
    "ModelCache",
    "ModelCatalog",
    "ModelSource",
    "RemoteModelSource",
    "RetryPolicy",
    "RetryState",
    "UsageAccumulator",
    "UsageTracker",
    "build_kwargs",
    "build_retry_state",
    "build_system_prompt",
    "format_call",
    "parse_stream",
    "stream_with_retry",
    "usage_to_dict",
]
