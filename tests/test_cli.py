"""CLI 测试：多行输入合并与缺配置时的启动退出。"""

import os
import sys

import pytest

from smithcode import config
from smithcode.cli import main
from smithcode.utils.terminal import (
    enable_readline,
    read_user_input,
    stdin_has_pending,
)

# ---------- 启动时缺配置：优雅退出而非裸 traceback ----------

def test_main_exits_gracefully_on_config_error(monkeypatch, capsys):
    """缺 API Key 时打印修复指引并以退出码 1 结束，不甩 SDK 异常栈。"""
    def broken_agent(*args, **kwargs):
        raise config.ConfigError("[启动失败] 缺少 API Key：没有找到任何配置")

    monkeypatch.setattr("smithcode.cli.Agent", broken_agent)
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 1
    assert "缺少 API Key" in capsys.readouterr().out


# ---------- read_user_input：多行粘贴合并 ----------

class _TtyStdin:
    def isatty(self):
        return True


class _NotTtyStdin:
    def isatty(self):
        return False


class _SelectableTtyStdin:
    """带真实 fd 的交互 stdin 桩：供 select() 探测排队数据。"""

    def __init__(self, fd):
        self._fd = fd

    def isatty(self):
        return True

    def fileno(self):
        return self._fd


def _fake_input(replies, calls):
    """按顺序返回预备好的输入行，并记录每次的提示符。"""
    def _input(prompt=""):
        calls.append(prompt)
        return replies.pop(0)
    return _input


def test_read_user_input_merges_pasted_lines(monkeypatch):
    """首次回车后缓冲区仍有排队内容（粘贴特征），应合并为同一条多行消息。"""
    monkeypatch.setattr(sys, "stdin", _TtyStdin())
    replies = ["第一行", "第二行", "第三行"]
    calls = []
    monkeypatch.setattr("builtins.input", _fake_input(replies, calls))
    # 首行后两次检测到排队内容，随后缓冲区静默
    pending = iter([True, True, False, False])
    monkeypatch.setattr("smithcode.utils.terminal.stdin_has_pending", lambda: next(pending))

    assert read_user_input() == "第一行\n第二行\n第三行"
    assert len(calls) == 3


def test_read_user_input_single_line_when_buffer_silent(monkeypatch):
    """普通单行输入：缓冲区静默即返回，不额外读取。"""
    monkeypatch.setattr(sys, "stdin", _TtyStdin())
    replies = ["就这一句"]
    calls = []
    monkeypatch.setattr("builtins.input", _fake_input(replies, calls))
    monkeypatch.setattr("smithcode.utils.terminal.stdin_has_pending", lambda: False)

    assert read_user_input() == "就这一句"
    assert len(calls) == 1


def test_read_user_input_skips_merge_for_piped_stdin(monkeypatch):
    """非交互 stdin（管道/脚本）不做粘贴合并，行为与原来一致。"""
    monkeypatch.setattr(sys, "stdin", _NotTtyStdin())
    replies = ["一行"]
    calls = []
    monkeypatch.setattr("builtins.input", _fake_input(replies, calls))

    assert read_user_input() == "一行"
    assert len(calls) == 1


# ---------- enable_readline：加载 readline 并关闭 bracketed paste ----------

def test_enable_readline_noop_on_windows(monkeypatch):
    """Windows 下为 no-op，不尝试导入 readline。"""
    monkeypatch.setattr(sys, "platform", "win32")
    real_import = __import__

    def fail_import(name, *args, **kwargs):
        if name == "readline":
            raise AssertionError("win32 不应尝试导入 readline")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fail_import)

    assert enable_readline() is None


def test_enable_readline_survives_missing_module(monkeypatch):
    """无 readline 模块（裁剪构建）时优雅跳过，不阻止启动。"""
    monkeypatch.setattr(sys, "platform", "linux")
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "readline":
            raise ImportError("no readline")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    assert enable_readline() is None


def test_enable_readline_disables_bracketed_paste(monkeypatch):
    """Linux 下导入 readline 并关闭 bracketed paste（保护多行合并的 select 探测）。"""
    calls = []

    class _FakeReadline:
        @staticmethod
        def parse_and_bind(binding):
            calls.append(binding)

    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "readline":
            return _FakeReadline()
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    monkeypatch.setattr(sys, "platform", "linux")

    assert enable_readline() is None
    assert calls == ["set enable-bracketed-paste off"]


def test_enable_readline_survives_bind_error(monkeypatch):
    """parse_and_bind 抛错（如终端异常）时优雅跳过，不阻止启动。"""
    class _BoomReadline:
        @staticmethod
        def parse_and_bind(binding):
            raise ValueError("terminal not ready")

    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "readline":
            return _BoomReadline()
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    monkeypatch.setattr(sys, "platform", "linux")

    assert enable_readline() is None


# ---------- stdin_has_pending：Linux 下 select 探测排队输入 ----------

def test_stdin_has_pending_detects_queued_input_on_posix(monkeypatch):
    """POSIX 交互终端：fd 有排队数据时 select 能探测到。"""
    r, w = os.pipe()
    try:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(sys, "stdin", _SelectableTtyStdin(r))
        os.write(w, b"x")

        assert stdin_has_pending() is True
    finally:
        os.close(r)
        os.close(w)


def test_stdin_has_pending_silent_when_empty_on_posix(monkeypatch):
    """POSIX 交互终端：fd 无数据时不误报排队。"""
    r, w = os.pipe()
    try:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(sys, "stdin", _SelectableTtyStdin(r))

        assert stdin_has_pending() is False
    finally:
        os.close(r)
        os.close(w)


def test_stdin_has_pending_false_for_piped_stdin(monkeypatch):
    """非交互 stdin（管道）在 POSIX 下一律返回 False，不做探测。"""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "stdin", _NotTtyStdin())

    assert stdin_has_pending() is False
