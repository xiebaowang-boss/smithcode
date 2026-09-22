"""TUI 复制到系统剪贴板的测试：优先系统工具、退回 OSC 52、文本经 stdin 传入。

全程 mock，绝不真的写当前用户的系统剪贴板。
"""
import asyncio

import pytest

from smithcode.agent import Agent
from smithcode.process import ProcessResult
from smithcode.session import Session
from smithcode.tui import clipboard
from smithcode.tui.app import SmithTUI


@pytest.fixture(autouse=True)
def _restore():
    clipboard.reset_cache()
    yield
    clipboard.reset_cache()


class FakeLLM:
    def chat_stream(self, messages, tools=None):
        yield ("message", {"role": "assistant", "content": "ok"})


def _app(monkeypatch):
    monkeypatch.setattr("smithcode.agent.LLMClient", lambda: FakeLLM())
    return SmithTUI(Agent(session=Session()))


def _fake_run(rc=0, status="ok", record=None):
    """伪造 process.run：按命令行返回结果，并记录 (命令, stdin 文本)。"""
    def run(command, *, timeout, cwd=None, env=None, input=None,
            capture_output=True, token=None):
        if record is not None:
            record.append((command, input))
        return ProcessResult(rc, "", "", status)
    return run


# ---------- clipboard.copy_to_system ----------

def test_copy_prefers_wl_copy_on_linux(monkeypatch):
    """Linux 下按 Wayland → X11 顺序挑工具，文本经 stdin 传入。"""
    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.setattr(clipboard.shutil, "which", lambda exe: f"/usr/bin/{exe}")
    calls = []
    monkeypatch.setattr(clipboard, "run_process", _fake_run(record=calls))

    assert clipboard.copy_to_system("你好\n世界") is True
    assert calls == [("wl-copy", "你好\n世界")]


def test_copy_falls_back_to_next_tool_on_failure(monkeypatch):
    """候选返回非 0（如 wl-copy 存在但当前非 Wayland 会话）时继续试下一个。"""
    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.setattr(clipboard.shutil, "which", lambda exe: f"/usr/bin/{exe}")
    attempted = []

    def run(command, *, timeout, cwd=None, env=None, input=None,
            capture_output=True, token=None):
        attempted.append(command)
        rc = 1 if command == "wl-copy" else 0
        return ProcessResult(rc, "", "", "ok")

    monkeypatch.setattr(clipboard, "run_process", run)
    assert clipboard.copy_to_system("文本") is True
    assert attempted == ["wl-copy", "xclip -selection clipboard"]


def test_copy_returns_false_when_no_tool_available(monkeypatch):
    monkeypatch.setattr(clipboard.shutil, "which", lambda exe: None)
    assert clipboard.copy_to_system("文本") is False


def test_copy_remembers_successful_tool(monkeypatch):
    """上次成功的工具排到最前，后续复制不再重复探测。"""
    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.setattr(clipboard.shutil, "which", lambda exe: f"/usr/bin/{exe}")

    def fail_first(command, *, timeout, cwd=None, env=None, input=None,
                   capture_output=True, token=None):
        rc = 1 if command == "wl-copy" else 0
        return ProcessResult(rc, "", "", "ok")

    monkeypatch.setattr(clipboard, "run_process", fail_first)
    assert clipboard.copy_to_system("第一次") is True  # wl-copy 失败 → 用 xclip

    calls = []
    monkeypatch.setattr(clipboard, "run_process", _fake_run(record=calls))
    assert clipboard.copy_to_system("第二次") is True
    assert calls == [("xclip -selection clipboard", "第二次")]  # 直接用记住的那个


def test_copy_never_puts_text_on_command_line(monkeypatch):
    """文本只走 stdin：含引号 / `$()` 的内容不得出现在命令行里。"""
    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.setattr(clipboard.shutil, "which", lambda exe: f"/usr/bin/{exe}")
    calls = []
    monkeypatch.setattr(clipboard, "run_process", _fake_run(record=calls))

    payload = '$(rm -rf /)"; echo pwned'
    assert clipboard.copy_to_system(payload) is True
    command, stdin_text = calls[0]
    assert command == "wl-copy"
    assert stdin_text == payload
    assert "$(" not in command and "pwned" not in command


def test_copy_does_not_capture_output(monkeypatch):
    """必须传 capture_output=False：剪贴板工具会 fork 到后台持有资源，
    捕获输出会让命令一直等到管道 EOF（实测从 0.1s 变成 6s 超时）。"""
    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.setattr(clipboard.shutil, "which", lambda exe: f"/usr/bin/{exe}")
    seen = {}

    def run(command, *, timeout, capture_output=True, **kwargs):
        seen["capture_output"] = capture_output
        return ProcessResult(0, "", "", "ok")

    monkeypatch.setattr(clipboard, "run_process", run)
    assert clipboard.copy_to_system("文本") is True
    assert seen["capture_output"] is False


# ---------- SmithTUI.copy_to_clipboard 接线 ----------

def test_app_copy_uses_system_tool_and_skips_osc52(monkeypatch):
    """系统工具成功时不写 OSC 52（VTE 系终端不支持该序列）。"""
    written = []
    monkeypatch.setattr(clipboard, "run_process", _fake_run())
    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.setattr(clipboard.shutil, "which", lambda exe: f"/usr/bin/{exe}")

    async def _case():
        app = _app(monkeypatch)
        async with app.run_test():
            monkeypatch.setattr(app._driver, "write", written.append)
            app.copy_to_clipboard("要复制的文本")
            assert app.clipboard == "要复制的文本"  # 应用内粘贴语义保留
            assert written == []  # 未写 OSC 52

    asyncio.run(_case())


def test_app_copy_falls_back_to_osc52(monkeypatch):
    """没有任何系统工具时退回 Textual 的 OSC 52 实现。"""
    written = []
    monkeypatch.setattr(clipboard.shutil, "which", lambda exe: None)

    async def _case():
        app = _app(monkeypatch)
        async with app.run_test():
            monkeypatch.setattr(app._driver, "write", written.append)
            app.copy_to_clipboard("要复制的文本")
            assert app.clipboard == "要复制的文本"
            assert written, "应退回 OSC 52"
            assert written[0].startswith("\x1b]52;c;")

    asyncio.run(_case())
