"""多项目工作区的 HTTP 面：项目列表、按项目查历史、跨项目查全、项目注册表。

slug 由 `project_slug()` 生成（可读前缀 + sha1 后缀）是**不可逆**的，这些用例盯的
就是「slug ↔ 目录」这条反查链——它断了，侧栏就只能看见当前项目，跨项目历史恒为空。
"""

from __future__ import annotations

import json
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
    """在一个工作区里建会话并跑一轮：日志懒物化，不发 prompt 就没有文件可列。"""
    status, body = _call(base, token, "POST", "/sessions", {"workspace": str(workspace)})
    assert status == 200, body
    sid = body["id"]
    status, body = _call(base, token, "POST", f"/sessions/{sid}/prompt", {"text": text})
    assert status == 200, body
    return sid


def test_projects_endpoint_carries_cwd_and_name(served, tmp_path):
    from smithcode.sessions import paths

    base, token, _ = served
    proj = tmp_path / "proj-a"
    proj.mkdir()
    _mk_session(base, token, proj)

    status, body = _call(base, token, "GET", "/projects")
    assert status == 200
    slug = paths.project_slug(str(proj.resolve()))
    entry = next((p for p in body["projects"] if p["slug"] == slug), None)
    assert entry is not None, body["projects"]
    assert entry["cwd"] == str(proj.resolve())
    assert entry["name"] == "proj-a"
    assert entry["sessionCount"] == 1
    assert entry["updated"] > 0


def test_post_projects_registers_directory(served, tmp_path):
    base, token, _ = served
    proj = tmp_path / "proj-b"
    proj.mkdir()

    status, body = _call(base, token, "POST", "/projects", {"path": str(proj)})
    assert status == 200, body
    assert body["cwd"] == str(proj.resolve()) and body["slug"]
    slug = body["slug"]

    # 还没建会话也要看得见（「打开目录…」后立即出现在侧栏）
    status, body = _call(base, token, "GET", "/projects")
    entry = next(p for p in body["projects"] if p["slug"] == slug)
    assert entry["cwd"] == str(proj.resolve()) and entry["sessionCount"] == 0

    # 不存在 / 空参数按输入问题回 400
    status, body = _call(base, token, "POST", "/projects",
                         {"path": str(tmp_path / "ghost")})
    assert status == 400
    status, body = _call(base, token, "POST", "/projects", {})
    assert status == 400


def test_history_filters_by_project(served, tmp_path):
    from smithcode.sessions import paths

    base, token, _ = served
    proj = tmp_path / "proj-a"
    proj.mkdir()
    sid_a = _mk_session(base, token, proj)
    sid_b = _mk_session(base, token, tmp_path)  # 默认工作区（fixture 指向 tmp_path）

    # 默认：当前项目（tmp_path），不含 proj-a 的会话
    status, body = _call(base, token, "GET", "/sessions/history")
    assert status == 200
    ids = {s["id"] for s in body["sessions"]}
    assert sid_a not in ids and sid_b in ids

    # 按项目查：只回那个项目的会话（slug 反查目录后再列，而不是拿 slug 比对 cwd）
    slug = paths.project_slug(str(proj.resolve()))
    status, body = _call(base, token, "GET", f"/sessions/history?project={slug}")
    assert status == 200
    assert [s["id"] for s in body["sessions"]] == [sid_a]

    # 未知项目按输入问题回 404，不静默返回空列表（否则前端显示成空会话）
    status, body = _call(base, token, "GET", "/sessions/history?project=nope")
    assert status == 404 and "未知项目" in body["error"]


def test_history_all_lists_across_projects(served, tmp_path):
    base, token, _ = served
    proj = tmp_path / "proj-a"
    proj.mkdir()
    sid_a = _mk_session(base, token, proj)
    sid_b = _mk_session(base, token, tmp_path)

    status, body = _call(base, token, "GET", "/sessions/history?all=1&limit=50")
    assert status == 200
    ids = {s["id"] for s in body["sessions"]}
    assert {sid_a, sid_b} <= ids


def test_registry_self_heals_from_session_log(served, tmp_path):
    """注册表是加速表：删掉也要能从会话日志回填 cwd，历史不因此失忆。"""
    from smithcode.sessions import paths, resolve_project

    base, token, _ = served
    proj = tmp_path / "proj-a"
    proj.mkdir()
    _mk_session(base, token, proj)
    slug = paths.project_slug(str(proj.resolve()))
    assert paths.read_project(slug) == str(proj.resolve())  # 建会话时已登记

    paths.registry_path().unlink()
    assert paths.read_project(slug) == ""

    assert resolve_project(slug) == str(proj.resolve())
    assert paths.read_project(slug) == str(proj.resolve())  # 回填后又有了

    # 回填的守卫：目录与日志 cwd 对不上时宁可返回空，也不登记假映射
    assert resolve_project("some-other-slug") == ""


def test_history_legacy_project_dir_self_heals(served, tmp_path):
    """事件架构之前的旧格式项目（只在磁盘上有日志、注册表没登记）：

    项目列表会把它列出来（扫盘按会话数），按它查历史却 404「未知项目」——
    侧栏展开就出红字「无法加载该目录的会话」。摘要扫描认旧格式后自愈。
    """
    from smithcode.sessions import paths

    base, token, _ = served
    proj = tmp_path / "old-proj"
    proj.mkdir()
    slug = paths.project_slug(str(proj.resolve()))
    sess_dir = paths.projects_dir() / slug / "sessions"
    sess_dir.mkdir(parents=True)
    legacy = [
        {"v": 1, "t": "meta", "id": "oldsess0001", "cwd": str(proj.resolve()),
         "created": 1789801945.5, "model": "m-old", "effort": "high",
         "oneshot": False},
        {"v": 1, "t": "msg", "m": {"role": "user", "content": "你好"}},
    ]
    (sess_dir / "oldsess0001.jsonl").write_text(
        "\n".join(json.dumps(r) for r in legacy) + "\n", encoding="utf-8")

    # 展开项目走的那条路：按 slug 查历史（回填 cwd 后应 200 且列出旧会话）
    status, body = _call(base, token, "GET", f"/sessions/history?project={slug}")
    assert status == 200, body
    assert [s["id"] for s in body["sessions"]] == ["oldsess0001"]
    assert body["sessions"][0]["cwd"] == str(proj.resolve())
    assert paths.read_project(slug) == str(proj.resolve())  # 顺带回填注册表


def test_projects_always_contains_default_workspace(served, tmp_path):
    """没有会话也没登记的默认工作区也要在列表里：侧栏按 cwd 反查 slug 用它高亮。"""
    base, token, _ = served

    status, body = _call(base, token, "GET", "/workspace")
    assert status == 200
    cwd = body["cwd"]
    assert cwd == str(tmp_path.resolve())

    status, body = _call(base, token, "GET", "/projects")
    assert status == 200
    entry = next((p for p in body["projects"] if p["cwd"] == cwd), None)
    assert entry is not None, body["projects"]
    assert entry["slug"] and entry["name"] and entry["sessionCount"] == 0

    # 列得出来就得查得动：展开这个文件夹时按 slug 查历史不能 404
    status, body = _call(base, token, "GET", f"/sessions/history?project={entry['slug']}")
    assert status == 200, body
    assert body["sessions"] == []
