"""MCP 工具的命名、schema 转换与结果映射。

对外暴露的工具名采用 `mcp__<服务器>__<工具>`（对齐 Claude Code）：权限
规则可用通配符直接命中（`mcp__github__*`），且与静态工具名不会冲突。
非 `[A-Za-z0-9_-]` 字符替换为 `_`，超长截断，冲突由调用方补后缀。

结果映射把 MCP 的 content 数组 / structuredContent / isError 统一翻译成
给模型的纯文本（工具返回字符串的项目惯例），并在此处完成脱敏与超长截断。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .. import config
from ..context import truncate_output
from .secrets import Redactor

_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
MAX_EXPOSED_LEN = 64
MAX_DESCRIPTION_LEN = 1024  # 部分服务商对工具描述有长度限制，统一切齐


@dataclass
class ToolSpec:
    """一个已暴露的 MCP 工具：原服务端定义 + 本地唯一暴露名。"""

    server: str
    original: str
    exposed: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)
    annotations: dict = field(default_factory=dict)
    enabled: bool = True

    @property
    def read_only(self) -> bool:
        """服务端标注的只读提示（不可信，仅作展示与并行策略参考）。"""
        return bool(self.annotations.get("readOnlyHint"))

    def to_schema(self) -> dict:
        """转成发给 LLM 的 function-calling schema。"""
        description = self.description or f"MCP 工具 {self.original}（{self.server}）"
        prefix = f"[MCP:{self.server}] "
        if not description.startswith(prefix):
            description = prefix + description
        return {
            "name": self.exposed,
            "description": _clip(description, MAX_DESCRIPTION_LEN),
            "parameters": normalize_schema(self.input_schema),
        }


def sanitize(text: str) -> str:
    return _NAME_UNSAFE.sub("_", str(text))


def build_spec(server: str, tool: dict, taken: set) -> ToolSpec:
    """把一个服务端工具定义转成 ToolSpec；暴露名在 taken 内保证唯一。"""
    original = str(tool.get("name") or "").strip() or "tool"
    base = f"mcp__{sanitize(server)}__{sanitize(original)}"
    exposed = _clip(base, MAX_EXPOSED_LEN)
    suffix = 2
    while exposed in taken:
        tail = f"_{suffix}"
        exposed = _clip(base, MAX_EXPOSED_LEN - len(tail)) + tail
        suffix += 1
    taken.add(exposed)
    description = tool.get("description")
    annotations = tool.get("annotations")
    schema = tool.get("inputSchema")
    return ToolSpec(
        server=server,
        original=original,
        exposed=exposed,
        description=description if isinstance(description, str) else "",
        input_schema=schema if isinstance(schema, dict) else {},
        annotations=annotations if isinstance(annotations, dict) else {},
    )


def normalize_schema(schema: dict) -> dict:
    """把 MCP 的 inputSchema 规整成模型可用的 object schema。"""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    out = {key: value for key, value in schema.items() if key != "$schema"}
    out["type"] = "object"
    if not isinstance(out.get("properties"), dict):
        out["properties"] = {}
    return out


def format_result(result: dict, redactor: Redactor) -> str:
    """把 CallToolResult 翻成回传模型的文本：脱敏 + 超长截断。"""
    if not isinstance(result, dict):
        return redactor.scrub(str(result))
    parts = []
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            parts.append(_format_content_item(item))
    structured = result.get("structuredContent")
    if structured is not None:
        try:
            parts.append(json.dumps(structured, ensure_ascii=False))
        except (TypeError, ValueError):
            parts.append(str(structured))
    text = "\n".join(part for part in parts if part) or "（空结果）"
    if result.get("isError"):
        text = "错误: " + text
    text = redactor.scrub(text)
    return truncate_output(text, config.MAX_TOOL_OUTPUT)


def _format_content_item(item) -> str:
    if not isinstance(item, dict):
        return str(item)
    kind = item.get("type")
    if kind == "text":
        return str(item.get("text") or "")
    if kind == "image":
        return "[图片内容，当前终端不支持展示]"
    if kind == "audio":
        return "[音频内容，当前终端不支持展示]"
    if kind == "resource":
        resource = item.get("resource") if isinstance(item.get("resource"), dict) else {}
        uri = resource.get("uri") or resource.get("name") or "?"
        text = resource.get("text")
        return text if isinstance(text, str) else f"[资源 {uri}]"
    if kind == "resource_link":
        return f"[链接] {item.get('uri') or item.get('name') or '?'}"
    try:
        return json.dumps(item, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(item)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
