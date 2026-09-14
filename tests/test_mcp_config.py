"""MCP 配置测试：双作用域加载/合并、条目内 enabled、原子写入与容错。"""

import json

import pytest

from smithcode import config
from smithcode.mcp import config as mcp_config
from smithcode.mcp import secrets as mcp_secrets


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """隔离用户目录与工作区；清空脱敏器避免串测。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    mcp_secrets.redactor().clear()
    yield workspace, home
    mcp_secrets.redactor().clear()


def _user_server(name="github", command=None, **kw):
    return mcp_config.ServerConfig(
        name=name,
        command=command or ["npx", "-y", "@modelcontextprotocol/server-github"],
        **kw,
    )


def test_user_roundtrip(isolated):
    _workspace, home = isolated
    path = mcp_config.write_user_server(_user_server(env={"TOKEN": "${TOKEN}"}, timeout=30))
    assert path == home / "config.toml"

    result = mcp_config.load_servers()
    assert result.diagnostics == []
    server = result.servers[0]
    assert server.name == "github"
    assert server.command == ["npx", "-y", "@modelcontextprotocol/server-github"]
    assert server.env == {"TOKEN": "${TOKEN}"}
    assert server.timeout == 30
    assert server.scope == "user"


def test_write_preserves_comments_and_other_settings(isolated):
    _workspace, home = isolated
    target = home / "config.toml"
    target.write_text(
        "# 我的配置\n[provider]\nmodel = \"deepseek\"\n", encoding="utf-8"
    )
    mcp_config.write_user_server(_user_server())
    text = target.read_text(encoding="utf-8")
    assert "# 我的配置" in text
    assert "model" in text


def test_project_overrides_user_wholesale(isolated):
    workspace, _home = isolated
    mcp_config.write_user_server(
        _user_server(command=["npx", "user-server"], env={"A": "1"})
    )
    project = workspace / ".smithcode" / "mcp.json"
    project.parent.mkdir(parents=True)
    project.write_text(
        '{"mcpServers": {"github": {"command": "node", "args": ["proj.js"]}}}',
        encoding="utf-8",
    )

    result = mcp_config.load_servers()
    assert len(result.servers) == 1
    server = result.servers[0]
    assert server.command == ["node", "proj.js"]
    assert server.scope == "project"
    assert server.env == {}  # 整条覆盖，不残留用户条目的字段


def test_env_rendered_inline(isolated):
    """一个服务器的属性（command + env）保持在同一段，env 用内联表。"""
    path = mcp_config.write_user_server(_user_server(env={"TOKEN": "${TOKEN}"}))
    text = path.read_text(encoding="utf-8")
    assert "env = {" in text
    assert "[mcp.servers.github.env]" not in text


def test_enabled_written_inline_in_user_entry(isolated):
    """用户级服务器的启停写进自己的条目，enabled 排在最前，不再单独开 [mcp.enabled] 表。"""
    _workspace, _home = isolated
    mcp_config.write_user_server(_user_server())
    path = mcp_config.set_enabled("github", False)

    text = path.read_text(encoding="utf-8")
    assert "enabled = false" in text
    assert text.index("enabled = false") < text.index("command =")
    assert "[mcp.enabled]" not in text
    assert mcp_config.load_servers().servers[0].enabled is False

    mcp_config.set_enabled("github", True)
    assert "enabled = false" not in path.read_text(encoding="utf-8")
    assert mcp_config.load_servers().servers[0].enabled is True


def test_enabled_written_into_project_entry(isolated):
    """项目级服务器：enabled 写进 .smithcode/mcp.json 自己的条目（不留覆盖表）。"""
    workspace, _home = isolated
    project = workspace / ".smithcode" / "mcp.json"
    project.parent.mkdir(parents=True)
    project.write_text(
        '{"mcpServers": {"sentry": {"command": "npx", "args": ["-y", "sentry-mcp"]}}}',
        encoding="utf-8",
    )
    mcp_config.set_enabled("sentry", False)
    entry = json.loads(project.read_text(encoding="utf-8"))["mcpServers"]["sentry"]
    assert entry["enabled"] is False
    assert next(iter(entry)) == "enabled"  # 启停字段排最前
    assert mcp_config.load_servers().servers[0].enabled is False

    mcp_config.set_enabled("sentry", True)
    entry = json.loads(project.read_text(encoding="utf-8"))["mcpServers"]["sentry"]
    assert "enabled" not in entry
    assert mcp_config.load_servers().servers[0].enabled is True


def test_project_entries_can_be_disabled_by_user_state(isolated):
    workspace, _home = isolated
    project = workspace / ".smithcode" / "mcp.json"
    project.parent.mkdir(parents=True)
    project.write_text(
        '{"mcpServers": {"sentry": {"command": "npx", "args": ["-y", "sentry-mcp"]}}}',
        encoding="utf-8",
    )
    mcp_config.set_enabled("sentry", False)
    server = mcp_config.load_servers().servers[0]
    assert server.enabled is False
    assert server.scope == "project"


def test_bad_entries_are_diagnosed_not_fatal(isolated):
    _workspace, home = isolated
    target = home / "config.toml"
    target.write_text(
        '[mcp.servers.good]\ncommand = ["npx", "ok"]\n\n[mcp.servers.no-command]\nenv = { A = "1" }\n\n[mcp.servers.http-type]\ntype = "http"\nurl = "https://example.com/mcp"\n\n[mcp.servers.bad-env]\ncommand = ["npx", "x"]\nenv = { N = 1 }' + "\n",
        encoding="utf-8",
    )

    result = mcp_config.load_servers()
    names = [s.name for s in result.servers]
    # 坏 env 键只忽略该键，服务器本体仍加载（容错）；缺 command / 不支持的 type 才整条跳过
    assert names == ["good", "bad-env"]
    assert result.servers[1].env == {}
    joined = "\n".join(result.diagnostics)
    assert "缺少 command" in joined
    assert "暂不支持" in joined
    assert "env.N" in joined


def test_project_json_write_preserves_unknown_fields(isolated):
    workspace, _home = isolated
    project = workspace / ".smithcode" / "mcp.json"
    project.parent.mkdir(parents=True)
    project.write_text(
        '{\n  "mcpServers": {"other": {"command": "node", "args": ["other.js"]}},\n'
        '  "unknownTop": 42\n}\n',
        encoding="utf-8",
    )

    mcp_config.write_project_server(
        mcp_config.ServerConfig(
            name="puppeteer", command=["npx", "-y", "puppeteer-mcp"], env={"K": "${K}"}
        )
    )

    import json
    data = json.loads(project.read_text(encoding="utf-8"))
    assert data["unknownTop"] == 42
    assert "other" in data["mcpServers"]
    entry = data["mcpServers"]["puppeteer"]
    assert entry["type"] == "stdio"
    assert entry["command"] == "npx"
    assert entry["args"] == ["-y", "puppeteer-mcp"]
    assert entry["env"] == {"K": "${K}"}

    loaded = {s.name: s for s in mcp_config.load_servers().servers}
    assert loaded["puppeteer"].command == ["npx", "-y", "puppeteer-mcp"]
    assert loaded["puppeteer"].scope == "project"


def test_remove_servers(isolated):
    _workspace, _home = isolated
    mcp_config.write_user_server(_user_server())
    assert mcp_config.remove_user_server("github") is True
    assert mcp_config.remove_user_server("github") is False
    assert mcp_config.load_servers().servers == []

    mcp_config.write_project_server(_user_server(name="proj"))
    assert mcp_config.remove_project_server("proj") is True
    assert mcp_config.remove_project_server("proj") is False


def test_fingerprint_tracks_command_changes():
    first = mcp_config.ServerConfig(name="a", command=["npx", "x"])
    same = mcp_config.ServerConfig(name="a", command=["npx", "x"])
    changed = mcp_config.ServerConfig(name="a", command=["npx", "y"])
    assert first.fingerprint == same.fingerprint
    assert first.fingerprint != changed.fingerprint
