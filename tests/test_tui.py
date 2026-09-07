"""TUI 测试：界面挂载、消息流式渲染、弹窗确认、工具折叠、计划侧边栏（headless pilot）。"""
import asyncio
import json
import threading

from textual.widgets import Static

from smithcode import __version__, config
from smithcode.agent import Agent
from smithcode.session import Session
from smithcode.tui.app import (
    ChatInput,
    Sidebar,
    SmithTUI,
    ThinkingBlock,
    ToolCall,
    TuiRenderer,
)
from smithcode.utils.terminal import confirmations_available


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


def test_tui_mounts_and_welcomes(monkeypatch):
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            assert pilot.app is app
            assert "SmithCode TUI" in _chat_text(app)

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
            assert app.query_one(ChatInput).display is False  # 输入框被替换
            await pilot.press("y")
            await pilot.pause()
            assert result.get("value") == "y"
            assert evt.is_set()
            assert app.query_one(ChatInput).display is True  # 面板关闭后恢复

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


def test_question_panel_resolves(monkeypatch):
    """无选项提问：输入框被提问面板替换，输入文本提交后恢复。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            result, evt = {}, threading.Event()
            app.show_question_panel("要继续吗?", [], False, result, evt)
            await pilot.pause()
            assert app.query_one(ChatInput).display is False  # 输入框被替换
            await pilot.press("是")
            await pilot.press("enter")
            await pilot.pause()
            assert result.get("value") == "是"
            assert evt.is_set()
            assert app.query_one(ChatInput).display is True  # 面板关闭后恢复

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
    """工具调用渲染为可折叠块：summary 模式默认收起，展开可见结果。"""
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
            assert body.display is False  # summary 模式默认收起
            block.action_toggle()
            assert body.display is True
            assert "文件内容" in str(body.content)

    _run(_run_case())


def test_tui_sidebar_shows_plan_section(monkeypatch):
    """侧边栏常驻；计划更新渲染到下半部分。"""
    no_prompting(monkeypatch)
    from smithcode import plan as plan_mod

    plan_mod.current().replace([{"content": "读文件", "status": "in_progress"}])

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            sidebar = app.query_one(Sidebar)
            assert sidebar.display is True  # 常驻可见
            assert f"v{__version__}" in str(sidebar.query_one(".sidebar-version").content)  # 底部版本号
            # 版本号下方展示当前工作区路径
            assert str(config.WORKSPACE_ROOT) in str(
                sidebar.query_one(".sidebar-workspace").content
            )
            TuiRenderer(app).plan("共 1 步", plan_mod.render_current(color=True))
            await pilot.pause()
            assert "读文件" in str(sidebar.query_one(".plan-body").content)

    _run(_run_case())


def test_tui_sidebar_usage_section(monkeypatch):
    """侧边栏上半展示会话用量与上下文占用。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test():
            text = str(app.query_one(Sidebar).query_one(".usage-body").content)
            assert "上下文" in text

    _run(_run_case())


def test_tui_status_bar(monkeypatch):
    """底部状态栏展示模型 / 思考强度 / 项目名 / git 信息。"""
    no_prompting(monkeypatch)

    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test():
            text = str(app.query_one("#status").content)
            assert config.MODEL in text
            assert "思考" in text
            assert "项目" in text

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


def test_turn_footer_after_task(monkeypatch):
    """轮次结束在会话末尾追加 opencode 式「▣ 模型 · 用时」页脚。"""
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
            assert "用时" in footer

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
    from smithcode.tui.app import git_branch

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
