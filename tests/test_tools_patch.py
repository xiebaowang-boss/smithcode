"""apply_patch 工具测试：解析、应用、原子性、沙箱。"""
import pytest

from smithcode import config
from smithcode.tools.patch import apply_patch, extract_patch_paths


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """把工作区指到临时目录，测试互不干扰。"""
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    return tmp_path


def test_add_file(workspace):
    out = apply_patch("*** Begin Patch\n*** Add File: hello.txt\n+hello\n+world\n*** End Patch\n")
    assert "已应用" in out
    assert (workspace / "hello.txt").read_text(encoding="utf-8") == "hello\nworld"


def test_add_creates_parent_dirs(workspace):
    apply_patch("*** Add File: sub/dir/b.txt\n+x\n")
    assert (workspace / "sub" / "dir" / "b.txt").read_text(encoding="utf-8") == "x"


def test_update_file(workspace):
    (workspace / "app.py").write_text("def greet():\n    print('Hi')\n", encoding="utf-8")
    out = apply_patch(
        "*** Update File: app.py\n"
        "@@ def greet():\n"
        "-    print('Hi')\n"
        "+    print('Hello')\n"
    )
    assert "已应用" in out
    assert (workspace / "app.py").read_text(encoding="utf-8") == "def greet():\n    print('Hello')\n"


def test_update_with_context_lines(workspace):
    (workspace / "f.txt").write_text("a\nb\nc\n", encoding="utf-8")
    apply_patch("*** Update File: f.txt\n a\n-b\n+B\n c\n")
    assert (workspace / "f.txt").read_text(encoding="utf-8") == "a\nB\nc\n"


def test_update_requires_unique_match(workspace):
    (workspace / "f.txt").write_text("x\nx\n", encoding="utf-8")
    out = apply_patch("*** Update File: f.txt\n-x\n+y\n")
    assert "匹配" in out and "2" in out


def test_update_missing_old_errors(workspace):
    (workspace / "f.txt").write_text("abc\n", encoding="utf-8")
    out = apply_patch("*** Update File: f.txt\n-zzz\n+aaa\n")
    assert "未找到" in out


def test_update_missing_file_errors(workspace):
    out = apply_patch("*** Update File: nope.txt\n-x\n+y\n")
    assert "不存在" in out


def test_delete_file(workspace):
    (workspace / "old.txt").write_text("x", encoding="utf-8")
    out = apply_patch("*** Delete File: old.txt\n")
    assert "已应用" in out
    assert "-" in out
    assert not (workspace / "old.txt").exists()


def test_add_existing_fails(workspace):
    (workspace / "a.txt").write_text("old", encoding="utf-8")
    out = apply_patch("*** Add File: a.txt\n+new\n")
    assert "已存在" in out
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "old"


def test_apply_is_atomic(workspace):
    """多文件 patch 任一失败则整体不落盘（原子性）。"""
    (workspace / "b.txt").write_text("context\n", encoding="utf-8")
    patch = "*** Add File: a.txt\n+x\n*** Update File: b.txt\n-notpresent\n+y\n"
    out = apply_patch(patch)
    assert "错误" in out
    assert not (workspace / "a.txt").exists()  # 回滚：a.txt 未创建
    assert (workspace / "b.txt").read_text(encoding="utf-8") == "context\n"


def test_path_outside_workspace_rejected(workspace):
    with pytest.raises(PermissionError):
        apply_patch("*** Add File: ../evil.txt\n+x\n")


def test_extract_patch_paths():
    patch = (
        "*** Add File: a.txt\n+x\n"
        "*** Update File: src/b.py\n@@ x\n-y\n+z\n"
        "*** Delete File: old.txt\n"
    )
    assert extract_patch_paths({"patch": patch}) == ["a.txt", "src/b.py", "old.txt"]


def test_invalid_patch_no_section(workspace):
    out = apply_patch("随便一行\n")
    assert "无法解析" in out