"""检索工具：文件名通配匹配（glob）与内容正则搜索（grep）。

在真实代码库里靠 list_dir + read_file 盲目翻找效率极低，
这两个工具是 Agent 定位代码的主要手段。
"""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from .. import config
from .base import register

# 检索时跳过的目录：依赖、缓存、版本控制等对定位代码没有价值
SKIP_DIRS = {
    ".git", ".idea", ".vscode", ".pytest_cache", ".ruff_cache",
    "__pycache__", "node_modules", ".venv", "venv", "dist", "build",
    "sessions",
}
MAX_RESULTS = 100  # 单次最多返回的文件数 / 匹配行数
MAX_FILE_SIZE = 1_000_000  # 超过 1MB 的文件跳过（多为构建产物或数据文件）
MAX_LINE_LEN = 200  # 单行匹配内容展示的最大长度


def _roots(path: str) -> tuple:
    """解析搜索起始路径，返回 (命中的授权根, 起始路径)。越界直接拒绝。

    相对路径锚定主工作区；结果落在任一授权目录内即放行，展示路径相对该根。
    """
    base = (Path(config.WORKSPACE_ROOT) / path).resolve()
    for root in config.allowed_roots():
        if base.is_relative_to(root):
            return root, base
    raise PermissionError(f"路径越界: {path}")


@register(
    {
        "name": "glob",
        "pattern_arg": "path",
        "description": "按通配符模式搜索工作区内的文件，支持 ** 递归，"
        "返回相对路径列表。示例：**/*.py、docs/**/*.md",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "通配符模式"},
                "path": {
                    "type": "string",
                    "description": "搜索起始目录，默认工作区根",
                },
            },
            "required": ["pattern"],
        },
    }
)
def glob(pattern: str, path: str = ".") -> str:
    root, base = _roots(path)
    try:
        found = sorted(base.glob(pattern))
    except ValueError as e:
        return f"错误: 无效的通配符模式 {pattern!r}: {e}"

    out = []
    for p in found:
        rp = p.resolve()
        if not rp.is_relative_to(root):
            continue
        rel = rp.relative_to(root)
        if SKIP_DIRS & set(rel.parts):
            continue
        out.append(rel.as_posix() + ("/" if rp.is_dir() else ""))
        if len(out) >= MAX_RESULTS:
            break
    if not out:
        return "(无匹配文件)"
    note = f"\n(已达 {MAX_RESULTS} 条上限，请收窄 pattern)" if len(out) >= MAX_RESULTS else ""
    return "\n".join(out) + note


@register(
    {
        "name": "grep",
        "pattern_arg": "path",
        "description": "在工作区文件内容中按正则表达式搜索，"
        "返回「路径:行号: 内容」列表。可用 include 按文件名过滤（如 *.py）。",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式"},
                "path": {
                    "type": "string",
                    "description": "搜索起始目录或单个文件，默认工作区根",
                },
                "include": {
                    "type": "string",
                    "description": "只搜索文件名匹配此通配符的文件，如 *.py",
                },
            },
            "required": ["pattern"],
        },
    }
)
def grep(pattern: str, path: str = ".", include: str | None = None) -> str:
    root, base = _roots(path)
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"错误: 无效的正则表达式: {e}"

    candidates = iter([base]) if base.is_file() else _iter_files(base)

    matches = []
    for fpath in candidates:
        if include and not fnmatch.fnmatch(fpath.name, include):
            continue
        try:
            if fpath.stat().st_size > MAX_FILE_SIZE:
                continue
            # errors="replace"：GBK 等非 UTF-8 文件也能搜到 ASCII 内容
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "\x00" in text:  # 含空字节，视为二进制文件
            continue
        rel = fpath.relative_to(root).as_posix()
        for lineno, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                matches.append(f"{rel}:{lineno}: {line.strip()[:MAX_LINE_LEN]}")
                if len(matches) >= MAX_RESULTS:
                    return (
                        "\n".join(matches)
                        + f"\n(已达 {MAX_RESULTS} 条上限，请收窄 pattern 或加 include)"
                    )
    return "\n".join(matches) if matches else "(无匹配)"


def _iter_files(base: Path):
    """遍历目录下的所有文件（修剪无关目录）。"""
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            yield Path(dirpath) / name
