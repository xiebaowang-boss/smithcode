"""上下文计量与运行时压缩。

meter：token 估算、分桶报告、真实锚点（"当前装了多少"的快照语义，
与 usage.py "累计花了多少"的流量账本相对）；
compact：opencode 式 checkpoint 压缩的纯逻辑（切尾、摘要请求、组装、溢出识别）；
prompts：压缩专用提示词。

公共 API 在此汇总，外部统一 `from smithcode.context import ...`，
包内文件如何拆分是对外不可见的实现细节。
"""

from .compact import (
    SUMMARY_PROMPT,
    assemble,
    build_summary_request,
    is_context_overflow,
    pick_tail,
    turn_starts,
    validate_summary,
)
from .meter import (
    ContextMeter,
    breakdown,
    estimate_message,
    estimate_text,
    report,
    total_tokens,
    truncate_output,
)

__all__ = [
    "SUMMARY_PROMPT",
    "ContextMeter",
    "assemble",
    "breakdown",
    "build_summary_request",
    "estimate_message",
    "estimate_text",
    "is_context_overflow",
    "pick_tail",
    "report",
    "total_tokens",
    "truncate_output",
    "turn_starts",
    "validate_summary",
]
