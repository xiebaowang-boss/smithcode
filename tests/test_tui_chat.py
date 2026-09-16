"""对话区统一消息模型测试：语义级别、唯一入口 apply、统一缩进对齐。"""
import asyncio

import pytest
from rich.text import Text
from textual.geometry import Region

import smithcode.renderer as renderer_module
from smithcode.agent import Agent
from smithcode.session import Session
from smithcode.tui.app import SmithTUI
from smithcode.tui.bridge import TuiRenderer
from smithcode.tui.chat import (
    LEVEL_MARK,
    LEVEL_STYLE,
    Assistant,
    Block,
    Footer,
    Level,
    Notice,
    StreamEnd,
    ThinkingStart,
    ToolStart,
    User,
    Welcome,
    coerce_level,
    level_from_style,
)
from smithcode.tui.widgets import ChatView, ToolCall


@pytest.fixture(autouse=True)
def restore_renderer():
    from smithcode import renderer

    backup = renderer._current
    yield
    renderer_module.set_renderer(backup)


class FakeLLM:
    def chat_stream(self, messages, tools=None):
        yield ("message", {"role": "assistant", "content": "ok"})


def _make_agent(monkeypatch):
    monkeypatch.setattr("smithcode.agent.LLMClient", lambda: FakeLLM())
    return Agent(session=Session())


def _run(coro):
    return asyncio.run(coro)


# ---------- 纯函数：级别映射 ----------


def test_coerce_level_accepts_level_and_string():
    assert coerce_level(Level.ERROR) is Level.ERROR
    assert coerce_level("warning") is Level.WARNING
    assert coerce_level("未知") is Level.INFO


def test_level_from_style_maps_legacy_colors():
    assert level_from_style("red") is Level.ERROR
    assert level_from_style("yellow") is Level.WARNING
    assert level_from_style("green") is Level.SUCCESS
    assert level_from_style(None) is Level.INFO


def test_level_tables_cover_all_levels():
    assert set(LEVEL_STYLE) == set(Level)
    assert set(LEVEL_MARK) == set(Level)
    # 固定 1 格图标：所有通知正文左起点因此恒定
    assert all(len(mark) == 1 for mark in LEVEL_MARK.values())


# ---------- apply：唯一入口与统一缩进 ----------


def test_apply_unknown_type_raises(monkeypatch):
    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            chat = app.query_one(ChatView)
            with pytest.raises(TypeError):
                chat.apply(object())
            await pilot.pause()

    _run(_run_case())


def test_notice_uses_unified_gutter_and_level(monkeypatch):
    """通知：带 chat-item（缩进 3）、按级别着色、固定图标前缀。"""
    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            chat = app.query_one(ChatView)
            chat.apply(Notice("出错了", Level.ERROR))
            await pilot.pause()
            notice = chat.query(".notice").first()
            assert notice.has_class("chat-item")
            assert notice.styles.padding.left == 3
            content = str(notice.content)
            assert content.startswith(f"{LEVEL_MARK[Level.ERROR]} ")
            assert "出错了" in content
            assert any(LEVEL_STYLE[Level.ERROR] in str(span.style) for span in notice.content.spans)

    _run(_run_case())


def test_all_top_level_items_carry_chat_item(monkeypatch):
    """对齐回归：任何顶层消息都带 chat-item，避免再次散落成齐左打印。"""
    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            chat = app.query_one(ChatView)
            chat.apply(User("你好"))
            chat.apply(Assistant("回复"))
            chat.apply(Welcome(Text("欢迎")))
            chat.apply(Notice("信息"))
            chat.apply(Block("块一\n块二"))
            chat.apply(Footer("m", "high", "1s"))
            chat.apply(ToolStart(1, "read a.py", "inline", "read_file"))
            chat.apply(ThinkingStart())
            await pilot.pause()
            children = list(chat.children)
            assert children  # 有内容
            assert all(child.has_class("chat-item") for child in children)

    _run(_run_case())


def test_block_parses_ansi_without_leaking_escapes(monkeypatch):
    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            chat = app.query_one(ChatView)
            chat.apply(Block("\x1b[31m红字\x1b[0m"))
            await pilot.pause()
            text = "\n".join(str(w.content) for w in chat.query(".notice"))
            assert "红字" in text
            assert "\x1b" not in text

    _run(_run_case())


def test_ui_notice_routes_through_apply(monkeypatch):
    """App 的 ui_notice 是通知的统一出口，级别决定图标与颜色。"""
    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            app.ui_notice("注意", "warning")
            await pilot.pause()
            notice = app.query_one(ChatView).query(".notice").first()
            assert str(notice.content).startswith(f"{LEVEL_MARK[Level.WARNING]} ")
            assert "注意" in str(notice.content)

    _run(_run_case())


def test_tui_renderer_info_warn_error_use_levels(monkeypatch):
    """TuiRenderer.info/warn/error 投递带级别的通知，不再手写 style。"""
    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            r = TuiRenderer(app)
            r.info("普通")
            r.warn("警告")
            r.error("错误")
            await pilot.pause()
            notices = [str(w.content) for w in app.query_one(ChatView).query(".notice")]
            joined = "\n".join(notices)
            assert f"{LEVEL_MARK[Level.INFO]} 普通" in joined
            assert f"{LEVEL_MARK[Level.WARNING]} 警告" in joined
            assert f"{LEVEL_MARK[Level.ERROR]} 错误" in joined

    _run(_run_case())


def test_stream_end_does_not_break(monkeypatch):
    """流结束事件经 apply 路由不报错（空流也安全）。"""
    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            chat = app.query_one(ChatView)
            chat.apply(StreamEnd())
            await pilot.pause()
            assert True

    _run(_run_case())


def test_run_command_shows_executing_then_static(monkeypatch):
    """命令工具：pending 期显式「执行中」+ 转轮，完成后转静态行。"""
    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            app.ui_tool_start(1, "command sleep 5", "block", "run_command")
            await pilot.pause()
            block = app.query_one(ToolCall)
            assert "执行中 · command sleep 5" in block._header_text()
            app.ui_tool_result(1, "done", False, False)
            await pilot.pause()
            header = block._header_text()
            assert "执行中" not in header
            assert "command sleep 5" in header

    _run(_run_case())


def test_non_command_tool_keeps_gear_spinner(monkeypatch):
    """非命令工具 pending 期仍是转轮 + 齿轮，不显示「执行中」。"""
    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            app.ui_tool_start(1, "read a.py", "inline", "read_file")
            await pilot.pause()
            block = app.query_one(ToolCall)
            header = block._header_text()
            assert "执行中" not in header
            assert "⚙ read a.py" in header

    _run(_run_case())


def test_spinner_tick_repaints_only_spinner_cell(monkeypatch):
    """转轮 tick 只把转轮那一格标脏。

    脏区是整个控件时终端每 100ms 整行重写：webfetch 这类网络工具 pending
    可持续数十秒，慢终端上会表现为闪烁。这里断言文案照常推进到下一帧，
    且重绘范围仍只有转轮那一格。
    """
    async def _run_case():
        app = SmithTUI(_make_agent(monkeypatch))
        async with app.run_test() as pilot:
            app.ui_tool_start(1, "fetch https://example.com/docs", "inline", "webfetch")
            await pilot.pause()
            block = app.query_one(ToolCall)
            header = block.query_one(".tool-header")
            before = str(header.content)
            block._spin()
            after = str(header.content)
            assert after != before
            assert after[1:] == before[1:]  # 只有首格（转轮字符）变化
            assert header._repaint_regions == {Region(0, 0, 1, 1)}

    _run(_run_case())
