"""MCP 服务测试：后台连接、动态注册、调用路由、启停与移除。"""

import json
import sys
from pathlib import Path

import pytest

from smithcode import config
from smithcode.mcp import config as mcp_config
from smithcode.mcp import secrets
from smithcode.mcp.errors import McpConfigError
from smithcode.mcp.service import (
    CONNECTED,
    DISABLED,
    MISSING_ENV,
    McpService,
)
from smithcode.tools import (
    DESCRIBERS,
    DYNAMIC,
    FUNCTIONS,
    SERIAL,
    unregister_dynamic,
    visible_schemas,
)

FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    monkeypatch.delenv("SMITHCODE_TEST_MISSING_VAR_XYZ", raising=False)
    secrets.redactor().clear()
    yield workspace, home
    for name in list(DYNAMIC):
        unregister_dynamic(name)
    secrets.redactor().clear()


def _write(name, mode=None, env=None):
    merged = dict(env or {})
    if mode:
        merged["FAKE_MCP_MODE"] = mode
    cfg = mcp_config.ServerConfig(
        name=name,
        command=[sys.executable, str(FIXTURE)],
        env=merged,
        timeout=10.0,
    )
    mcp_config.write_user_server(cfg)
    return cfg


def _started_service():
    service = McpService()
    service.start()
    assert service.wait(15)
    return service


def test_connect_registers_tools_with_metadata(isolated):
    _write("fake")
    service = _started_service()
    try:
        status = service.status()[0]
        assert status.state == CONNECTED
        assert status.tool_count == 5

        names = {schema["name"] for schema in visible_schemas()}
        assert "mcp__fake__echo" in names
        # 执行闭包与 Agent 调用路径一致
        assert FUNCTIONS["mcp__fake__echo"](msg="hi") == "echo: hi"
        assert SERIAL["mcp__fake__echo"] is True
        assert DESCRIBERS["mcp__fake__echo"]({}) == "mcp fake.echo"
        # 工具展示信息
        tools = {tool["original"]: tool for tool in service.tools("fake")}
        assert tools["echo"]["exposed"] == "mcp__fake__echo"
    finally:
        service.stop()
    assert "mcp__fake__echo" not in {schema["name"] for schema in visible_schemas()}


def test_missing_env_blocks_connection(isolated, monkeypatch):
    _write("needy", env={"TOKEN": "${SMITHCODE_TEST_MISSING_VAR_XYZ}"})
    service = _started_service()
    try:
        status = service.status()[0]
        assert status.state == MISSING_ENV
        assert status.missing == ["SMITHCODE_TEST_MISSING_VAR_XYZ"]
        assert "mcp__needy__echo" not in {s["name"] for s in visible_schemas()}
    finally:
        service.stop()


def test_missing_env_resolved_from_credentials(isolated, monkeypatch):
    monkeypatch.delenv("MCP_FAKE_TOKEN", raising=False)
    secrets.store_secret("needy", "MCP_FAKE_TOKEN", "cred-value")
    _write("needy", env={"TOKEN": "${MCP_FAKE_TOKEN}"})
    service = _started_service()
    try:
        assert service.status()[0].state == CONNECTED
    finally:
        service.stop()


def test_disabled_server_is_skipped(isolated):
    _write("fake")
    mcp_config.set_enabled("fake", False)
    service = _started_service()
    try:
        assert service.status()[0].state == DISABLED
        assert "mcp__fake__echo" not in {s["name"] for s in visible_schemas()}
    finally:
        service.stop()


def test_set_enabled_toggles_connection(isolated):
    _write("fake")
    service = _started_service()
    try:
        assert service.set_enabled("fake", False) is True
        assert service.status()[0].state == DISABLED
        assert "mcp__fake__echo" not in {s["name"] for s in visible_schemas()}

        assert service.set_enabled("fake", True) is True
        status = service.wait_for("fake", 15)
        assert status is not None and status.state == CONNECTED
        assert "mcp__fake__echo" in {s["name"] for s in visible_schemas()}
    finally:
        service.stop()


def test_reconnect(isolated):
    _write("fake")
    service = _started_service()
    try:
        assert service.reconnect("fake") is True
        status = service.wait_for("fake", 15)
        assert status is not None and status.state == CONNECTED
    finally:
        service.stop()


def test_remove_unregisters_and_deletes_config(isolated):
    _write("fake")
    service = _started_service()
    try:
        assert service.remove("fake") is True
        assert service.status() == []
        assert "mcp__fake__echo" not in {s["name"] for s in visible_schemas()}
        assert mcp_config.load_servers().servers == []
    finally:
        service.stop()


def test_cross_scope_conflict_rejected(isolated):
    _write("fake")
    service = _started_service()
    try:
        project_cfg = mcp_config.ServerConfig(
            name="fake", command=[sys.executable, str(FIXTURE)]
        )
        with pytest.raises(McpConfigError) as excinfo:
            service.add(project_cfg, scope="project")
        assert "已存在" in str(excinfo.value)
    finally:
        service.stop()


def test_project_scope_server_connects(isolated):
    workspace, _ = isolated
    project = workspace / ".smithcode" / "mcp.json"
    project.parent.mkdir(parents=True)
    project.write_text(
        json.dumps({
            "mcpServers": {
                "pfake": {
                    "command": sys.executable,
                    "args": [str(FIXTURE)],
                    "env": {"FAKE_MCP_MODE": "no-tools"},
                }
            }
        }),
        encoding="utf-8",
    )
    service = _started_service()
    try:
        status = service.status()[0]
        assert status.state == CONNECTED
        assert status.scope == "project"
        assert status.tool_count == 0
    finally:
        service.stop()
