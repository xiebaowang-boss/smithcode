"""CLI 测试：多行输入合并与缺配置时的启动退出。"""

import sys

import pytest

from smithcode import config
from smithcode.cli import main
from smithcode.utils.terminal import enable_utf8_erase, read_user_input

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


# ---------- enable_utf8_erase：Linux 下设置 IUTF8 修复中文退格 ----------

def test_enable_utf8_erase_noop_on_windows(monkeypatch):
    """Windows 下为 no-op，不尝试访问 termios。"""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdin", _TtyStdin())

    assert enable_utf8_erase() is None


def test_enable_utf8_erase_noop_for_piped_stdin(monkeypatch):
    """非交互 stdin（管道/CI）为 no-op，避免对非终端 fd 调 tcgetattr。"""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "stdin", _NotTtyStdin())

    assert enable_utf8_erase() is None


def test_enable_utf8_erase_survives_missing_termios(monkeypatch):
    """POSIX 交互终端但无 termios 模块时优雅跳过，不阻止启动。"""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "stdin", _TtyStdin())
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "termios":
            raise ImportError("no termios")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    assert enable_utf8_erase() is None
