"""TUI 纯函数工具：markdown 渲染、git 分支读取、token 缩写。

零 Textual 依赖，与界面组件解耦；测试可以直接调用验证输出。
"""
from __future__ import annotations

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
