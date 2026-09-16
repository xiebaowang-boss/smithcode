"""命令执行工具测试。"""
import sys

from smithcode.process import ProcessResult
from smithcode.tools import DESCRIBERS
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

    def fake_run(command, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        return ProcessResult(0, "ok", "", "ok")

    monkeypatch.setattr("smithcode.tools.shell.run_process", fake_run)

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
    blocking = f'"{sys.executable}" -c "import time; time.sleep(5)"'
    output = run_command(blocking, timeout=1)
    assert "超时" in output
    assert "300" in output


def test_run_command_interrupted_message(monkeypatch):
    """进程被中断时的结果文案（process 层 status=interrupted 的映射）。"""
    monkeypatch.setattr(
        "smithcode.tools.shell.run_process",
        lambda command, **kwargs: ProcessResult(None, "", "", "interrupted"),
    )
    assert "中断" in run_command("sleep 999")


def test_run_command_accepts_description(monkeypatch):
    """description 是可选展示参数，不影响执行与超时逻辑。"""
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["timeout"] = kwargs["timeout"]
        return ProcessResult(0, "ok", "", "ok")

    monkeypatch.setattr("smithcode.tools.shell.run_process", fake_run)

    assert "ok" in run_command("echo hi", description="打印问候")
    assert captured["command"] == "echo hi"
    assert captured["timeout"] == 60
    # 不传 description 也可用（兼容旧调用 / 模型省略可选参数）
    assert "ok" in run_command("echo hi")


def test_run_command_describe_puts_description_before_command():
    """终端摘要：描述在前、命令详情在后；无描述时退回原格式。"""
    describe = DESCRIBERS["run_command"]

    assert describe({"command": "pytest -q", "description": "运行单元测试"}) == (
        "运行单元测试 · command pytest -q"
    )
    assert describe({"command": "pytest -q", "description": "运行单元测试",
                     "timeout": 120}) == "运行单元测试 · command pytest -q (timeout=120s)"
    assert describe({"command": "pytest -q"}) == "command pytest -q"
    assert describe({"command": "pytest -q", "description": "   "}) == "command pytest -q"


def test_run_command_schema_declares_optional_description():
    """schema 暴露 description 参数但非必填，并推荐填写。"""
    from smithcode.tools.base import all_schemas

    schema = next(s for s in all_schemas() if s["name"] == "run_command")
    params = schema["parameters"]
    assert "description" in params["properties"]
    assert params["required"] == ["command"]
    assert "5-10" in params["properties"]["description"]["description"]
