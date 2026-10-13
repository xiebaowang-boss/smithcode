"""serve 全链路测试：会话建/删、prompt→reply→finished、鉴权、dispatch、settings。

用 FakeLLM 让 agent 不碰网络：只验证 serve 的接线（会话表、事件桥、问询往返、
HTTP 面），不测 agent 本体（那是既有套件的事）。`served` fixture 在 conftest.py。
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


def test_health_and_auth(served):
    base, token, _ = served
    status, body = _call(base, token, "GET", "/health")
    assert status == 200 and body["ok"] is True and body["version"]
    status, _ = _call(base, "wrong", "GET", "/sessions")
    assert status == 401


def test_session_lifecycle(served):
    base, token, _ = served
    status, body = _call(base, token, "POST", "/sessions", {})
    assert status == 200 and body["id"]
    sid = body["id"]
    status, body = _call(base, token, "GET", "/sessions")
    assert status == 200 and any(s["id"] == sid for s in body["sessions"])
    status, body = _call(base, token, "DELETE", f"/sessions/{sid}")
    assert status == 200 and body["ok"] is True
    status, body = _call(base, token, "GET", "/sessions")
    assert all(s["id"] != sid for s in body["sessions"])


def test_prompt_runs_without_network(served):
    """`prompt` 经 FakeLLM 跑完一轮（验证会话循环 + 事件桥不断）。"""
    base, token, _ = served
    _, body = _call(base, token, "POST", "/sessions", {})
    status, body = _call(base, token, "POST", f"/sessions/{body['id']}/prompt",
                         {"text": "你好"})
    assert status == 200 and body["status"] == "ok"


def test_dispatch_and_settings(served):
    base, token, _ = served
    _, body = _call(base, token, "POST", "/sessions", {})
    sid = body["id"]
    status, body = _call(base, token, "POST", f"/sessions/{sid}/dispatch",
                         {"text": "/help"})
    assert status == 200 and body["text"]
    status, body = _call(base, token, "POST", "/settings", {})
    assert status == 200 and "model" in body and "key" not in body
    from smithcode import config as config_mod

    saved = {name: getattr(config_mod, name) for name in (
        "KEY", "MODEL", "URL", "REASONING_EFFORT", "MAX_RETRIES",
        "MAX_ITERATIONS", "CONTEXT_TOKEN_BUDGET",
    )}
    try:
        status, body = _call(base, token, "POST", "/settings/reload", {})
        assert status == 200 and "changed" in body
    finally:
        for name, value in saved.items():
            setattr(config_mod, name, value)


def test_reply_round_trip_through_http(served):
    """问询全链路：`ask()` 挂起 → `GET pending` 可见 → `POST reply` 唤醒。"""
    import asyncio


    base, token, state = served
    _, body = _call(base, token, "POST", "/sessions", {})
    entry = state.get(body["id"])
    port = entry.agent.asks
    assert port.remote is not None  # serve 一律跨进程作答

    async def _ask():
        from smithcode.event.asks import AskRequest

        return await port.ask(AskRequest(kind="confirm", title="继续?"))

    task = asyncio.run_coroutine_threadsafe(_ask(), state._loop)
    for _ in range(100):
        status, body = _call(base, token, "GET", f"/sessions/{entry.id}/pending")
        if status == 200 and body["pending"]:
            break
        import time

        time.sleep(0.05)
    assert body["pending"] and body["pending"][0]["title"] == "继续?"
    pid = body["pending"][0]["id"]
    status, body = _call(
        base, token, "POST", f"/sessions/{entry.id}/prompts/{pid}/reply",
        {"outcome": "answered", "value": "y"},
    )
    assert status == 200 and body["ok"] is True
    assert task.result(timeout=10).value == "y"
    # 迟到重复作答幂等：返回 ok=False，不炸
    status, body = _call(
        base, token, "POST", f"/sessions/{entry.id}/prompts/{pid}/reply",
        {"outcome": "answered", "value": "n"},
    )
    assert status == 200 and body["ok"] is False


def test_origin_allowlist(served):
    """来源白名单：Tauri / 本地 dev 放行并回 ACAO，其余来源一律 403 且不回 ACAO。

    这条防的是"网页直连本地端口"——绑回环拦不住浏览器，跨站 simple request
    连预检都不发，所以 token 之外必须再核来源。
    """
    base, token, _ = served

    def call(origin, method="GET", path="/health"):
        req = urllib.request.Request(
            base + path, method=method,
            headers={"Origin": origin, "Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.headers.get("Access-Control-Allow-Origin")
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Access-Control-Allow-Origin")

    for origin in (
        "tauri://localhost",
        "http://tauri.localhost",
        "https://tauri.localhost",
        "http://localhost:1420",
    ):
        status, acao = call(origin)
        assert status == 200 and acao == origin

    status, acao = call("https://evil.example")
    assert status == 403 and acao is None
    # 跨站写操作与预检同样拒（带合法 token 也不行）
    status, _ = call("https://evil.example", method="POST", path="/sessions")
    assert status == 403
    status, _ = call("https://evil.example", method="OPTIONS", path="/sessions")
    assert status == 403


def test_allowed_origins_env_extends(monkeypatch):
    import smithcode.serve as serve_mod

    monkeypatch.setenv("SMITH_ALLOWED_ORIGINS", "https://app.example, http://localhost:5173")
    assert serve_mod._allowed_origins() == serve_mod._DEFAULT_ORIGINS + (
        "https://app.example",
        "http://localhost:5173",
    )


def test_token_from_env(monkeypatch):
    """token 走 `SMITH_AUTH_TOKEN`：桌面壳（sidecar）与 serve 的约定，不进命令行。"""
    import smithcode.serve as serve_mod

    monkeypatch.setenv("SMITH_AUTH_TOKEN", "env-token")
    state = serve_mod.ServeState()
    assert state.auth_token == "env-token"
    assert state.token_provided is True  # 调用方提供的不回显（不打印 token= 行）


def test_token_generated_when_unset(monkeypatch):
    import smithcode.serve as serve_mod

    monkeypatch.delenv("SMITH_AUTH_TOKEN", raising=False)
    state = serve_mod.ServeState()
    assert len(state.auth_token) == 32 and state.token_provided is False
    # 显式参数优先于环境变量
    assert serve_mod.ServeState(auth_token="explicit").auth_token == "explicit"


def test_workspace_endpoint(served):
    """工作区信息：桌面端的文件夹 chip / Projects 分组 / 关于页都要它。"""
    base, token, _ = served
    status, body = _call(base, token, "GET", "/workspace")
    assert status == 200
    assert body["name"] and body["cwd"]
    assert isinstance(body["extra"], list)


def test_commands_endpoint(served):
    base, token, _ = served
    status, body = _call(base, token, "GET", "/commands")
    assert status == 200
    names = {item["name"] for item in body["commands"]}
    assert {"help", "model", "skills"} <= names
    assert all(item["description"] for item in body["commands"])


def test_history_carries_project_fields(served):
    """history 要带 cwd/created（Projects 分组与文件夹 chip 依赖），limit 可调。"""
    base, token, _ = served
    _, body = _call(base, token, "POST", "/sessions", {})
    sid = body["id"]
    _call(base, token, "POST", f"/sessions/{sid}/prompt", {"text": "你好"})
    status, body = _call(base, token, "GET", "/sessions/history")
    assert status == 200
    item = next(s for s in body["sessions"] if s["id"] == sid)
    assert item["created"] > 0 and item["updated"] > 0
    assert item["cwd"]
    status, body = _call(base, token, "GET", "/sessions/history?limit=1")
    assert status == 200 and len(body["sessions"]) == 1


def test_purge_removes_session_file(served):
    """purge = 卸载 + 删日志（与 DELETE 的"仅卸载"区分开）。"""
    base, token, _ = served
    from smithcode import sessions

    _, body = _call(base, token, "POST", "/sessions", {})
    sid = body["id"]
    summary = sessions.find(sid)
    assert summary is not None and summary.path.exists()

    status, body = _call(base, token, "POST", f"/sessions/{sid}/purge", {})
    assert status == 200 and body["ok"] is True
    assert sessions.find(sid) is None
    assert not summary.path.exists()
    status, body = _call(base, token, "GET", "/sessions")
    assert all(s["id"] != sid for s in body["sessions"])


def test_rename_online_and_offline(served):
    """在线改标题发事件；卸载后改标题追加日志（两条路都要能走通）。"""
    base, token, _ = served
    _, body = _call(base, token, "POST", "/sessions", {})
    sid = body["id"]

    status, body = _call(base, token, "POST", f"/sessions/{sid}/rename", {"title": ""})
    assert status == 400  # 空标题拒绝

    status, body = _call(base, token, "POST", f"/sessions/{sid}/rename",
                         {"title": "在线改名"})
    assert status == 200 and body["ok"] is True

    _call(base, token, "DELETE", f"/sessions/{sid}")  # 卸载（日志保留）
    status, body = _call(base, token, "POST", f"/sessions/{sid}/rename",
                         {"title": "离线改名"})
    assert status == 200 and body["ok"] is True
    status, body = _call(base, token, "GET", "/sessions/history")
    item = next(s for s in body["sessions"] if s["id"] == sid)
    assert item["title"] == "离线改名"


def test_dispatch_select_projection(served, monkeypatch):
    """select 要带 description/trailing/readonly，且过滤纯间隔行。"""
    base, token, _ = served
    _, body = _call(base, token, "POST", "/sessions", {})
    sid = body["id"]

    from smithcode.commands.base import CommandChoice, CommandResult, CommandSelect

    def fake_dispatch(agent, text, interactive=True):
        return CommandResult(select=CommandSelect(
            title="候选", command="model", readonly=True,
            items=[
                CommandChoice(label="a", value="a", description="描述", trailing="当前"),
                CommandChoice(label="", value="", separator=True),
            ],
        ))

    monkeypatch.setattr("smithcode.commands.dispatch", fake_dispatch)
    status, body = _call(base, token, "POST", f"/sessions/{sid}/dispatch", {"text": "/model"})
    assert status == 200
    select = body["select"]
    assert select["readonly"] is True and select["title"] == "候选"
    assert select["items"] == [{"label": "a", "value": "a", "current": False,
                                "description": "描述", "trailing": "当前"}]


def test_dispatch_injects_skill_payload(served, monkeypatch):
    """技能载荷在服务端注入会话历史，任务也由服务端续跑（前端不再重复 prompt）。"""
    base, token, state = served
    _, body = _call(base, token, "POST", "/sessions", {})
    sid = body["id"]
    entry = state.get(sid)

    from smithcode.commands.base import CommandResult

    def fake_dispatch(agent, text, interactive=True):
        return CommandResult(text="已载入", inject_history=[("user", "技能正文载荷")],
                             start_task="按技能做事")

    monkeypatch.setattr("smithcode.commands.dispatch", fake_dispatch)
    status, body = _call(base, token, "POST", f"/sessions/{sid}/dispatch", {"text": "/demo"})
    assert status == 200
    assert body.get("start_task") is None and not body.get("skipped_busy")
    assert "技能正文载荷" in [m.get("content") for m in entry.agent.session.messages]


def test_dispatch_skips_injection_when_busy(served, monkeypatch):
    """运行中不做注入也不开跑（与 TUI 的 busy 守卫一致），并回一个跳过标记。"""
    base, token, state = served
    _, body = _call(base, token, "POST", "/sessions", {})
    sid = body["id"]
    entry = state.get(sid)

    from smithcode.agent.agent import Agent
    from smithcode.commands.base import CommandResult

    def fake_dispatch(agent, text, interactive=True):
        return CommandResult(inject_history=[("user", "不该出现")], start_task="不该开跑")

    monkeypatch.setattr("smithcode.commands.dispatch", fake_dispatch)
    monkeypatch.setattr(Agent, "busy", property(lambda self: True))
    status, body = _call(base, token, "POST", f"/sessions/{sid}/dispatch", {"text": "/demo"})
    assert status == 200 and body.get("skipped_busy") is True
    assert "不该出现" not in [m.get("content") for m in entry.agent.session.messages]


def test_resume_online_session_reuses_entry(served):
    """`{resume}` 指向仍在线会话时复用同一 Agent：不能开出两个写手写同一份日志。"""
    base, token, state = served
    _, body = _call(base, token, "POST", "/sessions", {})
    sid = body["id"]
    status, body = _call(base, token, "POST", "/sessions", {"resume": sid})
    assert status == 200 and body["id"] == sid
    assert state.get(sid) is not None
    # 复用而非重建：同一份 entry（对象身份相同）
    first = state.get(sid)
    status, body = _call(base, token, "POST", "/sessions", {"resume": sid})
    assert state.get(body["id"]) is first


def _read_stream(base, token, sid, after_seq, seconds=1.2):
    """连上 SSE 读一小段，返回收到的原始文本（含重放 + 直播）。"""
    import threading
    import time
    import urllib.request

    req = urllib.request.Request(
        f"{base}/events?session_id={sid}&after_seq={after_seq}",
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
    time.sleep(seconds)
    stop.set()
    return b"".join(lines).decode("utf-8", "replace")


def test_sse_replay_respects_cursor(served):
    """重放语义：after_seq 之后的历史整段补发；游标越过日志尾则什么都不补。

    桌面端因此**按会话各记一份游标**（全局游标会让旧会话回放成空会话）。
    SSE 不做服务端去重：订阅者拿到的信封 seq 恒为 None（seq 由日志写盘时分配，
    见 `event/envelope.py`），按 seq 猜"是否已重放"会把 volatile 一起吞掉；
    重复的那点流量由前端按事件 id 去重。
    """
    base, token, _ = served
    _, body = _call(base, token, "POST", "/sessions", {})
    sid = body["id"]
    _call(base, token, "POST", f"/sessions/{sid}/dispatch", {"text": "/rename 游标用例"})

    text = _read_stream(base, token, sid, 0)
    assert "session.created" in text and "游标用例" in text

    text = _read_stream(base, token, sid, 999999)
    assert "session.created" not in text


def test_sse_keeps_volatile_events_after_replay(served):
    """回归：连接后的直播流不能被任何"去重"过滤吞掉。

    教训来自实测：一度按 seq 水位跳过"已重放"的事件——可订阅者拿到的信封 seq
    恒为 None（落盘时才分配），于是 volatile 事件（排队 chip、流式增量、通知、
    忙碌态）被当成 0 一起吞掉，表现就是"消息发出去后界面什么都不显示"。
    """
    import threading
    import time
    import urllib.request

    base, token, state = served
    _, body = _call(base, token, "POST", "/sessions", {})
    sid = body["id"]
    _call(base, token, "POST", f"/sessions/{sid}/dispatch", {"text": "/rename 有历史了"})

    req = urllib.request.Request(
        f"{base}/events?session_id={sid}&after_seq=0",
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
    time.sleep(0.5)  # 等重放写完，进入直播阶段

    from smithcode.event.catalog import Notice

    state.get(sid).bus.publish(Notice(text="排队提示", level="info"))
    time.sleep(0.8)
    stop.set()
    text = b"".join(lines).decode("utf-8", "replace")
    assert "session.title.changed" in text  # 重放照旧
    assert "排队提示" in text  # volatile 通知照发


def test_sse_redaction_walks_nested_records():
    """回归：`_redact` 曾因导入不存在的 `redact` 静默失败，密钥原样进 SSE。"""
    import smithcode.serve as serve_mod
    from smithcode.mcp.secrets import redactor

    secret = "sk-live-abcdefghijklmnop"
    redactor().add(secret)
    try:
        record = {"data": {"result": f"用了 {secret}", "items": [secret, {"k": secret}]}}
        out = json.dumps(serve_mod._redact(record), ensure_ascii=False)
        assert secret not in out
        assert "***" in out
    finally:
        redactor().clear()


def test_sse_survives_unserializable_event_field(served):
    """回归：易失事件里混进不可 JSON 化的字段不该打断整条 SSE。

    声明侧只对 durable 事件查形状，易失事件允许携带进程内对象（如 RetryState）；
    SSE 却要写**所有**事件——发布侧一旦传错（owner 塞实例），`json.dumps` 原地抛
    TypeError 把 handler 线程炸掉，连接僵在半截，前端丢掉后续全部事件。
    现在降级成类型标记照发，坏字段之后的事件仍然到得了。
    """
    import threading
    import time
    import urllib.request

    base, token, state = served
    _, body = _call(base, token, "POST", "/sessions", {})
    sid = body["id"]

    req = urllib.request.Request(
        f"{base}/events?session_id={sid}&after_seq=0",
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
    time.sleep(0.5)  # 等重放写完，进入直播阶段

    from smithcode.event.catalog import Notice, StatusChanged

    # 契约里 owner 是 str | None，这里故意塞个实例对象模拟发布侧传错
    state.get(sid).bus.publish(
        StatusChanged(kind="retry", text="正在重试 2/3", owner=object())
    )
    state.get(sid).bus.publish(Notice(text="坏字段之后仍能收到", level="info"))
    # 轮询等坏字段之后那条到齐（慢机器也有余量），不赌固定 sleep
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if "坏字段之后仍能收到" in b"".join(lines).decode("utf-8", "replace"):
            break
        time.sleep(0.1)
    stop.set()
    text = b"".join(lines).decode("utf-8", "replace")
    assert "<builtins.object>" in text  # 降级成类型标记，事件照发
    assert "坏字段之后仍能收到" in text  # 流没断，后续事件仍到得了


def test_sse_live_not_throttled_by_idle_backoff(served):
    """回归：直播队列不许被 idle 背压拖住——忙时不睡、随到随发。

    教训：每轮只取一条**还照睡** `poll_timeout`，空闲期攒大的背压原样带进直播
    （实测突发 5 条按 2s 一条到达、第 5 条 8s 内没到）。前端表现：流式正文、
    工具卡、权限确认都慢半拍，回合越长越迟钝。
    """
    import threading
    import time
    import urllib.request

    base, token, state = served
    _, body = _call(base, token, "POST", "/sessions", {})
    sid = body["id"]

    req = urllib.request.Request(
        f"{base}/events?session_id={sid}&after_seq=0",
        headers={"Authorization": f"Bearer {token}"},
    )
    arrivals: list[tuple[float, bytes]] = []
    stop = threading.Event()

    def reader():
        with urllib.request.urlopen(req, timeout=20) as resp:
            while not stop.is_set():
                line = resp.readline()
                if not line:
                    return
                arrivals.append((time.time(), line))

    threading.Thread(target=reader, daemon=True).start()
    time.sleep(1.2)  # 先空闲一会：修复前这里已把背压攒到 2s

    from smithcode.event.catalog import Notice

    t0 = time.time()
    for i in range(5):
        state.get(sid).bus.publish(Notice(text=f"burst-{i}", level="info"))
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if sum(1 for _, line in arrivals if b"burst-" in line) >= 5:
            break
        time.sleep(0.05)
    stop.set()

    hits = [(t, line) for t, line in arrivals if b"burst-" in line]
    assert len(hits) == 5, f"5 条突发只到 {len(hits)} 条（修复前第 5 条 >8s）"
    assert max(t for t, _ in hits) - t0 < 1.5  # 同一批里到齐，不该 2s 一条
