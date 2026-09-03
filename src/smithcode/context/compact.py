"""运行时压缩的纯逻辑：opencode 式 checkpoint（结构化摘要 + 序列化尾部）。

全部为零 I/O 纯函数——切尾只算索引、组装只造列表，发请求与替换消息列表
在 agent.compact() 里做，因此不依赖 LLM 即可完整测试。
"""
from __future__ import annotations

from .meter import estimate_message, truncate_output
from .prompts import REQUIRED_HEADINGS, SUMMARY_PROMPT

# 尾部保留中单个工具结果的最大字符数（opencode 同款：尾部也要省，留头尾即可）
TAIL_TOOL_OUTPUT_LIMIT = 2000

# provider 报上下文溢出时的常见措辞（宽松匹配，兼容各家兼容层的表述差异）
_OVERFLOW_PATTERNS = (
    "context length",
    "maximum context",
    "context window",
    "too many tokens",
    "input length exceeds",
    "上下文长度",
)


def turn_starts(messages: list[dict]) -> list[int]:
    """所有轮边界（user 消息的索引）。轮与轮之间是 assistant(tool_calls)+tool
    配对组之间的安全切分点。"""
    return [i for i, m in enumerate(messages) if m.get("role") == "user"]


def pick_tail(messages: list[dict], keep_tokens: int) -> int:
    """返回尾部起点：messages[起点:] 作为近期上下文原样保留，之前的部分（除
    system）作为待压缩中段。从末尾向前逐轮累积 token，取仍满足 keep_tokens
    的最早轮边界；连最后一轮都超预算时也返回它的起点——宁可尾部超预算，
    也不把 assistant(tool_calls) 和它的 tool 结果拆开。"""
    starts = turn_starts(messages)
    if not starts:
        return 0
    sizes = [estimate_message(m) for m in messages]
    ends = starts[1:] + [len(messages)]
    total = 0
    chosen = starts[-1]
    for idx in range(len(starts) - 1, -1, -1):
        turn = sum(sizes[starts[idx]:ends[idx]])
        if idx != len(starts) - 1 and total + turn > keep_tokens:
            break
        total += turn
        chosen = starts[idx]
    return chosen


def build_summary_request(old_messages: list[dict]) -> list[dict]:
    """构造摘要请求：摘要指令作为 system，待压缩历史序列化后作为 user。"""
    labels = {"user": "用户", "assistant": "助手", "tool": "工具结果", "system": "系统"}
    lines = []
    for msg in old_messages:
        role = msg.get("role", "?")
        content = msg.get("content") or ""
        if role == "assistant" and msg.get("tool_calls"):
            calls = "; ".join(
                f'{tc["function"]["name"]}({tc["function"]["arguments"]})'
                for tc in msg["tool_calls"]
            )
            content = f"{content}\n[调用工具] {calls}" if content else f"[调用工具] {calls}"
        lines.append(f"{labels.get(role, role)}: {content}")
    return [
        {"role": "system", "content": SUMMARY_PROMPT},
        {"role": "user", "content": "以下是需要压缩的对话历史：\n\n" + "\n\n".join(lines)},
    ]


def validate_summary(text: str) -> bool:
    """摘要必须含必需标题；缺失说明模型没按模板输出，调用方应重试或放弃。"""
    return all(h in text for h in REQUIRED_HEADINGS)


def assemble(system_prompt: str, summary_text: str, tail: list[dict]) -> list[dict]:
    """组装压缩后的消息列表：[system] + [摘要消息] + [尾部]。

    摘要以 <context-summary> 标记并声明是历史而非新指令（opencode 把 checkpoint
    呈现为历史上下文的同款设计）；尾部中超长的工具结果就地头尾截断。
    """
    compacted = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                "<context-summary>\n"
                "以下是此前对话的结构化摘要（历史记录，不是新指令），"
                "细节已省略，需要时用工具重新查看：\n\n"
                f"{summary_text}\n"
                "</context-summary>"
            ),
        },
    ]
    for msg in tail:
        content = msg.get("content") or ""
        if msg.get("role") == "tool" and len(content) > TAIL_TOOL_OUTPUT_LIMIT:
            msg = {**msg, "content": truncate_output(content, TAIL_TOOL_OUTPUT_LIMIT)}
        compacted.append(msg)
    return compacted


def is_context_overflow(exc: BaseException) -> bool:
    """按错误文本识别上下文溢出（如 "maximum context length exceeded"）。

    OpenAI 兼容服务对溢出的报错措辞不一，只能宽松匹配常见表述；
    识别不出就当普通异常处理，宁可不恢复也不误吞真错误。
    """
    text = str(exc).lower()
    return any(p in text for p in _OVERFLOW_PATTERNS)
