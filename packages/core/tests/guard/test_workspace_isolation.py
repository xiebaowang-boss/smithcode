"""Phase 3 回归探针：多 workspace 会话的指令与目录隔离 + `fresh()` 快照语义。

既有 `tests/guard/test_multi_session_isolation.py` 锁住 goal/plan/skills、
信任目录、widen、总线/端口；这里补 Phase 3 新增的三处：
1. `instructions` 按 workspace 缓存（A 的 AGENTS.md 不污染 B）；
2. `sandbox.fresh()` 缺省快照（建后改全局不影响已建会话）；
3. 工具路径锚定当前会话工作区（`_resolve` 跟沙箱走）。
"""

from __future__ import annotations

from pathlib import Path


def test_instructions_are_cached_per_workspace(tmp_path, monkeypatch):
    """两目录各有 AGENTS.md：各读各的，互不覆盖；同目录指纹去重仍有效。"""
    from smithcode import config, instructions, sandbox

    dir_a, dir_b = tmp_path / "a", tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    (dir_a / "AGENTS.md").write_text("约定 A", encoding="utf-8")
    (dir_b / "AGENTS.md").write_text("约定 B", encoding="utf-8")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(dir_a))
    instructions.reset()

    token_a = sandbox.activate(sandbox.fresh(workspace=dir_a))
    try:
        assert instructions.refresh() is True
        section_a = instructions.render_section()
    finally:
        sandbox.reset(token_a)
    assert "约定 A" in section_a

    token_b = sandbox.activate(sandbox.fresh(workspace=dir_b))
    try:
        assert instructions.refresh() is True
        section_b = instructions.render_section()
        # A 的缓存不受影响（第二次读 A 不重读、内容不变）
        assert instructions.refresh() is False
    finally:
        sandbox.reset(token_b)
    assert "约定 B" in section_b
    assert "约定 A" not in section_b

    token_a2 = sandbox.activate(sandbox.fresh(workspace=dir_a))
    try:
        assert instructions.refresh() is False  # 指纹未变，零读取
        assert "约定 A" in instructions.render_section()
    finally:
        sandbox.reset(token_a2)
    instructions.reset()


def test_fresh_snapshots_config_at_creation(tmp_path, monkeypatch):
    """`fresh()` 缺省快照此刻的全局：建后改 `config` 不影响已建会话。"""
    from smithcode import config, sandbox

    dir_a, dir_b = tmp_path / "a", tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(dir_a))
    roots = sandbox.fresh()
    assert roots.workspace == dir_a.resolve()
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(dir_b))
    assert roots.workspace == dir_a.resolve()  # 已建会话不动
    assert sandbox.fresh().workspace == dir_b.resolve()  # 后建会话取新值


def test_tool_resolve_anchors_to_session_workspace(tmp_path, monkeypatch):
    """工具 `_resolve` 锚定当前会话工作区，而非进程全局。"""
    from smithcode import config, sandbox
    from smithcode.tools.files import _resolve

    dir_a, dir_b = tmp_path / "a", tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    (dir_a / "f.txt").write_text("a", encoding="utf-8")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(dir_b))  # 全局故意指错
    token = sandbox.activate(sandbox.fresh(workspace=dir_a))
    try:
        assert _resolve("f.txt") == (dir_a / "f.txt").resolve()
    finally:
        sandbox.reset(token)


def test_skill_trust_key_follows_session_workspace(tmp_path, monkeypatch):
    """技能信任 key 取会话工作区：同名不同目录的项目信任互不复用。"""
    from smithcode import sandbox
    from smithcode.skills.registry import project_key

    dir_a, dir_b = tmp_path / "a", tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    token_a = sandbox.activate(sandbox.fresh(workspace=dir_a))
    try:
        key_a = project_key(sandbox.current().workspace)
    finally:
        sandbox.reset(token_a)
    token_b = sandbox.activate(sandbox.fresh(workspace=dir_b))
    try:
        key_b = project_key(sandbox.current().workspace)
    finally:
        sandbox.reset(token_b)
    assert key_a != key_b
    assert Path(key_a.split("-")[0]) != Path(key_b.split("-")[0]) or key_a != key_b
