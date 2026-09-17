"""grep 工具测试：内容正则搜索、过滤、无关目录与沙箱。"""

import pytest

from smithcode import config
from smithcode.tools import grep as grep_mod


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """把工作区指到临时目录，测试互不干扰。"""
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    return tmp_path


def test_grep_finds_matches(workspace):
    (workspace / "app.py").write_text(
        "def foo():\n    return 42\n", encoding="utf-8"
    )
    out = grep_mod.grep("return 42")
    assert "app.py:2" in out
    assert "return 42" in out

def test_grep_include_filter(workspace):
    (workspace / "a.py").write_text("needle\n", encoding="utf-8")
    (workspace / "b.txt").write_text("needle\n", encoding="utf-8")
    out = grep_mod.grep("needle", include="*.txt")
    assert "b.txt" in out
    assert "a.py" not in out

def test_grep_skips_junk_dirs_and_binary(workspace):
    junk = workspace / ".git" / "hook.py"
    junk.parent.mkdir(parents=True)
    junk.write_text("needle\n", encoding="utf-8")
    (workspace / "bin.py").write_bytes(b"needle\x00binary")
    assert grep_mod.grep("needle") == "(无匹配)"

def test_grep_single_file(workspace):
    (workspace / "one.py").write_text("needle here\n", encoding="utf-8")
    out = grep_mod.grep("needle", path="one.py")
    assert "one.py:1" in out

def test_grep_invalid_regex_returns_message(workspace):
    assert "错误" in grep_mod.grep("([unclosed")

def test_grep_outside_workspace_rejected(workspace):
    with pytest.raises(PermissionError):
        grep_mod.grep("needle", "../elsewhere")

def test_grep_ignore_case(workspace):
    (workspace / "a.py").write_text("Needle\n", encoding="utf-8")
    assert grep_mod.grep("needle") == "(无匹配)"
    assert "a.py:1" in grep_mod.grep("needle", ignore_case=True)

def test_grep_files_with_matches_mode(workspace):
    (workspace / "a.py").write_text("hit\nhit\n", encoding="utf-8")
    (workspace / "b.py").write_text("hit\n", encoding="utf-8")
    out = grep_mod.grep("hit", output_mode="files_with_matches")
    assert "a.py" in out and "b.py" in out
    assert "a.py:1" not in out  # 不带行号与内容

def test_grep_count_mode(workspace):
    (workspace / "a.py").write_text("hit\nhit\nhit\n", encoding="utf-8")
    (workspace / "b.py").write_text("nope\n", encoding="utf-8")
    out = grep_mod.grep("hit", output_mode="count")
    assert "a.py:3" in out
    assert "b.py" not in out

def test_grep_context_lines(workspace):
    (workspace / "c.txt").write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")
    out = grep_mod.grep("three", path="c.txt", context=1)
    assert "c.txt-2- two" in out      # 上下文行用 - 分隔
    assert "c.txt:3: three" in out    # 匹配行用 : 分隔
    assert "c.txt-4- four" in out
    assert "five" not in out          # 窗口外不出现

def test_grep_keeps_line_indentation(workspace):
    """匹配行按文件原文输出缩进：内容会被直接复制成 old_string，去缩进必然匹配失败。"""
    (workspace / "a.py").write_text(
        "class A:\n    def m(self):\n        return 1\n", encoding="utf-8"
    )
    out = grep_mod.grep("return 1")
    assert "a.py:3:         return 1" in out

def test_grep_context_keeps_line_indentation(workspace):
    """context 模式下匹配行与上下文行同样保留缩进。"""
    (workspace / "a.py").write_text(
        "def m():\n    x = 1\n    return 2\n", encoding="utf-8"
    )
    out = grep_mod.grep("x = 1", path="a.py", context=1)
    assert "a.py:2:     x = 1" in out      # 匹配行用 : 分隔
    assert "a.py-3-     return 2" in out   # 上下文行用 - 分隔

def test_grep_invalid_output_mode(workspace):
    assert "output_mode" in grep_mod.grep("x", output_mode="bogus")

def test_grep_in_extra_root(workspace, monkeypatch):
    extra = workspace.parent / (workspace.name + "-extra")
    extra.mkdir()
    (extra / "m.py").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(config, "EXTRA_ROOTS", [str(extra)])

    assert "m.py:1" in grep_mod.grep("needle", str(extra))


# ---------- 安全与性能回归 ----------

def test_grep_symlink_escape_blocked(workspace, tmp_path, monkeypatch):
    """工作区内的符号链接指向授权目录之外时，grep 不得读出目标内容。"""
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir(exist_ok=True)
    secret = outside / "secret.txt"
    secret.write_text("TOPSECRET\n", encoding="utf-8")
    monkeypatch.setattr(config, "EXTRA_ROOTS", [])
    link = workspace / "link.txt"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("当前平台不支持创建符号链接")
    assert grep_mod.grep("TOPSECRET") == "(无匹配)"


def test_grep_files_with_matches_stops_at_first_hit(workspace):
    (workspace / "big.py").write_text("hit\n" * 5000, encoding="utf-8")
    out = grep_mod.grep("hit", path="big.py", output_mode="files_with_matches")
    assert out == "big.py"


def test_grep_negative_context_rejected(workspace):
    (workspace / "a.py").write_text("hit\n", encoding="utf-8")
    assert "非负整数" in grep_mod.grep("hit", path="a.py", context=-1)


def test_grep_scan_budget_caps_full_scan(workspace, monkeypatch):
    """无匹配的全量扫描超预算即停，并提示收窄范围。"""
    from smithcode.tools import _shared_local as shared

    for i in range(20):
        (workspace / f"f{i}.txt").write_text("nothing here\n", encoding="utf-8")
    monkeypatch.setattr(shared, "MAX_SCAN_FILES", 5)
    out = grep_mod.grep("zzz-no-such-needle")
    assert out == "(无匹配)" or "扫描上限" in out
    out = grep_mod.grep("nothing here")
    assert "扫描上限" in out


def test_grep_long_line_marked_truncated(workspace):
    (workspace / "long.py").write_text("x=" + "y" * 500 + "\n", encoding="utf-8")
    out = grep_mod.grep("x=", path="long.py")
    assert out.endswith("…")
    assert "y" * 500 not in out
