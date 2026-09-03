"""CLI 测试：用量速览格式与多行输入合并。"""

import sys

from smithcode.agent import Agent
from smithcode.cli import _print_usage_hint
from smithcode.session import Session
from smithcode.utils.terminal import read_user_input


def _agent_with_usage(monkeypatch, usage):
    class UsageLLM:
        def chat_stream(self, messages, tools=None):
            yield ("usage", usage)
            yield ("message", {"role": "assistant", "content": "ok"})

    monkeypatch.setattr("smithcode.agent.LLMClient", UsageLLM)
    agent = Agent(session=Session())
    agent.run("hi")
    return agent


def test_hint_shows_session_total_only(monkeypatch, capsys):
    """速览只剩会话累计一行（计费口径）；单次交互用量已由 run 逐条打印。"""
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "total_tokens": 110,
        "prompt_tokens_details": {"cached_tokens": 80},
    }
    agent = _agent_with_usage(monkeypatch, usage)
    capsys.readouterr()  # 丢弃 run() 里逐条打印的 [tokens] 行

    _print_usage_hint(agent)

    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    assert lines[0] == "[tokens] 会话累计 输入 100 (缓存 80) / 输出 10 / 合计 110"


def test_hint_omits_cache_when_zero(monkeypatch, capsys):
    usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
    agent = _agent_with_usage(monkeypatch, usage)
    capsys.readouterr()  # 丢弃 run() 里逐条打印的 [tokens] 行

    _print_usage_hint(agent)

    out = capsys.readouterr().out
    assert "输入 100 / 输出 10" in out
    assert "(缓存" not in out


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
