"""MCP 向导状态机测试：分支、校验、回退与计划生成。"""

from smithcode.mcp import secrets
from smithcode.mcp.wizard import McpWizard, run_line_mode


def _wizard(workspace="/ws"):
    return McpWizard(workspace=workspace)


def _drive(wizard, answers):
    """按步骤顺序喂答案；返回错误（None 表示全部成功）。"""
    for value in answers:
        step = wizard.current()
        assert step is not None, "向导提前结束"
        error = wizard.submit(value)
        if error:
            return error
    return None


def test_template_flow_plan():
    wizard = _wizard()
    error = _drive(wizard, ["template", "everything", "my-everything", "user", "finish"])
    assert error is None
    assert wizard.done
    plan = wizard.to_plan()
    assert plan.scope == "user"
    assert plan.config.name == "my-everything"
    assert plan.config.command[:2] == ["npx", "-y"]
    assert plan.config.env == {}
    assert plan.secrets == []


def test_template_workspace_substitution_and_defaults():
    wizard = _wizard(workspace="/home/me/proj")
    _drive(wizard, ["template", "filesystem"])
    step = wizard.current()
    assert step.key == "name"
    assert step.default == "filesystem"
    _drive(wizard, ["", "user", "finish"])
    plan = wizard.to_plan()
    assert "/home/me/proj" in plan.config.command


def test_manual_flow_with_secrets():
    wizard = _wizard()
    error = _drive(wizard, [
        "manual",
        "npx -y @example/server-thing",
        "TOKEN, OTHER",
        "my-thing",
        "project",
        "store",
        "s3cret",
        "env",
        "finish",
    ])
    assert error is None
    plan = wizard.to_plan()
    assert plan.scope == "project"
    assert plan.config.name == "my-thing"
    assert plan.config.command == ["npx", "-y", "@example/server-thing"]
    assert plan.config.env == {"TOKEN": "${TOKEN}", "OTHER": "${OTHER}"}
    assert plan.secrets == [("TOKEN", "s3cret")]
    assert plan.env_refs == ["OTHER"]


def test_validation_errors():
    wizard = _wizard()
    assert _drive(wizard, ["manual", "npx pkg", "x 1"]) == "环境变量名不合法: 1"
    wizard = _wizard()
    assert _drive(wizard, ["manual", "npx pkg", "", ""]) == "名称不能为空"
    wizard = _wizard()
    assert _drive(wizard, ["manual", "npx pkg", "TOKEN", "svc", "user", "store", ""]) == (
        "密钥不能为空（可返回选择其他方式）"
    )


def test_back_navigation():
    wizard = _wizard()
    _drive(wizard, ["template"])
    assert wizard.current().key == "template"
    assert wizard.back() is True
    assert wizard.current().key == "source"
    assert wizard.back() is False


def test_switching_source_clears_branch_state():
    wizard = _wizard()
    _drive(wizard, ["template", "github"])
    assert wizard.draft["env_vars"] == ["GITHUB_PERSONAL_ACCESS_TOKEN"]
    wizard.back()  # name -> template
    wizard.back()  # template -> source
    _drive(wizard, ["manual"])
    assert wizard.draft["env_vars"] == []
    assert wizard.draft["command"] == ""


def test_preview_is_redacted_and_mentions_scope():
    wizard = _wizard(workspace="/proj")
    _drive(wizard, ["manual", "npx -y pkg", "TOKEN", "svc", "project", "store", "hunter2", "finish"])
    plan = wizard.to_plan()
    preview = wizard.preview()
    assert ".smithcode" in preview and "mcp.json" in preview
    assert "hunter2" not in preview
    assert "凭据库" in preview
    assert plan.config.env["TOKEN"] == "${TOKEN}"


def test_default_name_derivation():
    wizard = _wizard()
    _drive(wizard, ["manual", "npx -y @modelcontextprotocol/server-github", ""])
    assert wizard.current().default == "github"
    wizard2 = _wizard()
    _drive(wizard2, ["manual", "npx -y some-package", ""])
    assert wizard2.current().default == "some-package"


def test_run_line_mode_scripted(monkeypatch):
    answers = iter(["", "", "", "", ""])

    def fake_input(prompt=""):
        return next(answers)

    plan = run_line_mode(input_fn=fake_input, print_fn=lambda *a, **k: None)
    assert plan is not None
    assert plan.config.name == "filesystem"


def test_run_line_mode_cancel():
    plan = run_line_mode(
        input_fn=lambda prompt="": "q", print_fn=lambda *a, **k: None
    )
    assert plan is None


def test_apply_plan_stores_secrets_then_adds(tmp_path, monkeypatch):
    from smithcode.mcp.config import ServerConfig
    from smithcode.mcp.wizard import WizardPlan, apply_plan

    monkeypatch.setenv("SMITHCODE_HOME", str(tmp_path / "home"))
    secrets.redactor().clear()
    calls = []

    class FakeService:
        def add(self, cfg, scope):
            calls.append((cfg.name, scope))
            return object()

    plan = WizardPlan(
        config=ServerConfig(name="svc", command=["npx", "x"]),
        scope="user",
        secrets=[("TOKEN", "abc123")],
    )
    apply_plan(FakeService(), plan)
    assert calls == [("svc", "user")]
    assert secrets.lookup("svc", "TOKEN") == "abc123"
