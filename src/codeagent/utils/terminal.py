"""终端环境处理：统一控制台 UTF-8 输出，避免 Windows 下中文乱码。"""
import io
import os
import sys


def setup_console_encoding():
    """把控制台与标准输出流切换到 UTF-8。

    Windows 控制台默认代码页可能不是 65001，且 Python 流编码跟随系统区域设置，
    因此两者都要处理；其他平台通常已是 UTF-8，重复设置无副作用。
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
