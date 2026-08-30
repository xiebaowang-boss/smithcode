"""权限系统测试：三级动作、通配符匹配、最后匹配优先、模式级记忆、配置加载。"""
import json

import pytest

from codeagent import config
from codeagent.permission import DEFAULT_RULES, Permission, evaluate, infer_trust_root


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


def test_rule_order_broad_first_narrow_last(make_perm, monkeypatch):
    """宽泛规则在前、精确规则在后（后者覆盖前者），配置文件书写顺序即优先级。"""
    refuse_input(monkeypatch)
    perm = make_perm(
        permissions={"run_command": {"*": "ask", "git status": "allow", "rm -rf*": "deny"}}
    )
    assert perm.check("run_command", {"command": "git status"}) is True
    assert perm.check("run_command", {"command": "rm -rf /"}) is False


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


# ---------- 路径模式归一化（多根授权 --add） ----------

def test_pattern_normalizes_to_root_relative(make_perm, tmp_path, monkeypatch):
    """相对、绝对、跨根三种写法归一化到同一种"相对授权根"模式。"""
    make_perm()  # 把 WORKSPACE_ROOT 指到 tmp_path
    extra = tmp_path.parent / (tmp_path.name + "-extra")
    extra.mkdir()
    monkeypatch.setattr(config, "EXTRA_ROOTS", [str(extra)])

    assert Permission._pattern("edit_file", {"path": "src/a.py"}) == "src/a.py"
    assert (
        Permission._pattern("edit_file", {"path": str(tmp_path / "src" / "a.py")})
        == "src/a.py"
    )
    assert (
        Permission._pattern("edit_file", {"path": f"../{extra.name}/src/a.py"})
        == "src/a.py"
    )


def test_pattern_keeps_command_text_verbatim(make_perm, tmp_path, monkeypatch):
    """command 类参数不做路径归一化，保持原文以便命令模式匹配。"""
    make_perm()
    assert (
        Permission._pattern("run_command", {"command": "git status"})
        == "git status"
    )


def test_user_rule_matches_extra_root_path(make_perm, tmp_path, monkeypatch):
    """附加授权根内的路径与主工作区按同一模式约定匹配用户规则。"""
    refuse_input(monkeypatch)
    extra = tmp_path.parent / (tmp_path.name + "-extra")
    extra.mkdir()
    monkeypatch.setattr(config, "EXTRA_ROOTS", [str(extra)])
    perm = make_perm(permissions={"write_file": {"src/*.py": "allow"}})

    assert perm.check("write_file", {"path": f"../{extra.name}/src/a.py"}) is True


# ---------- 越界访问确认（运行时动态信任） ----------

def test_infer_trust_root_finds_git_project_root(tmp_path):
    """目标路径的祖先存在 .git 时，信任整个项目根。"""
    proj = tmp_path / "projB"
    (proj / "src" / "deep").mkdir(parents=True)
    (proj / ".git").mkdir()
    target = proj / "src" / "deep" / "a.py"

    assert infer_trust_root(target) == proj


def test_infer_trust_root_falls_back_to_parent(tmp_path):
    """无 .git 祖先时退回目标所在目录。"""
    target = tmp_path / "plain" / "a.txt"
    assert infer_trust_root(target) == tmp_path / "plain"


def test_ask_outside_access_once_always_deny(tmp_path, monkeypatch):
    """[y] 仅本次不入库；[a] 信任根写入会话列表；[n] 拒绝。"""
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "SESSION_EXTRA_ROOTS", [])
    outside = tmp_path.parent / (tmp_path.name + "-out")
    outside.mkdir()
    perm = Permission()

    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert perm.ask_outside_access("x.py", outside / "x.py") == ("once", outside)
    assert config.SESSION_EXTRA_ROOTS == []

    monkeypatch.setattr("builtins.input", lambda _: "a")
    action, root = perm.ask_outside_access("x.py", outside / "x.py")
    assert (action, root) == ("always", outside)
    assert config.SESSION_EXTRA_ROOTS == [str(outside)]

    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert perm.ask_outside_access("x.py", outside / "x.py") == ("deny", None)


def test_widen_roots_is_temporary(tmp_path, monkeypatch):
    """widen_roots 只在 with 块内生效，退出后授权列表还原。"""
    extra = tmp_path / "widen-me"
    extra.mkdir()
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "SESSION_EXTRA_ROOTS", [])
    monkeypatch.setattr(config, "_WIDENED_ROOTS", [])

    base = config.allowed_roots()
    with config.widen_roots([extra]):
        widened = config.allowed_roots()
        assert len(widened) == len(base) + 1
        assert widened[-1] == extra.resolve()
    assert config.allowed_roots() == base


# ---------- 交互确认：非法输入重问，而不是静默判拒 ----------

def test_ask_reprompts_on_invalid_answer(make_perm, monkeypatch):
    """空行、乱文本等非 y/n/a 回答应重新询问，而不是当作拒绝。"""
    perm = make_perm()
    answers = iter(["", "随便粘贴的一行", "y"])
    prompts = []

    def _input(prompt=""):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", _input)
    assert perm.check("run_command", {"command": "ls"}) is True
    assert len(prompts) == 3  # 前两次无效、第三次 y，共问三轮


def test_ask_still_accepts_valid_answers(make_perm, monkeypatch):
    perm = make_perm()
    for answer, expected in (("y", True), ("n", False)):
        monkeypatch.setattr("builtins.input", lambda _, a=answer: a)
        assert perm.check("run_command", {"command": "ls"}) is expected


def test_ask_outside_access_reprompts_on_invalid_answer(tmp_path, monkeypatch):
    """越界路径确认同样对非法输入重问。"""
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "SESSION_EXTRA_ROOTS", [])
    outside = tmp_path.parent / (tmp_path.name + "-out2")
    outside.mkdir()
    perm = Permission()

    answers = iter(["糊了", "n"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert perm.ask_outside_access("x.py", outside / "x.py") == ("deny", None)
