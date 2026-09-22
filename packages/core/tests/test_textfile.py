"""textfile 模块测试：换行风格与 BOM 的探测、归一化、还原与往返无损。

这是「编辑不改变文件换行风格」的地基，覆盖 LF / CRLF / CR / BOM / 无尾换行
/ 空文件 / 混合换行 / 非 UTF-8 等边界。
"""
import pytest

from smithcode import textfile
from smithcode.textfile import CR, CRLF, LF, FileFormat, TextFileError


def _roundtrip(tmp_path, raw: bytes) -> bytes:
    """读原文再原样写回，返回写回后的字节——应逐字节等于输入。"""
    p = tmp_path / "f.txt"
    p.write_bytes(raw)
    text, fmt = textfile.read(p)
    textfile.write(p, text, fmt)
    return p.read_bytes()


# ---------- 探测 ----------

@pytest.mark.parametrize(
    "raw, newline",
    [
        (b"", LF),
        (b"a\nb\n", LF),
        (b"a\r\nb\r\n", CRLF),
        (b"a\rb\r", CR),
        (b"a\r\nb\r\nc\r\n", CRLF),
        (b"a\nb\nc\r\nd\r\n", CRLF),  # 混合：取占多数
    ],
)
def test_detect_newline_style(raw, newline):
    assert textfile.detect(raw.decode()).newline == newline


def test_detect_bom_flag():
    assert textfile.detect("\ufeffhello").bom is True
    assert textfile.detect("hello").bom is False


# ---------- 读：归一化为 LF ----------

def test_read_normalizes_to_lf(tmp_path):
    p = tmp_path / "crlf.txt"
    p.write_bytes(b"a\r\nb\r\n")
    text, fmt = textfile.read(p)
    assert text == "a\nb\n"          # 模型只看到 LF
    assert "\r" not in text
    assert fmt.newline == CRLF       # 但格式被记下来了


def test_read_strips_bom(tmp_path):
    p = tmp_path / "bom.txt"
    p.write_bytes(b"\xef\xbb\xbfhello\n")
    text, fmt = textfile.read(p)
    assert text == "hello\n"         # BOM 不进正文
    assert fmt.bom is True


def test_read_cr_only(tmp_path):
    p = tmp_path / "cr.txt"
    p.write_bytes(b"a\rb\r")
    assert textfile.read(p)[0] == "a\nb\n"


def test_read_invalid_utf8_raises_friendly_error(tmp_path):
    p = tmp_path / "latin.txt"
    p.write_bytes(b"# caf\xe9\n")
    with pytest.raises(TextFileError) as err:
        textfile.read(p)
    assert "UTF-8" in str(err.value)


def test_read_errors_replace_tolerates_invalid_utf8(tmp_path):
    """grep 这类检索要能扫过 GBK / latin-1 文件，不因编码中断。"""
    p = tmp_path / "latin.txt"
    p.write_bytes(b"needle caf\xe9\n")
    text, _fmt = textfile.read(p, errors="replace")
    assert "needle" in text


# ---------- 写：还原原文风格 ----------

def test_write_restores_crlf(tmp_path):
    p = tmp_path / "f.txt"
    textfile.write(p, "a\nB\n", FileFormat(newline=CRLF))
    assert p.read_bytes() == b"a\r\nB\r\n"


def test_write_restores_bom(tmp_path):
    p = tmp_path / "f.txt"
    textfile.write(p, "hi\n", FileFormat(bom=True))
    assert p.read_bytes() == b"\xef\xbb\xbfhi\n"


def test_write_default_is_lf_without_bom(tmp_path):
    """新建文件（fmt=None）默认 LF、无 BOM，与平台无关。"""
    p = tmp_path / "new.txt"
    textfile.write(p, "x\ny\n")
    assert p.read_bytes() == b"x\ny\n"


# ---------- 往返无损 ----------

@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"no trailing newline",
        b"lf\nlines\n",
        b"crlf\r\nlines\r\n",
        b"cr\rlines\r",
        b"\xef\xbb\xbfbom + crlf\r\nsecond\r\n",
        b"\xef\xbb\xbflf + bom\n",
    ],
)
def test_roundtrip_is_byte_lossless(tmp_path, raw):
    assert _roundtrip(tmp_path, raw) == raw


def test_edit_like_splice_keeps_crlf_and_trailing_newline(tmp_path):
    """模拟 edit_file：整串 replace 后写回，只有被改的那一行字节变化。"""
    p = tmp_path / "f.txt"
    p.write_bytes(b"a\r\nb\r\nc\r\n")
    text, fmt = textfile.read(p)
    textfile.write(p, text.replace("b", "B"), fmt)
    assert p.read_bytes() == b"a\r\nB\r\nc\r\n"


# ---------- format_of：只探测不整读 ----------

def test_format_of_detects_without_content(tmp_path):
    p = tmp_path / "f.txt"
    p.write_bytes(b"\xef\xbb\xbfa\r\nb\r\n")
    fmt = textfile.format_of(p)
    assert fmt == FileFormat(newline=CRLF, bom=True)


def test_format_of_missing_file_raises(tmp_path):
    with pytest.raises(TextFileError):
        textfile.format_of(tmp_path / "nope.txt")
