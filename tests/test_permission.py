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
    engine,
    evaluate,
    has_command_substitution,
    infer_trust_root,
    split_command,
)


@pytest.fixture(autouse=True)
def enable_prompting(monkeypatch):
    """pytest 环境下 stdin 非 TTY，显式放行交互确认，否则权限确认会全部 fail-closed 拒绝。"""
    monkeypatch.setattr("smithcode.permission.engine.confirmations_available", lambda: True)


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
    assert evaluate("webfetch", "https://example.com", DEFAULT_RULES)[2] == "allow"
    assert evaluate("websearch", "python", DEFAULT_RULES)[2] == "allow"
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


def test_webfetch_allowed_by_default(make_perm, monkeypatch):
    """webfetch 默认放行：抓取公开网页不再弹确认。"""
    refuse_input(monkeypatch)
    assert make_perm().check("webfetch", {"url": "https://example.com"}) is True


def test_webfetch_user_rule_can_tighten(make_perm, monkeypatch):
    """用户规则命中优先于内置默认：可把 webfetch 收紧回 ask。"""
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert make_perm({"webfetch": "ask"}).check("webfetch", {"url": "https://example.com"}) is False


def test_ask_denied_by_user(make_perm, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert make_perm().check("write_file", {"path": "a.txt"}) is False


def test_ask_approved_once_does_not_remember(make_perm, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "y")
    perm = make_perm()
    assert perm.check("write_file", {"path": "a.txt"}) is True
    assert perm.session_rules == []


@pytest.fixture
def capture():
    """捕获权限提示的两个通道：询问端口收到的参数 + Notice 事件。

    权限信息经 `frontend.current().confirm_choice(...)` 传参（标题 / 工具摘要 /
    选项小字都在那里成形），而提示文本走事件总线——所以这里两样都收。
    """
    from smithcode import event as event_module
    from smithcode import frontend
    from smithcode.event import Bus
    from smithcode.event.catalog import Notice

    class _Capture:
        def __init__(self):
            self.infos = []
            self.calls = []

        def confirm_choice(self, prompt, valid, hint, detail=None, descriptions=None,
                           content=None):
            self.calls.append((prompt, valid, detail, descriptions, content))
            return "n"

        def ask_form(self, questions):
            self.calls.append(("form", questions))
            return ["" for _ in questions]

        def ask_text(self, question):
            return ""

        def ask_choice(self, question, options, multiple=False, descriptions=None):
            return ""

        def on_event(self, env):
            if isinstance(env.data, Notice):
                self.infos.append(env.data.text)

    cap = _Capture()
    bus = Bus(session_id="t")
    bus.subscribe(cap.on_event)
    bus_token = event_module.activate(bus)
    asker_token = frontend.activate(cap)
    yield cap
    frontend.reset(asker_token)
    event_module.reset(bus_token)


def test_ask_renders_options_in_confirm_not_chat(make_perm, monkeypatch, capture):
    cap = capture
    """权限信息经 confirm_choice 传参：标题统一、工具摘要作为 content、副作用进选项小字。"""
    perm = make_perm(permissions={"write_file": "ask"})

    assert perm.check("write_file", {"path": "a.txt"}, content="write a.txt") is False
    assert cap.infos == []
    prompt, valid, detail, descriptions, content = cap.calls[0]
    assert "write_file" in prompt
    assert content == "write a.txt"  # 工具摘要紧跟标题
    assert not detail
    assert valid == "yna"
    assert "仅本次执行" in descriptions["y"]
    assert "本会话将记住" in descriptions["a"]


def test_ask_truncates_long_content_and_option_descriptions(make_perm, monkeypatch, capture):
    cap = capture
    """确认框不再全量打印：工具摘要与「总是允许」小字都压平并截断到 CONFIRM_LIMIT。"""
    perm = make_perm(permissions={"run_command": "ask"})
    command = "mytool --flag " + "x" * 200  # 未登记命令 → 记忆候选退回整段（足够长）

    assert perm.check("run_command", {"command": command},
                      content="运行测试 · command " + command) is False
    _prompt, _valid, _detail, descriptions, content = cap.calls[0]
    assert content.endswith("...")
    assert len(content) == engine.CONFIRM_LIMIT
    assert descriptions["a"].endswith("...")
    assert len(descriptions["a"]) <= engine.CONFIRM_LIMIT
    # 截断只影响展示，权限模式（记忆候选的规则键）仍是完整命令
    assert perm.session_rules == []


def test_ask_clips_content_to_single_line(make_perm, monkeypatch, capture):
    cap = capture
    """content 内含换行时压成单行，避免撑破确认框布局。"""
    perm = make_perm(permissions={"write_file": "ask"})

    assert perm.check("write_file", {"path": "a.txt"},
                      content="write a.txt\n第二行") is False
    _prompt, _valid, _detail, _descriptions, content = cap.calls[0]
    assert content == "write a.txt 第二行"


# ---------- check：模式级"总是允许" ----------

def test_always_remembers_pattern_not_tool(make_perm, monkeypatch):
    # 用非安全命令（npm 不在只读安全集内），否则会走安全免确认而不弹框
    answers = iter(["a", "n"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    perm = make_perm()

    assert perm.check("run_command", {"command": "npm test"}) is True
    # 相同模式不再询问
    assert perm.check("run_command", {"command": "npm test"}) is True
    # 其他模式仍会询问（第二次答案为 n）
    assert perm.check("run_command", {"command": "npm run build"}) is False


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


# ---------- run_command 复合命令拆分求值 ----------

def test_split_command_segments():
    assert split_command("git status && git log") == ["git status", "git log"]
    assert split_command("a; b | c & d || e") == ["a", "b", "c", "d", "e"]
    assert split_command("echo 'a && b'") == ["echo 'a && b'"]  # 引号内不切
    assert split_command('echo "x | y"') == ['echo "x | y"']
    assert split_command("git commit -m 'fix: a; b'") == ["git commit -m 'fix: a; b'"]
    assert split_command('echo "he said \\"hi; ok\\" now"') == ['echo "he said \\"hi; ok\\" now"']
    assert split_command("git add .;\n git commit") == ["git add .", "git commit"]  # 换行视同 ;
    assert split_command("  ") == []  # 空段丢弃


def test_has_command_substitution():
    assert has_command_substitution("git log $(rm -rf /)") is True
    assert has_command_substitution("echo `whoami`") is True
    assert has_command_substitution('git commit -m "$(date)"') is True  # 双引号内仍算
    assert has_command_substitution("echo '$(safe)'") is False  # 单引号内不展开
    assert has_command_substitution("git status") is False
    assert has_command_substitution("echo cost: $5") is False  # 非 $( 不算


def test_compound_command_any_ask_prompts(make_perm, monkeypatch):
    """放行 git status 后不能借 && 偷渡其他命令：任一段 ask 则整体询问。"""
    perm = make_perm(permissions={"run_command": {"git status": "allow"}})
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert perm.check("run_command", {"command": "git status && rm -rf /"}) is False


def test_compound_command_any_deny_blocks_even_approved_all(make_perm):
    """任一段命中 deny 即拒绝，-y 也不放行。"""
    perm = make_perm(permissions={"run_command": {"git *": "allow", "rm -rf*": "deny"}})
    perm.approved_all = True
    assert perm.check("run_command", {"command": "git status && rm -rf /"}) is False


def test_compound_command_all_allow_passes(make_perm, monkeypatch):
    """各段都有 allow 规则时整体放行，不弹确认。"""
    refuse_input(monkeypatch)
    perm = make_perm(permissions={"run_command": {"git *": "allow"}})
    assert perm.check("run_command", {"command": "git status && git log -1"}) is True


def test_pipe_segments_each_evaluated(make_perm, monkeypatch):
    """管道两段分别求值：前段 allow、后段 ask → 整体询问。"""
    perm = make_perm(permissions={"run_command": {"git log*": "allow", "grep *": "ask"}})
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert perm.check("run_command", {"command": "git log | grep TODO"}) is False
    assert perm.check("run_command", {"command": "git log"}) is True


def test_command_substitution_forces_ask(make_perm, monkeypatch):
    """命令替换体无法静态求值（$() / 反引号），即使外层命令被 allow 也强制询问。"""
    perm = make_perm(permissions={"run_command": {"git *": "allow"}})
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert perm.check("run_command", {"command": "git log $(rm -rf /)"}) is False
    assert perm.check("run_command", {"command": "git commit -m \"$(date)\""}) is False
    assert perm.check("run_command", {"command": "echo `git push`"}) is False
    # 单引号内的 $( 不展开，不算替换
    assert perm.check("run_command", {"command": "git commit -m '$(date)'"}) is True


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


def test_ask_outside_access_renders_detail(tmp_path, monkeypatch, capture):
    cap = capture
    """越界路径授权信息也走 confirm_choice 传参，聊天区不重复打印。"""
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "SESSION_EXTRA_ROOTS", [])
    outside = tmp_path.parent / (tmp_path.name + "-od")
    outside.mkdir()

    assert Permission().ask_outside_access("x.py", outside / "x.py") == ("deny", None)
    assert cap.infos == []
    prompt, _valid, detail, descriptions, content = cap.calls[0]
    assert "目录之外" in prompt and "x.py" in prompt
    assert not detail and content is None
    assert "仅本次访问" in descriptions["y"]
    assert "信任目录" in descriptions["a"]


def test_ask_outside_access_truncates_long_option_descriptions(tmp_path, monkeypatch, capture):
    cap = capture
    """越界授权的选项小字含长路径时同样截断，不把整条路径铺满终端。"""
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "SESSION_EXTRA_ROOTS", [])
    outside = tmp_path.parent / (tmp_path.name + "-" + "o" * 80)
    outside.mkdir()

    assert Permission().ask_outside_access("x.py", outside / "x.py") == ("deny", None)
    _prompt, _valid, _detail, descriptions, _content = cap.calls[0]
    assert descriptions["y"].endswith("...")
    assert descriptions["a"].endswith("...")
    assert len(descriptions["a"]) == engine.CONFIRM_LIMIT


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
    assert perm.check("run_command", {"command": "rm -rf build"}) is True
    assert len(prompts) == 3  # 前两次无效、第三次 y，共问三轮


def test_ask_still_accepts_valid_answers(make_perm, monkeypatch):
    perm = make_perm()
    for answer, expected in (("y", True), ("n", False)):
        monkeypatch.setattr("builtins.input", lambda _, a=answer: a)
        assert perm.check("run_command", {"command": "rm -rf build"}) is expected


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
    monkeypatch.setattr("smithcode.permission.engine.confirmations_available", lambda: False)
    refuse_input(monkeypatch)
    perm = make_perm()

    assert perm.check("write_file", {"path": "a.txt"}) is False


def test_non_interactive_outside_access_denied(tmp_path, monkeypatch):
    """非交互 stdin 下越界访问直接拒绝，不尝试询问。"""
    monkeypatch.setattr("smithcode.permission.engine.confirmations_available", lambda: False)
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


def test_check_paths_always_remembers_exact_patterns(make_perm, monkeypatch):
    """聚合检查的"总是允许"按路径精确模式记忆：同路径再次调用放行，新路径仍走确认。"""
    perm = make_perm()
    answers = iter(["a"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert perm.check_paths("apply_patch", ["a.py", "b.py"]) is True
    # "a" 选项已为 a.py / b.py 各记一条精确规则，不再弹确认
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("不应再次弹确认"))
    assert perm.check_paths("apply_patch", ["a.py", "b.py"]) is True
    # 新路径不在记忆范围内，仍然需要确认（非交互 fail-closed 拒绝）
    monkeypatch.setattr("smithcode.permission.engine.confirmations_available", lambda: False)
    assert perm.check_paths("apply_patch", ["a.py", "c.py"]) is False


def test_check_paths_approved_all_skips_ask(make_perm, monkeypatch):
    """-y（approved_all）跳过聚合检查中的 ask，但 deny 依然生效。"""
    refuse_input(monkeypatch)
    perm = make_perm()
    perm.approved_all = True
    assert perm.check_paths("apply_patch", ["a.py", "b.py"]) is True
    assert perm.check_paths("apply_patch", ["a.py", ".git/config"]) is False


# ---------- 会话级权限模式（smith / accept_edits / auto） ----------

def test_mode_defaults_to_smith_and_still_prompts(make_perm, monkeypatch):
    perm = make_perm()
    assert perm.mode == "smith"
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert perm.check("write_file", {"path": "a.txt"}) is False


def test_accept_edits_allows_edit_family_without_asking(make_perm, monkeypatch):
    """accept_edits：编辑族（edit_file / write_file / apply_patch 族）自动放行，不弹确认。"""
    refuse_input(monkeypatch)
    perm = make_perm()
    perm.mode = "accept_edits"
    assert perm.check("edit_file", {"path": "src/a.py", "old_string": "x", "new_string": "y"}) is True
    assert perm.check("write_file", {"path": "src/a.py"}) is True
    assert perm.check_paths("apply_patch", ["a.py", "b.py"]) is True


def test_accept_edits_still_asks_run_command(make_perm, monkeypatch):
    """accept_edits 不放行命令执行，run_command 仍走确认。"""
    perm = make_perm()
    perm.mode = "accept_edits"
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert perm.check("run_command", {"command": "rm -rf build"}) is False


def test_accept_edits_respects_deny(make_perm, monkeypatch):
    """accept_edits 下 .git 保护路径依然拒绝。"""
    refuse_input(monkeypatch)
    perm = make_perm()
    perm.mode = "accept_edits"
    assert perm.check("write_file", {"path": ".git/config"}) is False


def test_auto_mode_allows_all_ask_tools(make_perm, monkeypatch):
    """auto：全部 ask 自动放行（含命令与多路径聚合），不弹确认。"""
    refuse_input(monkeypatch)
    perm = make_perm()
    perm.mode = "auto"
    assert perm.check("write_file", {"path": "a.txt"}) is True
    assert perm.check("run_command", {"command": "anything"}) is True
    assert perm.check_paths("apply_patch", ["a.py"]) is True


def test_auto_mode_respects_deny(make_perm):
    """auto 下 .git 保护路径依然拒绝。"""
    perm = make_perm()
    perm.mode = "auto"
    assert perm.check("write_file", {"path": ".git/config"}) is False


def test_approved_all_alias_maps_to_auto(make_perm, monkeypatch):
    """-y 兼容：置 approved_all = True 等价切到 auto 档，读回亦一致。"""
    refuse_input(monkeypatch)
    perm = make_perm()
    perm.approved_all = True
    assert perm.mode == "auto"
    assert perm.approved_all is True
    assert perm.check("run_command", {"command": "ls"}) is True


def test_cycle_mode_loops_through_three_modes(make_perm):
    """Shift+Tab 循环：smith → accept_edits → auto → smith。"""
    perm = make_perm()
    assert perm.cycle_mode() == "accept_edits"
    assert perm.cycle_mode() == "auto"
    assert perm.cycle_mode() == "smith"
    assert perm.cycle_mode() == "accept_edits"


def test_cycle_mode_syncs_approved_all_alias(make_perm):
    """切到 auto 时 approved_all 读值为 True，切回 smith 恢复 False。"""
    perm = make_perm()
    perm.cycle_mode()
    perm.cycle_mode()
    assert perm.approved_all is True
    perm.cycle_mode()
    assert perm.approved_all is False


# ---------- 安全只读命令免确认（shell_policy 集成） ----------

def test_safe_command_skips_prompt(make_perm, monkeypatch):
    """内置默认 ask 下，安全只读命令不弹确认（含安全命令组成的复合命令）。"""
    refuse_input(monkeypatch)
    perm = make_perm()
    assert perm.check("run_command", {"command": "git status"}) is True
    assert perm.check("run_command", {"command": "git status && git diff"}) is True


def test_unsafe_command_still_prompts(make_perm, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "n")
    perm = make_perm()
    assert perm.check("run_command", {"command": "rm -rf /"}) is False


def test_user_broad_ask_disables_safe_set(make_perm, monkeypatch):
    """宽泛 ask 等于整体关闭安全集（显式规则优先于内置默认）。"""
    monkeypatch.setattr("builtins.input", lambda _: "n")
    perm = make_perm(permissions={"run_command": "ask"})
    assert perm.check("run_command", {"command": "git status"}) is False


def test_user_specific_ask_keeps_safe_set_for_others(make_perm, monkeypatch):
    """精确 ask 仅收紧该命令，其余安全命令仍免确认。"""
    monkeypatch.setattr("builtins.input", lambda _: "n")
    perm = make_perm(permissions={"run_command": {"git push *": "ask"}})
    assert perm.check("run_command", {"command": "git status"}) is True
    assert perm.check("run_command", {"command": "git push origin"}) is False


def test_user_deny_beats_safe_set(make_perm, monkeypatch):
    refuse_input(monkeypatch)
    perm = make_perm(permissions={"run_command": {"git status": "deny"}})
    assert perm.check("run_command", {"command": "git status"}) is False


def test_cd_with_git_prompts(make_perm, monkeypatch):
    """cd 改变目录 + git 同现：即使两段各自安全也整体询问。"""
    monkeypatch.setattr("builtins.input", lambda _: "n")
    perm = make_perm()
    assert perm.check("run_command", {"command": "cd sub && git status"}) is False


def test_cd_noop_with_git_allowed(make_perm, monkeypatch):
    """cd .（no-op）不触发守卫，安全命令整体放行。"""
    refuse_input(monkeypatch)
    perm = make_perm()
    assert perm.check("run_command", {"command": "cd . && git status"}) is True


def test_substitution_forces_prompt(make_perm, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "n")
    perm = make_perm()
    assert perm.check("run_command", {"command": "git status && echo $(rm -rf /)"}) is False


def test_unknown_action_degrades_to_ask_for_command(make_perm, monkeypatch):
    """非法动作名在命令路径同样降级为 ask（不静默放行）。"""
    monkeypatch.setattr("builtins.input", lambda _: "n")
    perm = make_perm(permissions={"run_command": {"git status": "block"}})
    assert perm.check("run_command", {"command": "git status"}) is False


# ---------- "总是允许"前缀记忆 ----------

def test_always_remembers_command_prefix(make_perm, monkeypatch):
    """批准 python -m pytest 后，同族命令（文件/参数变化）免确认。"""
    monkeypatch.setattr("builtins.input", lambda _: "a")
    perm = make_perm()
    assert perm.check("run_command", {"command": "python -m pytest tests/a.py"}) is True
    assert ("run_command", ("python", "-m", "pytest"), "allow") in perm.session_rules

    refuse_input(monkeypatch)  # 后续不应再弹确认
    assert perm.check("run_command", {"command": "python -m pytest tests/b.py"}) is True
    assert perm.check("run_command", {"command": "python -m pytest -k foo"}) is True
    assert perm.check("run_command", {"command": "CI=1 python -m pytest"}) is True


def test_prefix_memory_does_not_overreach(make_perm, monkeypatch):
    """前缀只覆盖 pytest 族：别的模块 / 内联代码仍询问。"""
    monkeypatch.setattr("builtins.input", lambda _: "a")
    perm = make_perm()
    perm.check("run_command", {"command": "python -m pytest tests/a.py"})

    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert perm.check("run_command", {"command": "python -m http.server"}) is False
    assert perm.check("run_command", {"command": "python -c 'print(1)'"}) is False
    assert perm.check("run_command", {"command": "python other.py"}) is False


def test_python_inline_code_memory_is_exact(make_perm, monkeypatch):
    """python -c 无法推导前缀 → 精确记忆，换代码仍询问。"""
    monkeypatch.setattr("builtins.input", lambda _: "a")
    perm = make_perm()
    assert perm.check("run_command", {"command": "python -c 'print(1)'"}) is True
    assert ("run_command", "python -c 'print(1)'", "allow") in perm.session_rules

    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert perm.check("run_command", {"command": "python -c 'print(2)'"}) is False


def test_unknown_command_memory_is_exact(make_perm, monkeypatch):
    """未登记命令不做首 token 放宽，只精确记忆整段。"""
    monkeypatch.setattr("builtins.input", lambda _: "a")
    perm = make_perm()
    assert perm.check("run_command", {"command": "mytool build --fast"}) is True
    assert ("run_command", "mytool build --fast", "allow") in perm.session_rules

    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert perm.check("run_command", {"command": "mytool build --slow"}) is False


def test_compound_remembers_per_segment(make_perm, monkeypatch):
    """复合命令逐段记忆前缀，后续各段分别命中。"""
    monkeypatch.setattr("builtins.input", lambda _: "a")
    perm = make_perm()
    assert perm.check(
        "run_command", {"command": "pytest -k a && git commit -m x"}
    ) is True
    keys = [rule[1] for rule in perm.session_rules]
    assert ("pytest",) in keys
    assert ("git", "commit") in keys

    refuse_input(monkeypatch)
    assert perm.check(
        "run_command", {"command": "pytest -k b && git commit --amend"}
    ) is True


def test_cd_git_guard_does_not_offer_always(make_perm, monkeypatch):
    """cd 改目录 + git 的组合守卫不提供"总是允许"（选项无 a，非法输入后按 n 拒绝）。"""
    answers = iter(["a", "n"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    perm = make_perm()
    assert perm.check("run_command", {"command": "cd sub && git status"}) is False
    assert perm.session_rules == []
