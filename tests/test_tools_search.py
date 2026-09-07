"""检索工具测试：glob 文件名匹配、grep 内容搜索、无关目录过滤与沙箱。"""

import pytest

from smithcode import config
from smithcode.tools import search


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """把工作区指到临时目录，测试互不干扰。"""
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    return tmp_path


def test_glob_matches_recursively(workspace):
    (workspace / "src" / "pkg").mkdir(parents=True)
    (workspace / "src" / "pkg" / "a.py").write_text("x", encoding="utf-8")
    (workspace / "readme.md").write_text("x", encoding="utf-8")
    out = search.glob("**/*.py")
    assert "src/pkg/a.py" in out
    assert "readme.md" not in out


def test_glob_skips_junk_dirs(workspace):
    (workspace / "node_modules" / "lib").mkdir(parents=True)
    (workspace / "node_modules" / "lib" / "dep.py").write_text("x", encoding="utf-8")
    (workspace / "main.py").write_text("x", encoding="utf-8")
    out = search.glob("**/*.py")
    assert "main.py" in out
    assert "node_modules" not in out


def test_glob_sorted_by_mtime_desc(workspace):
    """glob 结果按修改时间新→旧排序。"""
    import os

    old = workspace / "old.txt"
    new = workspace / "new.txt"
    old.write_text("x", encoding="utf-8")
    new.write_text("x", encoding="utf-8")
    past = old.stat().st_mtime - 100
    os.utime(old, (past, past))
    out = search.glob("*.txt")
    assert out.index("new.txt") < out.index("old.txt")


def test_glob_outside_workspace_rejected(workspace):
    with pytest.raises(PermissionError):
        search.glob("**/*.py", "../elsewhere")


def test_grep_finds_matches(workspace):
    (workspace / "app.py").write_text(
        "def foo():\n    return 42\n", encoding="utf-8"
    )
    out = search.grep("return 42")
    assert "app.py:2" in out
    assert "return 42" in out


def test_grep_include_filter(workspace):
    (workspace / "a.py").write_text("needle\n", encoding="utf-8")
    (workspace / "b.txt").write_text("needle\n", encoding="utf-8")
    out = search.grep("needle", include="*.txt")
    assert "b.txt" in out
    assert "a.py" not in out


def test_grep_skips_junk_dirs_and_binary(workspace):
    junk = workspace / ".git" / "hook.py"
    junk.parent.mkdir(parents=True)
    junk.write_text("needle\n", encoding="utf-8")
    (workspace / "bin.py").write_bytes(b"needle\x00binary")
    assert search.grep("needle") == "(无匹配)"


def test_grep_single_file(workspace):
    (workspace / "one.py").write_text("needle here\n", encoding="utf-8")
    out = search.grep("needle", path="one.py")
    assert "one.py:1" in out


def test_grep_invalid_regex_returns_message(workspace):
    assert "错误" in search.grep("([unclosed")


def test_grep_outside_workspace_rejected(workspace):
    with pytest.raises(PermissionError):
        search.grep("needle", "../elsewhere")


# ---------- grep 增强参数 ----------

def test_grep_ignore_case(workspace):
    (workspace / "a.py").write_text("Needle\n", encoding="utf-8")
    assert search.grep("needle") == "(无匹配)"
    assert "a.py:1" in search.grep("needle", ignore_case=True)


def test_grep_files_with_matches_mode(workspace):
    (workspace / "a.py").write_text("hit\nhit\n", encoding="utf-8")
    (workspace / "b.py").write_text("hit\n", encoding="utf-8")
    out = search.grep("hit", output_mode="files_with_matches")
    assert "a.py" in out and "b.py" in out
    assert "a.py:1" not in out  # 不带行号与内容


def test_grep_count_mode(workspace):
    (workspace / "a.py").write_text("hit\nhit\nhit\n", encoding="utf-8")
    (workspace / "b.py").write_text("nope\n", encoding="utf-8")
    out = search.grep("hit", output_mode="count")
    assert "a.py:3" in out
    assert "b.py" not in out


def test_grep_context_lines(workspace):
    (workspace / "c.txt").write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")
    out = search.grep("three", path="c.txt", context=1)
    assert "c.txt-2- two" in out      # 上下文行用 - 分隔
    assert "c.txt:3: three" in out    # 匹配行用 : 分隔
    assert "c.txt-4- four" in out
    assert "five" not in out          # 窗口外不出现


def test_grep_invalid_output_mode(workspace):
    assert "output_mode" in search.grep("x", output_mode="bogus")


# ---------- 多根授权（--add） ----------

def test_glob_in_extra_root(workspace, monkeypatch):
    """附加授权目录可检索，展示路径相对该根。"""
    extra = workspace.parent / (workspace.name + "-extra")
    (extra / "src").mkdir(parents=True)
    (extra / "src" / "x.py").write_text("y = 1\n", encoding="utf-8")
    monkeypatch.setattr(config, "EXTRA_ROOTS", [str(extra)])

    out = search.glob("**/*.py", str(extra))
    assert "src/x.py" in out


def test_grep_in_extra_root(workspace, monkeypatch):
    extra = workspace.parent / (workspace.name + "-extra")
    extra.mkdir()
    (extra / "m.py").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(config, "EXTRA_ROOTS", [str(extra)])

    assert "m.py:1" in search.grep("needle", str(extra))
