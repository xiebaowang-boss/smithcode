"""工具调用终端展示测试：describe 短摘要、summary/detail 两种模式、配置降级。"""

import json

import pytest

from codeagent import config
from codeagent.agent import MAX_SUMMARY_LEN, Agent
from codeagent.session import Session
from codeagent.tools import DESCRIBERS


@pytest.fixture(autouse=True)
def enable_prompting(monkeypatch):
    """pytest 环境下 stdin 非 TTY，显式放行交互确认，否则权限确认会全部 fail-closed 拒绝。"""
    monkeypatch.setattr("codeagent.permission.confirmations_available", lambda: True)


# ---------- 配置加载：tool_display 字段 ----------

def _config_file(tmp_path, text) -> str:
    path = tmp_path / "codeagent.json"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_tool_display_missing_field_uses_summary(tmp_path):
    assert config.load_tool_display(_config_file(tmp_path, '{"permissions": {}}')) == "summary"


def test_tool_display_missing_file_uses_summary(tmp_path):
    assert config.load_tool_display(str(tmp_path / "nope.json")) == "summary"


@pytest.mark.parametrize("value", ["summary", "detail"])
def test_tool_display_accepts_enum(tmp_path, value):
    assert config.load_tool_display(_config_file(tmp_path, f'{{"tool_display": "{value}"}}')) == value


def test_tool_display_invalid_value_degrades(tmp_path, capsys):
    path = _config_file(tmp_path, '{"tool_display": "verbose"}')
    assert config.load_tool_display(path) == "summary"
    assert "警告" in capsys.readouterr().out


def test_tool_display_broken_json_degrades(tmp_path, capsys):
    path = _config_file(tmp_path, "{ not valid json")
    assert config.load_tool_display(path) == "summary"
    assert "警告" in capsys.readouterr().out


# ---------- describe：短格式摘要 ----------

def test_describe_file_tools():
    assert DESCRIBERS["read_file"]({"path": "src/a.py"}) == "read src/a.py"
    assert DESCRIBERS["write_file"]({"path": "src/b.py"}) == "write src/b.py"
    assert DESCRIBERS["edit_file"]({"path": "src/c.py"}) == "edit src/c.py"
    assert DESCRIBERS["list_dir"]({"path": "src"}) == "ls src"


def test_describe_search_tools():
    assert DESCRIBERS["glob"]({"pattern": "**/*.py"}) == "glob **/*.py"
    assert DESCRIBERS["glob"]({"pattern": "*.md", "path": "docs"}) == "glob *.md docs"
    assert DESCRIBERS["grep"]({"pattern": "TODO"}) == "grep TODO"
    assert (
        DESCRIBERS["grep"]({"pattern": "TODO", "path": "src", "include": "*.py"})
        == "grep TODO src --include=*.py"
    )


def test_describe_command_and_patch():
    assert DESCRIBERS["run_command"]({"command": "git push"}) == "command git push"
    patch = "*** Add File: a.txt\n+a\n*** Update File: b.txt\n@@ x @@\n-y\n+z\n"
    assert DESCRIBERS["apply_patch"]({"patch": patch}) == "patch a.txt b.txt"
    assert DESCRIBERS["apply_patch"]({"patch": "没有段落"}) == "patch"


# ---------- 终端展示：summary / detail / 失败三态 ----------

class FakeLLM:
    def __init__(self, tool_call=None):
        self.calls = 0
        self.tool_call = tool_call

    def chat_stream(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            yield ("message", {"role": "assistant", "content": "", "tool_calls": [self.tool_call]})
        else:
            yield ("message", {"role": "assistant", "content": "完成"})


def _run_tool(monkeypatch, tmp_path, name, args_dict, display=None) -> Agent:
    """让 Agent 真实执行一次工具调用，返回 agent（终端输出用测试内的 capsys 读取）。"""
    tool_call = {
        "id": "1",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args_dict)},
    }
    monkeypatch.setattr("codeagent.agent.LLMClient", lambda: FakeLLM(tool_call))
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "SESSION_EXTRA_ROOTS", [])
    agent = Agent(session=Session())
    if display:
        agent.display_mode = display
    agent.run("工具测试")
    return agent


def test_summary_mode_prints_terse_line_only(monkeypatch, tmp_path, capsys):
    (tmp_path / "hi.txt").write_text("你好", encoding="utf-8")
    _run_tool(monkeypatch, tmp_path, "read_file", {"path": "hi.txt"})

    out = capsys.readouterr().out
    assert "read hi.txt" in out
    assert "[Result]" not in out  # summary 模式不展示结果内容
    assert "你好" not in out


def test_detail_mode_appends_result_block(monkeypatch, tmp_path, capsys):
    (tmp_path / "hi.txt").write_text("你好", encoding="utf-8")
    _run_tool(monkeypatch, tmp_path, "read_file", {"path": "hi.txt"}, display="detail")

    out = capsys.readouterr().out
    assert "read hi.txt" in out
    assert "[Result]" in out
    assert "你好" in out


def test_summary_mode_result_still_reaches_model(monkeypatch, tmp_path):
    """summary 只影响终端展示，回传给模型的内容不变。"""
    (tmp_path / "hi.txt").write_text("模型要读的内容", encoding="utf-8")
    agent = _run_tool(monkeypatch, tmp_path, "read_file", {"path": "hi.txt"})

    tool_msg = next(m for m in agent.session.messages if m["role"] == "tool")
    assert tool_msg["content"] == "模型要读的内容"


def test_error_shown_even_in_summary_mode(monkeypatch, tmp_path, capsys):
    """失败信息（错误/拒绝）不受展示粒度影响，始终原样展示。"""
    _run_tool(monkeypatch, tmp_path, "read_file", {"path": "不存在.txt"})

    out = capsys.readouterr().out
    assert "read 不存在.txt" in out  # 执行前的短摘要行照常打印
    assert "错误: " in out


def test_unregistered_tool_falls_back_to_raw_format(monkeypatch, tmp_path, capsys):
    """没有 describe 的工具回退为 [Tool] 名字(参数) 格式。"""
    _run_tool(monkeypatch, tmp_path, "mystery_tool", {"x": 1})

    out = capsys.readouterr().out
    assert "[Tool] mystery_tool" in out


def test_long_summary_line_truncated(monkeypatch, tmp_path, capsys):
    """超长目标（如长命令）在展示层截断，不影响回传内容。"""
    long_cmd = "echo " + "x" * 200
    _run_tool(monkeypatch, tmp_path, "run_command", {"command": long_cmd})

    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if "command echo" in l)
    assert len(line.strip()) <= MAX_SUMMARY_LEN + len("...") + 2  # 2 为缩进
