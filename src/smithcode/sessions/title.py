"""会话标题的纯逻辑：提示词、请求构造与结果清洗（不直接调用 LLM）。

自动标题在首轮任务正常结束后由 Agent 在后台线程触发（见 agent.py）；
本模块零 IO、零 LLM 依赖，可离线单测。参考实现：Claude Code 的
session title generator（小模型 + JSON 输出）与 opencode 的 title agent。
"""
from __future__ import annotations

import json

TITLE_PROMPT = (
    "你是会话标题生成器。根据用户的第一轮对话，生成一个简洁的会话标题："
    "概括用户要完成的任务，3-6 个词；使用与对话相同的语言；"
    "不要引号、不要标点结尾、不要任何解释或前缀。"
    '只输出 JSON：{"title": "标题"}。'
)

# 送入标题模型前每部分的截断长度（标题只需要开头信息）
MAX_PART_CHARS = 800


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return ""


def build_title_request(messages, max_chars: int = 60) -> list:
    """构造一次不带工具的标题补全请求：首轮 user + 助手正文截断。"""
    parts = []
    for message in messages:
        role = message.get("role")
        if role == "user":
            text = _text_of(message.get("content")).strip()
            if text:
                parts.append(f"用户: {text[:MAX_PART_CHARS]}")
        elif role == "assistant" and parts:
            text = _text_of(message.get("content")).strip()
            if text:
                parts.append(f"助手: {text[:MAX_PART_CHARS]}")
                break
    payload = "\n".join(parts) or "（空对话）"
    return [
        {"role": "system", "content": TITLE_PROMPT},
        {"role": "user", "content": f"对话开头：\n{payload}\n\n请生成标题。"},
    ]


def clean_title(text: str, max_chars: int = 60) -> str:
    """从模型输出提取合法标题：解析 JSON 的 title（容忍 ``` 围栏）。

    校验不过（非 JSON、超长、空串、错误句式）返回空串，由调用方保留
    fallback（首轮 prompt 截断），绝不把模型原话塞进标题。
    """
    if not isinstance(text, str):
        return ""
    raw = text.strip()
    if not raw:
        return ""
    if "```" in raw:
        for segment in raw.split("```"):
            candidate = _parse_json_title(segment.strip())
            if candidate:
                return _finalize(candidate, max_chars)
        return ""
    return _finalize(_parse_json_title(raw), max_chars)


def _parse_json_title(text: str) -> str:
    if not text:
        return ""
    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return ""
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return ""
    if not isinstance(data, dict):
        return ""
    title = data.get("title")
    return str(title).strip() if isinstance(title, str) else ""


def _finalize(title: str, max_chars: int) -> str:
    cleaned = " ".join(str(title or "").split())
    if not cleaned or len(cleaned) > max(1, int(max_chars)):
        return ""
    lowered = cleaned.lower()
    for rejected in ("错误", "无法", "抱歉", "对不起", "error", "sorry", "title:"):
        if lowered.startswith(rejected):
            return ""
    return cleaned


def should_generate(title: str, source: str) -> bool:
    """是否应自动生成：用户标题永不覆盖；自动标题只生成一次（有则跳过）。"""
    if source == "user":
        return False
    return not title
