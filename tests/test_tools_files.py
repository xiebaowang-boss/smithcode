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
