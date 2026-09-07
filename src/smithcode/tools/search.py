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
OUTPUT_MODES = ("content", "files_with_matches", "count")


def _roots(path: str) -> tuple:
    """解析搜索起始路径，返回 (命中的授权根, 起始路径)。越界直接拒绝。

    相对路径锚定主工作区；结果落在任一授权目录内即放行，展示路径相对该根。
    """
    base = (Path(config.WORKSPACE_ROOT) / path).resolve()
    for root in config.allowed_roots():
        if base.is_relative_to(root):
            return root, base
    raise PermissionError(f"路径越界: {path}")


def _mtime(p: Path) -> float:
    """取修改时间；stat 失败（坏链接等）按最旧处理。"""
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


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
    root, base = _roots(path)
    try:
        found = sorted(base.glob(pattern), key=_mtime, reverse=True)
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


def _describe_grep(args: dict) -> str:
    parts = [f"grep {args.get('pattern', '?')}"]
    path = args.get("path")
    if path and path != ".":
        parts.append(str(path))
    if args.get("include"):
        parts.append(f"--include={args['include']}")
    if args.get("ignore_case"):
        parts.append("-i")
    if args.get("context"):
        parts.append(f"-C {args['context']}")
    mode = args.get("output_mode")
    if mode and mode != "content":
        parts.append(f"--mode={mode}")
    return " ".join(parts)


@register(
    {
        "name": "grep",
        "pattern_arg": "path",
        "describe": _describe_grep,
        "description": "在工作区文件内容中按正则表达式搜索。"
        "默认返回「路径:行号: 内容」；output_mode=files_with_matches 只列包含匹配的文件，"
        "output_mode=count 返回「路径:匹配数」。ignore_case 忽略大小写，"
        "context=N 显示每个匹配的上下文 N 行。可用 include 按文件名过滤（如 *.py）。",
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
                "ignore_case": {
                    "type": "boolean",
                    "description": "忽略大小写（默认区分）",
                },
                "output_mode": {
                    "type": "string",
                    "enum": list(OUTPUT_MODES),
                    "description": "content（默认，逐行结果）/ files_with_matches（只列文件）/ count（每文件匹配数）",
                },
                "context": {
                    "type": "integer",
                    "description": "每个匹配前后各显示 N 行上下文（仅 content 模式）",
                },
            },
            "required": ["pattern"],
        },
    }
)
def grep(pattern: str, path: str = ".", include: str | None = None,
         ignore_case: bool = False, output_mode: str = "content",
         context: int | None = None) -> str:
    if output_mode not in OUTPUT_MODES:
        return f"错误: output_mode 只支持 {' / '.join(OUTPUT_MODES)}"
    root, base = _roots(path)
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        return f"错误: 无效的正则表达式: {e}"

    candidates = iter([base]) if base.is_file() else _iter_files(base)

    matches = []
    truncated = False
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
        lines = text.splitlines()
        matched = [(i, line) for i, line in enumerate(lines) if rx.search(line)]
        if not matched:
            continue

        if output_mode == "files_with_matches":
            matches.append(rel)
            if len(matches) >= MAX_RESULTS:
                truncated = True
                break
        elif output_mode == "count":
            matches.append(f"{rel}:{len(matched)}")
            if len(matches) >= MAX_RESULTS:
                truncated = True
                break
        else:
            if context:
                matches.extend(_render_context(rel, lines, matched, max(0, int(context))))
            else:
                matches.extend(f"{rel}:{i + 1}: {line.strip()[:MAX_LINE_LEN]}"
                               for i, line in matched)
            if len(matches) >= MAX_RESULTS:
                truncated = True
                break

    if not matches:
        return "(无匹配)"
    if truncated:
        matches = matches[:MAX_RESULTS]
        matches.append(f"(已达 {MAX_RESULTS} 条上限，请收窄 pattern 或加 include)")
    return "\n".join(matches)


def _render_context(rel: str, lines: list[str], matched: list, width: int) -> list[str]:
    """渲染匹配行及其上下文窗口：匹配行用 : 分隔，上下文行用 -，组间以 -- 隔开。"""
    matched_idx = {i for i, _ in matched}
    ranges = []
    for i, _ in matched:
        start, end = max(0, i - width), min(len(lines), i + width + 1)
        if ranges and start <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))

    out = []
    for gi, (start, end) in enumerate(ranges):
        if gi > 0:
            out.append("--")
        for i in range(start, end):
            sep = ":" if i in matched_idx else "-"
            out.append(f"{rel}{sep}{i + 1}{sep} {lines[i].strip()[:MAX_LINE_LEN]}")
            if len(out) >= MAX_RESULTS:
                return out
    return out


def _iter_files(base: Path):
    """遍历目录下的所有文件（修剪无关目录）。"""
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            yield Path(dirpath) / name
