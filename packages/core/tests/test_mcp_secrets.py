"""MCP 密钥测试：引用展开解析链、凭据库写入与脱敏。"""

import json
import os

import pytest

from smithcode import config
from smithcode.mcp import config as mcp_config
from smithcode.mcp import secrets as mcp_secrets


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    mcp_secrets.redactor().clear()
    yield workspace, home
    mcp_secrets.redactor().clear()


def _server(**kw):
    kw.setdefault("name", "github")
    kw.setdefault("command", ["npx", "server"])
    return mcp_config.ServerConfig(**kw)


def test_expand_from_process_env(isolated, monkeypatch):
    monkeypatch.setenv("MCP_TEST_TOKEN", "env-value")
    resolved = mcp_secrets.resolve(_server(env={"T": "${MCP_TEST_TOKEN}"}))
    assert resolved.env == {"T": "env-value"}
    assert resolved.missing == []
    assert "env-value" not in mcp_secrets.redactor().scrub("x env-value y")


def test_default_value_and_missing(isolated):
    resolved = mcp_secrets.resolve(
        _server(command=["npx", "${MISSING_BIN:-server}"], env={"T": "${NOPE}"})
    )
    assert resolved.command == ["npx", "server"]
    assert resolved.missing == ["NOPE"]
    assert resolved.env == {"T": "${NOPE}"}  # 原样保留，暴露给补录流程


def test_credentials_fallback_and_env_precedence(isolated, monkeypatch):
    mcp_secrets.store_secret("github", "TOKEN", "cred-value")
    resolved = mcp_secrets.resolve(_server(env={"T": "${TOKEN}"}))
    assert resolved.env == {"T": "cred-value"}

    monkeypatch.setenv("TOKEN", "env-value")
    assert mcp_secrets.resolve(_server(env={"T": "${TOKEN}"})).env == {"T": "env-value"}


def test_store_preserves_existing_credentials(isolated):
    _, home = isolated
    path = home / "credentials.json"
    path.write_text(
        json.dumps({"key": "sk-provider", "mcp": {"other": {"K": "v"}}}),
        encoding="utf-8",
    )

    mcp_secrets.store_secret("github", "TOKEN", "secret-token")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["key"] == "sk-provider"
    assert data["mcp"]["other"] == {"K": "v"}
    assert data["mcp"]["github"]["TOKEN"] == "secret-token"


@pytest.mark.skipif(os.name == "nt", reason="Windows 依赖用户目录 ACL，无 POSIX 权限位")
def test_credentials_file_is_private(isolated):
    _, home = isolated
    mcp_secrets.store_secret("github", "TOKEN", "secret-token")
    mode = (home / "credentials.json").stat().st_mode & 0o777
    assert mode == 0o600


def test_clear_secret(isolated):
    assert mcp_secrets.clear_secret("github", "TOKEN") is False
    mcp_secrets.store_secret("github", "TOKEN", "x")
    assert mcp_secrets.clear_secret("github", "TOKEN") is True
    assert mcp_secrets.lookup("github", "TOKEN") is None


def test_expand_headers(isolated, monkeypatch):
    monkeypatch.setenv("MCP_TEST_TOKEN", "env-value")
    resolved = mcp_secrets.resolve(_server(
        type="http", url="https://a/mcp",
        headers={"Authorization": "Bearer ${MCP_TEST_TOKEN}"},
    ))
    assert resolved.headers == {"Authorization": "Bearer env-value"}
    assert resolved.missing == []
    assert "env-value" not in mcp_secrets.redactor().scrub("x env-value y")


def test_header_missing_recorded(isolated):
    resolved = mcp_secrets.resolve(_server(
        command=[], headers={"X-Token": "${NOPE_HEADER_VAR}"}
    ))
    assert resolved.missing == ["NOPE_HEADER_VAR"]
    assert resolved.headers == {"X-Token": "${NOPE_HEADER_VAR}"}


def test_redactor_ignores_short_values():
    redactor = mcp_secrets.Redactor()
    redactor.add("ab")
    assert redactor.scrub("ab") == "ab"
    redactor.add("long-secret")
    assert redactor.scrub("has long-secret inside") == "has *** inside"
