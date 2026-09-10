"""TUI 测试：界面挂载、消息流式渲染、弹窗确认、工具折叠、计划侧边栏（headless pilot）。"""
import asyncio
import json
import threading
from pathlib import Path

import pytest
from textual.widgets import Static

import smithcode.renderer as renderer_module
from smithcode import __version__, config
from smithcode.agent import Agent
from smithcode.session import Session
from smithcode.tui.app import SmithTUI
from smithcode.tui.bridge import TuiRenderer
from smithcode.tui.panels import SelectionScreen
from smithcode.tui.render import format_duration, git_branch, split_md_blocks
from smithcode.tui.widgets import (
    MENU_VISIBLE_ITEMS,
    ChatInput,
    ChatView,
    CommandMenu,
    CommandMenuItem,
    Sidebar,
    ThinkingBlock,
    ToolCall,
)
from smithcode.utils.terminal import confirmations_available


@pytest.fixture(autouse=True)
def restore_renderer():
    """TuiRenderer.on_mount 会替换全局渲染后端，测试结束还原，避免污染同进程后续测试。"""
    from smithcode import renderer

    backup = renderer._current
    yield
    renderer_module.set_renderer(backup)


def no_prompting(monkeypatch):
    """headless 环境里 stdin 非 TTY，权限确认会 fail-closed，正好不用真弹窗。"""
    monkeypatch.setattr("smithcode.permission.confirmations_available", lambda: False)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", __file__)


class FakeLLM:
    """只回正文的假模型，配合 Agent 线程跑通流式渲染。"""

    def chat_stream(self, messages, tools=None):
        yield ("content", "你好，")
        yield ("content", "世界")
        yield ("message", {"role": "assistant", "content": "你好，世界"})


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


def test_choice_modal_resolves(monkeypatch):
    """权限申请面板：替换输入框，字母键直选，答后恢复输入框。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            app.show_permission_panel(
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
            app.show_permission_panel(
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
            app.show_permission_panel(
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
            plain = str(body.content)
            assert "-old" in plain
            assert "+new" in plain
            # 未配对 id 的预览不崩溃、不挂载孤儿块
            app.ui_tool_preview(None, "+x")
            await pilot.pause()

    _run(_run_case())


def test_question_panel_resolves(monkeypatch):
    """无选项提问：输入框被提问面板替换，输入文本提交后恢复。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            app.show_question_panel("要继续吗?", [], False, result, evt)
            await pilot.pause()
            assert app.query_one("#input-wrap").display is False  # 输入框（含框内状态行）被替换
            await pilot.press("是")
            await pilot.press("enter")
            await pilot.pause()
            assert result.get("value") == "是"
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
    monkeypatch.setattr(config, "SESSION_EXTRA_ROOTS", [])
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
            assert "文件内容" in str(body.content)

    _run(_run_case())


def test_tool_call_shows_diff_detail(monkeypatch):
    """write/edit 的调用详情：diff（改动内容）在前，「已编辑」确认语在后。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            detail = "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new"
            block = ToolCall("edit a.txt", "已编辑 a.txt", expanded=True,
                             display="block", detail=detail)
            app.query_one(ChatView).add_widget(block)
            await pilot.pause()
            plain = str(block.query_one(".tool-body").content)
            assert "-old" in plain
            assert "+new" in plain
            assert "已编辑 a.txt" in plain
            assert "@@ -1 +1 @@" in plain
            assert plain.find("+new") < plain.find("已编辑 a.txt")  # diff 在前

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
            TuiRenderer(app).plan("共 1 步", plan_mod.render_current(color=True))
            await pilot.pause()
            plan_body = str(sidebar.query_one(".plan-body").content)
            assert "读文件" in plan_body
            assert "详情内容" not in plan_body  # 侧边栏只展示标题

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
            TuiRenderer(app).plan("共 1 步", plan_mod.render_current(color=True))
            await pilot.pause()
            assert section.display is False

            # 出现 pending 步骤 → 展示
            plan_mod.current().replace(
                [{"title": "写代码", "status": "completed"}, {"title": "跑测试", "status": "in_progress"}]
            )
            TuiRenderer(app).plan("共 2 步", plan_mod.render_current(color=True))
            await pilot.pause()
            assert section.display is True
            assert "跑测试" in str(sidebar.query_one(".plan-body").content)
            assert "写代码" in str(sidebar.query_one(".plan-body").content)

            # 全部取消 → 再次隐藏
            plan_mod.current().replace(
                [{"title": "写代码", "status": "cancelled"}, {"title": "跑测试", "status": "cancelled"}]
            )
            TuiRenderer(app).plan("共 2 步", plan_mod.render_current(color=True))
            await pilot.pause()
            assert section.display is False

    _run(_run_case())


def test_tui_new_clears_chat_area(monkeypatch):
    """/new：聊天区彻底清空（含欢迎横幅），不追加任何提示文本；侧栏计划与会话一并重置。"""
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

            assert not app.query_one("#chat").query(Static)  # 聊天区清空
            assert "已开启新会话" not in _chat_text(app)  # 不展示提示文本
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
            assert "Used" in context and "Budget" in context
            # 有调用后：用量卡标题变为「Usage · Calls N」，正文只剩 In/Out
            app.agent.session.usage.add({"prompt_tokens": 1234, "completion_tokens": 567})
            app.ui_status()
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
            # 模型/思考/运行提示都在最底行 #bottom，最左侧依次排列
            bottom = app.query_one("#bottom")
            mode_w = app.query_one("#composer-mode")
            model_w = app.query_one("#composer-model")
            think_w = app.query_one("#composer-thinking")
            running = app.query_one("#running")
            assert model_w.parent is bottom
            assert think_w.parent is bottom
            assert running.parent is bottom
            assert mode_w.parent is bottom
            # 权限模式在最前，其后 模型 · 思考 · 运行提示
            assert bottom.children.index(mode_w) < bottom.children.index(model_w) < bottom.children.index(think_w) < bottom.children.index(running)
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
            app.show_permission_panel(
                "允许? [y]本次 / [n]拒绝 / [a]总是允许该模式: ", "yna", "y / n / a", result, evt
            )
            await pilot.pause()
            assert app.query_one("#input-wrap").display is False
            await pilot.press("y")
            await pilot.pause()
            assert app.query_one("#input-wrap").display is True
            # 底部状态栏：上下文占用条 + git 分支（不再含模型/思考强度/「上下文」「git」字样）
            status = str(app.query_one("#status").content)
            assert config.MODEL not in status
            assert "思考" not in status
            assert "项目" not in status
            assert "上下文" not in status
            assert "git" not in status
            assert "%" in status
            assert "█" in status or "░" in status  # 进度条字符（0% 或 100% 时可能只有一种）

    _run(_run_case())


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
            app.show_permission_panel(
                "允许? [y]本次 / [n]拒绝 / [a]总是允许该模式: ", "yna", "y / n / a", result, evt
            )
            await pilot.pause()
            app.action_cycle_permission_mode()
            await pilot.pause()
            assert app.agent.permission.mode == "smith"

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
            for _ in range(300):
                if not app._busy:
                    break
                await pilot.pause(0.02)
            for _ in range(100):  # 结束消息是异步投递的，轮询等动画隐藏
                if not running.display:
                    break
                await pilot.pause(0.01)
            assert running.display is False  # 结束隐藏

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

    _run(_run_case())


def test_bottom_line_same_row(monkeypatch):
    """运行动画与状态信息在同一行：动画在最左，状态靠右。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test():
            bottom = app.query_one("#bottom")
            running = app.query_one("#running")
            status = app.query_one("#status")
            assert running.parent is bottom
            assert status.parent is bottom  # 同一行
            assert running.display is False  # 空闲时动画隐藏，状态占满整行

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
    """git 分支读取：常规仓库 / 无 .git / detached HEAD。"""
    repo = tmp_path / "repo"
    (repo / ".git" / "refs" / "heads").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    assert git_branch(str(repo)) == "main"

    assert git_branch(str(tmp_path)) is None  # 无 .git

    detached = tmp_path / "detached"
    (detached / ".git").mkdir(parents=True)
    (detached / ".git" / "HEAD").write_text("1a2b3c4d5e6f\n", encoding="utf-8")
    assert git_branch(str(detached)) == "1a2b3c4"
def test_question_choice_modal_single_pick(monkeypatch):
    """选项提问弹窗：数字键快选，单选直接提交所选项。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            app.show_question_panel("用哪个？", ["甲", "乙"], False, result, evt)
            await pilot.pause()
            await pilot.press("2")  # 数字快选：直接提交
            await pilot.pause()
            assert result.get("value") == "乙"
            assert evt.is_set()

    _run(_run_case())


def test_question_choice_modal_multiple_toggle(monkeypatch):
    """多选：空格勾选两项，Enter 提交逗号拼接结果。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            app.show_question_panel("选特征", ["红", "大", "圆"], True, result, evt)
            await pilot.pause()
            await pilot.press("space")   # 勾选 1（红）
            await pilot.press("down")
            await pilot.press("down")
            await pilot.press("space")   # 勾选 3（圆）
            await pilot.press("enter")   # 提交
            await pilot.pause()
            assert result.get("value") == "红, 圆"
            assert evt.is_set()

    _run(_run_case())


def test_question_choice_modal_custom_answer(monkeypatch):
    """自定义回答：选最后一项进入编辑，输入文本提交。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            app.show_question_panel("颜色？", ["红", "蓝"], False, result, evt)
            await pilot.pause()
            await pilot.press("down")
            await pilot.press("down")    # 移到"输入自定义回答"
            await pilot.press("enter")   # 进入编辑态
            await pilot.pause()
            await pilot.press("紫", "色")  # 向输入框键入
            await pilot.press("enter")
            await pilot.pause()
            assert result.get("value") == "紫色"
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
                menu.move(-1)  # ↑ 回绕到最后一项（可视区外）
                await pilot.pause()
                assert menu.scroll_offset.y > 0  # 自动滚动
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
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            inp = app.query_one(ChatInput)
            menu = app.query_one(CommandMenu)
            inp.focus()
            inp.insert("/")
            await pilot.pause()
            await pilot.press("up")
            await pilot.pause()
            assert menu.accept() == "usage"  # up 从首项回绕到最后一项
            await pilot.press("down")
            await pilot.pause()
            assert menu.accept() == "compact"  # down 回到首项（回绕）
            await pilot.press("escape")
            await pilot.pause()
            assert not menu.open
            assert inp.text == "/"  # 输入内容保持不变

    _run(_run_case())


# ---------- 通用选择面板（居中弹窗） ----------

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
            scroll = app.screen.query_one(".selection-scroll")
            assert scroll.show_vertical_scrollbar  # 内容超出：出现滚动条
            for _ in range(39):  # 移到最后一个（可视区之外）
                await pilot.press("down")
            await pilot.pause()
            assert scroll.scroll_offset.y > 0  # 已自动滚到选中项

    _run(_run_case())
