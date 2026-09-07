"""命令执行工具测试。"""
from smithcode.tools.shell import run_command


def test_run_command_captures_output():
    output = run_command("echo hello")
    assert "hello" in output
    assert "[exit code: 0]" in output


def test_run_command_reports_failure():
    output = run_command("exit 3")
    assert "[exit code: 3]" in output


def test_run_command_timeout_clamped(monkeypatch):
    """timeout 参数被夹在 [1, 上限] 区间，未传时用默认值。"""
    captured = {}

    class FakeResult:
        stdout = "ok"
        stderr = ""
        returncode = 0

    def fake_run(command, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        return FakeResult()

    monkeypatch.setattr("smithcode.tools.shell.subprocess.run", fake_run)

    run_command("echo hi", timeout=9999)
    assert captured["timeout"] == 300
    run_command("echo hi", timeout=0)
    assert captured["timeout"] == 60
    run_command("echo hi", timeout=10)
    assert captured["timeout"] == 10
    run_command("echo hi")
    assert captured["timeout"] == 60


def test_run_command_timeout_expired():
    """超时返回带上限提示的错误，而不是抛异常。"""
    output = run_command("ping -n 5 127.0.0.1 >nul", timeout=1)
    assert "超时" in output
    assert "300" in output
