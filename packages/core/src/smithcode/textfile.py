"""文本文件读写的统一出口：换行风格与 BOM 的探测、归一化与还原。

对标 `process.py`（外部命令执行的唯一出口）：Agent 的文件工具
（read_file / write_file / edit_file / apply_patch / grep）全部经由此模块
读写磁盘，保证「编辑不改变文件既有的换行风格与 BOM」。

策略同 Claude Code 的 Edit 工具：

- **读**：按原文读取，探测换行风格（LF / CRLF / CR）与 UTF-8 BOM，把文本
  归一化为 LF 返回——模型看到的永远是 LF 内容，不需要知道目标文件是
  Windows 还是 Unix 换行，也就不会出现「往 CRLF 文件里插 LF 行」的错误。
- **写**：把 LF 文本按探测到的风格还原后写出，Python 不再做平台相关的翻译。

关键是**避开 `os.linesep` 翻译**：用默认参数读写时，`read_text()` 会把
CRLF 归一化成 LF、`write_text()` 又按 `os.linesep` 翻译回去，于是 Linux 上
编辑 CRLF 文件会把整个文件变成 LF、Windows 上编辑 LF 文件会整体变成 CRLF
（只改一行却全文件 diff）。这里用 `open(newline="")` 关掉两侧翻译。

注意：读侧不能用 `Path.read_text(newline=...)`——该参数 3.13 才加入
（`write_text` 的 newline 是 3.10 加的），用了会破坏 Python 3.10 兼容。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

LF = "\n"
CRLF = "\r\n"
CR = "\r"

# UTF-8 BOM（以 utf-8 解码后就是这一个字符）；读写两侧都保留
BOM = "\ufeff"

# 探测大文件格式时的采样字节数：BOM 在开头、换行风格对同一文件通常一致
_SAMPLE_BYTES = 65536


class TextFileError(Exception):
    """文本读写失败（编码不支持 / IO 错误等）。消息面向用户，可直接回给模型。"""


@dataclass(frozen=True)
class FileFormat:
    """文件的文本格式：换行风格 + 是否带 UTF-8 BOM。"""

    newline: str = LF
    bom: bool = False


def normalize(text: str) -> str:
    """把任意换行风格归一化为 LF（CRLF 与孤立 CR 都算换行）。"""
    return text.replace(CRLF, LF).replace(CR, LF)


def denormalize(text: str, fmt: FileFormat) -> str:
    """把 LF 文本按 fmt 还原为原文风格（换行 + BOM）。"""
    if fmt.newline != LF:
        text = text.replace(LF, fmt.newline)
    return BOM + text if fmt.bom else text


def detect(raw: str) -> FileFormat:
    """从原始文本探测格式：BOM + 占多数的换行风格（空文件按 LF）。

    孤立 CR 统计时要扣掉 CRLF 里的那个 CR；混合换行的文件统一为占多数的
    风格——这是与 Claude Code 一致的取舍，不做逐行保留。
    """
    bom = raw.startswith(BOM)
    if bom:
        raw = raw[len(BOM):]
    crlf = raw.count(CRLF)
    cr = raw.count(CR) - crlf
    lf = raw.count(LF) - crlf
    if crlf and crlf >= cr and crlf >= lf:
        newline = CRLF
    elif cr and cr > lf:
        newline = CR
    else:
        newline = LF
    return FileFormat(newline=newline, bom=bom)


def read(path: Path, *, errors: str | None = None) -> tuple[str, FileFormat]:
    """读取文本文件，返回 (归一化为 LF 的文本, 原文格式)。

    errors 为 None 时非法 UTF-8 抛 TextFileError（面向用户的友好错误）；
    传 "replace" 则替换非法字节——grep 这类只读检索要能扫过 GBK 等文件。
    """
    try:
        with open(path, "r", encoding="utf-8", errors=errors, newline="") as handle:
            raw = handle.read()
    except UnicodeDecodeError as e:
        raise TextFileError(f"文件不是有效的 UTF-8 文本: {e}") from e
    except OSError as e:
        raise TextFileError(f"读取失败: {e}") from e
    fmt = detect(raw)
    return normalize(raw[len(BOM):] if fmt.bom else raw), fmt


def format_of(path: Path) -> FileFormat:
    """只探测格式、不返回内容（采样文件头部，避免大文件整读）。

    write_file 覆盖已有文件时用来沿用原文格式——它不需要文件内容，
    没必要为此整读一遍。
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(_SAMPLE_BYTES)
    except OSError as e:
        raise TextFileError(f"读取失败: {e}") from e
    return detect(head.decode("utf-8", errors="replace"))


def write(path: Path, text: str, fmt: FileFormat | None = None) -> None:
    """把 LF 文本按 fmt 还原后写入；fmt 为 None 表示新建文件（LF、无 BOM）。"""
    fmt = fmt or FileFormat()
    try:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(denormalize(text, fmt))
    except OSError as e:
        raise TextFileError(f"写入失败: {e}") from e
