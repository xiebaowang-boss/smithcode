"""命令执行工具测试。"""
from codeagent.tools.shell import run_command


def test_run_command_captures_output():
    output = run_command("echo hello")
    assert "hello" in output
    assert "[exit code: 0]" in output


def test_run_command_reports_failure():
    output = run_command("exit 3")
    assert "[exit code: 3]" in output
