"""/mcp 命令测试：状态列表、添加向导意图、参数直通解析与子命令。"""

from types import SimpleNamespace

import pytest

from smithcode import commands, config
from smithcode.commands import mcp as mcp_command
from smithcode.mcp import secrets
from smithcode.mcp.service import McpService
from smithcode.tools import DYNAMIC, unregister_dynamic


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    secrets.redactor().clear()
    yield workspace, home
    for name in list(DYNAMIC):
        unregister_dynamic(name)
    secrets.redactor().clear()


def _agent():
    return SimpleNamespace(mcp=McpService())


def test_overview_without_servers_still_opens_panel():
    outcome = commands.dispatch(_agent(), "/mcp")
    assert outcome.select is not None
    assert outcome.select.items[0].label == "添加 MCP"
    assert outcome.select.items[0].value == "add"
    # 无服务器时不渲染间隔行
    assert len(outcome.select.items) == 1


def test_overview_layout_and_status_colors(isolated):
    service = McpService()

    class Status:
        name = "fake"
        scope = "user"
        state = "connected"
        tool_count = 3
        error = ""
        state_label = "已连接"

    service.status = lambda: [Status()]
    agent = SimpleNamespace(mcp=service)
    outcome = commands.dispatch(agent, "/mcp")

    items = outcome.select.items
    assert items[0].label == "添加 MCP"
    # 添加项与已有服务器之间有一个不可选中的间隔行
    assert items[1].separator is True
    server = items[2]
    assert server.label == "fake"
    assert server.description == "3 工具 · 全局"
    assert server.trailing == "已连接"
    assert server.trailing_style == "#23d18b"


def test_overview_project_scope_and_state_color(isolated):
    service = McpService()

    class Status:
        name = "proj"
        scope = "project"
        state = "missing_env"
        tool_count = 0
        error = ""
        state_label = "缺少密钥"

    service.status = lambda: [Status()]
    outcome = commands.dispatch(SimpleNamespace(mcp=service), "/mcp")
    server = outcome.select.items[-1]
    assert server.description == "0 工具 · 项目"
    assert server.trailing_style == "#fab283"


def test_add_returns_wizard_intent():
    outcome = commands.dispatch(_agent(), "/mcp add")
    assert outcome.wizard is not None
    assert outcome.wizard.name == "mcp.add"


def test_parse_add_arguments():
    name, scope, env, command, remote = mcp_command._parse_add(
        ["github", "-e", "TOKEN=abc", "--scope", "project", "--",
         "npx", "-y", "@modelcontextprotocol/server-github"]
    )
    assert name == "github"
    assert scope == "project"
    assert env == {"TOKEN": "abc"}
    assert command == ["npx", "-y", "@modelcontextprotocol/server-github"]
    assert remote == {"type": "", "url": "", "headers": {}, "oauth": False}


def test_parse_add_without_separator():
    name, scope, _env, command, _remote = mcp_command._parse_add(
        ["my", "npx", "-y", "pkg", "--flag"]
    )
    assert name == "my"
    assert scope == "user"
    assert command == ["npx", "-y", "pkg", "--flag"]


def test_parse_add_remote():
    name, _scope, env, command, remote = mcp_command._parse_add(
        ["linear", "--url", "https://mcp.linear.app/mcp", "--type", "http",
         "--header", "Authorization=Bearer token", "--oauth", "--scope", "user"]
    )
    assert name == "linear"
    assert command == []
    assert env == {}
    assert remote["type"] == "http"
    assert remote["url"] == "https://mcp.linear.app/mcp"
    assert remote["headers"] == {"Authorization": "Bearer token"}
    assert remote["oauth"] is True


def test_add_args_path_writes_config_without_spawning(isolated, monkeypatch):
    """直通路径只验证解析与写盘：把 service.add 换成记录器，不真的启动进程。"""
    service = McpService()
    recorded = []

    def fake_add(cfg, scope=None):
        recorded.append((cfg, scope))

    monkeypatch.setattr(service, "add", fake_add)
    agent = SimpleNamespace(mcp=service)
    outcome = commands.dispatch(
        agent, "/mcp add gh -e TOKEN=abc -- npx -y pkg"
    )
    assert outcome.style == "retry"  # 后台连接中：蓝色 ↻，不冒充成功
    cfg, scope = recorded[0]
    assert cfg.name == "gh"
    assert cfg.env == {"TOKEN": "${TOKEN}"}
    assert scope == "user"
    # 值走凭据库，配置只留引用
    assert secrets.lookup("gh", "TOKEN") == "abc"


def test_add_remote_url_stores_header_secret(isolated, monkeypatch):
    service = McpService()
    recorded = []

    def fake_add(cfg, scope=None):
        recorded.append((cfg, scope))

    monkeypatch.setattr(service, "add", fake_add)
    agent = SimpleNamespace(mcp=service)
    outcome = commands.dispatch(
        agent,
        "/mcp add linear --url https://mcp.linear.app/mcp --header Authorization=token123",
    )
    assert outcome.style == "retry"
    cfg, scope = recorded[0]
    assert cfg.type == "http"
    assert cfg.url == "https://mcp.linear.app/mcp"
    assert scope == "user"
    assert cfg.headers["Authorization"].startswith("${MCP_HEADER_")
    assert secrets.lookup("linear", "MCP_HEADER_AUTHORIZATION") == "token123"


def test_tools_unknown_server():
    outcome = commands.dispatch(_agent(), "/mcp tools nope")
    assert "未找到" in outcome.text


def test_lifecycle_unknown_server():
    outcome = commands.dispatch(_agent(), "/mcp remove nope")
    assert "未找到" in outcome.text


def test_status_list_renders_states(isolated):
    service = McpService()

    class Status:
        name = "fake"
        scope_label = "用户级"
        state_label = "已连接"
        tool_count = 3
        error = ""

    service.status = lambda: [Status()]
    agent = SimpleNamespace(mcp=service)
    outcome = commands.dispatch(agent, "/mcp list")
    assert "fake" in outcome.text
    assert "3 工具" in outcome.text
