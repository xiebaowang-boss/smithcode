"""Token 用量统计：按"应用启动以来"与"当前会话"两个口径累计，纯内存。

各服务商返回的 usage 字段并不统一：细节项可能缺失、为 null，
或嵌套在 *_details / 平铺顶层等不同位置（如 DeepSeek 的缓存字段）。
这里对已知字段做"取不到就按 0"的容错读取，统计过程绝不抛异常、
绝不影响对话主流程。
"""
from __future__ import annotations

# 累计字段 -> 在 usage dict 中的取值路径。输入/输出侧同名细节字段加前缀区分。
NUMERIC_FIELDS: dict[str, tuple[str, ...]] = {
    "prompt_tokens": ("prompt_tokens",),
    "completion_tokens": ("completion_tokens",),
    "total_tokens": ("total_tokens",),
    # 提示词侧细节
    "cached_tokens": ("prompt_tokens_details", "cached_tokens"),
    "cache_write_tokens": ("prompt_tokens_details", "cache_write_tokens"),
    "prompt_audio_tokens": ("prompt_tokens_details", "audio_tokens"),
    # DeepSeek 平铺的缓存字段（与 cached_tokens 语义相近，各自累计互不干扰）
    "prompt_cache_hit_tokens": ("prompt_cache_hit_tokens",),
    "prompt_cache_miss_tokens": ("prompt_cache_miss_tokens",),
    # 输出侧细节
    "reasoning_tokens": ("completion_tokens_details", "reasoning_tokens"),
    "completion_audio_tokens": ("completion_tokens_details", "audio_tokens"),
    "accepted_prediction_tokens": (
        "completion_tokens_details",
        "accepted_prediction_tokens",
    ),
    "rejected_prediction_tokens": (
        "completion_tokens_details",
        "rejected_prediction_tokens",
    ),
}

# 细节字段的展示名；summary 里只显示非零项
# （缓存命中已内联在 humanize 的输入之后，不在此重复展示）
DETAIL_LABELS: dict[str, str] = {
    "cache_write_tokens": "缓存写入",
    "prompt_audio_tokens": "音频输入",
    "prompt_cache_miss_tokens": "缓存未命中(DeepSeek)",
    "reasoning_tokens": "思维链",
    "completion_audio_tokens": "音频输出",
    "accepted_prediction_tokens": "预测采纳",
    "rejected_prediction_tokens": "预测作废",
}


def _read(usage: dict, path: tuple[str, ...]) -> int:
    """按路径容错取值：任一层缺失、值为 None 或非数值都按 0 处理。"""
    value = usage
    for key in path:
        if not isinstance(value, dict):
            return 0
        value = value.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def format_call(usage: dict) -> str:
    """格式化单次 LLM 调用的用量：输入(含缓存命中) / 输出 / 合计。

    逐次交互展示用：输入即该次请求携带的完整上下文，观察其随任务的增长轨迹。
    """
    acc = UsageAccumulator()
    acc.add(usage)
    return acc.humanize()


class UsageAccumulator:
    """单口径累计器：对每次调用的 usage 全量数值字段求和。"""

    def __init__(self):
        self.calls = 0
        self.totals: dict[str, int] = {field: 0 for field in NUMERIC_FIELDS}

    def add(self, usage: dict | None) -> None:
        """记一笔 LLM 调用的用量；usage 缺失或结构异常时静默跳过。"""
        if not isinstance(usage, dict):
            return
        self.calls += 1
        for field, path in NUMERIC_FIELDS.items():
            self.totals[field] += _read(usage, path)

    def get(self, field: str) -> int:
        return self.totals.get(field, 0)

    def cache_hit(self) -> int:
        """缓存命中的输入 token 数。

        嵌套的 prompt_tokens_details.cached_tokens 与 DeepSeek 平铺的
        prompt_cache_hit_tokens 语义相同，有的服务商只回其一，取大者去重。
        """
        return max(self.get("cached_tokens"), self.get("prompt_cache_hit_tokens"))

    def humanize(self) -> str:
        """一行摘要：输入(含缓存命中) / 输出 / 合计。"""
        if not self.calls:
            return "尚无调用"
        cache = self.cache_hit()
        cache_part = f" (缓存 {cache:,})" if cache else ""
        return (
            f"输入 {self.get('prompt_tokens'):,}{cache_part}"
            f" / 输出 {self.get('completion_tokens'):,}"
            f" / 合计 {self.get('total_tokens'):,}"
        )

    def detail_text(self) -> str:
        """非零细节字段拼成一句话；没有则返回空串。"""
        parts = [
            f"{DETAIL_LABELS[field]} {self.get(field):,}"
            for field in DETAIL_LABELS
            if self.get(field)
        ]
        return " · ".join(parts)

    def as_dict(self) -> dict:
        """序列化快照，留作以后的持久化接入点。"""
        return {"calls": self.calls, **self.totals}


class UsageTracker:
    """双口径账本：since_start 跨 /new 存活，current_session 随会话重置。"""

    def __init__(self):
        self.since_start = UsageAccumulator()
        self.current_session = UsageAccumulator()

    def add(self, usage: dict | None) -> None:
        """每次 LLM 调用成功后记一笔（usage 为 None 时两个口径都不动）。"""
        self.since_start.add(usage)
        self.current_session.add(usage)

    def reset_session(self) -> None:
        """/new 时只清会话口径，应用启动以来的累计保留。"""
        self.current_session = UsageAccumulator()

    def summary(self) -> str:
        """/usage 命令的展示文本：两个口径各一段 + 非零细节。"""

        def line(label: str, acc: UsageAccumulator) -> str:
            text = f"{label}: 共 {acc.calls} 次调用 | {acc.humanize()}"
            details = acc.detail_text()
            return f"{text}\n  其中 {details}" if details else text

        return "\n".join(
            [
                line("应用启动以来", self.since_start),
                line("当前会话", self.current_session),
            ]
        )
