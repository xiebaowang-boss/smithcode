"""终端环境处理：统一控制台 UTF-8 输出与交互输入。

交互输入走 prompt_toolkit：多行粘贴自动合并为一条消息、历史记录、
中文按显示宽度编辑，原生解决 input() 内核行编辑的字节宽度问题。
Enter 发送消息，Ctrl+Enter 手动插入换行（长文本自动折行显示）。
非交互 stdin（管道 / CI）一律退回普通 input()，行为与原先一致。
"""
from __future__ import annotations

import io
import os
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.enums import DEFAULT_BUFFER
from prompt_toolkit.filters import has_focus
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings

from .. import config

_SESSION: PromptSession | None = None


class SlashCompleter(Completer):
    """斜杠命令补全：输入以 / 开头且光标仍在首个 token 内时给出命令候选，
    描述显示在补全菜单右侧（opencode 式）。普通消息文本不触发。"""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return
        # 延迟导入：`commands` 反过来经渲染器引用本模块，顶层导入成环
        # （utils.terminal → commands → renderer → utils.terminal），此前靠
        # "谁先被导入"的偶然顺序维持，换个入口就报 partially initialized module
        from .. import commands

        for cmd in commands.complete_commands(text[1:]):
            yield Completion(
                "/" + cmd.name,
                start_position=-len(text),
                display="/" + cmd.name,
                display_meta=cmd.description,
            )


def _bindings() -> KeyBindings:
    """自定义按键：Enter 发送（等同单行模式），Ctrl+Enter 插入换行。

    Windows 下 Alt+Enter 会被终端拦截（全屏切换），因此换行改用 Ctrl+Enter
    （终端把它作为独立键事件传入，不会与 Enter 混淆）；Alt+Enter 在多数
    Linux 终端可直通，保留为备选。传入的绑定与 prompt_toolkit 默认键合并且
    优先，编辑键（Ctrl+W、Ctrl+A/E、上下翻历史、Ctrl+C 中断等）保持默认。
    """
    kb = KeyBindings()
    focused = has_focus(DEFAULT_BUFFER)

    @kb.add("enter", filter=focused)
    def _accept(event):
        buf = event.current_buffer
        state = buf.complete_state
        if state and state.current_completion:
            # 补全菜单开着且选中了候选：Enter 先应用补全（补命令名 + 空格），不发送
            buf.apply_completion(state.current_completion)
            buf.complete_state = None
            buf.insert_text(" ")
            return
        buf.validate_and_handle()

    @kb.add("c-j", filter=focused)  # Ctrl+Enter（\n），Windows 与多数 POSIX 终端可区分
    @kb.add("escape", "enter", filter=focused)  # Alt+Enter，Linux 终端备选
    def _newline(event):
        event.current_buffer.insert_text("\n")

    return kb


def _history():
    """输入历史：持久化到 smithcode_home/history；任何失败降级为进程内历史。"""
    try:
        config.smithcode_home().mkdir(parents=True, exist_ok=True)
        return FileHistory(str(config.smithcode_home() / "history"))
    except OSError:
        return InMemoryHistory()


def _session() -> PromptSession:
    """惰性创建全局 PromptSession，会话间复用同一份输入历史。"""
    global _SESSION
    if _SESSION is None:
        _SESSION = PromptSession(
            multiline=True,  # 支持缓冲区内换行；Enter 仍发送（见 _bindings）
            key_bindings=_bindings(),
            history=_history(),
            completer=SlashCompleter(),
            complete_while_typing=True,  # 敲 "/" 即时弹候选菜单
        )
    return _SESSION


def _read_line(prompt: str = "") -> str:
    """读一行输入：交互走 prompt_toolkit，非交互退回普通 input()。"""
    if confirmations_available():
        return _session().prompt(prompt)
    return input(prompt)


def setup_console_encoding():
    """把控制台与标准输出流切换到 UTF-8。

    Windows 控制台默认代码页可能不是 65001，且 Python 流编码跟随系统区域设置，
    因此两者都要处理；其他平台通常已是 UTF-8，重复设置无副作用。
    输入层由 prompt_toolkit 接管后，不再需要 readline 与内核队列探测。
    """
    if sys.platform == "win32":
        os.system("chcp 65001 > nul 2>&1")
        os.system("")  # 触发旧版控制台启用 ANSI 转义序列解析（Windows 10+）
    os.environ["PYTHONIOENCODING"] = "utf-8"

    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream.encoding and stream.encoding.lower() != "utf-8":
            try:
                setattr(sys, name, io.TextIOWrapper(stream.buffer, encoding="utf-8"))
            # 尽力而为：编码设置失败不应阻止程序启动
            except Exception:  # noqa: BLE001, S110
                pass


def stdout_is_tty() -> bool:
    """真实 stdout 是否连着终端。

    用 sys.__stdout__ 而非 sys.stdout：Textual 启动后会替换后者（`_PrintCapture`，
    isatty 恒为 True），拿它判断会在管道下误判。
    """
    stream = sys.__stdout__
    try:
        return bool(stream is not None and stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def write_terminal_control(seq: str) -> None:
    """把控制序列直写真实终端（窗口标题等），失败静默。

    同 stdout_is_tty：绕开 sys.stdout。sys.__stdout__ 在 TUI / REPL 下始终是
    进程启动时的那个流；它写不进去（非 ASCII 标题撞上旧代码页、流已关闭）时
    兜底 os.write(1, ...)——控制台此时已被 setup_console_encoding 切到 UTF-8。
    """
    stream = sys.__stdout__
    try:
        if stream is not None:
            stream.write(seq)
            stream.flush()
            return
    except (OSError, ValueError, UnicodeError):
        pass
    try:
        os.write(1, seq.encode("utf-8"))
    except OSError:
        pass


def flush_pending_input():
    """清空控制台输入缓冲区（尽力而为）。

    弹出交互确认前调用：提前键入或粘贴进缓冲区的内容会被就地丢弃，
    而不是被随后的确认框误当成回答——后者正是"权限确认莫名被拒"
    的根源。POSIX 用 termios.tcflush 丢弃输入队列；Windows 用 msvcrt 逐个
    读走。标准输入被重定向时自动退化为空操作。
    """
    try:
        if not sys.stdin.isatty():
            return
    except (AttributeError, OSError):
        return
    if sys.platform != "win32":
        try:
            import termios

            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        except Exception:  # noqa: BLE001, S110 - 清空失败不应影响后续确认
            pass
        return
    try:
        import msvcrt

        for _ in range(4096):  # 上限保护：异常情况下不陷入死循环
            if not msvcrt.kbhit():
                return
            msvcrt.getwch()
    except Exception:  # noqa: BLE001
        return


def confirmations_available() -> bool:
    """交互确认是否可用：标准输入被重定向（管道/CI/脚本）时无法询问用户，一律 fail-closed 拒绝。"""
    try:
        return sys.stdin.isatty()
    except (AttributeError, OSError):
        return False


def read_user_input(prompt: str = "\n你> ") -> str:
    """读一条用户输入（可指定提示符）。

    交互模式走 prompt_toolkit：Enter 发送，Ctrl+Enter 手动换行，粘贴的多行
    文本整体作为一条消息提交，支持历史记录与中文按宽度编辑；非交互 stdin
    （管道 / CI）退回普通 input()，不做合并，行为不变。
    """
    return _read_line(prompt)


def prompt_choice(prompt: str, valid: str, hint: str) -> str:
    """循环读取单个选择键（如 y/n/a），直到输入合法。

    交互走 prompt_toolkit，非交互退回普通 input()。hint 用于非法输入时的
    提示文案（如 "y / n / a"）。返回规范化后的小写选择键。
    """
    while True:
        answer = _read_line(prompt).strip().lower()
        if answer and answer in valid:  # 空串在 Python 里是任意字符串的子串，需先排除
            return answer
        shown = f"（收到: {answer[:40]!r}）" if answer else ""
        print(f"   无效输入{shown}，请输入 {hint}")