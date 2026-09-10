from __future__ import annotations

import difflib
from pathlib import Path

from .. import config
from .base import register
from .search import SKIP_DIRS

MAX_READ_LINES = 2000  # read_file 单次最多返回的行数（可用 limit 调整）
MAX_READ_LINE_LEN = 2000  # 单行展示的最大长度（超长行截断，避免撑爆上下文）

# 本会话已读过/写过的文件（绝对路径）：write_file 覆盖与 edit_file 编辑前的强制校验依据
READ_FILES: set[str] = set()


def reset_read_tracking() -> None:
    """清空会话级「已读文件」记录（新会话/新 Agent 开始时调用）。"""
    READ_FILES.clear()


def _remember(path: Path) -> None:
    READ_FILES.add(str(path))


def _resolve(path: str) -> Path:
    # 相对路径锚定主工作区；解析结果（含绝对路径、.. 逃逸后）落在任一授权目录内即放行
    p = (Path(config.WORKSPACE_ROOT) / path).resolve()
    for root in config.allowed_roots():
        if p.is_relative_to(root):
            return p
    raise PermissionError(f"路径越界: {path}")


def _is_binary(p: Path) -> bool:
    try:
        with p.open("rb") as f:
            return b"\x00" in f.read(8192)
    except OSError:
        return False


def _unified(old_text: str, new_text: str, path: str) -> str:
    """两份文本的 unified diff（带 a/ b/ 前缀头，行尾不补换行符）。"""
    diff = difflib.unified_diff(
        old_text.splitlines(),
        new_text.splitlines(),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        lineterm="",
    )
    return "\n".join(diff)


def _protected_path(p: Path) -> bool:
    """预览不回显内容的保护路径：.env（密钥）与 .git 子树（config 里常含令牌）。"""
    return p.name == ".env" or ".git" in p.parts


def _read_preview_text(p: Path) -> str | None:
    """预览用读取：文件不存在返回 None 之外的空串语义由调用方处理；任何读取失败返回 None。"""
    try:
        return p.read_text(encoding="utf-8")
    except (PermissionError, OSError, UnicodeDecodeError):
        return None


def _preview_write(args: dict) -> str | None:
    """write_file 的权限确认预览：现有内容 vs 新内容（新文件为全增行）。

    任何读取失败（越界/二进制/编码异常）都返回 None，宁可没有预览也不影响
    确认流程；保护路径（.env / .git）不展示内容，避免密钥回显终端。
    """
    path = args.get("path")
    if not path:
        return None
    try:
        p = _resolve(path)
        if _protected_path(p):
            return None
        old = p.read_text(encoding="utf-8") if p.exists() else ""
    except (PermissionError, OSError, UnicodeDecodeError):
        return None
    new = str(args.get("content", ""))
    if old == new:
        return None
    return _unified(old, new, path)


def _preview_edit(args: dict) -> str | None:
    """edit_file 的权限确认预览：替换应用后的文件 vs 原文件。

    与工具本体同样的匹配规则：old_string 找不到或多处匹配且未开 replace_all
    时工具会报错——预览仍按"第一处替换"尽力展示，帮用户看清改动意图。
    """
    path = args.get("path")
    old_string = args.get("old_string")
    if not path or not old_string:
        return None
    try:
        p = _resolve(path)
        if not p.exists() or _protected_path(p):
            return None
        text = p.read_text(encoding="utf-8")
    except (PermissionError, OSError, UnicodeDecodeError):
        return None
    if text.count(old_string) == 0:
        return None
    new_string = str(args.get("new_string", ""))
    if args.get("replace_all"):
        new_text = text.replace(old_string, new_string)
    else:
        new_text = text.replace(old_string, new_string, 1)
    if new_text == text:
        return None
    return _unified(text, new_text, path)


@register(
    {
        "name": "read_file",
        "pattern_arg": "path",
        "describe": lambda args: f"read {args.get('path', '?')}",
        "description": "读取工作区内一个文本文件，返回带行号的内容（形如「行号  代码」）。"
        "大文件用 offset/limit 分段读取；二进制文件会被拒绝。"
        "行号前缀仅供定位，edit_file 的 old_string 不要把它复制进去。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对路径"},
                "offset": {
                    "type": "integer",
                    "description": "起始行号（从 1 开始），默认从第 1 行读；配合上一次读取的显示范围可续读",
                },
                "limit": {
                    "type": "integer",
                    "description": "本次最多读取的行数，默认 2000",
                },
            },
            "required": ["path"],
        },
    }
)
def read_file(path: str, offset: int | None = None, limit: int | None = None) -> str:
    p = _resolve(path)
    if not p.exists():
        return f"错误: 文件不存在: {path}"
    if p.is_dir():
        return f"错误: {path} 是目录，请用 list_dir 查看"
    if _is_binary(p):
        return f"错误: {path} 是二进制文件，无法以文本读取"
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        return f"错误: 文件不是有效的 UTF-8 文本: {e}"
    _remember(p)

    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        return "(空文件)"
    start = max(1, int(offset) if offset else 1)
    max_lines = max(1, int(limit) if limit else MAX_READ_LINES)
    selected = lines[start - 1 : start - 1 + max_lines]
    if not selected:
        return f"(第 {start} 行超出范围，文件共 {total} 行)"

    width = len(str(start + len(selected) - 1))
    out = [
        f"{start + i:>{width}}  {line[:MAX_READ_LINE_LEN]}"
        for i, line in enumerate(selected)
    ]
    result = "\n".join(out)
    end = start + len(selected) - 1
    if start > 1 or end < total:
        result += f"\n(显示第 {start}-{end} 行，共 {total} 行；后续内容用 offset 参数续读)"
    return result


@register(
    {
        "name": "write_file",
        "pattern_arg": "path",
        "display": "block",
        "serial": True,
        "describe": lambda args: f"write {args.get('path', '?')}",
        "preview": _preview_write,
        "description": "创建新文件或覆盖写入；覆盖已存在的文件前必须先用 read_file 读取（工具强制校验）。",
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
    if p.exists() and str(p) not in READ_FILES:
        return (
            f"错误: {path} 已存在且本会话未读取过，先 read_file 查看现有内容后再覆盖"
        )
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    _remember(p)
    return f"已写入 {p} ({len(content)} 字符)"


@register(
    {
        "name": "edit_file",
        "pattern_arg": "path",
        "display": "block",
        "serial": True,
        "describe": lambda args: f"edit {args.get('path', '?')}",
        "preview": _preview_edit,
        "description": "精确替换文件中的一段文本。old_string 必须与文件内容逐字符完全一致"
        "（从 read_file 输出复制，不含行号前缀），且本会话须先 read_file 过该文件。"
        "默认要求唯一匹配（多带几行上下文保证唯一），replace_all=true 时替换全部匹配。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {
                    "type": "boolean",
                    "description": "为 true 时替换全部匹配（默认 false，要求唯一匹配）",
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
    }
)
def edit_file(path: str, old_string: str, new_string: str,
              replace_all: bool = False) -> str:
    p = _resolve(path)
    if not p.exists():
        return f"错误: 文件不存在: {path}"
    if str(p) not in READ_FILES:
        return f"错误: {path} 本会话未读取过，先 read_file 查看内容后再编辑"
    if not old_string:
        return "错误: old_string 不能为空"
    text = p.read_text(encoding="utf-8")
    count = text.count(old_string)
    if count == 0:
        return ("错误: old_string 未找到，请先用 read_file 核对最新内容"
                "（注意不要把行号前缀复制进去）")
    if count > 1 and not replace_all:
        linenos = _match_linenos(text, old_string)
        shown = "、".join(f"第 {n} 行" for n in linenos[:5])
        more = f" 等 {count} 处" if count > 5 else ""
        return (f"错误: old_string 匹配了 {count} 处（{shown}{more}），"
                "补充更多上下文保证唯一，或用 replace_all=true 全部替换")
    new_text = text.replace(old_string, new_string)
    p.write_text(new_text, encoding="utf-8")
    _remember(p)
    return f"已编辑 {p}" + (f"（替换 {count} 处）" if count > 1 else "")


def _match_linenos(text: str, needle: str) -> list[int]:
    """返回 needle 在 text 中各次出现的行号（从 1 开始）。"""
    linenos = []
    idx = 0
    while True:
        i = text.find(needle, idx)
        if i < 0:
            return linenos
        linenos.append(text.count("\n", 0, i) + 1)
        idx = i + len(needle) or i + 1


@register(
    {
        "name": "list_dir",
        "describe": lambda args: f"ls {args.get('path', '.')}",
        "description": "列出目录内容（文件带大小标注，自动跳过 .git/.venv/node_modules 等无关目录）",
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
        if item.name in SKIP_DIRS:
            continue
        if item.is_dir():
            entries.append(f"[目录] {item.name}/")
            continue
        try:
            entries.append(f"[文件] {item.name} ({_fmt_size(item.stat().st_size)})")
        except OSError:
            entries.append(f"[文件] {item.name}")
    return "\n".join(entries) or "(空目录)"


def _fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"
