"""前端共用的文本化纯函数（不持有状态、不打印）。

放这里的判断依据：**分类**是所有前端共同需要的语义（如 diff 行是增/删/位置头），
**样式**则各前端不同（终端用 ANSI，TUI 用主题色）。所以分类在此外露，
样式只在终端前端使用。
"""

from __future__ import annotations

DIM = "\033[90m"  # 思考内容灰色（90m 比 dim/2m 在 Windows 终端上兼容性好）
RESET = "\033[0m"
GREEN = "\033[32m"  # diff 增行
RED = "\033[31m"  # diff 删行
CYAN = "\033[36m"  # diff 位置头（@@）


def diff_line_kind(line: str) -> str | None:
    """diff 行的语义分类：add 增 / del 删 / hunk 位置头 / head 文件头 / None 普通。"""
    if line.startswith(("+++", "---")):
        return "head"
    if line.startswith("+"):
        return "add"
    if line.startswith("-"):
        return "del"
    if line.startswith("@@"):
        return "hunk"
    return None


def diff_line_style(line: str) -> str | None:
    """diff 行的 ANSI 颜色（终端用）：+/++ 绿、-/-- 红、@@ 与文件头青。"""
    return {"add": GREEN, "del": RED, "hunk": CYAN, "head": CYAN}.get(diff_line_kind(line))


def is_number_list(text: str) -> bool:
    """形如 "1" / "1,3" / "1 3" 的编号串（终端选择题的编号解析用）。"""
    parts = text.replace(",", " ").split()
    return bool(parts) and all(part.isdigit() for part in parts)
