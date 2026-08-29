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
    os.environ["PYTHONIOENCODING"] = "utf-8"

    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream.encoding and stream.encoding.lower() != "utf-8":
            try:
                setattr(sys, name, io.TextIOWrapper(stream.buffer, encoding="utf-8"))
            except Exception:
                pass
