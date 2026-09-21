"""TUI 测试：界面挂载、消息流式渲染、弹窗确认、工具折叠、计划侧边栏（headless pilot）。"""
import asyncio
import json
import threading
import time
from pathlib import Path

import httpx2
import pytest
from rich.color import ColorTriplet
from textual.geometry import Region
from textual.widgets import Static

from smithcode import __version__, config, sandbox
from smithcode import frontend as _frontend_mod
from smithcode.agent import Agent
from smithcode.event import asks as ask_port
from smithcode.event.asks import AskRequest
from smithcode.event.catalog import (
    MessageEnd,
    PlanUpdate,
    TitleChanged,
)
from smithcode.event.envelope import wrap
from smithcode.llm import RetryState
from smithcode.llm.request import TurnConfig
from smithcode.session import Session
from smithcode.tui.app import SmithTUI
from smithcode.tui.panels import (
    PermissionPanel,
    QuestionPanel,
    SelectionItem,
    SelectionPanel,
    SelectionScreen,
)
from smithcode.tui.render import (
    context_category,
    context_summary,
    format_duration,
    git_branch,
    split_md_blocks,
)
from smithcode.tui.widgets import (
    MENU_VISIBLE_ITEMS,
    ChatInput,
    ChatView,
    CommandMenu,
    CommandMenuItem,
    ContextGroup,
    RunningIndicator,
    Sidebar,
    ThinkingBlock,
    ToolCall,
)
from smithcode.utils.terminal import confirmations_available


@pytest.fixture(autouse=True)
def _fresh_goal():
    """持久目标是全局单例，逐用例清空防止跨测试污染。"""
    from smithcode import goal

    goal.reset()
    yield
    goal.reset()


def no_prompting(monkeypatch):
    """headless 环境里 stdin 非 TTY，权限确认会 fail-closed，正好不用真弹窗。"""
    monkeypatch.setattr("smithcode.permission.engine.confirmations_available", lambda: False)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", __file__)


class FakeLLM:
    """只回正文的假模型，配合 Agent 线程跑通流式渲染。"""

    def chat_stream(self, messages, tools=None):
        yield ("content", "你好，")
        yield ("content", "世界")
        yield ("message", {"role": "assistant", "content": "你好，世界"})



def publish_plan(app, summary: str = "共 1 步", *, created: bool = False,
                 tool_call_id: str | None = None) -> None:
    """把计划更新事件投给界面（替代从前直接调渲染后端）。

    两种渲染形态都随载荷发出（与生产一致）：前端不读会话状态。
    """
    from smithcode import plan as plan_mod

    app.agent.events.publish(PlanUpdate(
        summary=summary, rendered=plan_mod.render_current(color=True),
        titles=plan_mod.render_titles(color=True),
        created=created, tool_call_id=tool_call_id,
    ))

def _done(on_done):
    """面板完成信号：既接受 threading.Event，也接受回调。"""
    return on_done.set if hasattr(on_done, "set") else on_done


def _show_permission(app, prompt, valid, hint, result, on_done, detail=None,
                     descriptions=None, content=None):
    """测试辅助：按旧签名挂权限面板（内部转成 `AskRequest`）。"""
    app.show_ask_panel(
        AskRequest(kind="permission", title=prompt, options=tuple(valid),
                   detail=tuple(detail or ()),
                   payload={"hint": hint, "descriptions": descriptions or {},
                            "content": content}),
        result, _done(on_done),
    )


def _show_questions(app, questions, result, on_done):
    """测试辅助：按旧签名挂提问面板（内部转成 `AskRequest`）。"""
    app.show_ask_panel(
        AskRequest(kind="ask_user", title="", payload={"questions": tuple(questions)}),
        result, _done(on_done),
    )

def _make_agent(monkeypatch):
    monkeypatch.setattr("smithcode.agent.LLMClient", lambda: FakeLLM())
    return Agent(session=Session())


def _chat_text(app) -> str:
    return "\n".join(str(w.content) for w in app.query_one("#chat").query(Static))


def _run(coro):
    return asyncio.run(coro)


# ---------- split_md_blocks：流式按块增量渲染的切分器 ----------

def test_split_md_blocks_paragraphs():
    done, tail = split_md_blocks("第一段\n还在写")
    assert done == []  # 尾部块未完结（流式可能继续追加）
    assert tail == "第一段\n还在写"
    done, tail = split_md_blocks("第一段\n\n第二段完成\n")
    assert done == ["第一段"]
    assert tail == "第二段完成"  # 空行后的块视为未完结


def test_split_md_blocks_closed_fence_is_done():
    md = "前言\n\n```python\nprint(1)\n```\n\n后续"
    done, tail = split_md_blocks(md)
    assert done == ["前言", "```python\nprint(1)\n```"]
    assert tail == "后续"


def test_split_md_blocks_open_fence_stays_tail():
    """未闭合围栏整体留在尾部，围栏内空行不切分。"""
    md = "前言\n\n```py\n代码\n\n还是代码"
    done, tail = split_md_blocks(md)
    assert done == ["前言"]
    assert tail == "```py\n代码\n\n还是代码"


def test_split_md_blocks_empty():
    assert split_md_blocks("") == ([], "")
    assert split_md_blocks("\n\n") == ([], "")


def test_tui_stream_nested_list_no_content_loss(monkeypatch):
    """回归：列表前导空格单独成 chunk 时曾被误判为空行（块"完结"后缩回），
    数量对齐漏渲后续块导致整段列表丢失；现为内容对齐，任何分块粒度无缺失。"""
    no_prompting(monkeypatch)

    md = ("这是介绍。\n\n**核心内容**：\n\n"
          "- **是什么**：终端 AI 编程助手。\n"
          "- **主要能力**：\n"
          "  - 多轮工具循环\n"
          "  - 权限确认与沙箱\n"
          "- **怎么用**：直接说需求\n\n"
          "想深入了解可以告诉我。")

    class ChunkLLM:
        def __init__(self, size):
            self.size = size

        def chat_stream(self, messages, tools=None):
            full = ("message", {"role": "assistant", "content": md})
            for i in range(0, len(md), self.size):
                yield ("content", md[i:i + self.size])
            yield full

    async def _run_case():
        monkeypatch.setattr("smithcode.agent.LLMClient", lambda: ChunkLLM(3))
        app = SmithTUI(Agent(session=Session()))
        async with app.run_test() as pilot:
            inp = app.query_one(ChatInput)
            inp.focus()
            inp.insert("分块")
            await pilot.press("enter")
            for _ in range(200):
                if not app._busy:
                    break
                await pilot.pause(0.02)
            text = _chat_text(app)
            # 列表内嵌套项与最后条目必须完整上屏（此前会整体丢失）
            for key in ("是什么", "主要能力", "多轮工具循环", "权限确认与沙箱", "怎么用", "深入了解"):
                assert key in text, f"丢失内容: {key}"

    _run(_run_case())


def test_tui_mounts_and_welcomes(monkeypatch):
    """TUI 挂载欢迎语（headless）。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            assert pilot.app is app
            assert f"v{__version__}" in _chat_text(app)

    _run(_run_case())


def test_tui_mount_wires_terminal_title(monkeypatch):
    """挂载时把标题 sink 接到 Textual 写入通道，标题事件据此真正上到终端。"""
    no_prompting(monkeypatch)
    from smithcode import title as title_module

    monkeypatch.setattr(title_module.config, "load_terminal_title", lambda: True)
    monkeypatch.setattr(title_module, "stdout_is_tty", lambda: True)

    captured = {"writes": []}
    real_enable = title_module.enable_title

    def spy_enable(sink=None, workspace=""):
        captured["sink"] = sink

        def recording_sink(seq):
            captured["writes"].append(seq)

        return real_enable(sink=recording_sink, workspace=workspace)

    monkeypatch.setattr(title_module, "enable_title", spy_enable)

    async def _run_case():
        monkeypatch.setattr("smithcode.agent.LLMClient", lambda: FakeLLM())
        app = SmithTUI(Agent(session=Session(), persist=False))
        async with app.run_test():
            assert captured["sink"] == app._driver.write  # 走 Textual 写入队列
            app.agent.rename_session("标题接线")
            assert "\x1b]0;Smith · 标题接线\x07" in captured["writes"]

    _run(_run_case())


def test_tui_streams_assistant_reply(monkeypatch):
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            inp = app.query_one(ChatInput)
            inp.focus()
            inp.insert("说句话")
            await pilot.press("enter")
            for _ in range(200):
                if not app._busy:
                    break
                await pilot.pause(0.02)
            text = _chat_text(app)
            assert "说句话" in text  # 用户消息以面板形式回显
            assert "你好，世界" in text  # 助手正文无"助手>"前缀
            assert "助手>" not in text

    _run(_run_case())


class MdLLM:
    """带 markdown 结构的假模型：段落 + 完结代码块，验证流式期间即渲染。"""

    def chat_stream(self, messages, tools=None):
        yield ("content", "第一段说明\n\n```py\n")
        yield ("content", "print(1)\n```\n\n第二段")
        yield ("message", {"role": "assistant",
                           "content": "第一段说明\n\n```py\nprint(1)\n```\n\n第二段"})


def test_tui_stream_renders_markdown_incrementally(monkeypatch):
    """流式期间（未等 end_stream）完结块就已渲染定型：代码块行带样式缩进。"""
    no_prompting(monkeypatch)
    monkeypatch.setattr("smithcode.agent.LLMClient", lambda: MdLLM())

    async def _run_case():
        app = SmithTUI(Agent(session=Session()))
        async with app.run_test() as pilot:
            inp = app.query_one(ChatInput)
            inp.focus()
            inp.insert("md")
            await pilot.press("enter")
            for _ in range(200):
                if not app._busy:
                    break
                await pilot.pause(0.02)
            text = _chat_text(app)
            assert "第一段说明" in text
            assert "print(1)" in text
            assert "第二段" in text
            # 代码块经 rich 渲染后有缩进（非流式纯文本顶格），证明走了渲染路径
            stream_blocks = app.query_one("#chat").query(".assistant-stream")
            assert any("print(1)" in str(b.content) for b in stream_blocks)

    _run(_run_case())


def test_tui_assistant_text_rewraps_on_resize(monkeypatch):
    """窗口缩放后助手正文按新宽度重排：折行不固化在旧宽度上。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test(size=(70, 30)) as pilot:
            text = " ".join(f"w{i:03d}" for i in range(60))
            app.ui_stream("content", text)
            app.ui_stream_done()
            await pilot.pause()
            block = app.query_one(".assistant-stream")
            narrow = block.content.plain.split("\n")

            await pilot.resize_terminal(160, 30)
            await pilot.pause()
            wide = block.content.plain.split("\n")

            def flat(lines):
                return "".join("".join(line.split()) for line in lines)

            # 变宽：可用列变多，折行更少（且首屏不再固定按回退宽度折行）
            assert len(wide) < len(narrow)
            assert flat(wide) == flat(narrow)  # 只改折行，内容一字不丢

            # 再缩窄：折行重新变多，内容依然完整
            await pilot.resize_terminal(70, 30)
            await pilot.pause()
            again = block.content.plain.split("\n")
            assert len(again) == len(narrow)
            assert flat(again) == flat(narrow)

    _run(_run_case())


def test_tui_streaming_block_rewraps_on_resize(monkeypatch):
    """流式期间/结束后同样自适应：已定型的正文块随后缩放仍会重排。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test(size=(70, 30)) as pilot:
            chat = app.query_one(ChatView)
            chat.begin_stream("content")
            for chunk in ["第一段", "内容", "比较长" * 8, "\n\n", "第二段" * 10]:
                chat.append_stream("content", chunk)
            chat.end_stream()
            await pilot.pause()
            block = chat.query_one(".assistant-stream")
            narrow = len(block.content.split("\n"))

            await pilot.resize_terminal(160, 30)
            await pilot.pause()
            assert len(block.content.split("\n")) < narrow
            assert "第一段内容" in block.content.plain

    _run(_run_case())


def test_choice_modal_resolves(monkeypatch):
    """权限申请面板：替换输入框，字母键直选，答后恢复输入框。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            _show_permission(app, 
                "允许? [y]本次 / [n]拒绝 / [a]总是允许该模式: ", "yna", "y / n / a", result, evt
            )
            await pilot.pause()
            assert app.query_one("#input-wrap").display is False  # 输入框（含框内状态行）被替换
            await pilot.press("y")
            await pilot.pause()
            assert result.get("value") == "y"
            assert evt.is_set()
            assert app.query_one("#input-wrap").display is True  # 面板关闭后恢复

    _run(_run_case())


def test_permission_panel_escape_denies(monkeypatch):
    """权限申请面板：Esc = 拒绝（选 n）。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            _show_permission(app, 
                "允许? [y]仅本次 / [a]本会话总是信任该目录 / [n]拒绝: ", "yan", "y / a / n", result, evt
            )
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert result.get("value") == "n"
            assert evt.is_set()

    _run(_run_case())


def test_permission_panel_arrow_keys_do_not_answer(monkeypatch):
    """回归：方向键等非字符键不能被当成空回答直接拒绝（"" in valid 坑）。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            _show_permission(app, 
                "允许? [y]本次 / [n]拒绝 / [a]总是允许该模式: ", "yna", "y / n / a", result, evt
            )
            await pilot.pause()
            for key in ("up", "down", "left", "right"):
                await pilot.press(key)
                await pilot.pause()
            assert not evt.is_set()  # 方向键只移动选中项，不提交
            await pilot.press("enter")
            await pilot.pause()
            assert evt.is_set()
            assert result.get("value") == "y"  # 默认停在第一项"本次"

    _run(_run_case())


def test_permission_panel_shows_option_descriptions(monkeypatch):
    """权限面板：顶部只展示标题，选项副作用以小字渲染在 ask 框内，聊天区不重复。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            _show_permission(app, 
                "允许执行 run_command? [y]本次 / [n]拒绝 / [a]总是允许: ",
                "yna", "y / n / a", result, evt,
                None,
                {"y": "仅本次执行", "a": "本会话将记住: git status *"},
                "command git status",
            )
            await pilot.pause()
            panel = app.query_one(PermissionPanel)
            title_text = str(panel.query_one(".perm-title").content)
            assert "允许执行 run_command?" in title_text
            assert "command git status" in title_text  # 内容与标题同排
            assert not panel.query(".perm-detail")  # 不再有独立内容行
            joined = "\n".join(str(s.content) for s in panel.query(Static))
            assert "仅本次执行" in joined
            assert "本会话将记住: git status *" in joined
            assert not app.query("#chat .perm-detail")  # 详情只在 ask 框内
            await pilot.press("y")
            await pilot.pause()
            assert result.get("value") == "y"

    _run(_run_case())


def test_question_panel_shows_option_descriptions(monkeypatch):
    """提问面板：顶部只展示问题，选项说明以小字渲染在选项下方。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            _show_questions(app, 
                [{"question": "用哪个？", "options": ["甲", "乙"],
                  "descriptions": ["甲说明", "乙说明"], "multiple": False}], result, evt,
            )
            await pilot.pause()
            panel = app.query_one(QuestionPanel)
            assert str(panel.query_one(".ask-title").content) == "用哪个？"
            joined = "\n".join(str(s.content) for s in panel.query(Static))
            assert "甲说明" in joined and "乙说明" in joined

    _run(_run_case())


def test_tool_preview_shown_in_pending_block_before_approval(monkeypatch):
    """审核前变更预览：diff 推送到 pending 工具调用块（而非权限框），就地展开可见。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            widget = ToolCall("write a.txt", pending=True, display="block")
            app._tool_widgets[7] = widget
            app.query_one(ChatView).add_widget(widget)
            await pilot.pause()
            body = widget.query_one(".tool-body")
            assert body.display is False  # 无预览时 pending 不展示内容
            app.ui_tool_preview(7, "--- a/a.txt\n+++ b/a.txt\n-old\n+new")
            await pilot.pause()
            plain = str(body.render())
            assert "-old" in plain
            assert "+new" in plain
            # 未配对 id 的预览不崩溃、不挂载孤儿块
            app.ui_tool_preview(None, "+x")
            await pilot.pause()

    _run(_run_case())


def test_question_panel_resolves(monkeypatch):
    """无选项提问：输入框被提问面板替换，提交后进确认页，确认再提交并恢复。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            _show_questions(app, [{"question": "要继续吗?"}], result, evt)
            await pilot.pause()
            assert app.query_one("#input-wrap").display is False  # 输入框（含框内状态行）被替换
            await pilot.press("是")
            await pilot.press("enter")            # 纯输入题提交 → 进确认页
            await pilot.pause()
            panel = app.query_one(QuestionPanel)
            assert panel._review                  # 单问题同样先到确认页
            assert not evt.is_set()
            await pilot.press("enter")            # 确认页提交
            await pilot.pause()
            assert result.get("values") == ["是"]
            assert evt.is_set()
            assert app.query_one("#input-wrap").display is True  # 面板关闭后恢复

    _run(_run_case())


def test_confirmations_available_still_works():
    """confirmations_available 保持原语义（headless 下为 False）。"""
    assert confirmations_available() is False


class ToolLLM:
    """先调用工具、再回正文的假模型。"""

    def __init__(self, tool_call, final):
        self.calls = 0
        self.tool_call = tool_call
        self.final = final

    def chat_stream(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            yield ("message", {"role": "assistant", "content": "", "tool_calls": [self.tool_call]})
        else:
            yield ("content", self.final)
            yield ("message", {"role": "assistant", "content": self.final})


def test_tui_tool_call_collapsible(monkeypatch, tmp_path):
    """工具调用渲染为可折叠块：read 结果默认收起（count 计数），展开可见内容。"""
    no_prompting(monkeypatch)
    (tmp_path / "hi.txt").write_text("文件内容", encoding="utf-8")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(sandbox, "_default", sandbox.Roots())
    import smithcode.agent as agent_mod

    tool_call = {
        "id": "1",
        "type": "function",
        "function": {"name": "read_file", "arguments": json.dumps({"path": "hi.txt"})},
    }
    agent_mod.LLMClient = lambda: ToolLLM(tool_call, "读完了")

    async def _run_case():
        app = SmithTUI(Agent(session=Session()))
        async with app.run_test() as pilot:
            inp = app.query_one(ChatInput)
            inp.focus()
            inp.insert("读文件")
            await pilot.press("enter")
            for _ in range(200):
                if not app._busy:
                    break
                await pilot.pause(0.02)
            block = app.query_one("#chat").query(ToolCall).first()
            assert "read hi.txt" in str(block.query_one(".tool-header").content)
            body = block.query_one(".tool-body")
            assert body.display is False  # 读取工具默认收起（整文件内容不上屏）
            block.action_toggle()
            assert body.display is True
            assert "文件内容" in str(body.render())

    _run(_run_case())


def test_tool_call_shows_diff_detail(monkeypatch):
    """write/edit 的调用详情：diff 以左右对照渲染（带行号），成功确认语不再重复。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            detail = "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new"
            block = ToolCall("edit a.txt", "已编辑 a.txt", expanded=True,
                             display="block", detail=detail)
            app.query_one(ChatView).add_widget(block)
            await pilot.pause()
            plain = str(block.query_one(".tool-body").render())
            assert "- old" in plain and "+ new" in plain
            assert "│" in plain  # 左右两栏分隔
            assert "--- a/" not in plain  # 不再展示文件头
            assert "@@ -1 +1 @@" not in plain  # 行对取代了 @@ 头
            assert "已编辑 a.txt" not in plain  # 审核时已看过 diff，确认语冗余

    _run(_run_case())


def test_tool_call_diff_shows_error_result(monkeypatch):
    """带 diff 的工具执行失败时仍展示错误结果（只隐藏成功确认语）。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            detail = "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new"
            block = ToolCall("edit a.txt", "错误: old_string 未找到", expanded=True,
                             display="block", detail=detail, is_error=True)
            app.query_one(ChatView).add_widget(block)
            await pilot.pause()
            plain = str(block.query_one(".tool-body").render())
            assert "错误: old_string 未找到" in plain

    _run(_run_case())


def test_ask_user_block_shows_question_and_answer(monkeypatch):
    """ask_user 工具块：头部只显示「提问：问题」，用户回答作为结果默认展开。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            block = ToolCall("提问：用哪个？", "乙", expanded=True, display="block")
            app.query_one(ChatView).add_widget(block)
            await pilot.pause()
            assert "提问：用哪个？" in str(block.query_one(".tool-header").content)
            body = block.query_one(".tool-body")
            assert body.display is True  # 默认展开
            assert "乙" in str(body.render())

    _run(_run_case())


def test_side_by_side_diff_pairs_del_add_rows():
    """统一 diff 解析为左右对照：删/增并到同一行、带 +/- 前缀，不再各占一行。"""
    from smithcode.tui.render import side_by_side_diff

    unified = "--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n keep\n-old\n+new\n tail"
    plain = str(side_by_side_diff(unified, 80))
    assert "- old" in plain and "+ new" in plain  # 两侧改动带 +/- 前缀
    assert "│" in plain
    assert plain.count("\n") == 4  # 上 padding + keep + old/new 同行 + tail + 下 padding
    assert "@@" not in plain  # 丢掉 hunk 头
    assert "x.py" not in plain  # 块内不展示文件名
    assert plain.startswith(" ") and plain.endswith(" ")  # 上下各 1 行 padding


def test_side_by_side_diff_line_numbers_track_both_sides():
    """行号分别跟随新旧两侧：删除只进旧侧、新增只进新侧、上下文两侧同增。"""
    from smithcode.tui.render import _parse_unified

    unified = "--- a/f\n+++ b/f\n@@ -5,2 +5,3 @@\n ctx\n-a\n+b\n+c"
    rows = [r for r in _parse_unified(unified) if r[0] == "line"]
    assert rows[0] == ("line", 5, "ctx", "ctx", 5, "ctx", "ctx")
    assert rows[1] == ("line", 6, "del", "a", 6, "add", "b")
    assert rows[2] == ("line", None, "empty", "", 7, "add", "c")


def test_side_by_side_diff_narrow_falls_back():
    """宽度放不下两栏时返回 None，交由调用方回退逐行渲染。"""
    from smithcode.tui.render import side_by_side_diff

    unified = "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b"
    assert side_by_side_diff(unified, 12) is None


def test_side_by_side_diff_truncates_rows():
    """折叠态按行数上限截断并追加省略提示。"""
    from smithcode.tui.render import side_by_side_diff

    dels = "\n".join(f"-old{i}" for i in range(20))
    adds = "\n".join(f"+new{i}" for i in range(20))
    unified = f"--- a/f\n+++ b/f\n@@ -1,20 +1,20 @@\n{dels}\n{adds}"
    plain = str(side_by_side_diff(unified, 80, max_rows=5))
    assert "old0" in plain and "old4" in plain
    assert "old5" not in plain
    assert "…（+15 行" in plain


def test_side_by_side_diff_block_has_uniform_background():
    """整块 diff（文件头 / 上下文 / 留白侧）都铺统一底色，且每行等宽铺满。"""
    from smithcode.tui.render import side_by_side_diff

    unified = "--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n keep\n-old\n+new"
    text = side_by_side_diff(unified, 60)
    lines = text.split("\n")
    for line in lines:
        assert line.spans  # 每行都有样式
        assert any("on " in str(seg.style) for seg in line.spans)  # 每行都带背景
    assert len({len(str(line)) for line in lines}) == 1  # 行行等宽（铺满整块）


def test_tool_call_summary_markup_not_parsed(monkeypatch):
    """回归：工具摘要含 Textual markup 样式（如 URL 被包成 [link=https://...]）
    时不得当标签解析——否则 Static 在布局/退出时抛 MarkupError 直接崩掉 TUI。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            summary = "fetch [link=https://raw.githubusercontent.com/x/y]"
            block = ToolCall(summary)
            app.query_one(ChatView).add_widget(block)
            await pilot.pause()
            header = str(block.query_one(".tool-header").content)
            assert summary in header  # 原样展示，未被当作标签吞掉 / 报错

    _run(_run_case())


def test_tool_call_header_single_row_when_overlong(monkeypatch):
    """摘要超过可用列宽时按宽度省略，不折成第二行：工具行恒占一行。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            block = ToolCall("command " + "x" * 200)
            app.query_one(ChatView).add_widget(block)
            await pilot.pause()
            assert block.query_one(".tool-header").size.height == 1

    _run(_run_case())


def test_question_panel_question_markup_not_parsed(monkeypatch):
    """回归：模型提问文本含 markup 样式时，提问面板标题原样展示不崩溃。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            question = "访问 [link=https://raw.githubusercontent.com/x] 吗？"
            _show_questions(app, [{"question": question}], result, evt)
            await pilot.pause()
            title = str(app.query_one(".ask-title").content)
            assert question in title

    _run(_run_case())


def test_tui_sidebar_shows_plan_section(monkeypatch):
    """侧边栏常驻；计划更新渲染到下半部分。"""
    no_prompting(monkeypatch)
    from smithcode import plan as plan_mod

    plan_mod.current().replace(
        [{"title": "读文件", "status": "in_progress", "description": "详情内容"}]
    )

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test(size=(140, 30)) as pilot:  # 够宽：侧边栏可见
            sidebar = app.query_one(Sidebar)
            assert sidebar.display is True  # 常驻可见
            assert f"v{__version__}" in str(sidebar.query_one(".sidebar-version").content)  # 底部版本号
            # 版本号下方：项目名 | 工作区路径（路径前补名字，截断时仍可辨认项目）
            workspace = str(sidebar.query_one(".sidebar-workspace").content)
            assert workspace.startswith(f"{Path(config.WORKSPACE_ROOT).name} | ")
            assert str(config.WORKSPACE_ROOT) in workspace
            publish_plan(app, created=True, tool_call_id="1")
            await pilot.pause()
            plan_body = str(sidebar.query_one(".plan-body").content)
            assert "读文件" in plan_body
            assert "详情内容" not in plan_body  # 侧边栏只展示标题

    _run(_run_case())


def test_tui_plan_created_shows_expandable_detail(monkeypatch):
    """新建清单：详情作为 plan 工具块展示（可折叠、默认展开）；更新不再新增对话块。"""
    no_prompting(monkeypatch)
    from smithcode import plan as plan_mod

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test(size=(140, 30)) as pilot:
            plan_mod.current().replace(
                [{"title": "读文件", "status": "in_progress", "description": "细节内容"}]
            )
            app.ui_tool_start("1", "plan (1 步)", "block", "todo_write")
            await pilot.pause()
            pending = app.query_one(ToolCall)
            assert pending._pending is True
            assert pending._spin_timer is None  # pending 期也不转轮
            assert pending._header_text().startswith("☰ plan (1 步)")
            app.agent.events.publish(PlanUpdate(
                summary="共 1 步", rendered=plan_mod.render_current(color=True),
                titles=plan_mod.render_titles(color=True),
                created=True, tool_call_id="1",
            ))
            await pilot.pause()

            # plan 工具块已收尾，默认展开，正文含计划内容
            assert "1" not in app._tool_widgets
            block = app.query_one(ToolCall)
            assert block._pending is False
            assert block._expanded is True
            assert block._spin_timer is None  # 静态图标：不启用转轮
            assert "☰ plan (1 步)" in str(block.query_one(".tool-header").content)
            assert "读文件" in str(block.query_one(".tool-body").render())

            # 更新（created=False）：只刷侧边栏，不再新增对话块
            before = len(app.query(ToolCall))
            app.agent.events.publish(PlanUpdate(
                summary="共 1 步", rendered=plan_mod.render_current(color=True),
                titles=plan_mod.render_titles(color=True),
                created=False, tool_call_id=None,
            ))
            await pilot.pause()
            assert len(app.query(ToolCall)) == before

    _run(_run_case())


def test_tui_sidebar_plan_hidden_without_active_tasks(monkeypatch):
    """opencode 式：无任务或全部完成/取消时侧边栏任务区隐藏，有未完结步骤才展示。"""
    no_prompting(monkeypatch)
    from smithcode import plan as plan_mod

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test(size=(140, 30)) as pilot:  # 够宽：侧边栏可见
            sidebar = app.query_one(Sidebar)
            section = sidebar.query_one("#sidebar-plan-section")
            assert section.display is False  # 初始无任务：隐藏
            # 隐藏计划区后 #sidebar-top（1fr 弹性占位）仍在，底部版本/路径不被顶到上方
            assert sidebar.query_one("#sidebar-top").display is True

            # 全部完成 → 仍隐藏
            plan_mod.current().replace([{"title": "写代码", "status": "completed"}])
            publish_plan(app, created=True, tool_call_id="1")
            await pilot.pause()
            assert section.display is False

            # 出现 pending 步骤 → 展示
            plan_mod.current().replace(
                [{"title": "写代码", "status": "completed"}, {"title": "跑测试", "status": "in_progress"}]
            )
            publish_plan(app, summary="共 2 步")
            await pilot.pause()
            assert section.display is True
            assert "跑测试" in str(sidebar.query_one(".plan-body").content)
            assert "写代码" in str(sidebar.query_one(".plan-body").content)

            # 全部取消 → 再次隐藏
            plan_mod.current().replace(
                [{"title": "写代码", "status": "cancelled"}, {"title": "跑测试", "status": "cancelled"}]
            )
            publish_plan(app, summary="共 2 步")
            await pilot.pause()
            assert section.display is False

    _run(_run_case())


def test_tui_new_clears_chat_area(monkeypatch):
    """/new：清空旧消息并重新渲染欢迎横幅，不追加任何提示文本；侧栏计划与会话一并重置。"""
    no_prompting(monkeypatch)
    from smithcode import plan as plan_mod

    async def _run_case():
        agent = _make_agent(monkeypatch)
        app = SmithTUI(agent)
        async with app.run_test() as pilot:
            chat = app.query_one(ChatView)
            chat.add_line("旧消息一")
            chat.add_line("旧消息二")
            plan_mod.current().replace([{"title": "旧步骤", "status": "pending"}])
            await pilot.pause()
            assert app.query_one("#chat").query(Static)  # 聊天区有内容

            app.handle_command("/new")
            await pilot.pause()

            text = _chat_text(app)
            assert "旧消息一" not in text and "旧消息二" not in text  # 旧消息清空
            assert f"v{__version__}" in text  # 欢迎横幅随新会话重新渲染
            assert "已开启新会话" not in text  # 不展示提示文本
            assert agent.session.messages == []
            assert not plan_mod.has_active()

    _run(_run_case())


def test_tui_new_blocked_while_busy(monkeypatch):
    """对齐 opencode：任务运行中 /new 被拦截，只提示不执行（后台线程在写历史，中途重置会撕裂轮次）。"""
    no_prompting(monkeypatch)

    async def _run_case():
        agent = _make_agent(monkeypatch)
        agent.session.add("user", "旧消息")
        app = SmithTUI(agent)
        async with app.run_test() as pilot:
            app.query_one(ChatView).add_line("旧消息")
            await pilot.pause()

            app._busy = True  # 模拟后台任务运行中
            app.handle_command("/new")
            await pilot.pause()

            assert "任务运行中" in _chat_text(app)  # 只显示拦截提示
            assert agent.session.messages != []  # 会话未被重置
            app._busy = False
            app.handle_command("/new")
            await pilot.pause()
            assert "任务运行中" not in _chat_text(app)  # 空闲时正常重置

    _run(_run_case())


def test_tui_sidebar_hidden_narrow(monkeypatch):
    """窄窗口：宽度不足断点时不展示侧边栏。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test(size=(80, 24)) as pilot:  # 默认窄终端
            assert app.query_one(Sidebar).display is False  # 自适应隐藏
            await pilot.resize_terminal(140, 30)  # 拉宽后恢复显示
            assert app.query_one(Sidebar).display is True
            await pilot.resize_terminal(100, 30)  # 再缩窄重新隐藏
            assert app.query_one(Sidebar).display is False

    _run(_run_case())


def test_tui_resume_blocked_while_busy(monkeypatch):
    """空闲守卫覆盖 /sessions 切换：任务运行中不允许切换会话。"""
    no_prompting(monkeypatch)

    async def _run_case():
        agent = _make_agent(monkeypatch)
        app = SmithTUI(agent)
        async with app.run_test() as pilot:
            app._busy = True
            app.handle_command("/sessions abc")
            await pilot.pause()
            assert "不能切换会话" in _chat_text(app)

    _run(_run_case())


def test_tui_compact_runs_in_background_with_feedback(monkeypatch):
    """/compact 不阻塞 UI：立即提示「正在压缩」，完成后回主线程提示「压缩完成」。"""
    no_prompting(monkeypatch)

    async def _run_case():
        agent = _make_agent(monkeypatch)
        app = SmithTUI(agent)
        started, release = threading.Event(), threading.Event()

        async def fake_compact_manual():
            started.set()
            # 卡住后台线程的协程（用线程事件等待，模拟耗时的摘要请求）
            await asyncio.to_thread(release.wait, 2)
            return "ok"

        monkeypatch.setattr(agent, "compact_manual", fake_compact_manual)
        async with app.run_test() as pilot:
            app.handle_command("/compact")
            await pilot.pause()

            assert started.wait(1)  # 命令已交给后台线程执行
            assert "正在压缩上下文" in _chat_text(app)  # 发送后立即有反馈
            assert "上下文压缩完成" not in _chat_text(app)
            assert app._busy is True
            assert app.query_one("#running").display is True

            release.set()
            for _ in range(200):  # 等后台线程收尾并经 UiAction 回主线程
                if not app._busy:
                    break
                await pilot.pause(0.02)
            assert "上下文压缩完成" in _chat_text(app)
            assert app._busy is False
            assert app.query_one("#running").display is False

    _run(_run_case())


def test_tui_compact_blocked_while_busy(monkeypatch):
    """任务运行中 /compact 被忙守卫拦截：后台线程正写历史，压缩会撕裂轮次。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            app._busy = True
            app.handle_command("/compact")
            await pilot.pause()
            assert "任务运行中" in _chat_text(app)
            assert "正在压缩上下文" not in _chat_text(app)

    _run(_run_case())


def test_tui_replays_restored_history(monkeypatch):
    """启动时若会话已有历史（-c/--resume 恢复），TUI 回放 user/assistant 文本。"""
    no_prompting(monkeypatch)

    async def _run_case():
        agent = _make_agent(monkeypatch)
        agent.session.messages = [
            {"role": "user", "content": "恢复的问题"},
            {"role": "assistant", "content": "恢复的回答"},
        ]
        app = SmithTUI(agent)
        async with app.run_test() as pilot:
            await pilot.pause()
            text = _chat_text(app)
            assert "恢复的问题" in text
            assert "恢复的回答" in text
            assert "已恢复" not in text  # 不打印恢复提示

    _run(_run_case())


def test_tui_replays_skill_payload_as_single_line(monkeypatch, tmp_path):
    """恢复会话回放历史时，技能载荷折叠为一行提示，不整段铺开。"""
    no_prompting(monkeypatch)
    from smithcode.skills import render
    from smithcode.skills.registry import Skill

    base = tmp_path / "proj"
    base.mkdir()
    (base / "SKILL.md").write_text(
        "---\nname: proj\ndescription: d\n---\n载荷正文标记\n", encoding="utf-8"
    )
    skill = Skill(
        name="proj", description="d", location=base / "SKILL.md", base=base,
        root=base.parent, scope="project", body="载荷正文标记",
    )
    payload = render.payload(skill)

    async def _run_case():
        agent = _make_agent(monkeypatch)
        agent.session.messages = [
            {"role": "user", "content": payload},
            {"role": "assistant", "content": "已按技能处理"},
        ]
        app = SmithTUI(agent)
        async with app.run_test() as pilot:
            await pilot.pause()
            text = _chat_text(app)
            assert "已加载技能 proj" in text
            assert "载荷正文标记" not in text  # 正文不铺满聊天区
            assert "已按技能处理" in text

    _run(_run_case())


# ---------- 侧边栏会话标题 ----------

def test_tui_sidebar_shows_session_title(monkeypatch):
    """侧边栏顶部展示会话标题，且用户命名不被自动标题覆盖。"""
    no_prompting(monkeypatch)

    async def _run_case():
        agent = _make_agent(monkeypatch)
        agent.session.set_title("重构会话管理")
        app = SmithTUI(agent)
        async with app.run_test(size=(140, 30)) as pilot:  # 够宽：侧边栏可见
            await pilot.pause()
            sidebar = app.query_one(Sidebar)
            widget = sidebar.query_one(".sidebar-title")
            assert widget.display is True
            assert "重构会话管理" in str(widget.content)
            assert sidebar.children[0] is widget  # 置顶

            agent.session.set_title("数据库迁移", source="auto")  # 用户标题优先
            await pilot.pause()
            assert "重构会话管理" in str(widget.content)

    _run(_run_case())


def test_tui_sidebar_title_falls_back_to_first_prompt(monkeypatch):
    """标题未生成时回退首轮 user 消息截断（与 /sessions 一致）。"""
    no_prompting(monkeypatch)

    async def _run_case():
        agent = _make_agent(monkeypatch)
        agent.session.add("user", "帮我重构 session 持久化并补测试")
        app = SmithTUI(agent)
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            assert "帮我重构 session" in str(
                app.query_one(".sidebar-title").content
            )

    _run(_run_case())


def test_tui_sidebar_title_hidden_without_history(monkeypatch):
    """新会话尚无任何消息：标题段整体隐藏，不占侧边栏空间。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            assert app.query_one(".sidebar-title").display is False

    _run(_run_case())


def test_tui_title_changed_action_refreshes(monkeypatch):
    """后台自动标题生成完成：Renderer.title_changed → 侧边栏刷新。"""
    no_prompting(monkeypatch)

    async def _run_case():
        agent = _make_agent(monkeypatch)
        app = SmithTUI(agent)
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            assert app.query_one(".sidebar-title").display is False

            agent.session.set_title("后台生成的标题")
            app.agent.events.publish(TitleChanged("后台生成的标题"))
            await pilot.pause()
            widget = app.query_one(".sidebar-title")
            assert widget.display is True
            assert "后台生成的标题" in str(widget.content)

    _run(_run_case())


def test_tui_sidebar_usage_section(monkeypatch):
    """侧边栏上半：「用量」「上下文」两张卡片各自成块、内容就位。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            sidebar = app.query_one(Sidebar)
            # 用量卡：无调用时标题为「Usage」，正文直接展示各项值（不再显示占位语）
            usage = str(sidebar.query_one(".usage-body").content)
            assert "In" in usage and "Out" in usage
            assert "尚无调用" not in usage
            assert str(sidebar.query_one(".usage-card .section-title").content).strip() == "Usage"
            # 上下文卡：百分比跟在「Context · 」后，正文为当前用量/预算
            context_title = str(sidebar.query_one(".context-card .section-title").content)
            assert context_title.startswith("Context · ")
            assert "%" in context_title
            context = str(sidebar.query_one(".context-body").content)
            assert "Used" in context and "Budget" not in context
            # 有调用后：用量卡标题变为「Usage · Calls N」，正文只剩 In/Out。
            # 界面只从**事件**渲染——所以这里发一条 UsageChanged（与生产同路）。
            app.agent.session.usage.add({"prompt_tokens": 1234, "completion_tokens": 567})
            app.agent.events.publish(app.agent._usage_event())
            await pilot.pause()
            assert str(sidebar.query_one(".usage-card .section-title").content) == "Usage · Calls 1"
            usage = str(sidebar.query_one(".usage-body").content)
            assert "1.2K" in usage and "In" in usage and "Out" in usage
            assert "Calls" not in usage  # 调用次数已上移到标题

    _run(_run_case())


def test_tui_status_bar(monkeypatch):
    """模型与思考强度展示在输入框内底部状态行；底栏只剩上下文占用条 / git。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            # 模型/思考在底行 #bottom 最左侧依次排列；运行动画在输入框框内上方
            bottom = app.query_one("#bottom")
            mode_w = app.query_one("#composer-mode")
            model_w = app.query_one("#composer-model")
            think_w = app.query_one("#composer-thinking")
            running = app.query_one("#running")
            assert model_w.parent is bottom
            assert think_w.parent is bottom
            assert mode_w.parent is bottom
            assert running.parent is app.query_one("#input-wrap")
            # 权限模式在最前，其后 模型 · 思考
            assert bottom.children.index(mode_w) < bottom.children.index(model_w) < bottom.children.index(think_w)
            assert str(mode_w.content) == "Smith"  # 默认档展示名
            assert config.MODEL in str(model_w.content)
            assert not str(model_w.content).startswith("▣")  # 模型前无图标
            assert "思考" not in str(think_w.content)  # 思考字样已去掉
            assert str(think_w.content).startswith("· ")  # · 分隔符 + 强度值
            mode, model, thinking = app._composer_status()
            assert mode.style == "#808080"  # Smith 灰（默认档）
            assert str(mode) == "Smith"
            assert str(model) == f"· {config.MODEL}"  # 灰色 · 分隔符 + 模型名
            assert str(thinking).startswith("· ")  # 灰色 · 分隔符 + 强度值
            assert model.style == "#808080"  # 分隔符灰
            assert any(s.style == "#7aa2f7" for s in model.spans)  # 模型蓝色（span）
            assert any(s.style == "#e0af68" for s in thinking.spans)  # 思考黄色（span）
            # 提问/权限面板替换输入框时，输入框隐藏
            result, evt = {}, threading.Event()
            _show_permission(app, 
                "允许? [y]本次 / [n]拒绝 / [a]总是允许该模式: ", "yna", "y / n / a", result, evt
            )
            await pilot.pause()
            assert app.query_one("#input-wrap").display is False
            await pilot.press("y")
            await pilot.pause()
            assert app.query_one("#input-wrap").display is True
            # 底部状态栏：上下文占用文字 + git 分支（不再含模型/思考强度/「上下文」「git」字样）
            status = str(app.query_one("#status").content)
            assert config.MODEL not in status
            assert "思考" not in status
            assert "项目" not in status
            assert "上下文" not in status
            assert "git" not in status
            # 上下文为纯文字 `12.3K(10%)`，不再有 █░ 进度条
            assert "(0%)" in status
            assert "█" not in status and "░" not in status

    _run(_run_case())


def test_tui_goal_indicator(monkeypatch):
    """持久目标在底栏展示指示、在侧边栏计划区上方展示卡片；无目标均隐藏。"""
    no_prompting(monkeypatch)
    from smithcode import goal as goal_mod

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            indicator = app.query_one("#composer-goal")
            section = app.query_one("#sidebar-goal-section")
            assert indicator.display is False  # 无目标隐藏
            assert section.display is False

            goal_mod.set("迁移模块", max_turns=5)
            app.ui_status()
            await pilot.pause()
            assert indicator.display is True
            assert str(indicator.content) == "◎ 目标 0/5"
            # 侧边栏目标卡片：位于计划区上方，标题带进度、正文含目标与明细
            assert section.display is True
            top = app.query_one("#sidebar-top")
            plan_section = app.query_one("#sidebar-plan-section")
            assert top.children.index(section) < top.children.index(plan_section)
            assert str(section.query_one(".section-title").content) == "目标 · 0/5"
            body = str(app.query_one(".goal-body").content)
            assert "迁移模块" in body and "进行中" in body

            goal_mod.pause("用户暂停")
            app.ui_status()
            await pilot.pause()
            assert "已暂停" in str(indicator.content)
            assert str(section.query_one(".section-title").content) == "目标 · 已暂停"

            goal_mod.clear()
            app.ui_status()
            await pilot.pause()
            assert indicator.display is False
            assert section.display is False

    _run(_run_case())


def test_tui_goal_command_starts_task(monkeypatch):
    """/goal 设定目标后向宿主返回 start_task，宿主立即开跑并刷新指示。"""
    no_prompting(monkeypatch)
    from smithcode import goal as goal_mod

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        started: list = []
        app.start_task = lambda text: started.append(text)  # 捕获宿主动作，不起真实线程
        async with app.run_test() as pilot:
            app.handle_command("/goal 修复所有 lint 问题")
            await pilot.pause()

            assert goal_mod.is_active()
            assert started and "修复所有 lint 问题" in started[0]
            assert "已设定目标" in _chat_text(app)
            assert "目标" in str(app.query_one("#composer-goal").content)

    _run(_run_case())


def test_tui_dynamic_skill_command_injects_payload_and_echoes_task(monkeypatch, tmp_path):
    """技能名直达：载荷注入会话历史并提示一行；带任务时输入原文回显后开跑。"""
    no_prompting(monkeypatch)
    from smithcode import skills

    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    (home / "config.toml").write_text('[skills]\nproject = "on"\n', encoding="utf-8")
    skill_dir = workspace / ".agents" / "skills" / "proj"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: proj\ndescription: 测试技能\n---\n正文\n", encoding="utf-8"
    )
    skills.clear()
    try:
        skills.refresh()

        async def _run_case():
            app = SmithTUI(_make_agent(monkeypatch))
            started: list = []
            app.start_task = lambda text: started.append(text)  # 捕获宿主动作，不起真实线程
            async with app.run_test() as pilot:
                app.handle_command("/proj 帮我处理报告")
                await pilot.pause()

                chat = _chat_text(app)
                assert "/proj 帮我处理报告" in chat  # 完整输入原文回显
                assert "以下为技能" not in chat  # 载荷不铺满聊天区
                assert started == ["帮我处理报告"]
                injected = [
                    m["content"]
                    for m in app.agent.session.messages
                    if m.get("role") == "user"
                ]
                assert any("以下为技能「proj」的完整指令" in c for c in injected)
                assert skills.active_names() == ["proj"]

        _run(_run_case())
    finally:
        skills.clear()


def test_tui_skills_panel_is_readonly(monkeypatch, tmp_path):
    """技能面板只读展示：Enter 不确认加载，仅 ↑↓ 查看、Esc 关闭。"""
    no_prompting(monkeypatch)
    from smithcode import skills

    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    (home / "config.toml").write_text('[skills]\nproject = "on"\n', encoding="utf-8")
    skill_dir = workspace / ".agents" / "skills" / "proj"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: proj\ndescription: 测试技能\n---\n正文\n", encoding="utf-8"
    )
    skills.clear()
    try:
        skills.refresh()

        async def _run_case():
            app = SmithTUI(_make_agent(monkeypatch))
            started: list = []
            app.start_task = lambda text: started.append(text)
            async with app.run_test() as pilot:
                inp = app.query_one(ChatInput)
                inp.focus()
                inp.insert("/skills")
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                assert isinstance(app.screen, SelectionScreen)
                panel = app.screen.query_one("SelectionPanel")
                assert panel._readonly is True
                await pilot.press("enter")  # 只读：Enter 不确认、不分发
                await pilot.pause()

                assert isinstance(app.screen, SelectionScreen)  # 面板仍在
                assert started == []
                assert "/proj" not in _chat_text(app)
                await pilot.press("escape")  # Esc 关闭
                await pilot.pause()
                assert not isinstance(app.screen, SelectionScreen)

                # 加载技能请用 /<技能名> 直达，仍可加载并开跑
                app.handle_command("/proj")
                await pilot.pause()
                assert started and "以下为技能「proj」的完整指令" in started[0]

        _run(_run_case())
    finally:
        skills.clear()


def test_tui_skill_load_blocked_while_busy(monkeypatch, tmp_path):
    """运行中不加载技能：加载在分发期登记集合、正文要等宿主投递，放行会留下坏状态。"""
    no_prompting(monkeypatch)
    from smithcode import skills

    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    (home / "config.toml").write_text('[skills]\nproject = "on"\n', encoding="utf-8")
    skill_dir = workspace / ".agents" / "skills" / "proj"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: proj\ndescription: 测试技能\n---\n正文\n", encoding="utf-8"
    )
    skills.clear()
    try:
        skills.refresh()

        async def _run_case():
            app = SmithTUI(_make_agent(monkeypatch))
            started: list = []
            app.start_task = lambda text: started.append(text)  # 捕获宿主动作，不起真实线程
            async with app.run_test() as pilot:
                app._busy = True  # 模拟后台任务运行中
                app.handle_command("/proj")
                await pilot.pause()

                assert "任务运行中" in _chat_text(app)
                assert started == []
                assert skills.active_names() == []  # 关键：未被登记为已加载

                app._busy = False
                app.handle_command("/proj")
                await pilot.pause()

                assert started and "以下为技能「proj」的完整指令" in started[0]
                assert skills.active_names() == ["proj"]

        _run(_run_case())
    finally:
        skills.clear()


def test_tui_cycle_permission_mode(monkeypatch):
    """Shift+Tab 循环切换权限模式，底栏同步刷新；权限面板弹出期间不响应。"""
    no_prompting(monkeypatch)
    assert any(getattr(b, "key", None) == "shift+tab" for b in SmithTUI.BINDINGS)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            assert str(app.query_one("#composer-mode").content) == "Smith"
            app.action_cycle_permission_mode()
            await pilot.pause()
            assert app.agent.permission.mode == "accept_edits"
            assert str(app.query_one("#composer-mode").content) == "Accept Edits"
            app.action_cycle_permission_mode()
            await pilot.pause()
            assert str(app.query_one("#composer-mode").content) == "Auto"
            app.action_cycle_permission_mode()
            await pilot.pause()
            assert str(app.query_one("#composer-mode").content) == "Smith"
            # 权限面板弹出（瞬时态）期间不响应切换
            result, evt = {}, threading.Event()
            _show_permission(app, 
                "允许? [y]本次 / [n]拒绝 / [a]总是允许该模式: ", "yna", "y / n / a", result, evt
            )
            await pilot.pause()
            app.action_cycle_permission_mode()
            await pilot.pause()
            assert app.agent.permission.mode == "smith"

    _run(_run_case())


def test_tui_cycle_permission_mode_does_not_touch_sidebar_title(monkeypatch):
    """回归：Shift+Tab 只刷新底栏模式段，不碰侧边栏标题控件。

    标题控件此前随全量 ui_status() 被无条件重写（Static.update 恒触发重排），
    空闲时按一次整栏闪一下，看起来像标题"跟着变化"。
    """
    no_prompting(monkeypatch)

    async def _run_case():
        agent = _make_agent(monkeypatch)
        agent.session.set_title("重构会话管理")
        app = SmithTUI(agent)
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            sidebar = app.query_one(Sidebar)
            widget = sidebar.query_one(".sidebar-title")
            before = str(widget.content)
            sentinel = object()
            widget.update = lambda content, **kw: setattr(widget, "_touched", True) or sentinel
            widget._touched = False
            app.action_cycle_permission_mode()
            await pilot.pause()
            assert app.agent.permission.mode == "accept_edits"
            assert str(app.query_one("#composer-mode").content) == "Accept Edits"
            assert getattr(widget, "_touched", False) is False  # 标题控件一次都没被碰
            assert str(sidebar.query_one(".sidebar-title").content) == before

    _run(_run_case())


def test_tui_sidebar_title_update_dedupes_same_content(monkeypatch):
    """同内容跳过 update：标题控件不再因无关刷新整栏重排闪动。"""
    no_prompting(monkeypatch)

    async def _run_case():
        from rich.text import Text

        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            sidebar = app.query_one(Sidebar)
            widget = sidebar.query_one(".sidebar-title")
            calls = []
            orig_update = widget.update
            widget.update = lambda content, **kw: calls.append(str(content))
            sidebar.update_title(Text("同标题", style="#7dcfff"))
            sidebar.update_title(Text("同标题", style="#7dcfff"))
            sidebar.update_title(Text("新标题", style="#7dcfff"))
            assert len(calls) == 2  # 第二次同内容被跳过
            widget.update = orig_update

    _run(_run_case())


class ThinkLLM:
    """先输出长思考、再回正文的假模型。"""

    def chat_stream(self, messages, tools=None):
        yield ("reasoning", "这个问题的关键是要先理解需求，")
        yield ("reasoning", "然后分析现状再动手。")
        yield ("content", "结论是 A。")
        yield ("message", {"role": "assistant", "content": "结论是 A。"})


def test_tui_thinking_collapsed_block(monkeypatch):
    """思考过程渲染为可折叠块：默认收起只显示计数，展开可见全文。"""
    no_prompting(monkeypatch)
    import smithcode.agent as agent_mod

    agent_mod.LLMClient = lambda: ThinkLLM()

    async def _run_case():
        app = SmithTUI(Agent(session=Session()))
        async with app.run_test() as pilot:
            inp = app.query_one(ChatInput)
            inp.focus()
            inp.insert("分析一下")
            await pilot.press("enter")
            for _ in range(200):
                if not app._busy:
                    break
                await pilot.pause(0.02)
            block = app.query_one("#chat").query(ThinkingBlock).first()
            header = str(block.query_one(".think-header").content)
            assert "Thinking" in header
            assert "字符" in header  # 计数而非全文
            assert "这个问题的关键" not in header  # 不刷屏
            body = block.query_one(".think-body")
            assert body.display is False  # 默认收起
            block.action_toggle()
            assert body.display is True
            assert "这个问题的关键" in str(body.content)

    _run(_run_case())


def test_running_indicator_during_task(monkeypatch):
    """运行中动画：执行时显示、结束隐藏。"""
    no_prompting(monkeypatch)
    import time as _time

    import smithcode.agent as agent_mod

    class SlowLLM:
        def chat_stream(self, messages, tools=None):
            _time.sleep(0.5)
            yield ("content", "完成")
            yield ("message", {"role": "assistant", "content": "完成"})

    agent_mod.LLMClient = lambda: SlowLLM()

    async def _run_case():
        app = SmithTUI(Agent(session=Session()))
        async with app.run_test() as pilot:
            running = app.query_one("#running")
            menu = app.query_one(CommandMenu)
            assert running.display is False  # 初始隐藏
            inp = app.query_one(ChatInput)
            inp.focus()
            inp.insert("hi")
            await pilot.press("enter")
            for _ in range(100):  # 轮询等到动画出现
                if running.display:
                    break
                await pilot.pause(0.01)
            assert running.display is True  # 执行中显示动画
            assert menu.has_class("running")  # 动画占一行：命令菜单锚点上移
            for _ in range(300):
                if not app._busy:
                    break
                await pilot.pause(0.02)
            for _ in range(100):  # 结束消息是异步投递的，轮询等动画隐藏
                if not running.display:
                    break
                await pilot.pause(0.01)
            assert running.display is False  # 结束隐藏
            assert not menu.has_class("running")  # 锚点归位

    _run(_run_case())


def test_format_duration_levels():
    """时长分级格式：各级到点才出现，不凑零、不进位显示。"""
    assert format_duration(0.4) == "0s"
    assert format_duration(42.7) == "42s"
    assert format_duration(59.9) == "59s"
    assert format_duration(60) == "1m 0s"
    assert format_duration(330) == "5m 30s"
    assert format_duration(3599) == "59m 59s"
    assert format_duration(3600) == "1h 0m 0s"
    assert format_duration(4350) == "1h 12m 30s"


def test_running_indicator_width_stable_across_levels():
    """动画文案宽度不随时长分级变化：这是 _spin 用 layout=False 免重排的前提。"""
    from smithcode.tui.widgets import RunningIndicator

    indicator = RunningIndicator()
    widths = {len(indicator._spin_text(secs)) for secs in (0, 59, 60, 599, 3600, 3600 * 99)}
    assert len(widths) == 1


def test_running_indicator_first_show_has_width(monkeypatch):
    """回归：首次显示即有宽度——start() 立即定宽渲染，否则 layout=False 让首轮零宽不可见。"""
    no_prompting(monkeypatch)
    from smithcode.tui.widgets import RunningIndicator

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            running = app.query_one("#running", RunningIndicator)
            assert running.size.width == 0  # 空内容初始零宽
            running.display = True
            running.start()
            await pilot.pause(0.2)
            assert running.size.width > 0  # 首次显示即定宽可见
            assert "Working" in str(running.render())

    _run(_run_case())


def test_turn_footer_after_task(monkeypatch):
    """轮次结束在会话末尾追加 opencode 式「▣ 模型 · 思考强度 · 用时」页脚。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            inp = app.query_one(ChatInput)
            inp.focus()
            inp.insert("说句话")
            await pilot.press("enter")
            for _ in range(200):
                if not app._busy:
                    break
                await pilot.pause(0.02)
            footer = str(app.query_one("#chat").children[-1].content)
            assert footer.startswith("▣")
            assert config.MODEL in footer
            assert (config.REASONING_EFFORT or config.DEFAULT_EFFORT) in footer
            assert "用时" not in footer
            assert "已停止" not in footer  # 正常结束不带中断状态

    _run(_run_case())


def test_running_indicator_stopping_suffix():
    """Esc 停止态：运行动画行尾动态追加「· 正在停止…」，便于测试宽度稳定。"""
    indicator = RunningIndicator()
    base = indicator._spin_text(12.0)
    indicator.mark_stopping()
    stopping = indicator._spin_text(12.0)
    assert stopping.startswith(base)
    assert stopping.endswith(" · 正在停止…")


def test_esc_marks_running_indicator_without_chat_line(monkeypatch):
    """Esc 中断：不往对话区打「正在停止…」行，改让运行动画行尾动态显示。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            running = app.query_one("#running", RunningIndicator)
            running.display = True
            running.start()
            await pilot.pause()
            app._busy = True
            app.agent.interrupt = lambda: None
            before = len(app.query_one("#chat").children)
            app.action_interrupt()
            await pilot.pause()
            assert running._stopping is True
            assert "正在停止…" in str(running.render())
            assert len(app.query_one("#chat").children) == before  # 未新增聊天行

    _run(_run_case())


def test_turn_footer_shows_stopped_on_interrupt(monkeypatch):
    """中断收尾：轮次页脚行尾显示「· 已停止」。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            app._turn_start = time.monotonic()
            app.ui_turn_end("interrupted")
            await pilot.pause()
            footer = str(app.query_one("#chat").children[-1].content)
            assert footer.startswith("▣")
            assert footer.endswith(" · 已停止")

    _run(_run_case())


def test_turn_footer_reads_pinned_snapshot(monkeypatch):
    """页脚读轮级快照而非全局配置：轮内切换后显示实际发出的值。"""
    no_prompting(monkeypatch)
    monkeypatch.setattr(config, "MODEL", "m-switched")
    monkeypatch.setattr(config, "REASONING_EFFORT", "max")

    async def _run_case():
        agent = _make_agent(monkeypatch)
        # run() 已结束但快照保留：页脚读到的是 pin 住的值，不是切后的全局值
        agent._turn = TurnConfig(model="m-pinned", effort="low")
        app = SmithTUI(agent)
        async with app.run_test() as pilot:
            app._turn_start = time.monotonic()
            app.ui_turn_end("ok")
            await pilot.pause()
            footer = str(app.query_one("#chat").children[-1].content)
            assert "m-pinned" in footer
            assert "low" in footer
            assert "m-switched" not in footer

    _run(_run_case())


def test_running_indicator_above_input(monkeypatch):
    """运行动画固定于输入框上方（同一容器内、排在输入框之前）；状态行仍在底行。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test():
            input_wrap = app.query_one("#input-wrap")
            running = app.query_one("#running")
            chat_input = app.query_one(ChatInput)
            status = app.query_one("#status")
            assert running.parent is input_wrap  # 与输入框同一容器，紧邻输入框上方
            assert input_wrap.children.index(running) < input_wrap.children.index(chat_input)
            assert status.parent is app.query_one("#bottom")  # 上下文/git 仍在底行
            assert running.display is False  # 空闲时动画隐藏
            # 左侧竖线画在输入框自身：容器无边框，上方动画行不会被一起框住
            # （只校验画在哪一层，不锁具体字形：thick / heavy / solid 都算通过）
            assert input_wrap.styles.border_left[0] in ("", "none")
            assert chat_input.styles.border_left[0] not in ("", "none")

    _run(_run_case())


def test_user_message_panel(monkeypatch):
    """用户消息按 opencode 式面板渲染：左侧角色色竖线 + 面板底色，无前缀。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            inp = app.query_one(ChatInput)
            inp.focus()
            inp.insert("帮我改个 bug")
            await pilot.press("enter")
            await pilot.pause(0.05)
            panel = app.query_one("#chat").query(".user-msg").first()
            assert str(panel.content) == "帮我改个 bug"  # 无"你>"前缀
            assert "你>" not in str(panel.content)

    _run(_run_case())


def test_git_branch_detection(tmp_path):
    """git 分支读取：常规仓库 / 带斜杠的分支名 / 无 .git / detached HEAD。"""
    repo = tmp_path / "repo"
    (repo / ".git" / "refs" / "heads").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    assert git_branch(str(repo)) == "main"

    # 带斜杠的分支名保留完整路径（曾误截成最后一段）
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/feat/tui-statusbar\n", encoding="utf-8")
    assert git_branch(str(repo)) == "feat/tui-statusbar"

    assert git_branch(str(tmp_path)) is None  # 无 .git

    detached = tmp_path / "detached"
    (detached / ".git").mkdir(parents=True)
    (detached / ".git" / "HEAD").write_text("1a2b3c4d5e6f\n", encoding="utf-8")
    assert git_branch(str(detached)) == "1a2b3c4"
def test_question_choice_modal_single_pick(monkeypatch):
    """选项提问弹窗：数字键快选，单选先到确认页，Enter 确认后提交所选项。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            _show_questions(app, 
                [{"question": "用哪个？", "options": ["甲", "乙"],
                  "descriptions": [], "multiple": False}], result, evt)
            await pilot.pause()
            await pilot.press("2")  # 数字快选 → 进确认页（不直接提交）
            await pilot.pause()
            assert str(app.query_one(".ask-title").content) == "确认提交"
            assert not evt.is_set()
            await pilot.press("enter")  # 确认页提交
            await pilot.pause()
            assert result.get("values") == ["乙"]
            assert evt.is_set()

    _run(_run_case())


def test_question_choice_modal_multiple_toggle(monkeypatch):
    """多选：空格勾选两项，Enter 进确认页，再 Enter 提交逗号拼接结果。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            _show_questions(app, 
                [{"question": "选特征", "options": ["红", "大", "圆"],
                  "descriptions": [], "multiple": True}], result, evt)
            await pilot.pause()
            await pilot.press("space")   # 勾选 1（红）
            await pilot.press("down")
            await pilot.press("down")
            await pilot.press("space")   # 勾选 3（圆）
            await pilot.press("enter")   # 提交 → 确认页
            await pilot.pause()
            assert app.query_one(QuestionPanel)._review
            await pilot.press("enter")   # 确认页提交
            await pilot.pause()
            assert result.get("values") == ["红, 圆"]
            assert evt.is_set()

    _run(_run_case())


def test_question_choice_modal_multiple_empty_submits_skipped(monkeypatch):
    """多选一道未勾选时 Enter 照常提交：记为「（未选择）」，进确认页后可再确认。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            _show_questions(app, 
                [{"question": "选特征", "options": ["红", "大", "圆"],
                  "descriptions": [], "multiple": True}], result, evt)
            await pilot.pause()
            await pilot.press("enter")   # 一道未勾选直接提交 → 确认页
            await pilot.pause()
            assert app.query_one(QuestionPanel)._review
            await pilot.press("enter")   # 确认页提交
            await pilot.pause()
            assert result.get("values") == ["（未选择）"]
            assert evt.is_set()

    _run(_run_case())


def test_question_choice_modal_multiple_empty_advances(monkeypatch):
    """多题中的多选题空提交：记为「（未选择）」并前进到下一题，最后一题答完进确认页。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            _show_questions(app, [
                {"question": "选特征", "options": ["红", "大"], "descriptions": [],
                 "multiple": True},
                {"question": "用哪个？", "options": ["甲", "乙"], "descriptions": [],
                 "multiple": False},
            ], result, evt)
            await pilot.pause()
            panel = app.query_one(QuestionPanel)
            await pilot.press("enter")   # Q1 未勾选提交 → 前进到 Q2
            await pilot.pause()
            assert panel._index == 1
            assert panel._answers[0] == "（未选择）"
            await pilot.press("2")       # 数字快选 Q2 的「乙」→ 进确认页
            await pilot.pause()
            assert panel._review
            await pilot.press("enter")   # 确认页提交整组
            await pilot.pause()
            assert result.get("values") == ["（未选择）", "乙"]
            assert evt.is_set()

    _run(_run_case())


def test_question_choice_modal_custom_answer(monkeypatch):
    """自定义回答：光标在「输入自定义回答」行回车进入编辑，输入后回车进确认页，
    确认页 Enter 再提交。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            _show_questions(app, 
                [{"question": "颜色？", "options": ["红", "蓝"],
                  "descriptions": [], "multiple": False}], result, evt)
            await pilot.pause()
            panel = app.query_one(QuestionPanel)
            await pilot.press("down")
            await pilot.press("down")    # 移到"输入自定义回答"
            await pilot.press("enter")   # 进入编辑态
            await pilot.pause()
            assert panel._input.has_focus
            await pilot.press("紫", "色")  # 向输入框键入
            await pilot.press("enter")   # 提交 → 进确认页
            await pilot.pause()
            assert panel._review
            await pilot.press("enter")   # 确认页提交
            await pilot.pause()
            assert result.get("values") == ["紫色"]
            assert evt.is_set()

    _run(_run_case())


def test_question_panel_custom_esc_exits_input_then_reenter(monkeypatch):
    """有选项题：输入态 Esc 只退出输入回选项列表（不取消整组），回车可再次进入编辑。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            _show_questions(app, [
                {"question": "颜色？", "options": ["红", "蓝"],
                 "descriptions": [], "multiple": False}], result, evt)
            await pilot.pause()
            panel = app.query_one(QuestionPanel)
            await pilot.press("down", "down", "enter")  # 选「输入自定义回答」
            await pilot.pause()
            assert panel._input.has_focus
            await pilot.press("紫")
            await pilot.press("escape")                 # 退出输入，回选项列表
            await pilot.pause()
            assert not evt.is_set()
            assert panel._editing == [False]
            assert not panel._input.has_focus
            await pilot.press("enter")                  # 光标仍在自定义行 → 再次进入编辑
            await pilot.pause()
            assert panel._editing == [True]
            assert panel._input.has_focus
            assert panel._input.value == "紫"           # 已输入内容回填保留
            await pilot.press("色", "enter")            # 追加，回车提交 → 确认页
            await pilot.pause()
            assert panel._review
            await pilot.press("enter")                  # 确认页提交
            await pilot.pause()
            assert result.get("values") == ["紫色"]
            assert evt.is_set()

    _run(_run_case())


def test_question_panel_pure_input_esc_exits_not_cancel(monkeypatch):
    """纯输入题（无选项）：Esc 退出输入但保留输入框，Enter 可重新聚焦继续输入。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            _show_questions(app, [
                {"question": "随便说点什么", "options": [],
                 "descriptions": [], "multiple": False}], result, evt)
            await pilot.pause()
            panel = app.query_one(QuestionPanel)
            assert panel._input.has_focus
            await pilot.press("a")
            await pilot.press("escape")                 # 第一次 Esc：只退出输入
            await pilot.pause()
            assert not evt.is_set(), "Esc 不应取消整组"
            assert panel._editing == [True]             # 仍处编辑态
            assert panel._input.display is True         # 输入框保留
            assert not panel._input.has_focus
            assert panel._input.value == "a"
            await pilot.press("enter")                  # Enter 重新聚焦
            await pilot.pause()
            assert panel._input.has_focus
            await pilot.press("b", "enter")             # 追加并提交 → 确认页
            await pilot.pause()
            await pilot.press("enter")                  # 确认页提交
            await pilot.pause()
            assert result.get("values") == ["ab"]
            assert evt.is_set()

    _run(_run_case())


def test_question_panel_pure_input_typing_refocuses(monkeypatch):
    """纯输入题 Esc 失焦后，直接敲字自动重新聚焦并写入（省去先按 Enter）。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            _show_questions(app, [
                {"question": "Q", "options": [],
                 "descriptions": [], "multiple": False}], result, evt)
            await pilot.pause()
            panel = app.query_one(QuestionPanel)
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("x")                      # 失焦后敲字 → 自动聚焦并写入
            await pilot.pause()
            assert panel._input.has_focus
            assert panel._input.value == "x"
            await pilot.press("enter")                  # 提交 → 确认页
            await pilot.pause()
            await pilot.press("enter")                  # 确认页提交
            await pilot.pause()
            assert result.get("values") == ["x"]

    _run(_run_case())


def test_question_panel_pure_input_second_esc_cancels(monkeypatch):
    """纯输入题：输入框失焦后，再按 Esc 才取消整组。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            _show_questions(app, [
                {"question": "Q", "options": [],
                 "descriptions": [], "multiple": False}], result, evt)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert not evt.is_set()
            await pilot.press("escape")                 # 第二次 Esc（未聚焦）：取消整组
            await pilot.pause()
            assert evt.is_set()
            assert result.get("values") == [""]

    _run(_run_case())


def test_question_panel_switch_into_pure_input_keeps_focus_on_list(monkeypatch):
    """切进纯输入题不抢焦点：输入框可见但失焦，←/→ 立即可切题，敲字再进入输入。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            _show_questions(app, [
                {"question": "Q1", "options": ["A", "B"], "descriptions": [], "multiple": False},
                {"question": "Q2", "options": [], "descriptions": [], "multiple": False},
                {"question": "Q3", "options": ["C", "D"], "descriptions": [], "multiple": False},
            ], result, evt)
            await pilot.pause()
            panel = app.query_one(QuestionPanel)
            await pilot.press("right")                  # → Q2（纯输入题）
            await pilot.pause()
            assert str(app.query_one(".ask-title").content) == "(2/3) Q2"
            assert panel._input.display is True         # 输入框可见
            assert not panel._input.has_focus           # 但不抢焦点
            await pilot.press("right")                  # 焦点在列表 → 直接切题
            await pilot.pause()
            assert str(app.query_one(".ask-title").content) == "(3/3) Q3"
            await pilot.press("left")                   # ← 回到 Q2
            await pilot.pause()
            assert str(app.query_one(".ask-title").content) == "(2/3) Q2"
            assert not panel._input.has_focus
            await pilot.press("x")                      # 敲字 → 自动聚焦并写入
            await pilot.pause()
            assert panel._input.has_focus
            assert panel._input.value == "x"
            await pilot.press("enter")                  # 提交本题答案
            await pilot.pause()
            assert panel._answers[1] == "x"
            assert not evt.is_set()                     # Q1/Q3 未答，整组未提交

    _run(_run_case())


def test_question_panel_switch_back_keeps_focus_on_list(monkeypatch):
    """带选项题填了自定义回答后切走再切回：不聚焦输入框、选中项复位到第一项；
    选项名不变、答案缩进显示在其下。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            _show_questions(app, [
                {"question": "Q1", "options": ["红", "蓝"],
                 "descriptions": [], "multiple": False},
                {"question": "Q2", "options": ["C", "D"],
                 "descriptions": [], "multiple": False},
            ], result, evt)
            await pilot.pause()
            panel = app.query_one(QuestionPanel)
            await pilot.press("down", "down", "enter")  # 选「输入自定义回答」
            await pilot.pause()
            await pilot.press("紫", "enter")            # 回车提交并前进 → Q2
            await pilot.pause()
            assert str(app.query_one(".ask-title").content) == "(2/2) Q2"
            await pilot.press("left")                   # ← 切回 Q1
            await pilot.pause()
            assert str(app.query_one(".ask-title").content) == "(1/2) ✔ Q1"
            assert not panel._input.has_focus           # 不抢焦点
            assert panel._input.display is False        # 回列表态，输入框收起
            assert panel._selected[0] == 0              # 选中项复位到第一个选项
            body = str(panel._body.render())
            assert "3. 输入自定义回答…" in body           # 选项名不变
            assert "紫" in body                          # 答案缩进显示在末行下方
            assert not evt.is_set()
            # 回车进入编辑修改文本（已填内容回填），再回车提交前进
            await pilot.press("down", "down")           # 移回自定义行
            await pilot.press("enter")
            await pilot.pause()
            assert panel._input.has_focus
            assert panel._input.value == "紫"
            await pilot.press("色", "enter")            # 追加，回车提交并前进
            await pilot.pause()
            assert panel._answers[0] == "紫色"           # 改后答案为「紫色」
            assert not evt.is_set()                     # Q2 未答，整组未提交

    _run(_run_case())


def test_question_panel_multiple_with_custom_merges(monkeypatch):
    """多选 + 自定义输入：回车提交时把勾选项与自定义文本一起计入答案。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            _show_questions(app, [
                {"question": "想用哪些？", "options": ["A", "B", "C"],
                 "descriptions": [], "multiple": True},
            ], result, evt)
            await pilot.pause()
            panel = app.query_one(QuestionPanel)
            await pilot.press("space")                  # 勾 A
            await pilot.press("down")
            await pilot.press("space")                  # 勾 B
            await pilot.pause()
            await pilot.press("down")                   # 移到第三项 C
            await pilot.press("down")                   # 移到最后一行「输入自定义回答」
            await pilot.press("enter")
            await pilot.pause()
            assert panel._input.has_focus
            await pilot.press("X", "Y")
            await pilot.press("enter")                  # 提交：A, B + XY → 确认页
            await pilot.pause()
            assert panel._review
            await pilot.press("enter")                  # 确认页提交
            await pilot.pause()
            assert result.get("values") == ["A, B, XY"]
            assert evt.is_set()

    _run(_run_case())


def test_question_panel_switch_resets_cursor_to_first_option(monkeypatch):
    """切换问题时选中项复位到第一个选项（不沿用上一次的停留位置）。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            _show_questions(app, [
                {"question": "Q1", "options": ["A1", "A2", "A3"],
                 "descriptions": [], "multiple": False},
                {"question": "Q2", "options": ["B1", "B2"],
                 "descriptions": [], "multiple": False},
            ], result, evt)
            await pilot.pause()
            panel = app.query_one(QuestionPanel)
            await pilot.press("down", "down")           # Q1 光标移到第 3 项
            await pilot.pause()
            assert panel._selected[0] == 2
            await pilot.press("right")                  # → Q2
            await pilot.pause()
            assert panel._selected[1] == 0              # Q2 光标在第一个选项
            await pilot.press("left")                   # ← 回 Q1
            await pilot.pause()
            assert panel._selected[0] == 0              # Q1 也复位到第一项

    _run(_run_case())


def test_question_panel_multi_questions_auto_advance(monkeypatch):
    """多题面板：逐题作答后自动前进到下一题，答完最后一题进入确认页再提交。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            _show_questions(app, [
                {"question": "端口？", "options": ["8000", "3000"],
                 "descriptions": [], "multiple": False},
                {"question": "鉴权？", "options": ["要", "不要"],
                 "descriptions": [], "multiple": False},
            ], result, evt)
            await pilot.pause()
            assert str(app.query_one(".ask-title").content) == "(1/2) 端口？"
            await pilot.press("1")  # Q1 选 8000 → 自动跳到 Q2
            await pilot.pause()
            assert str(app.query_one(".ask-title").content) == "(2/2) 鉴权？"
            await pilot.press("2")  # Q2 选「不要」→ 进入确认页（不直接提交）
            await pilot.pause()
            assert str(app.query_one(".ask-title").content) == "确认提交"
            assert not evt.is_set()  # 确认页尚未提交
            await pilot.press("enter")  # 确认页 enter → 直接提交
            await pilot.pause()
            assert result.get("values") == ["8000", "不要"]
            assert evt.is_set()

    _run(_run_case())


def test_question_panel_review_page_navigation(monkeypatch):
    """确认页视作循环最后一页：←/→ 可切进切出，enter 提交，可回跳修改。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            _show_questions(app, [
                {"question": "Q1", "options": ["A1", "A2"],
                 "descriptions": [], "multiple": False},
                {"question": "Q2", "options": ["B1", "B2"],
                 "descriptions": [], "multiple": False},
            ], result, evt)
            await pilot.pause()
            await pilot.press("1")  # Q1 = A1 → Q2
            await pilot.press("1")  # Q2 = B1 → 确认页
            await pilot.pause()
            assert str(app.query_one(".ask-title").content) == "确认提交"
            joined = str(app.query_one(QuestionPanel)._body.render())
            assert "Q1" in joined and "A1" in joined and "Q2" in joined and "B1" in joined
            await pilot.press("left")  # 确认页 ← 回到 Q2
            await pilot.pause()
            assert str(app.query_one(".ask-title").content) == "(2/2) ✔ Q2"
            await pilot.press("left")  # 再 ← 回到 Q1
            await pilot.pause()
            assert str(app.query_one(".ask-title").content) == "(1/2) ✔ Q1"
            await pilot.press("2")     # 改成 A2 → 顺序进下一题（Q2），不直接跳到确认页
            await pilot.pause()
            assert str(app.query_one(".ask-title").content) == "(2/2) ✔ Q2"
            await pilot.press("right")  # 从最后一题 → 确认页
            await pilot.pause()
            assert str(app.query_one(".ask-title").content) == "确认提交"
            await pilot.press("enter")  # 提交
            await pilot.pause()
            assert result.get("values") == ["A2", "B1"]
            assert evt.is_set()

    _run(_run_case())


def test_question_panel_revisit_middle_advances_to_next(monkeypatch):
    """回归：全部答完进入确认页后回改**中间**某题，提交应顺序进下一题，
    而不是因后面都已答而直接跳回确认页。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            _show_questions(app, [
                {"question": "Q1", "options": ["A1", "A2"],
                 "descriptions": [], "multiple": False},
                {"question": "Q2", "options": ["B1", "B2"],
                 "descriptions": [], "multiple": False},
                {"question": "Q3", "options": ["C1", "C2"],
                 "descriptions": [], "multiple": False},
            ], result, evt)
            await pilot.pause()
            await pilot.press("1", "1", "1")        # Q1/Q2/Q3 依次作答 → 确认页
            await pilot.pause()
            assert str(app.query_one(".ask-title").content) == "确认提交"
            await pilot.press("left", "left")       # 确认页 ← ×2 → Q2（中间题）
            await pilot.pause()
            assert str(app.query_one(".ask-title").content) == "(2/3) ✔ Q2"
            await pilot.press("2")                  # 改选 B2 → 进下一题 Q3（非确认页）
            await pilot.pause()
            assert str(app.query_one(".ask-title").content) == "(3/3) ✔ Q3"
            assert not evt.is_set()
            await pilot.press("2")                  # Q3 改选 C2 → 已在最后一题且全答完 → 确认页
            await pilot.pause()
            assert str(app.query_one(".ask-title").content) == "确认提交"
            await pilot.press("enter")
            await pilot.pause()
            assert result.get("values") == ["A1", "B2", "C2"]
            assert evt.is_set()

    _run(_run_case())


def test_question_panel_manual_switch_revisit(monkeypatch):
    """多题面板：←/→ 手动切题，回跳可改已答（已答题标题带 ✔）。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            _show_questions(app, [
                {"question": "Q1", "options": ["A1", "A2"],
                 "descriptions": [], "multiple": False},
                {"question": "Q2", "options": ["B1", "B2"],
                 "descriptions": [], "multiple": False},
            ], result, evt)
            await pilot.pause()
            await pilot.press("2")     # Q1 选 A2 → 自动进 Q2
            await pilot.pause()
            await pilot.press("left")  # 回跳 Q1（已答，带 ✔）
            await pilot.pause()
            title = str(app.query_one(".ask-title").content)
            assert title == "(1/2) ✔ Q1"
            await pilot.press("1")     # 改选 A1 → 顺序进仍未答的 Q2
            await pilot.pause()
            assert str(app.query_one(".ask-title").content) == "(2/2) Q2"
            await pilot.press("right")  # Q2 未答仍可前进 → 确认页
            await pilot.pause()
            assert str(app.query_one(".ask-title").content) == "确认提交"
            await pilot.press("right")  # 确认页再右移 → 环绕回 Q1
            await pilot.pause()
            assert str(app.query_one(".ask-title").content) == "(1/2) ✔ Q1"
            await pilot.press("left")   # 环绕：Q1 左移 → 确认页
            await pilot.pause()
            assert str(app.query_one(".ask-title").content) == "确认提交"
            await pilot.press("left")   # ← 回到 Q2 作答
            await pilot.pause()
            await pilot.press("1")      # Q2 选 B1 → 自动进确认页
            await pilot.pause()
            assert str(app.query_one(".ask-title").content) == "确认提交"
            await pilot.press("enter")  # 提交
            await pilot.pause()
            assert result.get("values") == ["A1", "B1"]
            assert evt.is_set()

    _run(_run_case())


def test_question_panel_multi_escape_cancels_group(monkeypatch):
    """多题面板：Esc 取消整组，返回全空（调用方兜底为「已取消」）。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            _show_questions(app, [
                {"question": "Q1", "options": ["A", "B"], "descriptions": [], "multiple": False},
                {"question": "Q2", "options": ["C", "D"], "descriptions": [], "multiple": False},
            ], result, evt)
            await pilot.pause()
            await pilot.press("1")     # Q1 选 A → 跳 Q2
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert result.get("values") == ["", ""]
            assert evt.is_set()

    _run(_run_case())


# ---------- 斜杠命令菜单：输入 / 弹出、实时过滤、按键仲裁 ----------

def test_command_menu_shows_on_slash_and_filters(monkeypatch):
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            inp = app.query_one(ChatInput)
            menu = app.query_one(CommandMenu)
            inp.focus()
            chat = app.query_one("#chat")
            before = inp.region
            inp.insert("/")
            await pilot.pause()
            assert menu.open
            assert len(menu._candidates) >= 8  # 全量命令
            assert (inp.region.x, inp.region.y, inp.region.width, inp.region.height) == (
                before.x, before.y, before.width, before.height
            )  # 悬浮层：输入框位置尺寸不变
            assert menu.region.bottom <= inp.region.y  # 菜单锚在输入框正上方
            assert menu.region.y < chat.region.bottom  # 且盖住聊天区底部
            inp.insert("he")
            await pilot.pause()
            assert [c.name for c in menu._candidates] == ["help"]  # 前缀过滤
            inp.text = "普通消息"
            await pilot.pause()
            assert not menu.open  # 非 / 前缀自动收起

    _run(_run_case())


def test_command_menu_enter_accepts_without_sending(monkeypatch):
    """菜单开着时 Enter 只补全命令名（带尾随空格），不当作消息发送。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            inp = app.query_one(ChatInput)
            inp.focus()
            inp.insert("/he")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert inp.text == "/help "
            assert not app.query_one(CommandMenu).open
            assert not app._busy  # 未提交任务

    _run(_run_case())


def test_command_menu_immediate_command_executes(monkeypatch):
    """immediate 命令（/model）在菜单选中后立即执行，不填入输入框。"""
    no_prompting(monkeypatch)
    from types import SimpleNamespace

    monkeypatch.setattr(config, "MODEL", "a")

    async def _run_case():
        agent = _make_agent(monkeypatch)
        agent.models = SimpleNamespace(list=lambda: ["a", "b"])
        app = SmithTUI(agent)
        async with app.run_test() as pilot:
            inp = app.query_one(ChatInput)
            inp.focus()
            inp.insert("/model")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert inp.text == ""  # 未填入输入框
            assert isinstance(app.screen, SelectionScreen)  # 直接弹选择框

    _run(_run_case())


def test_command_menu_sessions_immediate_and_switch(monkeypatch, tmp_path):
    """/sessions immediate：菜单选中直接弹选择框，选中后切换会话并回放历史。"""
    no_prompting(monkeypatch)
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(workspace))

    from smithcode.sessions import SessionStore

    store = SessionStore.create(cwd=str(workspace))
    store.append_event(wrap(MessageEnd(message={"role": "user", "content": "旧会话的问题"})))
    store.append_event(wrap(MessageEnd(message={"role": "assistant", "content": "旧会话的回答"})))
    store.close()

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        # 终端够宽，large 档（88）不被 max-width: 90% 夹到，便于断言档位宽度
        async with app.run_test(size=(140, 40)) as pilot:
            inp = app.query_one(ChatInput)
            inp.focus()
            inp.insert("/sessions")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert inp.text == ""  # 未填入输入框
            assert isinstance(app.screen, SelectionScreen)  # 直接弹选择框
            # /sessions 声明 large 档：面板比默认 medium 宽（会话标题 + 说明较长）
            panel = app.screen.query_one("SelectionPanel")
            assert panel.has_class("size-large")
            assert panel.styles.width.value == 88
            # 行由行区控件自渲染（无逐行子控件）：trailing 贴行尾右对齐 = 该行
            # 文本单元格数等于可视宽度，且尾部无补白（空隙在中间）、内容含短 id
            view = panel._rows_view
            line = view.row_strip(panel._row_of_item[0])
            assert line.cell_length == view.scrollable_content_region.width
            assert not line.text.endswith(" ")  # 时间贴行尾，行尾不留补白
            assert store.id[:8] in line.text

            await pilot.press("enter")  # 选中唯一会话 → 切换
            await pilot.pause()
            text = _chat_text(app)
            assert "旧会话的问题" in text
            assert "旧会话的回答" in text

    _run(_run_case())


def test_command_menu_effort_immediate_and_switch(monkeypatch):
    """/effort immediate：菜单选中直接弹选择框，选定后切换思考强度。"""
    no_prompting(monkeypatch)
    monkeypatch.setattr(config, "REASONING_EFFORT", "low")

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            inp = app.query_one(ChatInput)
            inp.focus()
            inp.insert("/effort")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, SelectionScreen)
            panel = app.screen.query_one("SelectionPanel")
            # 命令未指定档位 → 默认 medium（宽度 64）
            assert panel.has_class("size-medium")
            assert panel.styles.width.value == 64
            # low 是当前项，↓ 移到下一档 high
            start = panel._selected
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert not isinstance(app.screen, SelectionScreen)
            assert config.REASONING_EFFORT != "low"
            assert panel._items[start].value == "low"

    _run(_run_case())


def test_command_menu_click_activates(monkeypatch):
    """鼠标点击菜单项：普通命令填入输入框（与 Enter 接受同一入口）。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            inp = app.query_one(ChatInput)
            inp.focus()
            inp.insert("/he")
            await pilot.pause()
            await pilot.click(app.query_one(CommandMenuItem))
            await pilot.pause()
            assert inp.text == "/help "
            assert not app.query_one(CommandMenu).open

    _run(_run_case())


def test_command_menu_scroll_when_overflow(monkeypatch):
    """候选超过固定展示行数：高度封顶 + ↑↓ 到可视区外自动滚动。"""
    no_prompting(monkeypatch)
    from smithcode.commands import base
    from smithcode.commands.base import CommandResult

    extras = []
    for i in range(5):  # 8 内置 + 5 临时 = 13 > 8，触发滚动
        name = f"scrolltest{i}"
        extras.append(name)
        base.register(name, "测试滚动")(lambda ctx: CommandResult())
    try:

        async def _run_case():
            app = SmithTUI(_make_agent(monkeypatch))
            async with app.run_test() as pilot:
                inp = app.query_one(ChatInput)
                menu = app.query_one(CommandMenu)
                inp.focus()
                inp.insert("/")
                await pilot.pause()
                assert len(menu._candidates) >= 13
                assert menu.size.height == MENU_VISIBLE_ITEMS  # 高度封顶
                assert menu.scrollbar_size_vertical == 0  # 不绘制滚动条（滚动功能不受影响）
                menu.move(-1)  # ↑ 回绕到最后一项（可视区外）
                await pilot.pause()
                assert menu.scroll_offset.y > 0  # 自动滚动
                assert menu.scroll_offset.y == menu.max_scroll_y  # 末项贴底
                menu.move(1)  # ↓ 回到第一项，滚回顶部
                await pilot.pause()
                assert menu.scroll_offset.y == 0

        _run(_run_case())
    finally:
        for name in extras:
            base.COMMANDS.pop(name, None)


def test_command_menu_escape_closes_and_arrows_move(monkeypatch):
    no_prompting(monkeypatch)

    async def _run_case():
        from smithcode import commands

        expected = [cmd.name for cmd in commands.complete_commands("")]
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            inp = app.query_one(ChatInput)
            menu = app.query_one(CommandMenu)
            inp.focus()
            inp.insert("/")
            await pilot.pause()
            await pilot.press("up")
            await pilot.pause()
            assert menu.accept() == expected[-1]  # up 从首项回绕到最后一项
            await pilot.press("down")
            await pilot.pause()
            assert menu.accept() == expected[0]  # down 回到首项（回绕）
            await pilot.press("escape")
            await pilot.pause()
            assert not menu.open
            assert inp.text == "/"  # 输入内容保持不变

    _run(_run_case())


# ---------- 通用选择面板（居中弹窗） ----------

def test_selection_panel_rerenders_once_per_move(monkeypatch):
    """大列表逐键移动只触发一次行区刷新，且行区无逐行子控件。

    回归背景：早先每行是一个子控件，滚动时「行刷新 + 滚动」分属两次刷新周期，
    每键要连续两次把内容写往终端，两次之间的中间态被渲染出来 → 逐键闪烁
    （600 项实测 2 次/键）。自渲染行区（SelectionRows）把两者收进同一次刷新
    （实测 1 次/键），这里锁定「无逐行子控件」与「每键只刷一次」两项。
    """
    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test(size=(120, 40)) as pilot:
            items = [
                SelectionItem(label=f"model-{i}", value=f"m{i}", current=(i == 2))
                for i in range(200)
            ]
            panel = SelectionPanel("选择模型", items, lambda value: None, size="large")
            app.push_screen(SelectionScreen(panel))
            await pilot.pause()
            panel.focus()

            assert panel._selected == 2  # 当前项
            view = panel._rows_view
            # 行区是单个自渲染控件：没有逐行子控件（200 项也只有一个控件）
            assert len(view.children) == 0
            assert "›" in view.line_text(panel._row_of_item[2])

            # 记录行区 refresh 次数：每键移动只应刷新一次
            calls = []
            orig_refresh = type(view).refresh

            def spy(self, *args, **kwargs):
                if self is view:
                    calls.append(1)
                return orig_refresh(self, *args, **kwargs)

            type(view).refresh = spy
            try:
                await pilot.press("down")
                await pilot.pause()
            finally:
                type(view).refresh = orig_refresh

            assert panel._selected == 3
            assert len(calls) == 1  # 每键一次刷新，不再有第二次写入

            # 旧行还原、新行高亮
            assert "›" not in view.line_text(panel._row_of_item[2])
            assert "(当前)" in view.line_text(panel._row_of_item[2])
            assert "›" in view.line_text(panel._row_of_item[3])

    _run(_run_case())


def test_selection_panel_scroll_renders_without_extra_pass(monkeypatch):
    """跨屏滚动时每键仍只渲染一次（自渲染行区的核心收益）。

    隔离滚动因素：先滚到列表中部（此时每次移动都会改变 scroll_offset），
    再统计移动若干次产生的渲染次数——应等于移动次数，而非其两倍。
    """
    from textual._compositor import Compositor

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test(size=(120, 40)) as pilot:
            items = [
                SelectionItem(label=f"model-{i}", value=f"m{i}", current=(i == 0))
                for i in range(300)
            ]
            panel = SelectionPanel("选择模型", items, lambda value: None, size="large")
            app.push_screen(SelectionScreen(panel))
            await pilot.pause()
            panel.focus()
            for _ in range(40):  # 滚到中部，确保后续每次移动都跨屏
                await pilot.press("down")
            await pilot.pause()
            assert panel._rows_view.scroll_offset.y > 0

            renders = []
            orig = Compositor.render_update

            def spy(self, *args, **kwargs):
                renders.append(1)
                return orig(self, *args, **kwargs)

            Compositor.render_update = spy
            try:
                for _ in range(10):
                    await pilot.press("down")
                    await pilot.pause()
            finally:
                Compositor.render_update = orig

            # 每次移动至多一次渲染（不再出现「行刷新 + 滚动」的两次写入）
            assert len(renders) <= 10, f"每键渲染次数过多: {len(renders)} 次/10 键"

    _run(_run_case())


def test_selection_panel_rows_share_panel_background(monkeypatch):
    """行区每个单元格都带底色（含表头 / 间隔行与行计划之外的留白）。

    回归背景：①自渲染行区若把非选中行的空白段留成「无背景」，这部分会透出
    半透明遮罩与下层界面，看起来就是选择框中间的颜色与外部不一致（旧结构
    里每行是子控件、背景自动继承面板，不存在该问题）；②列表短于可视区时，
    行计划之外的留白曾以 style=None 的 Segment 产出——那是 NO_COLOR 下崩溃的
    源头（见 test_selection_panel_renders_with_no_color）。
    """
    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test(size=(120, 40)) as pilot:
            items = [
                SelectionItem(label="a", value="a", category="组"),
                SelectionItem(label="", value="", separator=True),
                SelectionItem(label="b", value="b", trailing="12:00"),
            ]
            panel = SelectionPanel("标题", items, lambda value: None, size="medium")
            app.push_screen(SelectionScreen(panel))
            await pilot.pause()

            view = panel._rows_view
            panel_bg = panel.rich_style.bgcolor
            assert panel_bg is not None  # 面板有底色可继承
            assert view.size.height > len(panel._rows)  # 存在行计划之外的留白

            # 视口每一行的每个单元格都必须有背景（表头行 / 间隔行 / 越界留白一并覆盖）
            for y in range(view.size.height):
                strip = view.render_line(y)
                assert strip._segments, f"视口第 {y} 行没有段"
                for seg in strip._segments:
                    assert seg.style is not None and seg.style.bgcolor is not None, (
                        f"视口第 {y} 行有未着色的段，会透出下层背景或让 NO_COLOR 滤镜崩溃"
                    )
                row_index = int(view.scroll_offset.y) + y
                if row_index != panel._row_of_item[0]:
                    assert strip._segments[0].style.bgcolor == panel_bg

            # 选中行整行反白，非选中行用面板底色
            selected_strip = view.row_strip(panel._row_of_item[0])
            assert (
                selected_strip._segments[0].style.bgcolor.get_truecolor()
                == ColorTriplet(250, 178, 131)  # #fab283
            )

    _run(_run_case())


def test_selection_panel_keeps_trailing_when_label_overflows(monkeypatch):
    """label 超长时截断 label、保住 trailing（时间 / 状态列不被挤掉）。

    回归背景：自渲染行区若先按 label 铺满整行，`Strip.adjust_cell_length` 会从
    行尾把 trailing 整段裁掉——而 /sessions 的时间、/mcp 的状态正是靠 trailing
    常驻，旧结构（label `1fr` / trailing `auto`）始终给它留位。
    """
    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test(size=(120, 40)) as pilot:
            items = [
                SelectionItem(label="长标题" * 40, value="a", trailing="12:00"),
                SelectionItem(label="短的", value="b", trailing="12:00"),
            ]
            panel = SelectionPanel("标题", items, lambda value: None, size="medium")
            app.push_screen(SelectionScreen(panel))
            await pilot.pause()

            view = panel._rows_view
            overflow = view.row_strip(panel._row_of_item[0])
            assert overflow.cell_length == view.scrollable_content_region.width
            assert overflow.text.endswith("12:00")  # trailing 常驻行尾
            assert "…" in overflow.text             # 超长 label 以省略号收尾

            short = view.row_strip(panel._row_of_item[1])
            assert short.text.endswith("12:00")
            assert "…" not in short.text

    _run(_run_case())


def test_selection_panel_renders_with_no_color(monkeypatch):
    """NO_COLOR 下行区渲染不崩：行计划之外的留白也必须带样式。

    回归背景：Textual 在 NO_COLOR + 默认主题（ansi=False）下挂 Monochrome 滤镜，
    它直接对每个 Segment 的 style 解引用（`style.color`）——行区越界留白曾以
    style=None 的段产出，于是只要列表短于可视区（min-height 16 下几乎总是），
    一开选择面板就 AttributeError、整个 TUI 崩掉。
    """
    monkeypatch.setenv("NO_COLOR", "1")
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        assert app.no_color  # 环境确实让 Textual 进了无色模式
        async with app.run_test(size=(120, 40)) as pilot:
            items = [SelectionItem(label="a", value="a")]
            panel = SelectionPanel("标题", items, lambda value: None, size="medium")
            app.push_screen(SelectionScreen(panel))
            await pilot.pause()  # 首次渲染即经过 Monochrome 滤镜

            view = panel._rows_view
            assert view.size.height > len(panel._rows)  # 确认存在越界留白
            for y in range(view.size.height):
                strip = view.render_line(y)
                assert all(seg.style is not None for seg in strip._segments)

            await pilot.press("escape")  # 面板仍可正常交互（没有在渲染里崩掉）
            await pilot.pause()

    _run(_run_case())


def test_selection_panel_size_tiers_and_fallback():
    """size 档位落到 CSS 类（宽度值在 tui/app.tcss）；未知档位回退默认 medium。"""

    def make(size=None):
        kwargs = {} if size is None else {"size": size}
        return SelectionPanel("标题", [], lambda value: None, **kwargs)

    assert make().has_class("size-medium")        # 调用方不指定 → medium
    assert make("small").has_class("size-small")
    assert make("large").has_class("size-large")
    assert make("xlarge").has_class("size-xlarge")
    assert make("huge").has_class("size-medium")  # 未知档位回退，不撑坏面板


def test_selection_panel_centered_selects_and_redispatch(monkeypatch):
    """命令返回 select：弹出遮罩居中面板，选中后按 /命令 值 重新分发。"""
    no_prompting(monkeypatch)
    from smithcode.commands import base
    from smithcode.commands.base import CommandChoice, CommandResult, CommandSelect

    def handler(ctx):
        if ctx.args:
            return CommandResult(text=f"选中 {ctx.args[0]}")
        return CommandResult(
            select=CommandSelect(
                title="测试选择",
                command="picktest",
                items=[
                    CommandChoice("甲", "a", current=True),
                    CommandChoice("乙", "b"),
                ],
            )
        )

    base.register("picktest", "测试选择", accepts_args=True)(handler)
    try:

        async def _run_case():
            app = SmithTUI(_make_agent(monkeypatch))
            async with app.run_test() as pilot:
                inp = app.query_one(ChatInput)
                inp.focus()
                inp.insert("/picktest")
                await pilot.press("enter")
                await pilot.pause()
                assert isinstance(app.screen, SelectionScreen)  # 弹窗已挂载
                assert app.screen.query("SelectionPanel")
                # 半透明背景：底层界面变暗而非全黑（a=0 透明 / a=1 不透明）
                assert 0 < app.screen.styles.background.a < 1
                await pilot.press("down")  # 当前项「甲」→「乙」
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                assert not isinstance(app.screen, SelectionScreen)  # 已关闭
                assert "选中 b" in _chat_text(app)

        _run(_run_case())
    finally:
        base.COMMANDS.pop("picktest", None)


def test_selection_panel_escape_cancels(monkeypatch):
    """Esc 取消：不产生副作用、遮罩关闭。"""
    no_prompting(monkeypatch)
    from smithcode.commands import base
    from smithcode.commands.base import CommandChoice, CommandResult, CommandSelect

    def handler(ctx):
        if ctx.args:
            return CommandResult(text=f"选中 {ctx.args[0]}")
        return CommandResult(
            select=CommandSelect(
                title="测试选择",
                command="pickcancel",
                items=[CommandChoice("甲", "a", current=True)],
            )
        )

    base.register("pickcancel", "测试取消", accepts_args=True)(handler)
    try:

        async def _run_case():
            app = SmithTUI(_make_agent(monkeypatch))
            async with app.run_test() as pilot:
                inp = app.query_one(ChatInput)
                inp.focus()
                inp.insert("/pickcancel")
                await pilot.press("enter")
                await pilot.pause()
                assert isinstance(app.screen, SelectionScreen)
                await pilot.press("escape")
                await pilot.pause()
                assert not isinstance(app.screen, SelectionScreen)
                assert "选中" not in _chat_text(app)

        _run(_run_case())
    finally:
        base.COMMANDS.pop("pickcancel", None)


def test_model_command_picker_switches_model(monkeypatch):
    """真实 /model 无参：弹窗选择后切换 config.MODEL 并刷新底栏。"""
    no_prompting(monkeypatch)
    from types import SimpleNamespace

    monkeypatch.setattr(config, "MODEL", "a")

    async def _run_case():
        agent = _make_agent(monkeypatch)
        agent.models = SimpleNamespace(list=lambda: ["a", "b"])
        app = SmithTUI(agent)
        async with app.run_test() as pilot:
            inp = app.query_one(ChatInput)
            inp.focus()
            inp.insert("/model")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, SelectionScreen)
            await pilot.press("down")  # 当前项 a → b
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert config.MODEL == "b"
            assert not isinstance(app.screen, SelectionScreen)

    _run(_run_case())


# ---------- 上下文收集汇总（「已探索」分组） ----------


def test_context_category_mapping():
    assert context_category("read_file") == "read"
    assert context_category("list_dir") == "read"  # 列目录归入「读取」
    assert context_category("glob") == "search"
    assert context_category("grep") == "search"
    assert context_category("write_file") is None
    assert context_category("run_command") is None


def test_context_summary_only_nonzero_categories():
    assert context_summary({"read": 3, "search": 0}) == "3 次读取"
    assert context_summary({"read": 2, "search": 1}) == "2 次读取，1 次搜索"
    assert context_summary({"read": 0, "search": 0}) == ""


def _start_tools(app, specs):
    for tool_id, summary, name in specs:
        app.ui_tool_start(tool_id, summary, "inline", name)


def test_context_tools_grouped_with_counts(monkeypatch):
    """连续的读取/搜索工具汇总为一个「已探索」块，头行按类计数（ls 计入读取）。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            _start_tools(app, [
                (1, "read a.py", "read_file"),
                (2, "grep foo", "grep"),
                (3, "ls src", "list_dir"),
                (4, "glob **/*.py", "glob"),
            ])
            for tool_id in (1, 2, 3, 4):
                app.ui_tool_result(tool_id, "结果", False, False)
            await pilot.pause()
            groups = app.query(ContextGroup)
            assert len(groups) == 1
            assert groups.first().styles.margin.top == 1  # 与上方内容留出间隔
            # 整组结算：全部出结果但还没封口（还可能有新的同组工具加入），
            # 头行保持「探索中」，不闪变成「已探索」
            header = str(groups.first().query_one(".group-header").content)
            assert "探索中" in header
            # 非上下文工具到来 → 封口，已收齐 → 「已探索」
            _start_tools(app, [(5, "command ls", "run_command")])
            await pilot.pause()
            assert len(app.query(ContextGroup)) == 1  # 命令不新建组
            header = str(groups.first().query_one(".group-header").content)
            assert "已探索" in header
            assert "探索中" not in header
            assert "2 次读取" in header  # read_file + list_dir
            assert "2 次搜索" in header  # grep + glob
            assert "列目录" not in header
            assert len(groups.first().query(ToolCall)) == 4

    _run(_run_case())


def test_context_group_pending_then_done_wording(monkeypatch):
    """「已探索」汇总行两态文案：整组结算——封口且全部出结果后才「已探索」。

    未封口前即使全部出结果仍保持「探索中」（还可能有新的同组工具加入，
    中间空档不闪变）；非上下文工具到来封口后，已收齐才切「已探索」。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            _start_tools(app, [
                (1, "read a.py", "read_file"),
                (2, "grep foo", "grep"),
            ])
            await pilot.pause()
            group = app.query_one(ContextGroup)
            pending_header = str(group.query_one(".group-header").content)
            assert "探索中" in pending_header
            assert "正在探索" not in pending_header
            for tool_id in (1, 2):
                app.ui_tool_result(tool_id, "结果", False, False)
            await pilot.pause()
            # 全部出结果、但还没封口：仍是「探索中」，不闪变成「已探索」
            gap_header = str(group.query_one(".group-header").content)
            assert "探索中" in gap_header
            assert group._spin_timer is not None  # 转轮继续转
            # 非上下文工具到来 → 封口，已收齐 → 「已探索」
            _start_tools(app, [(3, "command ls", "run_command")])
            await pilot.pause()
            done_header = str(group.query_one(".group-header").content)
            assert "已探索" in done_header
            assert "探索中" not in done_header

    _run(_run_case())


def test_context_group_gap_between_tools_stays_exploring(monkeypatch):
    """同组工具的时间空档不闪变：第一个收工、第二个还没开始时仍「探索中」。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            _start_tools(app, [(1, "read a.py", "read_file")])
            await pilot.pause()
            group = app.query_one(ContextGroup)
            app.ui_tool_result(1, "结果", False, False)
            await pilot.pause()
            # 单个工具收工、组还没封口：保持「探索中」
            assert "探索中" in str(group.query_one(".group-header").content)
            assert group._spin_timer is not None
            # 后续同组工具加入同一组（不另起）
            _start_tools(app, [(2, "grep foo", "grep")])
            await pilot.pause()
            assert len(app.query(ContextGroup)) == 1
            assert "探索中" in str(group.query_one(".group-header").content)
            app.ui_tool_result(2, "结果", False, False)
            _start_tools(app, [(3, "command ls", "run_command")])  # 封口
            await pilot.pause()
            assert "已探索" in str(group.query_one(".group-header").content)
            assert group._spin_timer is None

    _run(_run_case())


def test_context_group_keeps_exploring_until_children_finish(monkeypatch):
    """封口 + 收齐才算完：混批里非上下文工具封口后，仍在跑的子工具仍「探索中」，
    收齐后才切「已探索」（整组结算）。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            _start_tools(app, [(1, "read a.py", "read_file")])
            await pilot.pause()
            group = app.query_one(ContextGroup)
            assert "探索中" in str(group.query_one(".group-header").content)

            # 同批的命令到来 → 汇总组封口（后续只读工具另起一组）
            _start_tools(app, [(2, "command ls", "run_command")])
            await pilot.pause()
            assert len(app.query(ContextGroup)) == 1  # 命令不新建组
            header = str(group.query_one(".group-header").content)
            assert "探索中" in header  # 读取还没出结果，仍算探索中
            assert group._spin_timer is not None  # 转轮继续转

            app.ui_tool_result(1, "结果", False, False)
            await pilot.pause()
            header = str(group.query_one(".group-header").content)
            assert "已探索" in header
            assert group._spin_timer is None

    _run(_run_case())


def test_context_group_drained_at_turn_end(monkeypatch):
    """轮次结束兜底：没等到结果的子工具被收尾，转轮停下、文案定格「已探索」。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            _start_tools(app, [(1, "read a.py", "read_file")])
            await pilot.pause()
            group = app.query_one(ContextGroup)
            assert group._spin_timer is not None

            app._turn_start = time.monotonic()  # ui_turn_end 以轮次起点为前置条件
            app.ui_turn_end("ok")  # 结果始终没来
            await pilot.pause()
            header = str(group.query_one(".group-header").content)
            assert "已探索" in header
            assert group._spin_timer is None

    _run(_run_case())


def test_context_group_spinner_repaints_third_cell(monkeypatch):
    """「已探索」头的转轮在第 3 列（「▸ 」之后）：tick 只重绘那一格。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            _start_tools(app, [(1, "read a.py", "read_file")])
            await pilot.pause()
            group = app.query_one(ContextGroup)
            header = group.query_one(".group-header")
            before = str(header.content)
            group._spin()
            after = str(header.content)
            assert after != before
            assert after[:2] == before[:2] and after[3:] == before[3:]  # 只有第 3 列变化
            assert header._repaint_regions == {Region(2, 0, 1, 1)}

    _run(_run_case())


def test_context_group_children_live_in_body_and_collapse(monkeypatch):
    """明细必须挂在 .group-body 里（标题之下），且折叠真的能收起明细。

    回归：缓冲列表曾与 Textual `Widget._pending_children` 同名串台——子控件被
    排到标题之前，`on_mount` 补挂时列表已被 `_compose` 清空，导致 `.group-body`
    恒为空、明细常驻可见、Enter / 点击折叠完全失效。
    """
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            _start_tools(app, [
                (1, "read a.py", "read_file"),
                (2, "ls src", "list_dir"),
            ])
            await pilot.pause()
            group = app.query_one(ContextGroup)
            header = group.query_one(".group-header")
            body = group.query_one(".group-body")
            tools = list(group.query(ToolCall))
            assert len(tools) == 2
            assert list(body.children) == tools  # 明细归位在正文容器内
            assert not body.display  # 默认收起

            group.action_toggle()
            await pilot.pause()
            assert body.display  # 展开可见
            assert header.region.y < tools[0].region.y  # 标题在明细之上
            assert tools[0].region.height > 0

            group.action_toggle()
            await pilot.pause()
            assert not body.display  # 再收起
            assert not tools[0].region  # 收起后明细不再占位

    _run(_run_case())


def test_non_context_tool_breaks_group(monkeypatch):
    """非上下文工具（如命令执行）把上下文组切断：前后各自成组。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            _start_tools(app, [
                (1, "read a.py", "read_file"),
                (2, "command ls", "run_command"),
                (3, "read b.py", "read_file"),
            ])
            await pilot.pause()
            assert len(app.query(ContextGroup)) == 2
            top_tools = [w for w in app.query_one(ChatView).children if isinstance(w, ToolCall)]
            assert len(top_tools) == 1  # run_command 独立成块，不归组
            assert "command ls" in str(top_tools[0].query_one(".tool-header").content)

    _run(_run_case())


def test_context_group_tolerates_interleaved_results(monkeypatch):
    """流式交错：上下文工具的结果晚于后续非上下文工具的 start 到达，分组仍正确。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            app.ui_tool_start(1, "read a.py", "inline", "read_file")
            app.ui_tool_start(2, "command ls", "block", "run_command")
            app.ui_tool_result(1, "结果", False, False)  # read1 的结果晚到（越过 cmd 的 start）
            app.ui_tool_start(3, "read b.py", "inline", "read_file")
            app.ui_tool_result(2, "out", False, False)
            app.ui_tool_result(3, "结果", False, False)
            await pilot.pause()
            groups = app.query(ContextGroup)
            assert len(groups) == 2  # read1 / read2 被 run_command 切开，各自成组
            assert len(groups[0].query(ToolCall)) == 1
            assert len(groups[1].query(ToolCall)) == 1

    _run(_run_case())


def test_context_group_expand_shows_children(monkeypatch):
    """汇总块默认收起，展开后可见逐条明细。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            _start_tools(app, [
                (1, "read a.py", "read_file"),
                (2, "read b.py", "read_file"),
            ])
            app.ui_tool_result(1, "内容A", False, False)
            app.ui_tool_result(2, "内容B", False, False)
            await pilot.pause()
            group = app.query(ContextGroup).first()
            body = group.query_one(".group-body")
            assert body.display is False
            group.action_toggle()
            await pilot.pause()
            assert body.display is True
            assert len(group.query(ToolCall)) == 2
            # 展开后按内容自适应，不得因 Vertical 默认 1fr 而撑满可用高度
            assert group.size.height <= 8

    _run(_run_case())


def test_stream_breaks_context_group(monkeypatch):
    """助手正文（可见输出）出现时上下文组封口，之后的工具另起一组。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            _start_tools(app, [(1, "read a.py", "read_file")])
            app.ui_tool_result(1, "内容", False, False)
            app.ui_stream("content", "下面解释")
            _start_tools(app, [(2, "read b.py", "read_file")])
            await pilot.pause()
            assert len(app.query(ContextGroup)) == 2

    _run(_run_case())


def test_top_level_blocks_uniform_top_margin(monkeypatch):
    """统一间距：顶层块靠 margin-top 与前一块隔一行、不用 margin-bottom（避免双倍间隔），
    组内工具行紧凑。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            app.ui_stream("content", "正文")
            app.ui_stream_done()
            app.ui_thinking_start()
            app.ui_thinking_tick("想")
            app.ui_thinking_done()
            _start_tools(app, [(1, "read a.py", "read_file")])
            app.ui_tool_result(1, "内容", False, False)
            _start_tools(app, [(2, "command ls", "run_command")])
            app.ui_tool_result(2, "ok", False, False)
            await pilot.pause()

            stream = app.query_one(".assistant-stream")
            assert (stream.styles.margin.top, stream.styles.margin.bottom) == (1, 0)
            thinking = app.query(ThinkingBlock).first()
            assert (thinking.styles.margin.top, thinking.styles.margin.bottom) == (1, 0)
            group = app.query(ContextGroup).first()
            assert (group.styles.margin.top, group.styles.margin.bottom) == (1, 0)
            assert group.query(ToolCall).first().styles.margin.top == 0  # 组内紧凑
            top_tool = next(
                w for w in app.query_one(ChatView).children if isinstance(w, ToolCall)
            )
            assert (top_tool.styles.margin.top, top_tool.styles.margin.bottom) == (1, 0)

    _run(_run_case())


def test_selection_panel_scrolls_to_selected(monkeypatch):
    """选项超出可视区出现滚动条，↑↓ 移动时选中项自动滚进视野。"""
    no_prompting(monkeypatch)
    from types import SimpleNamespace

    monkeypatch.setattr(config, "MODEL", "model-00")

    async def _run_case():
        agent = _make_agent(monkeypatch)
        agent.models = SimpleNamespace(
            list=lambda: [f"model-{i:02d}" for i in range(40)]
        )
        app = SmithTUI(agent)
        async with app.run_test() as pilot:
            inp = app.query_one(ChatInput)
            inp.focus()
            inp.insert("/model")
            await pilot.press("enter")
            await pilot.pause()
            view = app.screen.query_one(".selection-rows")
            assert view.max_scroll_y > 0  # 内容超出可视区：可滚动
            for _ in range(39):  # 移到最后一个（可视区之外）
                await pilot.press("down")
            await pilot.pause()
            assert view.scroll_offset.y > 0  # 已自动滚到选中项

    _run(_run_case())


def test_input_has_no_scrollbar(monkeypatch):
    """输入框不绘制滚动条（与聊天区 / 命令菜单 / 选择面板一致）。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            inp = app.query_one(ChatInput)
            inp.focus()
            inp.text = "\n".join(f"行{i}" for i in range(30))  # 远超 max-height
            await pilot.pause()
            assert inp.scrollbar_size_vertical == 0  # 不绘制
            assert inp.max_scroll_y > 0  # 但滚动功能不受影响

    _run(_run_case())


def test_input_grows_with_lines_and_caps(monkeypatch):
    """输入框随内容行数向上长高，封顶 max-height 后不再变高。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test(size=(140, 32)) as pilot:
            inp = app.query_one(ChatInput)
            inp.focus()
            await pilot.pause()
            baseline = inp.region.height

            inp.text = "一行"
            await pilot.pause()
            await pilot.pause()
            assert inp.region.height == baseline  # 单行：保持原高度（min-height）

            inp.text = "\n".join(f"行{i}" for i in range(6))
            await pilot.pause()
            await pilot.pause()
            assert inp.region.height > baseline  # 多行：变高

            inp.text = "\n".join(f"行{i}" for i in range(13))
            await pilot.pause()
            await pilot.pause()
            capped = inp.region.height
            assert capped == 13  # 到 max-height

            inp.text = "\n".join(f"行{i}" for i in range(20))
            await pilot.pause()
            await pilot.pause()
            assert inp.region.height == capped  # 封顶后不再变高

            inp.text = ""
            await pilot.pause()
            await pilot.pause()
            assert inp.region.height == baseline  # 清空回落

    _run(_run_case())


def test_command_menu_anchors_above_input_at_any_height(monkeypatch):
    """输入框长高后命令菜单仍贴住输入区顶部（锚点随实时几何重算）。"""
    no_prompting(monkeypatch)

    async def _run_case():
        from smithcode import commands

        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test(size=(140, 32)) as pilot:
            inp = app.query_one(ChatInput)
            menu = app.query_one(CommandMenu)
            wrap = app.query_one("#input-wrap")
            inp.focus()
            await pilot.pause()

            for rows, running in ((1, False), (5, False), (10, False), (5, True)):
                inp.text = "\n" * (rows - 1)
                app.query_one("#running").display = running
                await pilot.pause()
                menu.show_candidates(commands.complete_commands(""))
                await pilot.pause()
                app.anchor_command_menu()
                await pilot.pause()
                assert menu.region.bottom == wrap.region.y  # 贴住输入区顶部

    _run(_run_case())


def test_input_keeps_bottom_padding_when_multiline(monkeypatch):
    """换行后底部留白不消失：输入框上下 padding 各占 1 行，不靠 min-height 撑。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test(size=(140, 34)) as pilot:
            inp = app.query_one(ChatInput)
            inp.focus()
            await pilot.pause()

            for rows in (1, 2, 3, 5):
                inp.text = "\n".join(f"L{i}" for i in range(rows))
                await pilot.pause()
                await pilot.pause()
                # 内容区高度 == 文本行数，说明多出的行是 padding 而非空内容行
                assert inp.content_size.height == rows
                # 盒高 = 内容 + 上下 padding(各 1)
                assert inp.region.height == rows + 2

    _run(_run_case())


def test_shift_enter_inserts_newline_and_enter_sends(monkeypatch):
    """Shift+Enter 插入换行不发送，Enter 发送且多行内容完整送达。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            inp = app.query_one(ChatInput)
            inp.focus()
            inp.insert("第一行")
            await pilot.press("shift+enter")
            await pilot.pause()
            inp.insert("第二行")
            await pilot.pause()
            assert inp.text == "第一行\n第二行"  # 换行已插入、未发送
            assert app._busy is False

            await pilot.press("enter")
            for _ in range(200):
                if not app._busy:
                    break
                await pilot.pause(0.02)
            await pilot.pause()
            assert inp.text == ""  # 已发送并清空
            text = _chat_text(app)
            assert "第一行" in text and "第二行" in text  # 两行都进了对话区

    _run(_run_case())


def test_input_placeholder_hints_newline_keys(monkeypatch):
    """空输入时占位提示给出换行键（含不支持 kitty 协议终端的 Ctrl+J 兜底）。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            inp = app.query_one(ChatInput)
            assert "Shift+Enter" in inp.placeholder
            assert "Ctrl+J" in inp.placeholder  # 终端不支持时的可靠替代
            inp.focus()
            inp.insert("x")
            await pilot.pause()
            assert inp.text == "x"  # placeholder 只是提示，不干扰输入

    _run(_run_case())


# ---------- 对话区滚动跟随（贴底意图 + 布局期贴底） ----------


def _fill_chat(app, lines: int = 40) -> None:
    """把对话区填到超出视口，供滚动跟随用例使用。"""
    for i in range(lines):
        app.ui_notice(f"填充行 {i}")


def _bottom_reached(chat) -> bool:
    """贴底判定：滚动位置到顶，且**排版结果**同步（末块底边贴在内容区底边）。

    只断言 scroll_y 会漏掉"偏移改了但画面按旧位置重排"的情况。"""
    return (
        chat.scroll_y == chat.max_scroll_y
        and chat.max_scroll_y > 0
        and abs(chat.children[-1].region.bottom - chat.content_region.bottom) <= 1
    )


def test_chat_never_scrolls_above_top_during_first_turn(monkeypatch):
    """第一轮（用户消息 + 流式正文）全程不得出现负偏移。

    回归：曾用 Textual 原生锚定（`anchor()`）实现贴底，而锚定的贴底位置按
    「内容底 - 容器高」算、内容不足一屏时为负，又经不校验的 set_reactive 写入，
    于是第一轮一有输出整块内容就被推到视口下方（欢迎 Logo 落到底部再向上滚）。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test(size=(100, 30)) as pilot:
            chat = app.query_one(ChatView)
            await pilot.pause()
            assert chat.scroll_y == 0

            app.ui_notice("你：看看这个问题")
            await pilot.pause()
            assert chat.scroll_y == 0  # 不足一屏：内容从顶部排，不产生负偏移
            assert chat.query(".welcome").first().region.y == chat.content_region.y

            for i in range(10):
                app.ui_stream("content", f"第 {i} 段回答正文，写一些内容。\n\n")
                await pilot.pause()
                assert chat.scroll_y >= 0  # 全程不得为负
                if chat.max_scroll_y > 0:  # 长过一屏后必须贴底
                    assert _bottom_reached(chat)

            app.ui_stream_done()
            await pilot.pause()
            assert _bottom_reached(chat)

    _run(_run_case())


def test_chat_follows_content_growth_when_at_bottom(monkeypatch):
    """贴底时任何内容增长都保持贴底：普通块 / 流式正文（含节流窗口内的 chunk）/
    工具结果展开 / 变更预览展开 / 聊天区变矮（输入框增高）。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test(size=(80, 20)) as pilot:
            chat = app.query_one(ChatView)
            _fill_chat(app)
            await pilot.pause()
            assert _bottom_reached(chat)

            app.ui_notice("普通通知")
            await pilot.pause()
            assert _bottom_reached(chat)

            # 两个 chunk 落在同一节流窗口（MessageBody 每帧至多重排一次）：
            # 第二次追加不触发布局，贴底必须由布局期重算兜住
            app.ui_stream("content", "第一段正文。\n\n")
            app.ui_stream("content", "第二段正文，再多写一些内容让它长高。\n\n")
            await pilot.pause()
            assert _bottom_reached(chat)

            # 非上下文工具（独立块）：结果展开让 body 由隐藏转可见
            app.ui_tool_start(1, "write b.py", "block", "write_file")
            await pilot.pause()
            app.ui_tool_result(1, "\n".join(f"结果行 {i}" for i in range(12)), True, False)
            await pilot.pause()
            assert _bottom_reached(chat)

            # pending 期收到变更预览：diff 就地展开
            app.ui_tool_preview(
                1, "--- a/b.py\n+++ b/b.py\n@@ -1,1 +1,1 @@\n-旧\n"
                   + "\n".join(f"+新增行 {i}" for i in range(10))
            )
            await pilot.pause()
            assert _bottom_reached(chat)

            # 聊天区变矮：输入框增高会让容器高度变化，贴底位置必须重算
            app.query_one(ChatInput).text = "\n".join(f"第 {i} 行" for i in range(5))
            await pilot.pause()
            assert _bottom_reached(chat)
            app.ui_notice("容器变矮后的新内容")
            await pilot.pause()
            assert _bottom_reached(chat)

    _run(_run_case())


def test_welcome_stays_at_top_when_content_shorter_than_view(monkeypatch):
    """内容不足一屏时贴底即顶部（不得出现负偏移把内容推到底部），清屏（/new）
    缩回一屏内同样要复位。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test(size=(100, 30)) as pilot:
            chat = app.query_one(ChatView)
            await pilot.pause()
            assert chat.scroll_y == 0
            assert chat.query(".welcome").first().region.y == chat.content_region.y

            _fill_chat(app)
            await pilot.pause()
            assert _bottom_reached(chat)

            chat.reset()
            app._show_welcome()  # /new 路径：清屏 + 重挂欢迎横幅
            await pilot.pause()
            assert chat.scroll_y == 0
            assert chat.query(".welcome").first().region.y == chat.content_region.y

    _run(_run_case())


def test_chat_follows_stream_starting_from_short_content(monkeypatch):
    """对话区还没满一屏时流式输出：跨过一屏那一刻必须自动接上贴底跟随。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test(size=(100, 30)) as pilot:
            chat = app.query_one(ChatView)
            chat.reset()
            await pilot.pause()
            assert chat.max_scroll_y == 0  # 空对话区：无从滚动
            for i in range(12):
                # 每轮两个 chunk 落在同一节流窗口，逼近"不足一屏 → 超过一屏"的边界
                app.ui_stream("content", f"第 {i} 段正文，继续写一些内容。\n\n")
                app.ui_stream("content", f"第 {i} 段补充（同帧第二块）。\n\n")
                await pilot.pause()
            assert chat.max_scroll_y > 0  # 已长过一屏
            assert _bottom_reached(chat)

    _run(_run_case())


def test_chat_keeps_position_while_reading_history(monkeypatch):
    """翻历史（离底）时新内容不移动视口；滚回底部后恢复跟随。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test(size=(80, 20)) as pilot:
            chat = app.query_one(ChatView)
            _fill_chat(app)
            await pilot.pause()

            for _ in range(4):  # 逐格上滚（每格一帧，同真实滚轮）
                chat.scroll_up(animate=False)
                await pilot.pause()
            assert not chat._at_bottom()
            parked = chat.scroll_offset.y

            app.ui_notice("上滚期间的新内容")
            app.ui_stream("content", "上滚期间的流式正文，写长一点。\n\n")
            await pilot.pause()
            assert chat.scroll_offset.y == parked  # 视口不动

            for _ in range(20):  # 滚回底部
                chat.scroll_down(animate=False)
                await pilot.pause()
            assert chat._at_bottom()
            app.ui_notice("回底后的新内容")
            await pilot.pause()
            assert _bottom_reached(chat)  # 跟随恢复

    _run(_run_case())


def test_user_message_returns_to_bottom_while_reading_history(monkeypatch):
    """翻历史时提交自己的消息：无条件回到最新位置并恢复跟随。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test(size=(80, 20)) as pilot:
            chat = app.query_one(ChatView)
            _fill_chat(app)
            await pilot.pause()

            for _ in range(4):
                chat.scroll_up(animate=False)
                await pilot.pause()
            assert not chat._at_bottom()

            chat.add_user("翻历史时的新提问")
            await pilot.pause()
            assert _bottom_reached(chat)
            app.ui_notice("提问后的新内容")
            await pilot.pause()
            assert _bottom_reached(chat)  # 跟随保持

    _run(_run_case())


# ---------- 流中断：正文块收口，下一轮不黏进上一轮 ----------

class TimeoutLLM:
    """先吐一段正文再断流：模拟读完超时（原始故障的触发条件）。"""

    def __init__(self):
        self.calls = 0

    def chat_stream(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            yield ("content", "已修改完成，")
            raise httpx2.ReadTimeout("The read operation timed out")
        yield ("content", "第二轮正文")


def test_tui_stream_block_closed_after_timeout(monkeypatch):
    """流中断后正文块必须收口：下一轮的增量另起新块，不能黏进上一轮那个块。

    回归的是「ReadTimeout 打断 _chat 的 for 循环 → r.stream_done() 不执行 →
    ChatView._kind 停在 content、_body 仍是旧块 → 第二轮正文追加进同一个块」
    这条链（用户看到两轮内容叠在一起、错位渲染）。
    """
    no_prompting(monkeypatch)
    agent = _make_agent(monkeypatch)
    fake = TimeoutLLM()
    agent.llm = fake  # 同一实例供两轮调用（第一轮断流，第二轮正常）

    def _drive():
        first = asyncio.run(agent.run("第一轮"))
        second = asyncio.run(agent.run("第二轮"))
        return first, second

    async def _run_case():
        app = SmithTUI(agent)
        async with app.run_test() as pilot:
            chat = app.query_one(ChatView)
            first, second = await asyncio.to_thread(_drive)
            await pilot.pause()

            assert first.status == "stream_error"
            assert second.status == "ok"
            blocks = [
                b.content.plain for b in chat.query(".assistant-stream")
                if "已修改完成" in b.content.plain or "第二轮正文" in b.content.plain
            ]
            assert len(blocks) == 2, f"两轮内容应各占一块，实际 {blocks!r}"
            assert "第二轮正文" not in blocks[0]
            assert "已修改完成" not in blocks[1]

    _run(_run_case())


# ---------- 重试进度：落在输入框上方那一行 ----------

def test_running_indicator_shows_retry_progress(monkeypatch):
    """重试态显示为 Working 行后缀，含尝试序号与实时倒计时（opencode 同款位置）。

    owner 用于区分发起方：后台标题的重试结束不得清掉前台任务的进度。
    """
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            running = app.query_one(RunningIndicator)
            running.display = True
            running.start()

            state = RetryState(attempt=2, total=3, reason="读取超时", wait=8.0,
                               next_at=time.monotonic() + 8.0)
            app.ui_retry_start(state, owner="task")
            await pilot.pause()
            text = str(running.render())
            assert "Working" in text
            assert "正在重试 2/3" in text
            assert "s 后" in text

            # 另一个发起方（后台标题）结束：不得清掉前台任务的重试态
            app.ui_retry_end(owner="title")
            await pilot.pause()
            assert "正在重试 2/3" in str(running.render())

            app.ui_retry_end(owner="task")
            await pilot.pause()
            assert "正在重试" not in str(running.render())

    _run(_run_case())


def test_retry_marks_interrupted_stream_block(monkeypatch):
    """重试开始：已上屏的那段正文被标记「已中断」，重试产出另起一块。

    两段都保留（对齐 opencode 的 text part 累积），但必须能看出哪段是残缺的；
    同时历史（Agent 侧）与屏幕一致，见 test_agent 的累积用例。
    """
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            chat = app.query_one(ChatView)
            chat.begin_stream("content")
            chat.append_stream("content", "已修改完成，")
            app.ui_retry_start(
                RetryState(attempt=2, total=3, reason="读取超时", wait=2.0,
                           next_at=time.monotonic() + 2.0),
                owner="task",
            )
            await pilot.pause()
            first = chat.query_one(".assistant-stream")
            assert "已中断" in first.raw
            assert chat._kind is None  # 已收口：重试的增量会另起新块

            chat.append_stream("content", "总结如下：改了 commands/base.py。")
            await pilot.pause()
            blocks = [b.raw for b in chat.query(".assistant-stream")]
            assert len(blocks) == 2
            assert "已中断" in blocks[0] and "已中断" not in blocks[1]

    _run(_run_case())


def test_retry_end_from_other_owner_keeps_state(monkeypatch):
    """settle 事件的 owner 语义：不是自己发起的重试就不动当前显示。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            running = app.query_one(RunningIndicator)
            running.display = True
            running.start()
            state = RetryState(attempt=2, total=3, reason="连接中断", wait=4.0,
                               next_at=time.monotonic() + 4.0)
            app.ui_retry_start(state, owner="A")
            app.ui_retry_end(owner="B")
            await pilot.pause()
            assert "正在重试" in str(running.render())

    _run(_run_case())


def test_stream_error_footer_carries_reason(monkeypatch):
    """页脚「输出中断」要带上失败原因（只报中断不报原因无法排障）。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            app._turn_start = time.monotonic() - 3
            app._turn_reason = "读取超时: The read operation timed out"
            app.ui_turn_end("stream_error")
            await pilot.pause()
            text = _chat_text(app)
            assert "输出中断" in text
            assert "读取超时" in text

    _run(_run_case())


# ---------- 界面收尾：挂起的弹窗等待方必须被放行 ----------
#
# 失败模式是「那一轮永远不结束」（等待方停在 await 上），故这两条用例把等待放进
# `asyncio.wait_for`（回归表现为 5s 超时失败）——现在没有阻塞线程，收尾不会被拖住。


def test_unanswered_panel_exit_releases_waiter(monkeypatch):
    """面板未作答就退出界面：等待方被放行，并按 fail-closed 收口（取消/拒绝）。

    为什么必须放行：等待方是**事件循环上的一个 await**（不再有阻塞线程），界面
    卸载后没人能作答，任其悬挂就意味着这一轮永远不结束、进程回不到收尾路径。
    放行口是 `AskPort.cancel_in_flight`（`SmithTUI.on_unmount` 会调它）。
    """
    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            port = app.agent.asks
            port.bind_loop(asyncio.get_running_loop())
            token = ask_port.activate(port)
            frontend_token = _frontend_mod.activate(app._frontend)
            try:
                task = asyncio.ensure_future(port.ask(
                    AskRequest(kind="permission", title="允许执行 run_command? [y]本次 / [n]拒绝: ",
                               options=("y", "n"), payload={"hint": "y / n"})
                ))
                for _ in range(200):
                    await pilot.pause(0.02)
                    if app.query(PermissionPanel):
                        break
                assert app.query(PermissionPanel), "权限面板应已弹出"
                assert not task.done()  # 确实停在等待上（无人作答）
            finally:
                _frontend_mod.reset(frontend_token)
                ask_port.reset(token)
        # 界面收尾（on_unmount 已跑）之后才断言：靠的就是那里的取消
        answer = await asyncio.wait_for(task, timeout=5)
        assert answer.outcome == "cancelled"  # fail-closed：按拒绝/取消收口

    _run(_run_case())


def test_ask_after_unmount_returns_default_without_blocking(monkeypatch):
    """界面已收尾后到达的询问：直接走 fail-closed 默认值，不挂面板、不阻塞。"""
    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test():
            pass  # 挂载又卸载：界面已收尾
        port = app.agent.asks
        port.bind_loop(asyncio.get_running_loop())
        token = ask_port.activate(port)
        frontend_token = _frontend_mod.activate(app._frontend)
        try:
            denied = await asyncio.wait_for(port.ask(
                AskRequest(kind="permission", title="允许? [y]本次 / [n]拒绝: ",
                           options=("y", "n"))
            ), timeout=5)
            assert not denied.answered  # 拒绝/取消，且立刻返回
            answers = await asyncio.wait_for(port.ask_user_questions(
                [{"question": "用哪个？", "options": ["甲", "乙"]}]
            ), timeout=5)
            assert not answers.answered and not answers.values
        finally:
            _frontend_mod.reset(frontend_token)
            ask_port.reset(token)

    _run(_run_case())

