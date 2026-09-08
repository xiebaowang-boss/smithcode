"""文件工具测试：正常读写、精确编辑、路径越界拦截。"""
import pytest

from smithcode import config
from smithcode.tools import files


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """把工作区指到临时目录，测试互不干扰；已读文件记录每测清空。"""
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    files.reset_read_tracking()
    return tmp_path


def test_write_and_read_file(workspace):
    assert "已写入" in files.write_file("a.txt", "你好")
    assert "你好" in files.read_file("a.txt")


def test_read_file_returns_numbered_lines(workspace):
    files.write_file("n.txt", "a\nb\nc")
    out = files.read_file("n.txt")
    assert "1  a" in out
    assert "3  c" in out
    assert "(显示" not in out  # 全部内容一次读完，不加范围提示


def test_read_file_offset_limit(workspace):
    files.write_file("m.txt", "\n".join(f"line{i}" for i in range(1, 101)))
    out = files.read_file("m.txt", offset=50, limit=10)
    assert "50  line50" in out
    assert "59  line59" in out
    assert "line60" not in out
    assert "共 100 行" in out


def test_read_file_missing_file_friendly_error(workspace):
    assert "文件不存在" in files.read_file("nope.txt")


def test_read_file_rejects_binary(workspace):
    (workspace / "bin.dat").write_bytes(b"\x00\x01binary")
    assert "二进制" in files.read_file("bin.dat")


def test_read_file_rejects_directory(workspace):
    (workspace / "sub").mkdir()
    assert "list_dir" in files.read_file("sub")


def test_write_creates_parent_dirs(workspace):
    files.write_file("sub/dir/b.txt", "x")
    assert (workspace / "sub" / "dir" / "b.txt").read_text(encoding="utf-8") == "x"


def test_edit_file_unique_match(workspace):
    files.write_file("c.txt", "hello world")
    assert "已编辑" in files.edit_file("c.txt", "world", "python")
    assert "hello python" in files.read_file("c.txt")


def test_edit_file_requires_unique_match(workspace):
    files.write_file("d.txt", "abc abc")
    assert "匹配了 2 处" in files.edit_file("d.txt", "abc", "x")
    assert "未找到" in files.edit_file("d.txt", "xyz", "x")


def test_edit_file_multi_match_error_shows_lines(workspace):
    files.write_file("d2.txt", "abc\nabc\nabc")
    err = files.edit_file("d2.txt", "abc", "x")
    assert "匹配了 3 处" in err
    assert "第 1 行" in err and "第 3 行" in err


def test_edit_file_replace_all(workspace):
    files.write_file("r.txt", "x=1\ny=1\nz=1")
    assert "已编辑" in files.edit_file("r.txt", "=1", "=2", replace_all=True)
    assert (workspace / "r.txt").read_text(encoding="utf-8") == "x=2\ny=2\nz=2"


def test_edit_file_rejects_empty_old_string(workspace):
    files.write_file("e2.txt", "hi")
    assert "不能为空" in files.edit_file("e2.txt", "", "x")


def test_edit_file_requires_read_first(workspace):
    """工具外直接落盘的文件（本会话未读过），编辑前必须先 read_file。"""
    (workspace / "f.txt").write_text("hello", encoding="utf-8")
    assert "未读取过" in files.edit_file("f.txt", "hello", "hi")
    files.read_file("f.txt")
    assert "已编辑" in files.edit_file("f.txt", "hello", "hi")


def test_write_overwrite_requires_read_first(workspace):
    """覆盖已存在的文件前必须先读过，防止覆盖未查看的内容。"""
    files.write_file("w.txt", "old")
    files.reset_read_tracking()  # 模拟新会话：已读记录清空
    err = files.write_file("w.txt", "new")
    assert "未读取过" in err
    assert (workspace / "w.txt").read_text(encoding="utf-8") == "old"
    assert "old" in files.read_file("w.txt")
    assert "已写入" in files.write_file("w.txt", "new")


def test_list_dir(workspace):
    files.write_file("e.txt", "")
    listing = files.list_dir()
    assert "[文件] e.txt" in listing


def test_list_dir_shows_size_and_skips_junk(workspace):
    """文件带大小标注，.git/.venv 等无关目录不出现在列表里。"""
    (workspace / ".venv").mkdir()
    (workspace / ".venv" / "hidden.txt").write_text("x", encoding="utf-8")
    files.write_file("s.txt", "abc")
    out = files.list_dir()
    assert ".venv" not in out
    assert "[文件] s.txt (3 B)" in out


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
    assert "x" in files.read_file("sub/../sub/f.txt")


# ---------- 多根授权（--add） ----------

def test_extra_root_read_and_write(workspace, monkeypatch):
    """附加授权目录内可正常读写。"""
    extra = workspace.parent / (workspace.name + "-extra")
    extra.mkdir()
    (extra / "b.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(config, "EXTRA_ROOTS", [str(extra)])

    assert "x" in files.read_file(str(extra / "b.txt"))
    assert "已写入" in files.write_file(str(extra / "c.txt"), "y")
    assert (extra / "c.txt").read_text(encoding="utf-8") == "y"


def test_dotdot_into_extra_root_allowed(workspace, monkeypatch):
    """相对主工作区的 .. 逃逸若落在附加授权目录内，应放行。"""
    extra = workspace.parent / (workspace.name + "-extra")
    extra.mkdir()
    (extra / "d.txt").write_text("z", encoding="utf-8")
    monkeypatch.setattr(config, "EXTRA_ROOTS", [str(extra)])

    assert "z" in files.read_file(f"../{extra.name}/d.txt")


def test_outside_all_roots_still_rejected(workspace, monkeypatch):
    """有附加授权目录时，未授权路径依然被拒。"""
    extra = workspace.parent / (workspace.name + "-extra")
    extra.mkdir()
    monkeypatch.setattr(config, "EXTRA_ROOTS", [str(extra)])

    with pytest.raises(PermissionError):
        files.read_file(str(workspace.parent / "unrelated.txt"))

# ---------- 权限确认的 diff 预览 ----------

def test_preview_write_shows_unified_diff(workspace):
    files.write_file("a.txt", "hello\n")
    detail = files._preview_write({"path": "a.txt", "content": "hello world\n"})
    assert "--- a/a.txt" in detail
    assert "-hello" in detail
    assert "+hello world" in detail


def test_preview_write_new_file_all_additions(workspace):
    detail = files._preview_write({"path": "new.txt", "content": "x\ny\n"})
    assert detail.startswith("--- a/new.txt")
    assert "+x" in detail
    assert "+y" in detail


def test_preview_write_identical_content_none(workspace):
    files.write_file("same.txt", "abc\n")
    assert files._preview_write({"path": "same.txt", "content": "abc\n"}) is None


def test_preview_write_missing_args_none(workspace):
    assert files._preview_write({}) is None


def test_preview_write_outside_workspace_none(workspace):
    assert files._preview_write({"path": "../outside.txt", "content": "x"}) is None


def test_preview_write_env_file_hidden(workspace):
    """禁读文件（.env）不生成预览，避免密钥回显终端。"""
    (workspace / ".env").write_text("SECRET=1\n", encoding="utf-8")
    assert files._preview_write({"path": ".env", "content": "SECRET=2\n"}) is None


def test_preview_edit_shows_replacement(workspace):
    files.write_file("c.py", "x = 1\ny = 2\n")
    detail = files._preview_edit({"path": "c.py", "old_string": "y = 2", "new_string": "y = 3"})
    assert "-y = 2" in detail
    assert "+y = 3" in detail


def test_preview_edit_replace_all(workspace):
    files.write_file("d.txt", "a\nb\na\n")
    detail = files._preview_edit(
        {"path": "d.txt", "old_string": "a", "new_string": "z", "replace_all": True}
    )
    assert detail.count("-a") == 2
    assert detail.count("+z") == 2


def test_preview_edit_not_found_none(workspace):
    files.write_file("e.txt", "abc")
    assert files._preview_edit({"path": "e.txt", "old_string": "zzz", "new_string": "q"}) is None
