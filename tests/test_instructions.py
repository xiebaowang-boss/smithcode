"""instructions 测试：AGENTS.md 装载、优先级、指纹刷新、预算截断与容错。

全部经 SMITHCODE_HOME + monkeypatch WORKSPACE_ROOT 隔离，不读真实家目录。
"""

import os
from pathlib import Path

import pytest

from smithcode import config, instructions


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每个用例独立的 home / workspace，并清空模块缓存。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    instructions.reset()
    yield workspace, home
    instructions.reset()


def _write(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)
    return path


def _touch_bump(path: Path) -> None:
    """把 mtime 前推，规避文件系统 mtime 精度（内容等长时也能触发重载）。"""
    st = path.stat()
    os.utime(path, (st.st_atime + 5, st.st_mtime + 5))


# ---------- 装载与优先级 ----------

def test_no_files_renders_empty(_isolated):
    assert instructions.render_section() == ""


def test_user_then_project_priority_order(_isolated):
    workspace, home = _isolated
    _write(home / "AGENTS.md", "用户级约定 U")
    _write(workspace / "AGENTS.md", "项目级约定 P")
    instructions.refresh()

    section = instructions.render_section()
    assert section.startswith("## 项目约定")
    assert 'scope="用户"' in section and 'scope="项目"' in section
    assert section.index("用户级约定 U") < section.index("项目级约定 P")


def test_config_paths_appended_last_with_relative_and_absolute(_isolated, tmp_path):
    workspace, home = _isolated
    _write(home / "AGENTS.md", "U-MARK")
    _write(workspace / "AGENTS.md", "P-MARK")
    _write(workspace / "docs" / "team.md", "REL-MARK")
    abs_path = _write(tmp_path / "outside" / "notes.md", "ABS-MARK")
    _write(home / "config.toml", f'[instructions]\npaths = ["docs/team.md", "{abs_path.as_posix()}"]\n')

    instructions.refresh()
    section = instructions.render_section()
    assert section.index("U-MARK") < section.index("P-MARK")
    assert section.index("P-MARK") < section.index("REL-MARK")
    assert section.index("REL-MARK") < section.index("ABS-MARK")
    assert 'scope="附加"' in section


def test_same_path_listed_twice_deduped(_isolated):
    workspace, home = _isolated
    _write(workspace / "AGENTS.md", "唯一内容")
    _write(home / "config.toml", '[instructions]\npaths = ["AGENTS.md"]\n')
    instructions.refresh()
    assert instructions.render_section().count("唯一内容") == 1


def test_files_config_probes_custom_names(_isolated):
    workspace, home = _isolated
    _write(home / "CLAUDE.md", "CLAUDE-MARK")
    _write(workspace / "AGENTS.md", "默认名不该被读")
    _write(home / "config.toml", '[instructions]\nfiles = ["CLAUDE.md"]\n')
    instructions.refresh()

    section = instructions.render_section()
    assert "CLAUDE-MARK" in section
    assert "默认名不该被读" not in section


def test_files_empty_list_keeps_only_explicit_paths(_isolated):
    workspace, home = _isolated
    _write(workspace / "AGENTS.md", "默认名不该被读")
    _write(workspace / "extra.md", "EXTRA-MARK")
    _write(home / "config.toml", '[instructions]\nfiles = []\npaths = ["extra.md"]\n')
    instructions.refresh()

    section = instructions.render_section()
    assert "EXTRA-MARK" in section
    assert "默认名不该被读" not in section


def test_disabled_renders_empty(_isolated):
    workspace, home = _isolated
    _write(workspace / "AGENTS.md", "不应注入")
    _write(home / "config.toml", "[instructions]\nenabled = false\n")
    instructions.refresh()
    assert instructions.render_section() == ""


# ---------- 项目链（向上到 git 根） ----------

def test_project_chain_walks_up_to_git_root(tmp_path, monkeypatch):
    """项目级从 git 根逐级向下探测到工作区，越深越靠后（更具体）。"""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    _write(root / "AGENTS.md", "仓库根约定")
    _write(root / "packages" / "AGENTS.md", "中间层约定")
    deep = root / "packages" / "web"
    deep.mkdir(parents=True)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(deep))

    instructions.reset()
    instructions.refresh()
    section = instructions.render_section()
    assert "仓库根约定" in section
    assert "中间层约定" in section
    assert section.index("仓库根约定") < section.index("中间层约定")


def test_git_file_marker_stops_ancestor_walk(tmp_path, monkeypatch):
    """`.git` 为文件（worktree）同样视为仓库根，且不越过它继续向上。"""
    outside = tmp_path / "outside"
    _write(outside / "AGENTS.md", "仓库外约定")
    repo = outside / "repo"
    repo.mkdir(parents=True)
    (repo / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    workspace = repo / "sub"
    workspace.mkdir()
    _write(workspace / "AGENTS.md", "工作区约定")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(workspace))

    instructions.reset()
    instructions.refresh()
    section = instructions.render_section()
    assert "工作区约定" in section
    assert "仓库外约定" not in section


def test_workspace_is_git_root_ignores_parents(tmp_path, monkeypatch):
    """工作区自身是仓库根：父目录的指令文件不参与探测。"""
    parent = tmp_path / "space"
    _write(parent / "AGENTS.md", "父目录约定")
    workspace = parent / "repo"
    (workspace / ".git").mkdir(parents=True)
    _write(workspace / "AGENTS.md", "工作区约定")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(workspace))

    instructions.reset()
    instructions.refresh()
    section = instructions.render_section()
    assert "工作区约定" in section
    assert "父目录约定" not in section


# ---------- 指纹与刷新 ----------

def test_refresh_detects_change_and_is_stable(_isolated):
    workspace, _ = _isolated
    path = _write(workspace / "AGENTS.md", "v1 内容")
    assert instructions.refresh() is True
    first = instructions.render_section()

    assert instructions.refresh() is False
    assert instructions.render_section() == first

    _write(path, "version-2 改后的内容")
    _touch_bump(path)
    assert instructions.refresh() is True
    second = instructions.render_section()
    assert "version-2" in second
    assert second != first


def test_deleted_file_removes_section(_isolated):
    workspace, _ = _isolated
    path = _write(workspace / "AGENTS.md", "临时约定")
    instructions.refresh()
    assert "临时约定" in instructions.render_section()

    path.unlink()
    assert instructions.refresh() is True
    assert instructions.render_section() == ""


def test_render_section_lazy_loads(_isolated):
    workspace, _ = _isolated
    _write(workspace / "AGENTS.md", "懒加载")
    assert "懒加载" in instructions.render_section()


# ---------- 预算 ----------

def test_budget_keeps_high_priority_and_truncates_low(_isolated):
    workspace, home = _isolated
    long_text = "用户长文内容" * 300  # 1800 字符
    _write(home / "AGENTS.md", long_text)
    _write(workspace / "AGENTS.md", "项目短约定")
    _write(home / "config.toml", "[instructions]\nmax_chars = 1000\n")
    instructions.refresh()

    section = instructions.render_section()
    assert section.startswith("## 项目约定")
    assert "项目短约定" in section  # 高优先级完整保留
    assert "内容过长，此处截断" in section  # 低优先级被截断
    assert long_text[:40] in section  # 截断保留头部
    assert len(section) <= 1000


def test_budget_drops_files_without_room(_isolated):
    workspace, home = _isolated
    _write(home / "AGENTS.md", "低优先级内容" * 200)
    _write(workspace / "AGENTS.md", "高优先级内容" * 200)
    _write(home / "config.toml", "[instructions]\nmax_chars = 400\n")
    instructions.refresh()

    section = instructions.render_section()
    assert "因预算未加载" in section


# ---------- 容错 ----------

def test_oversize_file_skipped_with_warning(_isolated, monkeypatch, capsys):
    workspace, _ = _isolated
    monkeypatch.setattr(instructions, "MAX_FILE_BYTES", 8)
    _write(workspace / "AGENTS.md", "0123456789")
    instructions.refresh()
    assert instructions.render_section() == ""
    assert "过大" in capsys.readouterr().out


def test_bom_and_invalid_bytes_tolerated(_isolated):
    workspace, _ = _isolated
    (workspace / "AGENTS.md").write_bytes(b"\xef\xbb\xbfBOM-OK \xff\xfe END-OK")
    instructions.refresh()
    section = instructions.render_section()
    assert "BOM-OK" in section
    assert "END-OK" in section
    assert "\ufffd" in section  # 非法字节被替换，不抛异常


def test_blank_file_ignored(_isolated):
    workspace, _ = _isolated
    _write(workspace / "AGENTS.md", "   \n\n  \n")
    instructions.refresh()
    assert instructions.render_section() == ""


def test_explicit_missing_path_warns_once(_isolated, capsys):
    _, home = _isolated
    _write(home / "config.toml", '[instructions]\npaths = ["nope/missing.md"]\n')
    instructions.refresh()
    assert "警告" in capsys.readouterr().out

    instructions.refresh(force=True)
    assert "警告" not in capsys.readouterr().out  # 同一消息只警告一次


def test_explicit_directory_path_warns(_isolated, capsys):
    workspace, home = _isolated
    (workspace / "docs").mkdir()
    _write(home / "config.toml", '[instructions]\npaths = ["docs"]\n')
    instructions.refresh()
    assert "不是普通文件" in capsys.readouterr().out


def test_unreadable_explicit_file_warns(_isolated, monkeypatch, capsys):
    workspace, home = _isolated
    target = _write(workspace / "notes.md", "内容").resolve()
    _write(home / "config.toml", '[instructions]\npaths = ["notes.md"]\n')
    real_read_text = Path.read_text

    def guarded(self, *args, **kwargs):
        if self.resolve() == target:
            raise OSError("拒绝访问")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    instructions.refresh()
    assert "无法读取" in capsys.readouterr().out


def test_default_missing_files_are_silent(_isolated, capsys):
    instructions.refresh()
    assert capsys.readouterr().out == ""
