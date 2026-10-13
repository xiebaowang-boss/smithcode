"""会话工作区：跨项目恢复的沙箱落点、切项目只影响新建、附加目录直达 serve 会话。

三条不变量，都是「文件面板/工具看到的目录」这条链上的：
1. 恢复 B 项目的会话 → 沙箱根必须是 B，不是当前进程默认目录；
2. `POST /workspace` 只改新建落点，已建会话的快照不许被追改；
3. `--add` 附加目录进得去 serve 会话（曾经被 `extra or []` 静默丢掉）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


def _call(base, token, method, path, body=None):
    data = json.dumps(body or {}).encode() if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _mk_session(base, token, workspace, text="你好"):
    """在指定工作区建会话并跑一轮：日志懒物化，不发 prompt 就没有文件可列。"""
    status, body = _call(base, token, "POST", "/sessions", {"workspace": str(workspace)})
    assert status == 200, body
    sid = body["id"]
    status, body = _call(base, token, "POST", f"/sessions/{sid}/prompt", {"text": text})
    assert status == 200, body
    return sid


def test_resume_carries_session_workspace(served, tmp_path):
    """从默认项目恢复 B 项目的会话：沙箱根跟着会话走，不落回默认目录。"""
    base, token, state = served
    proj = tmp_path / "proj-b"
    proj.mkdir()
    sid = _mk_session(base, token, proj)

    state.drop(sid)  # 卸载成离线：在线复用走的是另一条路，测不到推导
    status, body = _call(base, token, "POST", "/sessions", {"resume": sid})
    assert status == 200, body
    assert body["id"] == sid

    status, ws = _call(base, token, "GET", f"/sessions/{sid}/workspace")
    assert status == 200, ws
    assert ws["cwd"] == str(proj.resolve())
    assert ws["name"] == "proj-b"

    # 在线列表也带沙箱根（前端按会话定位文件面板，不读进程默认）
    status, body = _call(base, token, "GET", "/sessions")
    assert status == 200
    online = next(s for s in body["sessions"] if s["id"] == sid)
    assert online["workspace"] == str(proj.resolve())


def test_resume_target_failure_is_not_masked(served):
    """沙箱推导不许吞掉恢复自己的报错：定位不到就回退默认，报错照旧回给前端。"""
    base, token, _ = served

    status, body = _call(base, token, "POST", "/sessions", {"resume": "nope-not-a-session"})
    assert status == 500, body
    assert "未找到会话" in body["error"]


def test_post_workspace_switches_default_only(served, tmp_path):
    """切项目 = 改进程默认：新建落点变了，已建会话的快照原地不动。"""
    base, token, _ = served
    proj = tmp_path / "proj-a"
    proj.mkdir()
    old = _mk_session(base, token, tmp_path)  # 切换前建在默认目录

    status, body = _call(base, token, "POST", "/workspace", {"path": str(proj)})
    assert status == 200, body
    assert body["ok"] and body["cwd"] == str(proj.resolve())

    # 新建会话落在新项目
    status, body = _call(base, token, "POST", "/sessions", {})
    assert status == 200, body
    new = body["id"]
    status, ws = _call(base, token, "GET", f"/sessions/{new}/workspace")
    assert ws["cwd"] == str(proj.resolve())

    # 已建会话不被追改（sandbox.fresh 快照）
    status, ws = _call(base, token, "GET", f"/sessions/{old}/workspace")
    assert status == 200
    assert ws["cwd"] == str(tmp_path.resolve())

    # 默认工作区信息与历史默认项目跟着走
    status, body = _call(base, token, "GET", "/workspace")
    assert body["cwd"] == str(proj.resolve())


def test_post_workspace_validates_path(served, tmp_path):
    base, token, _ = served

    status, body = _call(base, token, "POST", "/workspace", {})
    assert status == 400 and "缺少 path" in body["error"]

    status, body = _call(base, token, "POST", "/workspace",
                         {"path": str(tmp_path / "ghost")})
    assert status == 400 and "目录不存在" in body["error"]


def test_session_workspace_unknown_id_is_404(served):
    base, token, _ = served
    status, body = _call(base, token, "GET", "/sessions/nonexistent/workspace")
    assert status == 404 and "会话不存在" in body["error"]


def test_extra_roots_reach_serve_sessions(served, tmp_path, monkeypatch):
    """`--add` 的附加目录要进 serve 会话：曾经被 `extra or []` 悄悄丢掉。"""
    from smithcode import config

    base, token, _ = served
    extra = tmp_path / "shared"
    extra.mkdir()
    monkeypatch.setattr(config, "EXTRA_ROOTS", [str(extra)])

    status, body = _call(base, token, "POST", "/sessions", {})
    assert status == 200, body
    status, ws = _call(base, token, "GET", f"/sessions/{body['id']}/workspace")
    assert status == 200
    assert [str(p) for p in ws["extra"]] == [str(extra)]

    # 显式 extra 覆盖 config（请求里指定了就用指定的）
    other = tmp_path / "other"
    other.mkdir()
    status, body = _call(base, token, "POST", "/sessions", {"extra": [str(other)]})
    assert status == 200, body
    status, ws = _call(base, token, "GET", f"/sessions/{body['id']}/workspace")
    assert [str(p) for p in ws["extra"]] == [str(other)]
