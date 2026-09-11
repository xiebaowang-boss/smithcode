"""工具调用终端展示测试：describe 短摘要、summary/detail 两种模式、配置降级。"""

import json

import pytest

from smithcode import config
from smithcode.agent import MAX_SUMMARY_LEN, Agent
from smithcode.llm.session import Session
from smithcode.tools import DESCRIBERS


@pytest.fixture(autouse=True)
def enable_prompting(monkeypatch):
    """pytest 环境下 stdin 非 TTY，显式放行交互确认，否则权限确认会全部 fail-closed 拒绝。"""
    monkeypatch.setattr("smithcode.permission.engine.confirmations_available", lambda: True)


# ---------- 配置加载：tool_display 字段 ----------

def _write_home_config(monkeypatch, tmp_path, text):
    """把内容写进隔离的 SMITHCODE_HOME 下的 config.toml。"""
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(text, encoding="utf-8")
    monkeypatch.setenv("SMITHCODE_HOME", str(home))


def test_tool_display_missing_field_uses_summary(monkeypatch, tmp_path):
    _write_home_config(monkeypatch, tmp_path, '[permissions]\nread_file = "allow"\n')
    assert config.load_tool_display() == "summary"


def test_tool_display_missing_file_uses_summary(monkeypatch, tmp_path):
    monkeypatch.setenv("SMITHCODE_HOME", str(tmp_path))
    assert config.load_tool_display() == "summary"


@pytest.mark.parametrize("value", ["summary", "detail"])
def test_tool_display_accepts_enum(monkeypatch, tmp_path, value):
    _write_home_config(monkeypatch, tmp_path, f'tool_display = "{value}"')
    assert config.load_tool_display() == value


def test_tool_display_invalid_value_degrades(monkeypatch, tmp_path, capsys):
    _write_home_config(monkeypatch, tmp_path, 'tool_display = "verbose"')
    assert config.load_tool_display() == "summary"
    assert "警告" in capsys.readouterr().out


def test_tool_display_broken_toml_degrades(monkeypatch, tmp_path, capsys):
    _write_home_config(monkeypatch, tmp_path, "this is not valid toml")
    assert config.load_tool_display() == "summary"
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
    monkeypatch.setattr("smithcode.agent.LLMClient", lambda: FakeLLM(tool_call))
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "SESSION_EXTRA_ROOTS", [])
    if display:
        monkeypatch.setattr(config, "load_tool_display", lambda: display)
    agent = Agent(session=Session())
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
    assert "模型要读的内容" in tool_msg["content"]


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


# ---------- 工具调用详情的变更预览（diff）与默认展开 ----------

def test_write_new_file_shows_diff_in_summary_mode(monkeypatch, tmp_path, capsys):
    """write_file 的调用详情展示 diff（全增行），summary 模式也展示。"""
    monkeypatch.setattr("builtins.input", lambda _: "y")
    _run_tool(monkeypatch, tmp_path, "write_file", {"path": "new.txt", "content": "a\nb\n"})

    out = capsys.readouterr().out
    assert "write new.txt" in out
    assert "+a" in out
    assert "+b" in out


def test_edit_file_shows_diff(monkeypatch, tmp_path, capsys):
    """edit_file 先读后改的正常路径：详情展示删行 + 增行。"""
    import smithcode.tools.files as files_mod

    (tmp_path / "c.txt").write_text("x = 1\n", encoding="utf-8")
    # Agent.__init__ 会清空已读记录，这里禁用清空以便预置"已读"状态
    monkeypatch.setattr("smithcode.agent.reset_read_tracking", lambda: None)
    files_mod.READ_FILES.add(str((tmp_path / "c.txt").resolve()))
    monkeypatch.setattr("builtins.input", lambda _: "y")
    _run_tool(
        monkeypatch, tmp_path, "edit_file",
        {"path": "c.txt", "old_string": "x = 1", "new_string": "x = 2"},
    )

    out = capsys.readouterr().out
    assert "-x = 1" in out
    assert "+x = 2" in out


def test_edit_result_confirmation_shown_after_execution(monkeypatch, tmp_path, capsys):
    """「已编辑 xxx」的执行确认语在真正调用后展示，且晚于执行前的 diff。"""
    import smithcode.tools.files as files_mod

    (tmp_path / "c.txt").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr("smithcode.agent.reset_read_tracking", lambda: None)
    files_mod.READ_FILES.add(str((tmp_path / "c.txt").resolve()))
    monkeypatch.setattr("builtins.input", lambda _: "y")
    _run_tool(
        monkeypatch, tmp_path, "edit_file",
        {"path": "c.txt", "old_string": "x = 1", "new_string": "x = 2"},
    )

    out = capsys.readouterr().out
    diff_pos = out.find("-x = 1")
    confirm_pos = out.find("已编辑")
    assert confirm_pos != -1  # 确认语有展示
    assert diff_pos != -1 and diff_pos < confirm_pos  # diff（执行前）在前，确认语（执行后）在后


def test_failed_tool_call_has_no_diff(monkeypatch, tmp_path, capsys):
    """批准后执行失败：diff 只在执行前展示一次，错误结果不重复带预览。"""
    (tmp_path / "a.txt").write_text("secret\n", encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda _: "y")
    _run_tool(monkeypatch, tmp_path, "write_file", {"path": "a.txt", "content": "x\n"})

    out = capsys.readouterr().out
    assert "错误: " in out  # 工具本体报错（覆盖未读文件被拒）
    assert out.count("+x") == 1  # diff 仅出现在 ask 确认阶段，执行失败后不再展示


def test_diff_preview_snapshot_before_execution(monkeypatch, tmp_path):
    """_diff_preview 是执行前快照：文件尚未变更时能取到 diff。"""
    from smithcode.agent import _diff_preview

    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "a.txt").write_text("old\n", encoding="utf-8")
    preview = _diff_preview(
        "edit_file", {"path": "a.txt", "old_string": "old", "new_string": "new"}
    )
    assert "-old" in preview
    assert "+new" in preview


def test_diff_preview_failure_returns_empty(monkeypatch, tmp_path):
    """预览生成抛异常只影响展示（返回空串），不影响执行。"""
    from smithcode.agent import _diff_preview
    from smithcode.tools import base

    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))

    def boom(args):
        raise RuntimeError("boom")

    monkeypatch.setitem(base.PREVIEWS, "write_file", boom)
    assert _diff_preview("write_file", {"path": "a.txt", "content": "x"}) == ""


def test_diff_preview_truncates_long_diff(monkeypatch, tmp_path):
    from smithcode.agent import MAX_PREVIEW_LINES, _diff_preview
    from smithcode.tools import base

    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setitem(
        base.PREVIEWS, "write_file",
        lambda args: "\n".join(f"line{i}" for i in range(200)),
    )
    preview = _diff_preview("write_file", {"path": "a.txt", "content": "x"})
    lines = preview.splitlines()
    assert len(lines) == MAX_PREVIEW_LINES + 1
    assert "省略" in lines[-1]


def test_finish_expands_write_edit_tools_only(monkeypatch, tmp_path, capsys):
    """_finish 只为写/编辑类工具带 expand 标记（TUI 默认展开），其他工具不带。"""
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    from smithcode import agent as agent_mod

    captured = []

    class CapRenderer:
        def tool_result(self, result, tool_id=None, expand=False):
            captured.append(expand)

        def info(self, text):
            pass

    monkeypatch.setattr(agent_mod.renderer, "current", lambda: CapRenderer())
    bare = agent_mod.Agent.__new__(agent_mod.Agent)
    agent_mod.Agent._finish(bare, "已写入", None, "write_file")
    agent_mod.Agent._finish(bare, "ok", None, "run_command")
    assert captured == [True, False]
