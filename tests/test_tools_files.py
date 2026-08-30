"""文件工具测试：正常读写、精确编辑、路径越界拦截。"""
import pytest

from codeagent import config
from codeagent.tools import files


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """把工作区指到临时目录，测试互不干扰。"""
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    return tmp_path


def test_write_and_read_file(workspace):
    assert "已写入" in files.write_file("a.txt", "你好")
    assert files.read_file("a.txt") == "你好"


def test_write_creates_parent_dirs(workspace):
    files.write_file("sub/dir/b.txt", "x")
    assert (workspace / "sub" / "dir" / "b.txt").read_text(encoding="utf-8") == "x"


def test_edit_file_unique_match(workspace):
    files.write_file("c.txt", "hello world")
    assert "已编辑" in files.edit_file("c.txt", "world", "python")
    assert files.read_file("c.txt") == "hello python"


def test_edit_file_requires_unique_match(workspace):
    files.write_file("d.txt", "abc abc")
    assert "匹配了 2 处" in files.edit_file("d.txt", "abc", "x")
    assert "未找到" in files.edit_file("d.txt", "xyz", "x")


def test_list_dir(workspace):
    files.write_file("e.txt", "")
    listing = files.list_dir()
    assert "[文件] e.txt" in listing


def test_path_outside_workspace_rejected(workspace):
    with pytest.raises(PermissionError):
        files.read_file("../outside.txt")


def test_path_escape_via_sibling_prefix_rejected(workspace):
    """兄弟目录名与工作区共享前缀：旧的 startswith 检查会误放行。"""
    with pytest.raises(PermissionError):
        files.read_file(f"../{workspace.name}-evil/secrets.txt")


def test_path_escape_via_parent_rejected(workspace):
    with pytest.raises(PermissionError):
        files.write_file("../../evil.txt", "x")


def test_absolute_path_outside_rejected(workspace):
    with pytest.raises(PermissionError):
        files.read_file(str(workspace.parent / "elsewhere.txt"))


def test_dotdot_within_workspace_still_allowed(workspace):
    """工作区内的 .. 相对路径正常解析，不误伤。"""
    files.write_file("sub/f.txt", "x")
    assert files.read_file("sub/../sub/f.txt") == "x"


# ---------- 多根授权（--add） ----------

def test_extra_root_read_and_write(workspace, monkeypatch):
    """附加授权目录内可正常读写。"""
    extra = workspace.parent / (workspace.name + "-extra")
    extra.mkdir()
    (extra / "b.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(config, "EXTRA_ROOTS", [str(extra)])

    assert files.read_file(str(extra / "b.txt")) == "x"
    assert "已写入" in files.write_file(str(extra / "c.txt"), "y")
    assert (extra / "c.txt").read_text(encoding="utf-8") == "y"


def test_dotdot_into_extra_root_allowed(workspace, monkeypatch):
    """相对主工作区的 .. 逃逸若落在附加授权目录内，应放行。"""
    extra = workspace.parent / (workspace.name + "-extra")
    extra.mkdir()
    (extra / "d.txt").write_text("z", encoding="utf-8")
    monkeypatch.setattr(config, "EXTRA_ROOTS", [str(extra)])

    assert files.read_file(f"../{extra.name}/d.txt") == "z"


def test_outside_all_roots_still_rejected(workspace, monkeypatch):
    """有附加授权目录时，未授权路径依然被拒。"""
    extra = workspace.parent / (workspace.name + "-extra")
    extra.mkdir()
    monkeypatch.setattr(config, "EXTRA_ROOTS", [str(extra)])

    with pytest.raises(PermissionError):
        files.read_file(str(workspace.parent / "unrelated.txt"))
