"""glob 工具：按通配符模式搜索工作区内的文件名（本地检索）。

在真实代码库里靠 list_dir + read_file 盲目翻找效率极低，
glob 与 grep 是 Agent 定位代码的主要手段。
与 grep 共用 _shared_local 基座（SKIP_DIRS、沙箱根判定、有序遍历）。
"""
from __future__ import annotations

import fnmatch
import stat
from pathlib import Path

from .. import config
from . import _shared_local as ls
from .base import register


def _split_pattern(pattern: str) -> list[str]:
    """通配符按段切分：兼容 Windows 反斜杠，容忍首尾的 ./ 与 /。"""
    s = pattern.replace("\\", "/").strip()
    while s.startswith("./"):
        s = s[2:]
    if len(s) > 1 and s.endswith("/"):
        s = s.rstrip("/")
    if not s or s == ".":
        return []
    return s.split("/")


def _match_parts(path: list[str], pat: list[str]) -> bool:
    """按段匹配：** 匹配零个或多个段，其余段走 fnmatch（与 Path.glob 的 ** 语义对齐）。"""
    if not pat:
        return not path
    if pat[0] == "**":
        if _match_parts(path, pat[1:]):  # ** 匹配零个段
            return True
        return bool(path) and _match_parts(path[1:], pat)
    if not path:
        return False
    if not fnmatch.fnmatch(path[0], pat[0]):
        return False
    return _match_parts(path[1:], pat[1:])


def _glob_match(rel_posix: str, pattern: str) -> bool:
    """相对路径是否命中通配符（lexical 路径，不 resolve）。"""
    if rel_posix == ".":
        return pattern.strip() in (".", "./", "**", "**/")
    return _match_parts(rel_posix.split("/"), _split_pattern(pattern))


def _describe_glob(args: dict) -> str:
    path = args.get("path")
    suffix = "" if not path or path == "." else f" {path}"
    return f"glob {args.get('pattern', '?')}{suffix}"


@register(
    {
        "name": "glob",
        "pattern_arg": "path",
        "describe": _describe_glob,
        "description": "按通配符模式搜索工作区内的文件，支持 ** 递归，"
        "返回相对路径列表（按修改时间新→旧排序，最近改动的文件排前面）。"
        "示例：**/*.py、docs/**/*.md",
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
    _, base = ls.roots(path)
    if not pattern:
        return f"错误: 无效的通配符模式 {pattern!r}: pattern 不能为空"
    if base.is_file() or not base.is_dir():
        return "(无匹配文件)"
    roots = config.read_roots()

    matched: list[tuple[float, str]] = []  # (mtime, 展示路径)：只对命中项 stat
    truncated = False
    scan_capped = False
    scanned = 0

    def _collect(lex: Path) -> None:
        """单项校验 + 收集（lexical 匹配 → resolve 沙箱 → 命中集）。"""
        try:
            rp = lex.resolve()
        except OSError:
            return
        root = ls.containing_root(rp, roots)
        if root is None:
            return
        rel_path = rp.relative_to(root)
        if ls.SKIP_DIRS & set(rel_path.parts):
            return
        try:
            st = rp.stat()
        except OSError:
            return
        display = rel_path.as_posix() + ("/" if stat.S_ISDIR(st.st_mode) else "")
        matched.append((st.st_mtime, display))

    # pattern 直指起始目录自身（如 "**"）：把 base 本体也纳入候选（与 Path.glob 对齐）
    if _glob_match(".", pattern):
        _collect(base)

    for lex in ls.walk_entries(base, yield_dirs=True):
        scanned += 1
        if scanned > ls.MAX_SCAN_FILES:
            scan_capped = True
            truncated = True
            break
        try:
            rel_lex = lex.relative_to(base).as_posix()
        except ValueError:
            continue
        if not _glob_match(rel_lex, pattern):
            continue
        _collect(lex)
        if len(matched) >= ls.MAX_RESULTS:
            truncated = True
            break

    if not matched:
        if scan_capped:
            return f"(已扫描 {scanned} 项，达到扫描上限，请用 path 或更具体的 pattern 收窄范围)"
        return "(无匹配文件)"
    matched.sort(key=lambda item: item[0], reverse=True)  # 稳定排序：mtime 相同保持遍历序
    out = [display for _, display in matched]
    if scan_capped:
        note = f"\n(已扫描 {scanned} 项，达到扫描上限，请用 path 或更具体的 pattern 收窄范围)"
    else:
        note = f"\n(已达 {ls.MAX_RESULTS} 条上限，请收窄 pattern)" if truncated else ""
    return "\n".join(out) + note
