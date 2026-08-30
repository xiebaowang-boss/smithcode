"""检索工具测试：glob 文件名匹配、grep 内容搜索、无关目录过滤与沙箱。"""

import pytest

from codeagent import config
from codeagent.tools import search


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
