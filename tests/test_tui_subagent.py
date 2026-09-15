"""TUI 子代理展示测试：task 块挂载、子工具嵌套、流式抑制与报告展示（headless pilot）。"""
import asyncio
import json
from dataclasses import replace

import pytest
from textual.widgets import Static

import smithcode.renderer as renderer_module
from smithcode import config, subagents
from smithcode.agent import Agent
from smithcode.session import Session
from smithcode.tui.app import SmithTUI
from smithcode.tui.widgets import ChatInput, SubAgentBlock


@pytest.fixture(autouse=True)
def restore_renderer():
    """TuiRenderer.on_mount 会替换全局渲染后端，测试结束还原，避免污染后续测试。"""
    from smithcode import renderer

    backup = renderer._current
    yield
    renderer_module.set_renderer(backup)


@pytest.fixture(autouse=True)
def _restore_subagents():
    before = config.SUBAGENTS
    yield
    config.SUBAGENTS = before
    subagents.reset()


def _tc(name, args, call_id="1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _chat_text(app) -> str:
    return "\n".join(str(w.content) for w in app.query_one("#chat").query(Static))


def test_tui_subagent_block_nested_and_report(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "auth.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr("smithcode.permission.engine.confirmations_available", lambda: False)

    class ScriptLLM:
        """父 → 子工具调用 → 子报告 → 父总结 的脚本化回放。"""

        def __init__(self):
            self.calls = 0

        def chat_stream(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                yield ("message", {"role": "assistant", "content": "", "tool_calls": [
                    _tc("task", {"description": "找认证", "prompt": "找出认证代码",
                                 "subagent_type": "explore"}),
                ]})
            elif self.calls == 2:
                yield ("message", {"role": "assistant", "content": "", "tool_calls": [
                    _tc("read_file", {"path": "auth.py"}, "2"),
                ]})
            elif self.calls == 3:
                yield ("content", "子代理流式正文（默认不上屏）")
                yield ("message", {"role": "assistant", "content": "子代理报告：认证在 auth.py"})
            else:
                yield ("content", "主代理总结")
                yield ("message", {"role": "assistant", "content": "主代理总结"})

    monkeypatch.setattr("smithcode.agent.LLMClient", ScriptLLM)
    app = SmithTUI(Agent(session=Session()))

    async def _case():
        async with app.run_test() as pilot:
            inp = app.query_one(ChatInput)
            inp.focus()
            inp.insert("调查认证")
            await pilot.press("enter")
            for _ in range(300):
                if not app._busy:
                    break
                await pilot.pause(0.02)
            await pilot.pause(0.2)  # 让尚未处理的 UiAction 全部落地

            blocks = app.query(SubAgentBlock)
            assert len(blocks) == 1
            block = blocks.first()
            assert block._child_count == 1  # 子代理的一次 read_file 嵌套进 task 块
            assert "子代理报告：认证在 auth.py" in block._result

            text = _chat_text(app)
            assert "task explore: 找认证" in text
            assert "read auth.py" in text  # 子工具行嵌套可见
            assert "主代理总结" in text
            assert "子代理流式正文（默认不上屏）" not in text  # summary 模式抑制子代理流

    asyncio.run(_case())


def test_scoped_console_renderer_prefix_and_suppression(monkeypatch, capsys):
    """Console 后端：子代理事件带来源前缀；流式正文默认抑制、错误仍可见。"""
    from smithcode import renderer

    monkeypatch.setattr(config, "SUBAGENTS", replace(config.SUBAGENTS, display="summary"))
    base = renderer.ConsoleRenderer()
    scoped = base.scoped(renderer.Scope(task_id=7, agent="explore"))

    scoped.tool_call("grep auth", "inline", "grep")
    scoped.stream("content", "隐藏正文")
    scoped.info("提示")
    scoped.tool_result("错误: 失败了")

    out = capsys.readouterr().out
    assert "[explore] grep auth" in out
    assert "隐藏正文" not in out
    assert "[explore] 提示" in out
    assert "[explore] 错误: 失败了" in out
