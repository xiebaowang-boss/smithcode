"""`GET /sessions/{id}/skills`：会话级技能投影。

要点是**读会话持有的缓存**而不是触发扫盘：`skills.*` 模块函数按 ContextVar
解析，HTTP 线程上会落到进程默认实例，等于每次开面板扫一遍磁盘（非交互下项目
技能还会 fail-closed 跳过）。这里用「discover 一旦被调用就失败」的哨兵守住它。
"""

from __future__ import annotations

from pathlib import Path


def _sentinel_discover(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("技能端点不该触发磁盘扫描")

    monkeypatch.setattr("smithcode.skills.registry.discover", boom)


def test_skills_payload_reads_session_cache(served, serve_call, monkeypatch, tmp_path):
    base, token, state = served
    _, body = serve_call(base, token, "POST", "/sessions", {})
    sid = body["id"]
    entry = state.get(sid)

    from smithcode.skills.registry import Skill

    base_dir = tmp_path / "skills" / "demo"
    base_dir.mkdir(parents=True)
    (base_dir / "SKILL.md").write_text("# demo", encoding="utf-8")
    (base_dir / "notes.md").write_text("note", encoding="utf-8")
    skill = Skill(
        name="demo", description="演示技能", location=base_dir / "SKILL.md",
        base=base_dir, root=tmp_path / "skills", scope="user", body="正文内容",
    )
    skills_state = entry.agent.session_owner.skills_state
    skills_state.skills = [skill]
    skills_state.index = {"demo": skill}
    skills_state.loaded = True
    skills_state.diagnostics = ["一条诊断"]

    _sentinel_discover(monkeypatch)
    status, body = serve_call(base, token, "GET", f"/sessions/{sid}/skills")
    assert status == 200
    assert body["loaded"] is True and body["diagnostics"] == ["一条诊断"]
    item = body["skills"][0]
    assert item["name"] == "demo" and item["description"] == "演示技能"
    assert item["active"] is False and item["model_invocable"] is True
    assert item["body"] == "正文内容"
    assert item["resources"] == ["notes.md"]  # SKILL.md 自身不进资源列表
    assert Path(item["location"]).name == "SKILL.md"

    # 载入标记跟着会话状态走
    skills_state.active.append("demo")
    status, body = serve_call(base, token, "GET", f"/sessions/{sid}/skills")
    assert body["skills"][0]["active"] is True


def test_skills_unknown_session(served, serve_call):
    base, token, _ = served
    status, body = serve_call(base, token, "GET", "/sessions/nope/skills")
    assert status == 404 and "不存在" in body["error"]
