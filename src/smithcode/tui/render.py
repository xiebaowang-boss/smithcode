"""TUI 纯函数工具：markdown 渲染、git 分支读取、token 缩写。

零 Textual 依赖，与界面组件解耦；测试可以直接调用验证输出。
"""
from __future__ import annotations

import re
from itertools import zip_longest
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown as RichMarkdown
from rich.text import Text


def render_markdown(text: str, width: int) -> Text:
    """把 markdown 文本渲染成带样式的 Text（标题/粗体/代码块着色）。

    直接消费 rich 的 render_lines 段（pad=False，无整行填充），样式随段
    附加；宽度用组件实际宽度，由 rich 负责换行。
    """
    console = Console(width=width, force_terminal=True, color_system="standard")
    result = Text()
    for index, line in enumerate(console.render_lines(RichMarkdown(text), options=console.options, pad=False)):
        if index:
            result.append("\n")
        for seg in line:
            if seg.text:
                result.append(seg.text, style=seg.style or None)
    return result


def split_md_blocks(text: str):
    """把 markdown 文本切成（已完结块列表, 尾部未完结块）。

    按「块级边界」切分：段落以空行分隔；``` 围栏以开/闭状态机处理（围栏内
    的空行不切分，未闭合前整体留在尾部）。列表项之间不切（rich 对列表整体
    渲染更准确）。已完结块渲染一次即可缓存复用，尾部块在流式期间整块重渲染
    ——这是流式 markdown 丝滑的关键（Claude Code / opencode 式按块增量）。
    """
    lines = text.rstrip("\n").split("\n")  # 末尾换行不代表块完结（流式常见）
    done: list[str] = []
    fence = False  # 是否处于 ``` 围栏内
    start = 0  # 当前块起始行

    def emit(end: int) -> None:
        block = "\n".join(lines[start:end]).strip("\n")
        if block.strip():
            done.append(block)

    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            if fence:  # 围栏闭合：整块完结（含围栏本身）
                emit(i + 1)
                start = i + 1
            fence = not fence
        elif not fence and not line.strip():  # 空行 = 块边界
            emit(i)
            start = i + 1
    tail = "\n".join(lines[start:]).strip("\n")
    return done, tail


def git_branch(workspace: str) -> str | None:
    """当前工作区的 git 分支名；非 git 仓库或读取失败返回 None。

    直接读 .git/HEAD（"ref: refs/heads/main" → main），不调 git 命令，
    免依赖、速度快。子模块/worktree 的 .git 是指向实际 gitdir 的文本文件。
    """
    root = Path(workspace)
    git = root / ".git"
    try:
        if git.is_dir():
            head = git / "HEAD"
        elif git.is_file():
            gitdir = git.read_text(encoding="utf-8").strip()
            if not gitdir.startswith("gitdir:"):
                return None
            head = (root / gitdir[7:].strip() / "HEAD").resolve()
        else:
            return None
        text = head.read_text(encoding="utf-8").strip()
        if text.startswith("ref: "):
            return text[5:].split("/")[-1]
        return text[:7]  # detached HEAD：显示短提交号
    except OSError:
        return None


def human_tokens(n: int) -> str:
    """token 数的人性化缩写：980 → 980，12345 → 12.3K，234567 → 235K，1234567 → 1.2M。"""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        value = n / 1_000
        return f"{value:.0f}K" if value >= 100 else f"{value:.1f}K"
    return str(n)


def format_duration(secs: float) -> str:
    """时长的分级人性化格式，各级到点才出现：不足 1 分钟只显示秒（`42s`），
    不足 1 小时显示分+秒（`5m 30s`），再往上时+分+秒（`1h 12m 30s`）。

    运行中动画与轮次页脚共用，保证两处口径一致。
    """
    total = int(secs)
    if total < 60:
        return f"{total}s"
    minutes, seconds = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {seconds}s"


# 上下文收集类工具 → 汇总类别（opencode 式「已探索」）：读取 / 搜索。
# `list_dir` 归入「读取」（同属浏览项目结构）；不在表内的工具不参与分组
# （写文件、命令执行等有副作用，必须逐条可见）。
CONTEXT_TOOLS = {
    "read_file": "read",
    "list_dir": "read",
    "glob": "search",
    "grep": "search",
}

_CONTEXT_LABELS = {"read": "读取", "search": "搜索"}
_CONTEXT_ORDER = ("read", "search")


def context_category(name: str) -> str | None:
    """工具名 → 上下文类别（read / search / list）；非上下文工具返回 None。"""
    return CONTEXT_TOOLS.get(name)


def context_summary(counts: dict) -> str:
    """把各类别计数渲染成中文汇总（只列非零项），如「3 次读取，2 次搜索」。"""
    return "，".join(
        f"{counts[key]} 次{_CONTEXT_LABELS[key]}" for key in _CONTEXT_ORDER if counts.get(key)
    )


# ---------- IDEA 式左右对照 diff ----------

# 我们自己的 _unified 固定输出 `--- a/<path>` / `+++ b/<path>` 头，用 a/ 前缀
# 可靠识别文件头（删除行即使内容以 `--` 开头也不会误判成头）。
_OLD_FILE_RE = re.compile(r"^--- a/(.*)$")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# 整块 diff 的底色（与侧边栏/面板同色），让未着色行也有统一背景；
# 行号灰；增/删行用前景色 + 暗底整格高亮（左右各占半屏，近似 IDEA 的色块）
_BLOCK_BG = "#141414"
_GUTTER_STYLE = "#5c6370"
_NOTE_STYLE = "#808080"
_SEP_STYLE = "#3b4048"
_CELL_STYLES = {
    "del": "#f8c8cf on #35171e",
    "add": "#b9f0cf on #102a1d",
}
# 左右两栏的内容前缀：删除 `-`、新增 `+`、上下文留一个空格——等宽前缀保证
# 代码列始终对齐，同时让不依赖颜色的用户也能一眼看出增删。
_SIGNS = {"del": "-", "add": "+"}


def is_unified_diff(text: str) -> bool:
    """粗判一段文本是否是统一 diff（TUI 决定是否走左右对照渲染）。"""
    return "--- a/" in text and any(line.startswith("@@") for line in text.splitlines())


def _parse_unified(unified: str) -> list[tuple]:
    """把统一 diff 解析成带行号的行对。

    行对形如 `("line", old_no, old_kind, old_text, new_no, new_kind, new_text)`，
    kind ∈ {ctx, del, add, empty}，空侧的行号/文本为空；文件头为 `("file", path)`，
    无法识别的行（截断提示、无法预览说明等）原样透传为 `("text", line)`。
    """
    rows: list[tuple] = []
    old_no = new_no = 0
    lines = unified.splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        m = _OLD_FILE_RE.match(line)
        if m:
            rows.append(("file", m.group(1)))
            i += 1
            continue
        if line.startswith("+++ "):
            i += 1
            continue
        hm = _HUNK_RE.match(line)
        if hm:
            old_no, new_no = int(hm.group(1)), int(hm.group(2))
            i += 1
            continue
        if line.startswith("\\"):  # "\ No newline at end of file"
            i += 1
            continue
        if line.startswith("-"):
            dels = []
            while i < n and lines[i].startswith("-") and not _OLD_FILE_RE.match(lines[i]):
                dels.append(lines[i][1:])
                i += 1
            adds = []
            while i < n and lines[i].startswith("+"):
                adds.append(lines[i][1:])
                i += 1
            for d, a in zip_longest(dels, adds):
                left = (old_no, "del", d) if d is not None else (None, "empty", "")
                right = (new_no, "add", a) if a is not None else (None, "empty", "")
                rows.append(("line", *left, *right))
                if d is not None:
                    old_no += 1
                if a is not None:
                    new_no += 1
            continue
        if line.startswith("+"):
            while i < n and lines[i].startswith("+"):
                rows.append(("line", None, "empty", "", new_no, "add", lines[i][1:]))
                new_no += 1
                i += 1
            continue
        if line.startswith(" "):
            rows.append(("line", old_no, "ctx", line[1:], new_no, "ctx", line[1:]))
            old_no += 1
            new_no += 1
            i += 1
            continue
        rows.append(("text", line))
        i += 1
    return rows


def _gutter(no, width: int) -> Text:
    """行号格（含尾部空格）：右对齐定宽，空侧留白，铺整块底色。"""
    body = " " * width if no is None else f"{no:>{width}}"
    return Text(body + " ", style=f"{_GUTTER_STYLE} on {_BLOCK_BG}")


def _code_cell(content: str, width: int, kind: str) -> Text:
    """代码格：增/删行带 `+`/`-` 前缀，按显示宽度截断、补空格到整格，再整体着色。

    先补白再 `stylize`，背景铺满整格（未配对的留白侧也带底色）——这样左右
    两栏像 IDEA 一样整行贯通。制表符先展开，避免终端 tab 位错乱对齐。"""
    prefix = f"{_SIGNS.get(kind, ' ')} "
    cell = Text()
    clipped = Text(content.expandtabs(4))
    clipped.truncate(max(0, width - len(prefix)), overflow="ellipsis")
    cell.append(prefix)
    cell.append_text(clipped)
    pad = width - cell.cell_len
    if pad > 0:
        cell.append(" " * pad)
    cell.stylize(_CELL_STYLES.get(kind) or f"on {_BLOCK_BG}")
    return cell


def _block_row(content: str, style: str, width: int) -> Text:
    """铺满整块底色的普通行（省略提示等），右侧补齐到整宽。"""
    row = Text(content, style=f"{style} on {_BLOCK_BG}")
    pad = width - row.cell_len
    if pad > 0:
        row.append(" " * pad, style=f"{style} on {_BLOCK_BG}")
    return row


def _blank_row(width: int) -> Text:
    """整宽空白行（带整块底色）：diff 块上下各留 1 行 padding。"""
    return Text(" " * width, style=f"on {_BLOCK_BG}")


def side_by_side_diff(unified: str, width: int, max_rows: int | None = None) -> Text | None:
    """把统一 diff 渲染成 IDEA 式左右对照（左右行号 + 增删色块）。

    宽度不足以放下两栏时返回 None，由调用方回退到逐行统一 diff；`max_rows`
    非空时只展示前若干行并追加省略提示（对应折叠块的收起态）。
    """
    rows = [r for r in _parse_unified(unified) if r[0] != "file"]  # 块内不展示文件名
    if not rows:
        return None

    num_w = max((r[1] or 0 for r in rows if r[0] == "line"), default=0)
    for r in rows:
        if r[0] == "line":
            num_w = max(num_w, r[4] or 0)
    num_w = max(3, len(str(num_w)))

    # 固定开销：左行号+空格 + 分隔 + 右行号+空格；两栏代码均分剩余宽度
    fixed = (num_w + 1) * 2 + 3
    code_total = width - fixed
    if code_total < 10:
        return None
    left_w = code_total // 2
    right_w = code_total - left_w

    if max_rows is not None and len(rows) > max_rows:
        kept = []
        shown = 0
        for row in rows:
            if row[0] == "line":
                if shown >= max_rows:
                    break
                shown += 1
            kept.append(row)
        hidden = sum(1 for row in rows[len(kept):] if row[0] == "line")
        if hidden:
            kept.append(("text", f"…（+{hidden} 行，Enter / 点击展开）"))
        rows = kept

    text = Text()
    text.append_text(_blank_row(width))  # 上 padding
    for row in rows:
        text.append("\n")
        if row[0] == "text":
            text.append_text(_block_row(f"  {row[1]}", _NOTE_STYLE, width))
        else:
            _, l_no, l_kind, l_text, r_no, r_kind, r_text = row
            text.append_text(_gutter(l_no, num_w))
            text.append_text(_code_cell(l_text, left_w, l_kind))
            text.append(" │ ", style=f"{_SEP_STYLE} on {_BLOCK_BG}")
            text.append_text(_gutter(r_no, num_w))
            text.append_text(_code_cell(r_text, right_w, r_kind))
    text.append("\n")
    text.append_text(_blank_row(width))  # 下 padding
    return text
