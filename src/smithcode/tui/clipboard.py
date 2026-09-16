"""把文本写入系统剪贴板：优先调用平台工具，失败则退回 OSC 52。

Textual 的 `App.copy_to_clipboard` 只写 OSC 52 转义序列（`\\x1b]52;c;<base64>\\a`），
能不能进系统剪贴板完全取决于终端是否支持——VTE 系终端（GNOME Terminal /
Console / Tilix / xfce4-terminal，环境里带 `VTE_VERSION`）明确不支持该序列，
于是「选中 + ctrl+c」会静默失败：应用内部拿到了文本，系统剪贴板却没变。

这里改为优先调用系统剪贴板命令（`wl-copy` / `xclip` / `xsel` / `pbcopy` /
`clip`），它们直接写系统剪贴板、与终端能力无关；都没有或执行失败时返回
False，由调用方退回 Textual 的 OSC 52 实现。

文本一律经 stdin 传给命令（见 `process.run` 的 `input` 参数），不拼进命令行——
对话内容含引号、换行、`$()` 等，拼接即命令注入。
"""
from __future__ import annotations

import shutil
import sys

from ..process import run as run_process

# 写剪贴板是交互动作（ctrl+c 之后立刻生效），超时取小值：正常实现是 fork 到
# 后台持有剪贴板后立即返回，超过这个时间说明该工具在本环境不可用
_CLIPBOARD_TIMEOUT = 2.0

# 上次成功的候选（可执行名, 命令行），下次优先用它，避免每次都逐个探测
_preferred: tuple[str, str] | None = None


def _candidates() -> list[tuple[str, str]]:
    """按平台给出候选（可执行名, 命令行）；文本另行经 stdin 传入。"""
    if sys.platform == "win32":
        return [("clip", "clip")]
    if sys.platform == "darwin":
        return [("pbcopy", "pbcopy")]
    # Linux / BSD：Wayland 优先（GNOME / KDE 默认会话），再退回 X11
    return [
        ("wl-copy", "wl-copy"),
        ("xclip", "xclip -selection clipboard"),
        ("xsel", "xsel --clipboard --input"),
    ]


def copy_to_system(text: str) -> bool:
    """把文本写入系统剪贴板；无可用工具或全部失败时返回 False。

    候选里上次成功的会排在最前；某个候选返回非 0 会继续试下一个（例如
    `wl-copy` 存在但当前不是 Wayland 会话时必然失败）。
    """
    global _preferred
    ordered = [pair for pair in _candidates() if shutil.which(pair[0])]
    if _preferred in ordered:
        ordered.remove(_preferred)
        ordered.insert(0, _preferred)
    for pair in ordered:
        try:
            # capture_output=False：剪贴板工具会 fork 到后台持有剪贴板，子进程
            # 继承输出管道会让捕获模式一直等不到 EOF（见 process.run 文档）
            result = run_process(pair[1], timeout=_CLIPBOARD_TIMEOUT, input=text,
                                 capture_output=False)
        except OSError:
            continue
        if result.status == "ok" and result.returncode == 0:
            _preferred = pair
            return True
    return False


def reset_cache() -> None:
    """清掉「上次成功的工具」缓存（测试用）。"""
    global _preferred
    _preferred = None
