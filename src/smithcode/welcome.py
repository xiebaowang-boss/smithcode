"""启动欢迎语：ASCII Logo + 会话信息行 + 随机问候 + 小贴士。

供 TUI（Textual 界面）与 REPL（cli.repl）共用：TUI 用完整版（多行 Logo），
REPL 用紧凑版（单行 Logo）。问候语池与小贴士池各随机抽一条，让每次启动
的欢迎语都有变化；同时保证测试可用——`welcome_text()` 纯函数、随机源可注入。
"""
from __future__ import annotations

import datetime
import platform
import random

from rich.text import Text

from . import __version__, config
from .permission import MODE_LABELS, MODES

# TUI 用的多行 Logo（块状字）。行宽 40 列，终端 ≥ 52 列才显示完整版，
# 更窄时调用方降级为紧凑版；REPL 始终用紧凑版。
LOGO = """\
  ███████╗███╗   ███╗██╗████████╗██╗  ██╗
  ██╔════╝████╗ ████║██║╚══██╔══╝██║  ██║
  ███████╗██╔████╔██║██║   ██║   ███████║
  ╚════██║██║╚██╔╝██║██║   ██║   ██╔══██║
  ███████║██║ ╚═╝ ██║██║   ██║   ██║  ██║
  ╚══════╝╚═╝     ╚═╝╚═╝   ╚═╝   ╚═╝  ╚═╝
"""
LOGO_WIDTH = 42
"""Logo 最宽行的显示宽度（含行首缩进），供调用方判断终端是否放得下。"""

COMPACT = "Smith Code"
"""单行紧凑 Logo（REPL 与窄终端降级用）。"""

GREETS = [
    "铁匠铺已开张，炉火正旺。",
    "锤子已备好，需要打点什么？",
    "今天的代码，交给 Smith 打磨。",
    "淬火完成，随时可以开工。",
    "铁砧已就位，说吧，打什么？",
    "炉温正好，代码拿过来。",
]

TIPS = [
    "输入 /help 查看全部命令",
    "Shift+Tab 循环切换权限模式（Smith → Accept Edits → Auto）",
    "输入 / 唤出命令菜单，↑↓ 选择后回车执行",
    "Ctrl+O 切换计划侧边栏显示",
    "Ctrl+Q 退出",
]

_HOUR_GREETS = (
    ((5, 11), "早，工头。"),
    ((11, 14), "午安，工头。"),
    ((14, 22), "晚上好，工头。"),
    ((22, 29), "深夜了，工头。"),  # 22~24 与 0~5（+24 折算）都算深夜
)


def greet_by_hour(hour: int) -> str:
    """按时段返回问候前缀；hour 可注入固定值便于测试。"""
    for (lo, hi), text in _HOUR_GREETS:
        if lo <= hour % 24 < hi or (hi > 24 and hour + 24 < hi):
            return text
    return _HOUR_GREETS[0][1]


def _current_hour() -> int:
    return datetime.datetime.now().astimezone().hour  # 本地时区（避免 DTZ005）


def _info_line(mode: str) -> Text:
    """会话信息行：模型 · 权限模式 · 版本 · Python · git 分支（分支缺省省略）。"""
    line = Text("  ", style="#808080")
    line.append(config.MODEL, style="#7aa2f7")
    line.append(" · ", style="#808080")
    line.append(MODE_LABELS.get(mode, mode), style="#808080")
    line.append(f" · v{__version__}", style="#808080")
    line.append(f" · Python {platform.python_version()}", style="#808080")
    from .tui.render import git_branch

    branch = git_branch(config.WORKSPACE_ROOT)
    if branch:
        line.append(" · ", style="#808080")
        line.append(branch, style="#808080")
    return line


def _pick(rng: random.Random, seq: list[str]) -> str:
    return rng.choice(seq)


def banner(mode: str | None = None, compact: bool = False,
           rng: random.Random | None = None) -> Text:
    """欢迎语（Text 对象，内嵌配色）。

    mode：权限模式键（如 agent.permission.mode），缺省用默认模式；
    compact：紧凑版（单行 Logo，REPL 用）；TUI 用完整版（compact=False）。
    rng：随机源，测试可注入固定种子。
    """
    rng = rng or random.Random()
    parts: list[Text] = []

    if compact:
        logo = Text()
        logo.append(f"  {COMPACT}", style="bold #23d18b")
        logo.append(f"  v{__version__}", style="#808080")
    else:
        logo = Text(LOGO, style="#23d18b")
    parts.append(logo)

    parts.append(_info_line(mode or MODES[0]))

    greet = Text("  ")
    greet.append(greet_by_hour(_current_hour()), style="#e0af68")
    greet.append(" ", style="#808080")
    greet.append(_pick(rng, GREETS), style="#eeeeee")
    parts.append(greet)

    tip = Text("  ", style="#808080")
    tip.append("小贴士：", style="#808080")
    tip.append(_pick(rng, TIPS), style="#808080")
    parts.append(tip)

    result = Text()
    for i, part in enumerate(parts):
        if i:
            result.append("\n")
        result.append_text(part)
    return result


def welcome_text(mode: str | None = None, compact: bool = False,
                 seed: int | None = None) -> str:
    """纯文本版：去掉配色，便于测试与日志。"""
    rng = random.Random(seed)
    return str(banner(mode=mode, compact=compact, rng=rng))
