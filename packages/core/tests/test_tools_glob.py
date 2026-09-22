"""glob 工具测试：文件名通配匹配、无关目录过滤、排序与沙箱。"""

import pytest

from smithcode import config
from smithcode.tools import _shared_local as shared_mod
from smithcode.tools import glob as glob_mod


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """把工作区指到临时目录，测试互不干扰。"""
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    return tmp_path


def test_glob_matches_recursively(workspace):
    (workspace / "src" / "pkg").mkdir(parents=True)
    (workspace / "src" / "pkg" / "a.py").write_text("x", encoding="utf-8")
    (workspace / "readme.md").write_text("x", encoding="utf-8")
    out = glob_mod.glob("**/*.py")
    assert "src/pkg/a.py" in out
    assert "readme.md" not in out


def test_glob_skips_junk_dirs(workspace):
    (workspace / "node_modules" / "lib").mkdir(parents=True)
    (workspace / "node_modules" / "lib" / "dep.py").write_text("x", encoding="utf-8")
    (workspace / "main.py").write_text("x", encoding="utf-8")
    out = glob_mod.glob("**/*.py")
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
    out = glob_mod.glob("*.txt")
    assert out.index("new.txt") < out.index("old.txt")


def test_glob_outside_workspace_rejected(workspace):
    with pytest.raises(PermissionError):
        glob_mod.glob("**/*.py", "../elsewhere")


def test_glob_in_extra_root(workspace, monkeypatch):
    """附加授权目录可检索，展示路径相对该根。"""
    extra = workspace.parent / (workspace.name + "-extra")
    (extra / "src").mkdir(parents=True)
    (extra / "src" / "x.py").write_text("y = 1\n", encoding="utf-8")
    monkeypatch.setattr(config, "EXTRA_ROOTS", [str(extra)])

    out = glob_mod.glob("**/*.py", str(extra))
    assert "src/x.py" in out


def test_glob_skips_skip_dirs_without_descending(workspace, monkeypatch):
    """SKIP_DIRS 在遍历入口剪枝：node_modules 下再多文件也不影响结果与上限。"""
    junk = workspace / "node_modules" / "deep"
    junk.mkdir(parents=True)
    for i in range(30):
        (junk / f"d{i}.py").write_text("x", encoding="utf-8")
    (workspace / "main.py").write_text("x", encoding="utf-8")
    monkeypatch.setattr(shared_mod, "MAX_RESULTS", 10)
    out = glob_mod.glob("**/*.py")
    assert "main.py" in out
    assert "node_modules" not in out
    assert "上限" not in out


def test_glob_scan_budget_caps_full_walk(workspace, monkeypatch):
    """无匹配的大遍历超预算即停，并提示收窄范围。"""
    for i in range(20):
        (workspace / f"f{i}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(shared_mod, "MAX_SCAN_FILES", 5)
    out = glob_mod.glob("**/*.nomatch")
    assert "扫描上限" in out
    out = glob_mod.glob("**/*.txt")
    assert "扫描上限" in out
