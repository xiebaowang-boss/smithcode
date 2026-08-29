"""权限控制测试：白名单、逐个确认、会话内记忆。"""
from codeagent.permission import Permission


def test_safe_tools_always_allowed():
    p = Permission()
    assert p.check("read_file") is True
    assert p.check("list_dir") is True


def test_denied_without_input(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "n")
    p = Permission()
    assert p.check("write_file") is False


def test_approved_once(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "y")
    p = Permission()
    assert p.check("write_file") is True
    # 未选 "a"，下次仍需询问
    assert "write_file" not in p.session_approved


def test_approved_for_session(monkeypatch):
    answers = iter(["a"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    p = Permission()
    assert p.check("run_command") is True
    # 之后不再询问
    assert p.check("run_command") is True
    assert "run_command" in p.session_approved


def test_approved_all_skips_asking():
    p = Permission()
    p.approved_all = True
    assert p.check("run_command") is True
