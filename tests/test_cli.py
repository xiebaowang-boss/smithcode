"""CLI 测试：输入层（prompt_toolkit 交互 / 非交互回退）与缺配置时的启动退出。"""

import pytest

from smithcode import config
from smithcode.cli import main
from smithcode.utils.terminal import prompt_choice, read_user_input

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


# ---------- read_user_input：prompt_toolkit 交互 / 非交互回退 ----------

class _FakeSession:
    """记录提示符并返回预设输入行的 PromptSession 桩。"""

    def __init__(self, replies, calls):
        self._replies = replies
        self._calls = calls

    def prompt(self, prompt=""):
        self._calls.append(prompt)
        return self._replies.pop(0)


def _patch_session(monkeypatch, replies):
    """把 _session() 换成返回预设行的假会话，交互分支全部走它。"""
    calls = []
    monkeypatch.setattr(
        "smithcode.utils.terminal._session", lambda: _FakeSession(replies, calls)
    )
    return calls


def test_read_user_input_merges_pasted_lines(monkeypatch):
    """交互模式下粘贴的多行（含换行）作为同一条消息返回。"""
    monkeypatch.setattr("smithcode.utils.terminal.confirmations_available", lambda: True)
    calls = _patch_session(monkeypatch, ["第一行\n第二行\n第三行"])

    assert read_user_input() == "第一行\n第二行\n第三行"
    assert len(calls) == 1


def test_read_user_input_single_line(monkeypatch):
    """交互模式下普通单行输入原样返回。"""
    monkeypatch.setattr("smithcode.utils.terminal.confirmations_available", lambda: True)
    _patch_session(monkeypatch, ["就这一句"])

    assert read_user_input() == "就这一句"


def test_read_user_input_uses_given_prompt(monkeypatch):
    """提示符原样传给 prompt_toolkit（REPL 与 ask_user 共用）。"""
    monkeypatch.setattr("smithcode.utils.terminal.confirmations_available", lambda: True)
    calls = _patch_session(monkeypatch, ["hi"])

    read_user_input("回答> ")
    assert calls == ["回答> "]


def test_read_user_input_falls_back_to_input_for_piped_stdin(monkeypatch):
    """非交互 stdin（管道/CI）退回普通 input()，不依赖 prompt_toolkit。"""
    monkeypatch.setattr("smithcode.utils.terminal.confirmations_available", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "一行")

    assert read_user_input() == "一行"


# ---------- prompt_choice：y/n/a 选择，非法输入循环重试 ----------

def test_prompt_choice_accepts_valid_key(monkeypatch):
    """合法选择键原样返回。"""
    monkeypatch.setattr("smithcode.utils.terminal.confirmations_available", lambda: True)
    _patch_session(monkeypatch, ["y"])

    assert prompt_choice("  允许? ", "yan", "y / a / n") == "y"


def test_prompt_choice_retries_on_invalid(monkeypatch, capsys):
    """非法输入提示后重试，直至输入合法。"""
    monkeypatch.setattr("smithcode.utils.terminal.confirmations_available", lambda: True)
    _patch_session(monkeypatch, ["x", "a"])

    assert prompt_choice("  允许? ", "yan", "y / a / n") == "a"
    assert "无效输入" in capsys.readouterr().out


# ---------- 按键绑定：Enter 发送，Ctrl+Enter 插入换行 ----------

def _prompt_with_keys(keys: bytes) -> str:
    """用管道输入模拟按键序列，返回提交结果（真实 PromptSession + 自定义绑定）。"""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from smithcode.utils.terminal import _bindings

    with create_pipe_input() as inp, create_app_session(
        input=inp, output=DummyOutput()
    ):
        session = PromptSession(multiline=True, key_bindings=_bindings())
        inp.send_bytes(keys)
        return session.prompt("> ")


def test_ctrl_enter_inserts_newline_then_enter_submits():
    """Ctrl+Enter 插入换行，Enter 提交整段多行消息。"""
    result = _prompt_with_keys("第一行".encode() + b"\x0a" + "第二行".encode() + b"\r")
    assert result == "第一行\n第二行"


def test_enter_submits_single_line():
    """单行输入直接按 Enter 提交，行为与单行模式一致。"""
    assert _prompt_with_keys(b"hello\r") == "hello"
