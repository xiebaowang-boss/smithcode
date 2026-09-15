"""文件工具测试：正常读写、精确编辑、路径越界拦截。"""
import re

import pytest

from smithcode import config
from smithcode.tools import files, search


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
    assert "1│a" in out
    assert "3│c" in out
    assert "1  a" not in out  # 分隔符不是空格：避免被误当成正文缩进（old_string 复制陷阱）
    assert "(显示" not in out  # 全部内容一次读完，不加范围提示


def test_read_file_offset_limit(workspace):
    files.write_file("m.txt", "\n".join(f"line{i}" for i in range(1, 101)))
    out = files.read_file("m.txt", offset=50, limit=10)
    assert "50│line50" in out
    assert "59│line59" in out
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


def test_edit_file_accepts_multiline_anchor_copied_from_grep(workspace):
    """回归：grep 输出曾被 strip 掉行首缩进，照抄成多行 old_string 会匹配不上文件。"""
    files.write_file("a.py", "def m():\n    warn = log\n    error = log\n")
    out = search.grep("warn = log|error = log", path="a.py")
    # 「路径:行号: 内容」去掉前缀后应逐字等于原文（含缩进）
    anchor = "\n".join(line.split(": ", 1)[1] for line in out.splitlines())
    assert anchor == "    warn = log\n    error = log"

    assert "已编辑" in files.edit_file("a.py", anchor, anchor + "\n    return 1")
    assert "return 1" in (workspace / "a.py").read_text(encoding="utf-8")


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
    assert "e.txt" in listing
    assert "[文件]" not in listing and "[目录]" not in listing  # 不再用类型前缀


def test_list_dir_shows_size_and_skips_junk(workspace):
    """文件带大小标注，.git/.venv 等无关目录不出现在列表里。"""
    (workspace / ".venv").mkdir()
    (workspace / ".venv" / "hidden.txt").write_text("x", encoding="utf-8")
    files.write_file("s.txt", "abc")
    out = files.list_dir()
    assert ".venv" not in out
    assert "s.txt" in out
    assert "3 B" in out


def test_list_dir_dirs_first_then_files_aligned(workspace):
    """目录在前（以 / 结尾），文件在后，名称/大小/修改时间三列对齐。"""
    (workspace / "sub").mkdir()
    files.write_file("a.txt", "x")
    files.write_file("longer_name.txt", "xx")
    lines = files.list_dir().splitlines()
    assert lines[0].startswith("sub/")
    file_lines = lines[1:]
    assert len(file_lines) == 2
    assert all("B" in line for line in file_lines)
    assert len(file_lines[0]) == len(file_lines[1])  # 三列对齐后两行等长
    # 每行都以本地时间 YYYY-MM-DD HH:MM 收尾
    assert all(re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", line) for line in lines)


def test_list_dir_aligns_cjk_names_by_display_width(workspace):
    """中文文件名按终端显示宽度（全角 2 列）对齐，不因字符数少而错位。"""
    files.write_file("中文.txt", "x")
    files.write_file("abcdef.txt", "x")
    lines = files.list_dir().splitlines()
    assert len(lines) == 2
    assert files._display_width(lines[0]) == files._display_width(lines[1])


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


# ---------- 换行风格与 BOM 保真 ----------

def test_read_file_hides_line_endings(workspace):
    """模型只看到 LF：无需知道目标文件是 CRLF，写回由工具还原原格式。"""
    (workspace / "c.txt").write_bytes(b"a\r\nb\r\n")
    assert files.read_file("c.txt") == "1│a\n2│b"


def test_edit_file_preserves_crlf_bytes(workspace):
    """回归：曾把整个 CRLF 文件的换行重写成 LF——只改一行却全文件 diff。"""
    (workspace / "c.txt").write_bytes(b"a\r\nb\r\nc\r\n")
    files.read_file("c.txt")
    assert "已编辑" in files.edit_file("c.txt", "b", "B")
    assert (workspace / "c.txt").read_bytes() == b"a\r\nB\r\nc\r\n"


def test_edit_file_preserves_bom(workspace):
    (workspace / "b.txt").write_bytes(b"\xef\xbb\xbfhello\n")
    assert "\ufeff" not in files.read_file("b.txt")  # BOM 不进正文
    files.edit_file("b.txt", "hello", "hi")
    assert (workspace / "b.txt").read_bytes() == b"\xef\xbb\xbfhi\n"


def test_write_file_preserves_existing_crlf(workspace):
    """覆盖已有 CRLF 文件时沿用原文风格，不按 os.linesep 翻译。"""
    (workspace / "w.txt").write_bytes(b"x\r\ny\r\n")
    files.read_file("w.txt")
    files.write_file("w.txt", "x\nZ\n")
    assert (workspace / "w.txt").read_bytes() == b"x\r\nZ\r\n"


def test_write_file_new_file_uses_lf(workspace):
    """新建文件默认 LF（平台无关），不随 os.linesep 漂移。"""
    files.write_file("n.txt", "a\nb\n")
    assert (workspace / "n.txt").read_bytes() == b"a\nb\n"


def test_edit_file_non_utf8_returns_friendly_error(workspace):
    """非 UTF-8 返回中文错误串，不抛裸 UnicodeDecodeError（会中断 agent 循环）。"""
    files.write_file("latin.py", "x = 1\n")  # 先记录为已读
    (workspace / "latin.py").write_bytes(b"# caf\xe9\nx = 1\n")  # 外部改成非 UTF-8
    assert "UTF-8" in files.edit_file("latin.py", "x = 1", "x = 2")


# ---------- 技能目录只读白名单 ----------

def test_skill_read_root_readable_but_not_writable(workspace, tmp_path, monkeypatch):
    """技能目录在授权目录之外时：读放行（免越界确认），写仍被沙箱拒绝。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(ws))
    skills_root = tmp_path / "ext-skills"
    skill_dir = skills_root / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "reference.md").write_text("参考资料", encoding="utf-8")
    config.set_skill_roots([skills_root])
    try:
        assert "参考资料" in files.read_file(str(skill_dir / "reference.md"))
        with pytest.raises(PermissionError):
            files.write_file(str(skill_dir / "new.md"), "x")
        with pytest.raises(PermissionError):
            files.edit_file(str(skill_dir / "reference.md"), "参考", "x")
    finally:
        config.set_skill_roots([])
