"""权限系统测试：三级动作、通配符匹配、最后匹配优先、模式级记忆、配置加载。"""
import sys

import pytest

from smithcode import config
from smithcode.permission import (
    ALLOW,
    ASK,
    DEFAULT_RULES,
    DENY,
    Permission,
    evaluate,
    infer_trust_root,
)


@pytest.fixture(autouse=True)
def enable_prompting(monkeypatch):
    """pytest 环境下 stdin 非 TTY，显式放行交互确认，否则权限确认会全部 fail-closed 拒绝。"""
    monkeypatch.setattr("smithcode.permission.confirmations_available", lambda: True)


def refuse_input(monkeypatch):
    """让任何未预期的交互确认直接使测试失败。"""
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("不应弹出交互确认"))


@pytest.fixture
def make_perm(tmp_path, monkeypatch):
    """工厂：可选地写入全局 config.toml（经 SMITHCODE_HOME 隔离），并把工作区指向临时目录。"""

    def _make(permissions=None, raw=None):
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        monkeypatch.setenv("SMITHCODE_HOME", str(home))
        if raw is not None:
            (home / "config.toml").write_text(raw, encoding="utf-8")
        elif permissions is not None:
            lines = ["[permissions]"]
            for tool, value in permissions.items():
                if isinstance(value, str):
                    lines.append(f'{tool} = "{value}"')
                else:
                    pairs = ", ".join(f'"{pattern}" = "{action}"' for pattern, action in value.items())
                    lines.append(f"{tool} = {{ {pairs} }}")
            (home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
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


def test_default_protected_paths():
    """保护路径：.git 只读（禁止写入/编辑），读取放行；.gitignore 不受影响。"""
    assert evaluate("read_file", ".git/config", DEFAULT_RULES)[2] == "allow"
    assert evaluate("write_file", ".git/config", DEFAULT_RULES)[2] == "deny"
    assert evaluate("write_file", "src/.git/hooks/x.py", DEFAULT_RULES)[2] == "deny"
    assert evaluate("edit_file", ".git/index", DEFAULT_RULES)[2] == "deny"
    assert evaluate("write_file", ".gitignore", DEFAULT_RULES)[2] == "ask"


def test_windows_case_insensitive_matching():
    """Windows 下规则匹配大小写不敏感（对齐 opencode v2），其他平台保持大小写敏感。"""
    result = evaluate("read_file", "SRC/A.PY", [("read_file", "src/*.py", ALLOW)])[2]
    if sys.platform == "win32":
        assert result == ALLOW
    else:
        assert result == ASK


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
    perm = make_perm(raw="this is not valid toml")
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


# ---------- 非交互 fail-closed ----------

def test_non_interactive_ask_denied(make_perm, monkeypatch):
    """非交互 stdin 下 ask 操作直接拒绝，不调用 input、不因 EOFError 崩溃。"""
    monkeypatch.setattr("smithcode.permission.confirmations_available", lambda: False)
    refuse_input(monkeypatch)
    perm = make_perm()

    assert perm.check("write_file", {"path": "a.txt"}) is False


def test_non_interactive_outside_access_denied(tmp_path, monkeypatch):
    """非交互 stdin 下越界访问直接拒绝，不尝试询问。"""
    monkeypatch.setattr("smithcode.permission.confirmations_available", lambda: False)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "SESSION_EXTRA_ROOTS", [])
    outside = tmp_path.parent / (tmp_path.name + "-out")
    outside.mkdir()
    perm = Permission()

    assert perm.ask_outside_access("x.py", outside / "x.py") == ("deny", None)


def test_approved_all_auto_approves_outside_access(tmp_path, monkeypatch):
    """/-y（approved_all）覆盖越界访问确认：静默放行本次访问，不弹确认、不留会话级信任。"""
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "SESSION_EXTRA_ROOTS", [])
    outside = tmp_path.parent / (tmp_path.name + "-out")
    outside.mkdir()
    perm = Permission()
    perm.approved_all = True

    action, root = perm.ask_outside_access("x.py", outside / "x.py")
    assert (action, root) == ("once", outside)
    assert config.SESSION_EXTRA_ROOTS == []  # "仅本次"语义，不写入会话级信任


# ---------- family 机制与多资源聚合 ----------

def test_evaluate_multi_key_family():
    """evaluate 的 permission 参数支持 (工具名, family) 元组，任一 key 命中即匹配。"""
    rules = [("edit_file", "*", ASK)]
    assert evaluate(("apply_patch", "edit_file"), "src/a.py", rules)[2] == ASK
    assert evaluate(("apply_patch", "edit_file"), "x", [])[0] == "apply_patch"  # 无匹配默认用工具名


def test_apply_patch_inherits_edit_protected_paths():
    """apply_patch（family=edit_file）自动继承 edit_file 的 .git 保护规则。"""
    keys = ("apply_patch", "edit_file")
    assert evaluate(keys, ".git/config", DEFAULT_RULES)[2] == "deny"
    assert evaluate(keys, "src/.git/hooks/x.py", DEFAULT_RULES)[2] == "deny"
    assert evaluate(keys, "src/main.py", DEFAULT_RULES)[2] == "ask"


def test_specific_rule_overrides_family_rule():
    """工具名精确规则排在 family 规则之后时覆盖 family（last match wins）。"""
    rules = [("edit_file", "*", ASK), ("apply_patch", "*", DENY)]
    assert evaluate(("apply_patch", "edit_file"), "a.py", rules)[2] == "deny"
    assert evaluate(("edit_file",), "a.py", rules)[2] == "ask"


def test_check_paths_any_deny_rejects(make_perm, monkeypatch):
    """聚合检查：任一路径命中 deny（.git 保护路径）即整体拒绝，不询问。"""
    refuse_input(monkeypatch)
    perm = make_perm()
    assert perm.check_paths("apply_patch", ["src/a.py", ".git/config"]) is False
    assert perm.check_paths("apply_patch", ["src/a.py", "x/.git/hooks/y.py"]) is False


def test_check_paths_any_ask_prompts(make_perm, monkeypatch):
    """聚合检查：任一 ask 弹一次交互确认（而非每路径各问一次）。"""
    perm = make_perm()
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert perm.check_paths("apply_patch", ["a.py", "b.py"]) is True


def test_check_paths_approved_all_skips_ask(make_perm, monkeypatch):
    """-y（approved_all）跳过聚合检查中的 ask，但 deny 依然生效。"""
    refuse_input(monkeypatch)
    perm = make_perm()
    perm.approved_all = True
    assert perm.check_paths("apply_patch", ["a.py", "b.py"]) is True
    assert perm.check_paths("apply_patch", ["a.py", ".git/config"]) is False
