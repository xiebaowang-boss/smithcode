from pathlib import Path

from .. import config
from .base import register


def _resolve(path: str) -> Path:
    # 相对路径锚定主工作区；解析结果（含绝对路径、.. 逃逸后）落在任一授权目录内即放行
    p = (Path(config.WORKSPACE_ROOT) / path).resolve()
    for root in config.allowed_roots():
        if p.is_relative_to(root):
            return p
    raise PermissionError(f"路径越界: {path}")


@register(
    {
        "name": "read_file",
        "pattern_arg": "path",
        "describe": lambda args: f"read {args.get('path', '?')}",
        "description": "读取工作区内一个文本文件的内容",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对路径"},
            },
            "required": ["path"],
        },
    }
)
def read_file(path: str) -> str:
    p = _resolve(path)
    return p.read_text(encoding="utf-8")


@register(
    {
        "name": "write_file",
        "pattern_arg": "path",
        "describe": lambda args: f"write {args.get('path', '?')}",
        "description": "创建或覆盖写入文件",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对路径"},
                "content": {"type": "string", "description": "完整文件内容"},
            },
            "required": ["path", "content"],
        },
    }
)
def write_file(path: str, content: str) -> str:
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"已写入 {p} ({len(content)} 字符)"


@register(
    {
        "name": "edit_file",
        "pattern_arg": "path",
        "describe": lambda args: f"edit {args.get('path', '?')}",
        "description": "精确替换文件中的一段文本，old_string 必须唯一匹配",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    }
)
def edit_file(path: str, old_string: str, new_string: str) -> str:
    p = _resolve(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old_string)
    if count == 0:
        return "错误: old_string 未找到"
    if count > 1:
        return f"错误: old_string 匹配了 {count} 处，需要更多上下文"
    p.write_text(text.replace(old_string, new_string), encoding="utf-8")
    return f"已编辑 {p}"


@register(
    {
        "name": "list_dir",
        "describe": lambda args: f"ls {args.get('path', '.')}",
        "description": "列出目录内容",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "默认当前目录"},
            },
        },
    }
)
def list_dir(path: str = ".") -> str:
    p = _resolve(path)
    entries = []
    for item in sorted(p.iterdir()):
        tag = "[目录]" if item.is_dir() else "[文件]"
        entries.append(f"{tag} {item.name}")
    return "\n".join(entries) or "(空目录)"
