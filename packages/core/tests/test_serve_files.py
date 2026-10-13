"""`/fs/list` 与 `/fs/read`：工作区只读浏览的沙箱边界与截断语义。

重点不是"能不能读文件"（那是文件工具的既有测试），而是 **HTTP 面有没有绕开沙箱**：
路径必须经 `files._resolve` 判定，越界 403、二进制拒读、超限截断、目录不递归。
"""

from __future__ import annotations

import urllib.parse

import pytest


def _get(call, base, token, endpoint, **params):
    query = urllib.parse.urlencode(params)
    return call(base, token, "GET", f"{endpoint}?{query}")


def test_list_and_read_round_trip(served, serve_call, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x", encoding="utf-8")
    base, token, _ = served

    status, body = _get(serve_call, base, token, "/fs/list", path="")
    assert status == 200
    assert body["path"] == ""  # 根目录规范成空串（前端面包屑直接用）
    names = [entry["name"] for entry in body["entries"]]
    assert "src" in names
    assert ".git" not in names  # 与 list_dir 一致：依赖/缓存/版本控制目录不列
    assert body["entries"][0]["type"] == "dir"  # 目录排前面

    status, body = _get(serve_call, base, token, "/fs/list", path="src")
    assert status == 200 and body["path"] == "src"
    assert body["entries"] == [{"name": "a.py", "type": "file",
                                "size": len("print('hi')\n")}]

    status, body = _get(serve_call, base, token, "/fs/read", path="src/a.py")
    assert status == 200
    assert body["content"] == "print('hi')\n"
    assert body["truncated"] is False and body["size"] > 0


def test_read_outside_workspace_is_rejected(served, serve_call):
    base, token, _ = served
    escape = "../" * 8 + "etc/hostname"
    status, body = _get(serve_call, base, token, "/fs/read", path=escape)
    assert status == 403 and "越界" in body["error"]
    status, _ = _get(serve_call, base, token, "/fs/list", path=escape)
    assert status == 403


def test_symlink_escape_is_rejected(served, serve_call, tmp_path):
    """解析后的真实路径才算数：软链指向工作区外也要拦下。"""
    link = tmp_path / "escape-link"
    try:
        link.symlink_to(tmp_path.parent)
    except (OSError, NotImplementedError):
        pytest.skip("当前平台不允许普通用户创建软链")
    base, token, _ = served
    status, body = _get(serve_call, base, token, "/fs/list", path="escape-link")
    assert status == 403 and "越界" in body["error"]


def test_binary_and_missing_paths(served, serve_call, tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
    base, token, _ = served

    status, body = _get(serve_call, base, token, "/fs/read", path="blob.bin")
    assert status == 400 and "二进制" in body["error"]

    status, _ = _get(serve_call, base, token, "/fs/read", path="")
    assert status == 404  # 目录不当作文件读

    status, body = _get(serve_call, base, token, "/fs/read", path="missing.txt")
    assert status == 404 and "不存在" in body["error"]


def test_read_truncates_long_files(served, serve_call, tmp_path, monkeypatch):
    from smithcode import config

    monkeypatch.setattr(config, "MAX_TOOL_OUTPUT", 200)
    (tmp_path / "long.txt").write_text("每一行都不长\n" * 200, encoding="utf-8")
    base, token, _ = served
    status, body = _get(serve_call, base, token, "/fs/read", path="long.txt")
    assert status == 200 and body["truncated"] is True
    assert "输出过长" in body["content"]
    assert len(body["content"]) < body["size"]


def test_list_truncates_huge_directories(served, serve_call, tmp_path, monkeypatch):
    import smithcode.serve as serve_mod

    monkeypatch.setattr(serve_mod, "_FS_LIST_MAX", 3)
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
    base, token, _ = served
    status, body = _get(serve_call, base, token, "/fs/list", path="")
    assert status == 200 and body["truncated"] is True
    assert len(body["entries"]) == 3


def _mk_session(serve_call, base, token, workspace):
    status, body = serve_call(base, token, "POST", "/sessions",
                              {"workspace": str(workspace)})
    assert status == 200, body
    sid = body["id"]
    serve_call(base, token, "POST", f"/sessions/{sid}/prompt", {"text": "你好"})
    return sid


def test_session_fs_uses_session_sandbox(served, serve_call, tmp_path):
    """会话级路由挂会话沙箱：A 项目的面板列不到 B 项目的文件，反之亦然。"""
    base, token, _ = served
    proj_a = tmp_path / "proj-a"
    (proj_a / "only-a").mkdir(parents=True)
    (proj_a / "only-a" / "a.txt").write_text("A", encoding="utf-8")
    proj_b = tmp_path / "proj-b"
    (proj_b / "only-b").mkdir(parents=True)
    (proj_b / "only-b" / "b.txt").write_text("B", encoding="utf-8")
    (tmp_path / "root.txt").write_text("root", encoding="utf-8")

    sid_a = _mk_session(serve_call, base, token, proj_a)
    sid_b = _mk_session(serve_call, base, token, proj_b)

    status, body = _get(serve_call, base, token, f"/sessions/{sid_a}/fs/list", path="")
    assert status == 200
    assert {e["name"] for e in body["entries"]} == {"only-a"}

    status, body = _get(serve_call, base, token, f"/sessions/{sid_b}/fs/list", path="")
    assert status == 200
    assert {e["name"] for e in body["entries"]} == {"only-b"}

    status, body = _get(serve_call, base, token, f"/sessions/{sid_a}/fs/read",
                        path="only-a/a.txt")
    assert status == 200 and body["content"] == "A"

    # 逃出本会话沙箱（进到共同父目录）同样 403：会话根就是边界
    status, body = _get(serve_call, base, token, f"/sessions/{sid_a}/fs/read",
                        path="../root.txt")
    assert status == 403 and "越界" in body["error"]

    # 兜底路由仍是进程默认沙箱（没有会话上下文时的老客户端）
    status, body = _get(serve_call, base, token, "/fs/list", path="")
    assert status == 200 and "root.txt" in {e["name"] for e in body["entries"]}


def test_session_fs_unknown_session_is_404(served, serve_call):
    base, token, _ = served
    status, body = _get(serve_call, base, token, "/sessions/nope/fs/list", path="")
    assert status == 404 and "会话不存在" in body["error"]
