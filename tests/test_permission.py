"""权限系统测试：三级动作、通配符匹配、最后匹配优先、模式级记忆、配置加载。"""
import json

import pytest

from codeagent import config
from codeagent.permission import DEFAULT_RULES, Permission, evaluate


def refuse_input(monkeypatch):
    """让任何未预期的交互确认直接使测试失败。"""
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("不应弹出交互确认"))


@pytest.fixture
def make_perm(tmp_path, monkeypatch):
    """工厂：可选地写入 codeagent.json，并把工作区指向临时目录。"""

    def _make(permissions=None, raw=None):
        if raw is not None:
            (tmp_path / "codeagent.json").write_text(raw, encoding="utf-8")
        elif permissions is not None:
            (tmp_path / "codeagent.json").write_text(
                json.dumps({"permissions": permissions}), encoding="utf-8"
            )
        monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
        return Permission()

    return _make


# ---------- evaluate：规则求值 ----------

def test_evaluate_last_match_wins():
    rules = [
        ("run_command", "*", "ask"),
        ("run_command", "git *", "allow"),
        ("run_command", "git push*", "deny"),
    ]
    assert evaluate("run_command", "git status", rules)[2] == "allow"
    assert evaluate("run_command", "git push origin main", rules)[2] == "deny"
    assert evaluate("run_command", "ls -la", rules)[2] == "ask"


def test_evaluate_defaults_to_ask_when_no_match():
    assert evaluate("unknown_tool", "*", [])[2] == "ask"


def test_default_rules_actions():
    assert evaluate("read_file", "a.txt", DEFAULT_RULES)[2] == "allow"
    assert evaluate("list_dir", ".", DEFAULT_RULES)[2] == "allow"
    assert evaluate("write_file", "a.txt", DEFAULT_RULES)[2] == "ask"
    assert evaluate("run_command", "ls", DEFAULT_RULES)[2] == "ask"


# ---------- check：基础动作 ----------

def test_safe_tools_pass_without_asking(make_perm, monkeypatch):
    refuse_input(monkeypatch)
    assert make_perm().check("read_file", {"path": "a.txt"}) is True
    assert make_perm().check("list_dir", {}) is True


def test_ask_denied_by_user(make_perm, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert make_perm().check("write_file", {"path": "a.txt"}) is False


def test_ask_approved_once_does_not_remember(make_perm, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "y")
    perm = make_perm()
    assert perm.check("write_file", {"path": "a.txt"}) is True
    assert perm.session_rules == []


# ---------- check：模式级"总是允许" ----------

def test_always_remembers_pattern_not_tool(make_perm, monkeypatch):
    answers = iter(["a", "n"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    perm = make_perm()

    assert perm.check("run_command", {"command": "git status"}) is True
    # 相同模式不再询问
    assert perm.check("run_command", {"command": "git status"}) is True
    # 其他模式仍会询问（第二次答案为 n）
    assert perm.check("run_command", {"command": "rm -rf /"}) is False


# ---------- check：deny 与 -y ----------

def test_deny_blocks_even_with_approved_all(make_perm):
    perm = make_perm(permissions={"run_command": {"rm -rf*": "deny"}})
    perm.approved_all = True
    assert perm.check("run_command", {"command": "rm -rf /"}) is False


def test_approved_all_overrides_ask(make_perm):
    refuse_input = None  # noqa: F841
    perm = make_perm(permissions={"write_file": "ask"})
    perm.approved_all = True
    assert perm.check("write_file", {"path": "a.txt"}) is True


def test_user_allow_skips_asking(make_perm, monkeypatch):
    refuse_input(monkeypatch)
    perm = make_perm(permissions={"run_command": {"git *": "allow"}})
    assert perm.check("run_command", {"command": "git status"}) is True


def test_user_rules_override_defaults(make_perm, monkeypatch):
    # 默认 read_file 是 allow，用户可收紧为 ask
    monkeypatch.setattr("builtins.input", lambda _: "n")
    perm = make_perm(permissions={"read_file": "ask"})
    assert perm.check("read_file", {"path": "secret.env"}) is False


def test_unknown_action_degrades_to_ask(make_perm, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "n")
    perm = make_perm(permissions={"write_file": {"*.env": "block"}})
    assert perm.check("write_file", {"path": ".env"}) is False


# ---------- 配置加载 ----------

def test_missing_config_uses_defaults(make_perm, monkeypatch):
    refuse_input(monkeypatch)
    perm = make_perm()
    assert perm.user_rules == []
    assert perm.check("read_file", {"path": "a.txt"}) is True


def test_broken_json_degrades_gracefully(make_perm, monkeypatch, capsys):
    refuse_input(monkeypatch)
    perm = make_perm(raw="{ not valid json")
    assert perm.user_rules == []
    assert "警告" in capsys.readouterr().out
