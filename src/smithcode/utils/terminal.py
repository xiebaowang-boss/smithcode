"""终端环境处理：统一控制台 UTF-8 输出，避免 Windows 下中文乱码。"""
import io
import os
import sys
import time


def enable_utf8_erase():
    """Linux 下为终端设置 IUTF8，让内核按完整 UTF-8 字符擦除退格。

    Python input() 未加载 readline 时，行编辑由内核（canonical 模式）完成，
    默认按"字节"擦除：中文是 3 字节、2 列宽，退格一次只删掉半个字，残留
    空格、需按两次、删到一半字节/列计数错位后整行卡死。IUTF8 让内核识别
    多字节字符，退格一次删掉整个字。Windows / 非交互 stdin / 非 Linux
    （无 IUTF8 标志）自动跳过。
    """
    if sys.platform == "win32" or not sys.stdin.isatty():
        return
    try:
        import termios
    except ImportError:  # pragma: no cover - 非 POSIX
        return
    if not hasattr(termios, "IUTF8"):
        return
    try:
        attrs = termios.tcgetattr(sys.stdin.fileno())
        if attrs[0] & termios.IUTF8:
            return
        attrs[0] |= termios.IUTF8
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, attrs)
    except Exception:  # noqa: BLE001, S110 - 终端设置失败不应阻止启动
        pass


def setup_console_encoding():
    """把控制台与标准输出流切换到 UTF-8。

    Windows 控制台默认代码页可能不是 65001，且 Python 流编码跟随系统区域设置，
    因此两者都要处理；其他平台通常已是 UTF-8，重复设置无副作用。
    """
    if sys.platform == "win32":
        os.system("chcp 65001 > nul 2>&1")
        os.system("")  # 触发旧版控制台启用 ANSI 转义序列解析（Windows 10+）
    os.environ["PYTHONIOENCODING"] = "utf-8"
    enable_utf8_erase()

    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream.encoding and stream.encoding.lower() != "utf-8":
            try:
                setattr(sys, name, io.TextIOWrapper(stream.buffer, encoding="utf-8"))
            # 尽力而为：编码设置失败不应阻止程序启动
            except Exception:  # noqa: BLE001, S110
                pass


def stdin_has_pending() -> bool:
    """控制台输入缓冲区里是否已有排队内容（粘贴的后续行会先进入缓冲区）。

    仅在 Windows 控制台生效；标准输入被重定向（管道、测试）时一律返回 False。
    """
    if sys.platform != "win32":
        return False
    try:
        import msvcrt

        return bool(msvcrt.kbhit())
    except Exception:  # noqa: BLE001
        return False


def flush_pending_input():
    """清空控制台输入缓冲区（尽力而为）。

    弹出交互确认前调用：提前键入或粘贴进缓冲区的内容会被就地丢弃，
    而不是被随后的 input() 误当成确认回答——后者正是"权限确认莫名被拒"
    的根源。标准输入被重定向时 msvcrt 探测不到控制台，自动退化为空操作。
    """
    if sys.platform != "win32":
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
    """读一条用户输入（可指定提示符）；粘贴的后续行会合并进同一条消息。

    input() 是按行读的，多行粘贴会被逐行消费成多条独立消息（还会在工具
    确认时被误当成回答）。交互模式下首次回车后，只要控制台缓冲区仍有
    排队内容就继续读取，直到出现短暂静默——手动输入的下一句话必然晚于
    该静默窗口，不会被误并进来。非交互 stdin（管道）不做合并，行为不变。
    """
    first = input(prompt)
    if not sys.stdin.isatty():
        return first
    lines = [first]
    while True:
        if not stdin_has_pending():
            time.sleep(0.05)
            if not stdin_has_pending():
                return "\n".join(lines)
        lines.append(input())
