"""serve 安全与协议回归：P0 修复的守卫测试。

覆盖：Host 白名单（DNS-rebind）、严格 body（400/413）、一次性 ticket、
token 轮换、prompt_async、queue take/cancel、drop/purge 未知会话 404、
per-session cursors、replay 上限、after_seq 非法 400。
"""

from __future__ import annotations

import urllib.error
import urllib.request

import smithcode.serve as serve_mod


def _status(url: str, *, bearer: str = "") -> int:
    """发一个 GET，返回 HTTP 状态码（4xx/5xx 也返回码而非抛）。

    SSE 成功建立时立刻取状态码再关闭，不读流。
    """
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def test_host_allowlist_blocks_dns_rebind(served, serve_call):
    """无 Origin + 伪造 Host（DNS-rebind）必须 403；回环 Host 放行。"""
    import urllib.request

    base, token, _ = served

    def call(host):
        req = urllib.request.Request(
            base + "/health", method="GET",
            headers={"Host": host, "Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code

    assert call("evil.example") == 403
    assert call("evil.example:18789") == 403
    assert call("127.0.0.1") == 200


def test_strict_body_rejects_garbage(served, serve_call):
    """超限/坏 JSON/非对象 body 不再吞成 `{}` 建会话。"""
    base, token, _ = served

    def raw_call(data: bytes, content_type="application/json"):
        req = urllib.request.Request(
            base + "/sessions", data=data, method="POST",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": content_type},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code

    assert raw_call(b"{not json") in (400, 413)
    assert raw_call(b"[1, 2, 3]") in (400, 413)
    assert raw_call(b"x" * ((1 << 20) + 1)) in (400, 413)


def test_ticket_single_use(served, serve_call):
    """ticket 一次有效：第二次用同一张必须 401；长效 token 不再进 URL。"""
    base, token, _ = served
    status, body = serve_call(base, token, "POST", "/events/ticket", {})
    assert status == 200 and body["ticket"]

    ticket = body["ticket"]
    # 第一次：票据有效，但会话不存在 → 404（票据在鉴权阶段已被消费）
    assert _status(f"{base}/events?session_id=x&ticket={ticket}") == 404
    # 第二次：同一张票据已消费 → 401
    assert _status(f"{base}/events?session_id=x&ticket={ticket}") == 401

    status, _ = serve_call(base, token, "GET", "/sessions/nonexistent/pending")
    assert status == 404


def test_token_rotate(served, serve_call):
    """轮换后旧 token 401、新 token 200。"""
    base, token, _ = served
    status, body = serve_call(base, token, "POST", "/auth/rotate", {})
    assert status == 200 and body["token"] and body["token"] != token
    status, _ = serve_call(base, token, "GET", "/sessions")
    assert status == 401
    status, _ = serve_call(base, body["token"], "GET", "/sessions")
    assert status == 200


def test_prompt_async_returns_202(served, serve_call):
    """`prompt_async` 不抱 HTTP 线程：202 直接回。"""
    base, token, _ = served
    _, body = serve_call(base, token, "POST", "/sessions", {})
    status, body = serve_call(
        base, token, "POST", f"/sessions/{body['id']}/prompt_async",
        {"text": "你好"},
    )
    assert status == 202 and body["queued"] is True


def test_queue_take_cancel_round_trip(served, serve_call):
    """排队项 take（取回文本）/cancel（撤销）走专用端点，不再经 `/take` dispatch。"""
    import time

    base, token, state = served
    _, body = serve_call(base, token, "POST", "/sessions", {})
    entry = state.get(body["id"])
    item = entry.agent.enqueue("排队文本")
    status, body = serve_call(
        base, token, "POST", f"/sessions/{entry.id}/queue/take", {"id": item.id},
    )
    assert status == 200 and body["text"] == "排队文本"
    item2 = entry.agent.enqueue("撤销文本")
    status, body = serve_call(
        base, token, "POST", f"/sessions/{entry.id}/queue/cancel", {"id": item2.id},
    )
    assert status == 200 and body["ok"] is True
    status, body = serve_call(
        base, token, "POST", f"/sessions/{entry.id}/queue/take", {"id": "nope"},
    )
    assert status == 404
    time.sleep(0.1)


def test_drop_purge_unknown_404(served, serve_call):
    """未知会话 drop/purge 不再回 `ok:false` 200，一律 404。"""
    base, token, _ = served
    status, _ = serve_call(base, token, "DELETE", "/sessions/nope")
    assert status == 404
    status, _ = serve_call(base, token, "POST", "/sessions/nope/purge", {})
    assert status == 404
    status, _ = serve_call(
        base, token, "POST", "/sessions/nope", {"method": "DELETE"},
    )
    assert status == 404


def test_sse_per_session_cursors(served):
    """按会话游标：新前端传 `cursors`，各会话只补自己的增量。"""
    import json
    import threading
    import time
    import urllib.request

    base, token, _ = served

    def call(base, token, method, path, body=None):
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

    _, body = call(base, token, "POST", "/sessions", {})
    sid_a = body["id"]
    _, body = call(base, token, "POST", "/sessions", {})
    sid_b = body["id"]
    call(base, token, "POST", f"/sessions/{sid_a}/dispatch", {"text": "/rename 会话A"})
    call(base, token, "POST", f"/sessions/{sid_b}/dispatch", {"text": "/rename 会话B"})

    def read_stream(params: str) -> str:
        req = urllib.request.Request(
            f"{base}/events?{params}",
            headers={"Authorization": f"Bearer {token}"},
        )
        lines: list[bytes] = []
        stop = threading.Event()

        def reader():
            with urllib.request.urlopen(req, timeout=20) as resp:
                while not stop.is_set():
                    line = resp.readline()
                    if not line:
                        return
                    lines.append(line)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        time.sleep(1.0)
        stop.set()
        thread.join(timeout=3)
        return b"".join(lines).decode("utf-8", "replace")

    cursors = json.dumps({sid_a: 0, sid_b: 999999})
    import urllib.parse

    params = urllib.parse.urlencode(
        {"session_id": [sid_a, sid_b], "cursors": cursors}, doseq=True
    )
    text = read_stream(params)
    assert "会话A" in text
    assert "会话B" not in text or '"seq": null' in text  # B 只允许直播副本，不许重放


def test_sse_bad_after_seq_400(served):
    """非法 after_seq 显式 400，不再静默变 0 全量重放。"""
    base, token, _ = served
    assert _status(f"{base}/events?session_id=x&after_seq=abc", bearer=token) == 400


def test_live_queue_bounded():
    """直播队列有界：512 个 volatile 之后不涨内存（丢最旧 volatile）。"""
    import queue as queue_mod

    from smithcode.event.catalog import Notice
    from smithcode.event.envelope import wrap

    q: queue_mod.Queue = queue_mod.Queue(maxsize=8)
    for i in range(20):
        serve_mod._put_live(q, wrap(Notice(text=f"n{i}")))
    assert q.qsize() <= 8


def test_health_carries_version(served, serve_call):
    """`/health` 带版本号：前端可据此提示 serve/桌面版本漂移。"""
    from smithcode import __version__

    base, token, _ = served
    status, body = serve_call(base, token, "GET", "/health")
    assert status == 200 and body["version"] == __version__
