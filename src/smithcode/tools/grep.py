"""grep 工具：在工作区文件内容中按正则表达式搜索（本地检索）。

与 glob 共用 _shared_local 基座（SKIP_DIRS、沙箱根判定、有序遍历、行截断）。
"""
from __future__ import annotations

import fnmatch
import re
import stat
from pathlib import Path

from .. import config, textfile
from . import _shared_local as ls
from .base import register

MAX_FILE_SIZE = 1_000_000  # 超过 1MB 的文件跳过（多为构建产物或数据文件）

OUTPUT_MODES = ("content", "files_with_matches", "count")


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
        "description": "在工作区文件内容中按正则表达式搜索（按单行匹配，不跨行）。"
        "默认返回「路径:行号: 内容」；output_mode=files_with_matches 只列包含匹配的文件，"
        "output_mode=count 返回「路径:匹配数」。ignore_case 忽略大小写，"
        "context=N 显示每个匹配的上下文 N 行。可用 include 按文件名过滤（如 *.py）。"
        "匹配内容按文件原文输出、保留行首缩进，去掉「路径:行号: 」前缀后可直接用作 "
        "edit_file 的 old_string（多行锚点每行缩进都要照原样）；超长行截断处有 … 标记，"
        "不可逐字复制。",
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
    if context is not None:
        try:
            width = int(context)
        except (TypeError, ValueError):
            return "错误: context 必须是非负整数"
        if width < 0:
            return "错误: context 必须是非负整数"
    else:
        width = 0
    _, base = ls.roots(path)
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        return f"错误: 无效的正则表达式: {e}"
    roots = config.read_roots()

    if base.is_file():
        candidates: object = [base]
    elif base.is_dir():
        candidates = ls.walk_entries(base, yield_dirs=False)
    else:
        return "(无匹配)"

    matches = []
    truncated = False
    scan_capped = False
    scanned_files = 0
    scanned_bytes = 0
    for fpath in candidates:  # type: ignore[union-attr]
        assert isinstance(fpath, Path)
        if include and not fnmatch.fnmatch(fpath.name, include):
            continue
        # 沙箱：walk 产出的是 lexical 路径，符号链接必须 resolve 后重判——
        # 工作区内的 link -> /etc/passwd 在此被拦下（此前缺这一步直接读出）。
        try:
            rp = fpath.resolve()
        except OSError:
            continue
        root = ls.containing_root(rp, roots)
        if root is None:
            continue
        rel_path = rp.relative_to(root)
        if ls.SKIP_DIRS & set(rel_path.parts):
            continue
        try:
            st = rp.stat()
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode):
            continue  # 链接到目录的按目录处理，内容检索只读文件
        if st.st_size > MAX_FILE_SIZE:
            continue
        scanned_files += 1
        scanned_bytes += st.st_size
        if scanned_files > ls.MAX_SCAN_FILES or scanned_bytes > ls.MAX_SCAN_BYTES:
            scan_capped = True
            truncated = True
            break
        try:
            with open(rp, "rb") as head:
                if b"\x00" in head.read(8192):
                    continue  # 二进制预检：命中空字节直接跳过，省下整文件解码
        except OSError:
            continue
        try:
            # errors="replace"：GBK 等非 UTF-8 文件也能搜到 ASCII 内容
            text, _fmt = textfile.read(rp, errors="replace")
        except (OSError, textfile.TextFileError):
            continue
        if "\x00" in text:  # 8KB 之后才出现的空字节，解码后二次确认
            continue
        rel = rel_path.as_posix()

        if output_mode == "files_with_matches":
            # 只需存在性：首个命中即停，不建全文件匹配表
            if any(rx.search(line) for line in text.splitlines()):
                matches.append(rel)
                if len(matches) >= ls.MAX_RESULTS:
                    truncated = True
                    break
            continue

        lines = text.splitlines()
        matched = [(i, line) for i, line in enumerate(lines) if rx.search(line)]
        if not matched:
            continue

        if output_mode == "count":
            matches.append(f"{rel}:{len(matched)}")
            if len(matches) >= ls.MAX_RESULTS:
                truncated = True
                break
        else:
            if width:
                matches.extend(_render_context(rel, lines, matched, width))
            else:
                matches.extend(f"{rel}:{i + 1}: {ls.clip_line(line)}"
                               for i, line in matched)
            if len(matches) >= ls.MAX_RESULTS:
                truncated = True
                break

    if not matches:
        return "(无匹配)"
    if truncated:
        matches = matches[:ls.MAX_RESULTS]
        if scan_capped:
            matches.append(
                f"(已扫描 {scanned_files} 个文件，达到扫描上限，请用 path 或 include 收窄范围)"
            )
        else:
            matches.append(f"(已达 {ls.MAX_RESULTS} 条上限，请收窄 pattern 或加 include)")
    return "\n".join(matches)


def _render_context(rel: str, lines: list[str], matched: list, width: int) -> list[str]:
    """渲染匹配行及其上下文窗口：匹配行用 : 分隔，上下文行用 -，组间以 -- 隔开。

    内容按文件原文输出（保留行首缩进）：Agent 会把其中内容复制成 edit_file 的
    old_string，去缩进会让多行锚点匹配失败。
    """
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
            out.append(f"{rel}{sep}{i + 1}{sep} {ls.clip_line(lines[i])}")
            if len(out) >= ls.MAX_RESULTS:
                return out
    return out
