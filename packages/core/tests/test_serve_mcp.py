"""`/mcp/*` 面板端点：列表/日志/工具/启停/授权/删除/添加。

用假 McpService 顶替真实连接（面板只关心投影与状态码，真连接由 test_mcp_* 覆盖）。
"""

from __future__ import annotations

from smithcode.mcp.service import ServerStatus


class FakeMcp:
    """只实现面板用到的读/写入口，并记录调用。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.statuses = [
            ServerStatus(name="demo", scope="user", state="connected", command="npx demo",
                         transport="stdio", tool_count=2),
            ServerStatus(name="web", scope="project", state="needs_auth", url="https://mcp.example",
                         transport="http", oauth=True, error="token 过期"),
        ]

    def status(self):
        return list(self.statuses)

    def get(self, name):
        return name if name in {s.name for s in self.statuses} else None

    def tools(self, name):
        return [{"original": "read", "exposed": "mcp__demo__read",
                 "description": "读一个文件", "read_only": True}]

    def logs(self, name, lines=20):
        self.calls.append(("logs", name, lines))
        return "启动日志\n连接成功"

    def set_enabled(self, name, enabled):
        self.calls.append(("set_enabled", name, enabled))
        return True

    def reconnect(self, name):
        self.calls.append(("reconnect", name))
        return True

    def authorize(self, name):
        self.calls.append(("authorize", name))
        return True

    def remove(self, name):
        self.calls.append(("remove", name))
        return True

    def add(self, cfg, scope=None):
        self.calls.append(("add", cfg.name, cfg.type, scope))
        return ServerStatus(name=cfg.name, scope=scope or "user", state="connecting")


def _install(monkeypatch, state) -> FakeMcp:
    fake = FakeMcp()
    monkeypatch.setattr(state, "_mcp", fake)
    return fake


def test_mcp_servers_list(served, serve_call, monkeypatch):
    base, token, state = served
    fake = _install(monkeypatch, state)
    status, body = serve_call(base, token, "GET", "/mcp/servers")
    assert status == 200
    names = [s["name"] for s in body["servers"]]
    assert names == ["demo", "web"]
    demo = body["servers"][0]
    assert demo["state_label"] == "已连接" and demo["scope_label"] == "全局"
    assert demo["tool_count"] == 2 and demo["target"] == "npx demo"
    assert body["servers"][1]["oauth"] is True
    assert fake.calls == []


def test_mcp_lifecycle_actions(served, serve_call, monkeypatch):
    base, token, state = served
    fake = _install(monkeypatch, state)
    for action, expect in (
        ("enable", ("set_enabled", "demo", True)),
        ("disable", ("set_enabled", "demo", False)),
        ("reconnect", ("reconnect", "demo")),
        ("auth", ("authorize", "demo")),
        ("remove", ("remove", "demo")),
    ):
        status, body = serve_call(base, token, "POST", f"/mcp/servers/demo/{action}", {})
        assert status == 200 and body["ok"] is True
        assert fake.calls[-1] == expect

    status, body = serve_call(base, token, "POST", "/mcp/servers/nope/enable", {})
    assert status == 404 and "未配置" in body["error"]


def test_mcp_logs_and_tools(served, serve_call, monkeypatch):
    base, token, state = served
    _install(monkeypatch, state)
    status, body = serve_call(base, token, "GET", "/mcp/servers/demo/logs?lines=5")
    assert status == 200 and "启动日志" in body["logs"]
    status, body = serve_call(base, token, "GET", "/mcp/servers/demo/tools")
    assert status == 200 and body["tools"][0]["exposed"] == "mcp__demo__read"
    status, _ = serve_call(base, token, "GET", "/mcp/servers/nope/logs")
    assert status == 404


def test_mcp_add_validates_and_records(served, serve_call, monkeypatch):
    base, token, state = served
    fake = _install(monkeypatch, state)

    status, body = serve_call(base, token, "POST", "/mcp/servers", {"name": ""})
    assert status == 400
    status, body = serve_call(base, token, "POST", "/mcp/servers",
                              {"name": "x", "type": "stdio", "command": []})
    assert status == 400 and "命令" in body["error"]
    status, body = serve_call(base, token, "POST", "/mcp/servers",
                              {"name": "x", "type": "http", "url": ""})
    assert status == 400 and "url" in body["error"]
    status, body = serve_call(base, token, "POST", "/mcp/servers",
                              {"name": "x", "type": "stdio", "scope": "oops",
                               "command": ["echo"]})
    assert status == 400 and "作用域" in body["error"]

    status, body = serve_call(base, token, "POST", "/mcp/servers", {
        "name": "fs", "type": "stdio", "scope": "project",
        "command": ["npx", "-y", "server-filesystem"],
    })
    assert status == 200 and body["server"]["name"] == "fs"
    assert fake.calls[-1] == ("add", "fs", "stdio", "project")


def test_mcp_status_is_redacted(served, serve_call, monkeypatch):
    """面板返回的命令/地址/报错都要过脱敏：展开后的密钥可能夹在里面。"""
    from smithcode.mcp.secrets import redactor

    secret = "sk-live-mcp-secret-1234"
    redactor().add(secret)
    try:
        base, token, state = served
        fake = _install(monkeypatch, state)
        fake.statuses[0].command = f"npx --token {secret}"
        fake.statuses[1].error = f"授权失败：{secret}"
        status, body = serve_call(base, token, "GET", "/mcp/servers")
        assert status == 200
        dumped = str(body)
        assert secret not in dumped and "***" in dumped
    finally:
        redactor().clear()
