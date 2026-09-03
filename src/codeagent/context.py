"""上下文计量：估算 session.messages 的 token 占用，生成 /context 报告。

多服务商兼容层不值得为分词器引入依赖（各家 tokenizer 本就不统一），
用字符构成启发式估算；每次请求返回的真实 prompt_tokens 作为锚点校准，
报告里同时展示两者与偏差，估算失真时一眼可见。
纯读取、纯计算，绝不影响对话主流程。
"""
from __future__ import annotations

# 角色 -> 展示名。工具结果是将来微压缩的首要回收目标（通常占大头），单独标注。
_LABELS = {
    "system": "系统提示词",
    "user": "用户消息",
    "assistant": "助手回复",
    "tool": "工具结果",
}


def estimate_text(text: str) -> int:
    """按字符构成估算 token 数：CJK 约 1.5 字符/token，其余约 4 字符/token。"""
    if not text:
        return 0
    wide = sum(1 for ch in text if ord(ch) > 0x2E80)  # CJK 与全角区
    return int(wide / 1.5 + (len(text) - wide) / 4)


def estimate_message(msg: dict) -> int:
    """单条消息的估算：正文 + tool_calls 的函数名与参数 + 每条固定结构开销。"""
    size = estimate_text(msg.get("content") or "")
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        size += estimate_text(fn.get("name", ""))
        size += estimate_text(fn.get("arguments", ""))
    return size + 8  # 角色、tool_call_id 等结构开销的粗略常数


def breakdown(messages: list[dict]) -> dict[str, int]:
    """按角色分桶统计估算 token，供 /context 展示占用构成。"""
    buckets = dict.fromkeys(_LABELS, 0)
    for msg in messages:
        buckets[msg["role"]] += estimate_message(msg)
    return buckets


def total_tokens(messages: list[dict]) -> int:
    return sum(breakdown(messages).values())


def report(
    messages: list[dict],
    budget: int,
    trigger: float,
    actual: int | None = None,
) -> str:
    """/context 的展示文本：总占用、分桶构成、距压缩阈值的距离、真实锚点。"""
    buckets = breakdown(messages)
    total = sum(buckets.values())
    lines = [f"上下文占用（估算）: {total:,} / {budget:,} tokens ({total / budget:.0%})"]
    for role, label in _LABELS.items():
        if buckets[role]:
            hint = "  ← 旧工具结果是压缩的首要回收目标" if role == "tool" else ""
            lines.append(f"  {label}  {buckets[role]:,}{hint}")
    threshold = int(budget * trigger)
    gap = "已越过" if total >= threshold else f"还差 {threshold - total:,}"
    lines.append(f"压缩阈值: {threshold:,} ({trigger:.0%})，{gap}")
    if actual is not None:
        dev = (total - actual) / actual if actual else 0
        lines.append(f"上次请求实际 prompt_tokens: {actual:,}（估算偏差 {dev:+.1%}）")
    return "\n".join(lines)


class ContextMeter:
    """请求锚点与阈值提醒：记录真实的 prompt_tokens，临近压缩阈值时提醒一次。

    与 usage.py 的用量账本分工不同：账本是"累计花了多少"的流量语义，
    这里是"当前装了多少"的快照语义，故独立于 UsageTracker。
    """

    def __init__(self):
        self.last_actual: int | None = None
        self._warned = False

    def new_run(self) -> None:
        """新任务开始：阈值提醒重新武装。"""
        self._warned = False

    def record(self, usage: dict | None) -> None:
        """每次 LLM 调用成功后记一笔真实 prompt_tokens；缺失时静默跳过。"""
        if isinstance(usage, dict) and usage.get("prompt_tokens"):
            self.last_actual = int(usage["prompt_tokens"])

    def warn_once(self, messages: list[dict], budget: int, trigger: float) -> None:
        """估算越过压缩阈值的 90% 时提醒一次（每次任务最多一次）。

        提前 10% 说，是留给用户余量决定 /new 还是等自动压缩，而不是压线惊吓。
        """
        if self._warned:
            return
        total = total_tokens(messages)
        if total < budget * trigger * 0.9:
            return
        self._warned = True
        print(
            f"\n[context] 上下文估算约 {total:,} tokens，"
            f"接近压缩阈值（预算的 {trigger:.0%}），/context 可查看占用分布",
            flush=True,
        )
