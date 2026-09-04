"""终端环境处理：统一控制台 UTF-8 输出，避免 Windows 下中文乱码。"""
import contextlib
import io
import os
import sys
import time


def enable_readline():
    """Linux/macOS 下加载 readline，让 input() 按字符宽度编辑。

    内核 canonical 模式的行编辑按"字节/单列"处理退格回显，双宽中文字
    （wcwidth=2）退格后会残留一列空格；readline 按 wcwidth 感知宽度，可
    一次完整擦除。同时关闭 bracketed paste：开启时粘贴会整段被 readline
    吞入内部缓冲，内核输入队列里不再有排队数据，select() 探测不到后续行，
    多行合并失效。Windows 无 readline，跳过；导入失败时静默退回内核
    canonical 模式。
    """
    if sys.platform == "win32":
        return
    try:
        import readline  # 导入即接管 input() 行编辑；下方 parse_and_bind 也引用它
    except ImportError:  # pragma: no cover - 裁剪构建/非标准平台无 readline
        return
    try:
        # 旧版 readline（如 macOS 的 libedit）不认识该变量会向 stderr 报错，临时重定向掉
        with contextlib.redirect_stderr(io.StringIO()):
            readline.parse_and_bind("set enable-bracketed-paste off")
    except Exception:  # noqa: BLE001, S110 - 尽力而为，失败不应阻止启动
        pass


def setup_console_encoding():
    """把控制台与标准输出流切换到 UTF-8，并启用 readline 行编辑。

    Windows 控制台默认代码页可能不是 65001，且 Python 流编码跟随系统区域设置，
    因此两者都要处理；其他平台通常已是 UTF-8，重复设置无副作用。
    """
    if sys.platform == "win32":
        os.system("chcp 65001 > nul 2>&1")
        os.system("")  # 触发旧版控制台启用 ANSI 转义序列解析（Windows 10+）
    os.environ["PYTHONIOENCODING"] = "utf-8"
    enable_readline()

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

    Windows 用 msvcrt.kbhit()；POSIX 交互终端用 select() 短超时探测 stdin：
    内核 canonical 模式下粘贴的后续行会留在终端输入队列，select 能读到。
    标准输入被重定向（管道、测试）时一律返回 False。
    """
    try:
        if not sys.stdin.isatty():
            return False
    except (AttributeError, OSError):
        return False
    if sys.platform == "win32":
        try:
            import msvcrt

            return bool(msvcrt.kbhit())
        except Exception:  # noqa: BLE001
            return False
    try:
        import select

        return bool(select.select([sys.stdin], [], [], 0.0)[0])
    except Exception:  # noqa: BLE001
        return False


def flush_pending_input():
    """清空控制台输入缓冲区（尽力而为）。

    弹出交互确认前调用：提前键入或粘贴进缓冲区的内容会被就地丢弃，
    而不是被随后的 input() 误当成确认回答——后者正是"权限确认莫名被拒"
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
    """读一条用户输入（可指定提示符）；粘贴的后续行会合并进同一条消息。

    input() 是按行读的，多行粘贴会被逐行消费成多条独立消息（还会在工具
    确认时被误当成回答）。交互模式下首次回车后，只要控制台缓冲区仍有
    排队内容就继续读取，直到出现短暂静默——手动输入的下一句话必然晚于
    该静默窗口，不会被误并进来。Linux/macOS 依赖 readline 关闭 bracketed
    paste 后内核队列保留的后续行（select 探测），Windows 用 msvcrt。非交互
    stdin（管道）不做合并，行为不变。
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
