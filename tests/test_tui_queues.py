"""TUI 排队面板：运行中提交入队 → 面板显示 → 逐条撤销 / Esc 取回。

面板位置（`#running` 之下、输入框之上）与「不新增任何按键」是这次改造的硬要求，
所以这里除了渲染断言，还锁住「运行中按 Enter 只是入队、不打断当前任务」。
"""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from smithcode import config
from smithcode.agent import Agent
from smithcode.session import Session
from smithcode.tui.app import SmithTUI
from smithcode.tui.widgets import ChatInput, QueuePanel, QueueRow, display_width


@pytest.fixture(autouse=True)
def restore_renderer():

    yield


@pytest.fixture(autouse=True)
def no_prompting(monkeypatch):
    monkeypatch.setattr("smithcode.permission.engine.confirmations_available", lambda: False)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", __file__)


class FakeLLM:
    def chat_stream(self, messages, tools=None):
        yield ("message", {"role": "assistant", "content": "好"})


def _make_agent(monkeypatch) -> Agent:
    monkeypatch.setattr("smithcode.agent.LLMClient", lambda: FakeLLM())
    return Agent(session=Session())


def _queue_config(monkeypatch, **values) -> None:
    monkeypatch.setattr("smithcode.config._read_config_file", lambda: {"queue": values})


def _rows(app) -> list[QueueRow]:
    return list(app.query(QueueRow))



def test_running_input_is_queued_and_shown_in_panel(monkeypatch):
    """运行中按 Enter：入队并出现在 `#running` 之下、输入框之上的面板里。"""
    agent = _make_agent(monkeypatch)

    async def case():
        app = SmithTUI(agent)
        async with app.run_test() as pilot:
            app._busy = True  # 假装任务在跑（本用例只验输入路径与面板）
            app.start_task("顺便补一下文档")
            await pilot.pause()

            panel = app.query_one("#queued", QueuePanel)
            assert panel.display is True
            assert agent.pending_message_count == 1
            assert panel.size.height >= 1, f"面板没有可见高度: {panel.size}"
            rows = _rows(app)
            assert [row.item.text for row in rows] == ["顺便补一下文档"]
            # 无表头行：面板的子控件就是排队条目本身
            assert len(panel.children) == 1
            assert all(isinstance(child, QueueRow) for child in panel.children)
            # 行首续行标记 + 右侧图标组（✎ 编辑 / ✕ 撤销，贴最后一列）
            line = rows[0].render()
            assert line.plain.startswith("↳ ")
            assert line.plain.endswith("✎   ✕")
            assert "edit" not in line.plain and "cancel" not in line.plain
            assert display_width(line.plain) == panel.size.width
            # 位置：排在 #running 之后、输入框之前
            order = [child.id for child in app.query_one("#input-wrap").children]
            assert order.index("running") < order.index("queued") < order.index("input")
            # 空队列时整块隐藏
            app.agent.clear_queue()
            await pilot.pause()
            assert panel.display is False

    asyncio.run(case())



def test_rows_no_longer_show_the_delivery_mode(monkeypatch):
    """行内不再写投递方式（默认配置下它是常量，写出来只占宽度）。

    方式仍可从 `agent.steering_queue` / `follow_up_queue` 查到，只是不上屏。
    """
    _queue_config(monkeypatch, delivery="steer")
    agent = _make_agent(monkeypatch)

    async def case():
        app = SmithTUI(agent)
        async with app.run_test() as pilot:
            app._busy = True
            app.start_task("先别改 config")
            await pilot.pause()
            assert agent.steering_queue.count == 1  # 确实走的是 steer
            line = _rows(app)[0].render()
            assert line.plain.startswith("↳ ")
            assert line.plain.endswith("✎   ✕")
            assert "Steer" not in line.plain and "Follow" not in line.plain

            def style_at(char: str) -> str:
                index = line.plain.index(char)
                return next(s.style for s in line.spans if s.start <= index < s.end)

            assert style_at("✎") == "#7aa2f7"  # 动作色
            assert style_at("✕") == "#f7768e"  # 破坏性色
            assert style_at("↳") == "#606060"
            assert style_at("先别改") == "#808080"

    asyncio.run(case())


def test_clicking_the_close_mark_cancels_that_item(monkeypatch):
    """行尾 ✕ = 逐条撤销（终端上报鼠标时生效；这里按坐标直接触发点击）。"""
    agent = _make_agent(monkeypatch)

    async def case():
        app = SmithTUI(agent)
        async with app.run_test() as pilot:
            app._busy = True
            app.start_task("一")
            app.start_task("二")
            await pilot.pause()
            first = _rows(app)[0]
            _edit_zone, cancel_zone = first._icon_zones()
            first.on_click(SimpleNamespace(x=cancel_zone[0] + 1, y=0))  # 点在 ✕ 命中区
            await pilot.pause()
            assert [row.item.text for row in _rows(app)] == ["二"]
            assert agent.pending_message_count == 1

    asyncio.run(case())



def test_clicking_the_text_area_does_not_cancel(monkeypatch):
    """点正文**不**撤销：以前点整行都会撤销，想选中/复制排队文本时一点就没了。"""
    agent = _make_agent(monkeypatch)

    async def case():
        app = SmithTUI(agent)
        async with app.run_test() as pilot:
            app._busy = True
            app.start_task("别删我")
            await pilot.pause()
            row = _rows(app)[0]
            # 行首标记、正文各段、正文与按钮之间的空白：点它们都不该有副作用
            for column in (0, 2, 10, row.size.width - 20):
                row.on_click(SimpleNamespace(x=column, y=0))
            await pilot.pause()
            assert [r.item.text for r in _rows(app)] == ["别删我"]
            assert agent.pending_message_count == 1

    asyncio.run(case())


def test_escape_recalls_queued_text_into_the_editor(monkeypatch):
    """Esc 中止：排队内容回到输入框，队列清空（复用既有按键，不新增）。"""
    agent = _make_agent(monkeypatch)

    async def case():
        app = SmithTUI(agent)
        async with app.run_test() as pilot:
            app._busy = True
            app.start_task("一")
            app.start_task("二")
            await pilot.pause()

            app.action_interrupt()  # Esc
            await pilot.pause()

            editor = app.query_one("#input", ChatInput)
            assert "一" in editor.text and "二" in editor.text
            assert agent.pending_message_count == 0
            assert app.query_one("#queued", QueuePanel).display is False

    asyncio.run(case())


def test_new_session_clears_the_queue(monkeypatch):
    """`/new` 不继承排队输入（会话边界连队列一起重置）。"""
    agent = _make_agent(monkeypatch)
    agent.steer("旧会话的插话")

    agent.new_session()

    assert agent.pending_message_count == 0


def _record_chat(monkeypatch, app):
    """记录对话区实际收到的语义消息（不依赖控件内部结构）。"""
    from smithcode.tui.chat import User

    chat = app.query_one("#chat")
    recorded: list = []
    original = chat.apply

    def spy(item):
        recorded.append(item)
        return original(item)

    monkeypatch.setattr(chat, "apply", spy)
    return recorded, User


def test_submitting_while_busy_does_not_echo_into_the_chat(monkeypatch):
    """真实入口（按 Enter → `on_chat_input_submitted`）：忙时只入队，**不回显对话区**。

    这条用例存在的理由：其余用例直接调 `start_task`，绕过了真实入口，于是
    "回显发生在入队之前"这类缺陷不会被任何一条用例发现——用户第一次真用就撞上了。
    """
    agent = _make_agent(monkeypatch)

    async def case():
        app = SmithTUI(agent)
        async with app.run_test() as pilot:
            recorded, User = _record_chat(monkeypatch, app)
            app._busy = True  # 假装任务在跑
            app.on_chat_input_submitted(ChatInput.Submitted("排队这句"))
            await pilot.pause()

            assert agent.pending_message_count == 1  # 已入队
            assert [row.item.text for row in _rows(app)] == ["排队这句"]  # 在面板里
            assert [item for item in recorded if isinstance(item, User)] == []  # 不在对话区

            # 非运行态：照旧立刻回显（避免"发出去没反应"）；这一轮跑完时，先前
            # 排队的那条被投递、才上屏——顺序即"直接发的在前、排队的在后"
            app._busy = False
            app.on_chat_input_submitted(ChatInput.Submitted("直接发这句"))
            await pilot.pause()
            assert [item.text for item in recorded if isinstance(item, User)] == [
                "直接发这句", "排队这句",
            ]
            assert agent.pending_message_count == 0  # 已被投递

    asyncio.run(case())


def test_delivered_queued_prompt_lands_in_the_chat(monkeypatch):
    """投递（本轮结束 / 工具批之间抽水）时才落到对话区，并同时从面板消失。"""
    agent = _make_agent(monkeypatch)

    async def case():
        app = SmithTUI(agent)
        async with app.run_test() as pilot:
            recorded, User = _record_chat(monkeypatch, app)
            app._busy = True
            app.on_chat_input_submitted(ChatInput.Submitted("排队这句"))
            await pilot.pause()
            assert agent.pending_message_count == 1

            agent.get_follow_up_messages()  # 循环的抽水动作：投递
            await pilot.pause()

            assert [item.text for item in recorded if isinstance(item, User)] == ["排队这句"]
            assert _rows(app) == []  # 已出队，面板随之隐藏
            assert agent.session.messages[-1]["content"] == "排队这句"  # 确实进了历史

    asyncio.run(case())


def test_steering_delivery_marks_the_event(monkeypatch):
    """投递事件带得动"是插话还是续跑"：前端不必自己猜（具体事件，不靠类型推断）。"""
    from smithcode.event.catalog import InboxDelivered

    agent = _make_agent(monkeypatch)
    seen: list = []
    agent.events.subscribe(seen.append)
    agent.steer("插话")
    agent.follow_up("续跑")

    agent.get_steering_messages()
    agent.get_follow_up_messages()

    delivered = [env.data for env in seen if isinstance(env.data, InboxDelivered)]
    # 事件带完整项（含 kind）：前端不必自己推断"是插话还是续跑"
    assert [(e.item.text, e.item.kind) for e in delivered] == [
        ("插话", "steer"), ("续跑", "follow_up"),
    ]


class BlockingLLM:
    """可控阻塞的假模型：任务停在流式读取里，直到用例显式放行。

    测试排队要卡在"队列非空"的时刻——用定时 sleep 的假模型会自己跑完并投递，
    断言就变成在测投递之后的状态（我第一版就是这么写的，结果测了个空）。
    """

    def __init__(self):
        self.release = threading.Event()

    def chat_stream(self, messages, tools=None):
        self.release.wait(timeout=10)
        yield ("content", "收到")
        yield ("message", {"role": "assistant", "content": "收到"})


def test_pressing_enter_during_a_live_task_shows_the_panel(monkeypatch):
    """真实按键路径（focus + insert + press enter），任务确实卡住时按 Enter。

    这条用例补两个洞：① 消息要经 ChatInput.on_key → post_message(Submitted) →
    事件分发 → on_chat_input_submitted，任何一环出问题都只在真实按键下暴露；
    ② 断言**屏幕上真的有这块区域**（size/region），而不只是 `display is True`
    ——面板 display 为真但高度为 0 时，用户看到的就是"面板没出现"。
    """
    llm = BlockingLLM()
    monkeypatch.setattr("smithcode.agent.LLMClient", lambda: llm)
    agent = Agent(session=Session())

    async def case():
        app = SmithTUI(agent)
        async with app.run_test(size=(100, 30)) as pilot:
            inp = app.query_one(ChatInput)
            inp.focus()
            inp.insert("第一句")
            await pilot.press("enter")
            await pilot.pause(0.05)
            assert app._busy is True  # 任务卡在流式读取里

            inp.insert("排队这句")
            await pilot.press("enter")
            await pilot.pause(0.1)

            panel = app.query_one("#queued", QueuePanel)
            assert agent.pending_message_count == 1, "按 Enter 后应当已入队"
            assert panel.display is True, "排队面板应当显示"
            assert [row.item.text for row in _rows(app)] == ["排队这句"]
            # 不只是 display：面板要**真的有可见高度**（display 真但高 0 = 用户看不见）
            assert panel.size.height >= 1, f"面板没有可见高度: {panel.size}"
            assert panel.region.height >= 1, f"面板没有占据屏幕区域: {panel.region}"
            # 位置仍在运行动画之下、输入框之上
            wrap = app.query_one("#input-wrap")
            order = [child.id for child in wrap.children if child.id]
            assert order.index("running") < order.index("queued") < order.index("input")

            llm.release.set()  # 放行，避免用例结束时留一个挂住的 worker
            for _ in range(200):
                if not app._busy:
                    break
                await pilot.pause(0.02)

    asyncio.run(case())



def test_long_cjk_text_is_clipped_by_display_width(monkeypatch):
    """中文长句按**显示宽度**截断：右侧按钮组的列位不受影响。

    用 len() 算会把中文当 1 格，长句截不准、按钮跟着漂移——这条用例锁住列位。
    """
    agent = _make_agent(monkeypatch)
    long_text = "把 packages 下所有 README 里指向旧仓库的绝对链接批量替换成相对路径并跑一遍检查"

    async def case():
        app = SmithTUI(agent)
        async with app.run_test() as pilot:
            app._busy = True
            app.start_task(long_text)
            await pilot.pause()

            panel = app.query_one("#queued", QueuePanel)
            plain = _rows(app)[0].render().plain

            assert plain.endswith("✎   ✕")  # 图标组没有被挤走
            assert plain.count("✎") == 1 and plain.count("✕") == 1
            assert "…" in plain  # 截断了
            assert display_width(plain) == panel.size.width
            assert long_text not in plain

    asyncio.run(case())



def test_each_row_has_its_own_buttons(monkeypatch):
    """每行都有自己的 ✎ / ✕（图标属于该条，不是面板共用一个）。"""
    agent = _make_agent(monkeypatch)

    async def case():
        app = SmithTUI(agent)
        async with app.run_test() as pilot:
            app._busy = True
            app.start_task("第一条")
            agent.steer("第二条")
            await pilot.pause()

            lines = [row.render().plain for row in _rows(app)]
            assert len(lines) == 2
            assert all(line.endswith("✎   ✕") for line in lines)
            assert not any("排队中" in line for line in lines)

            # 面板顺序是 steer 组在前、follow 组在后（不是入队顺序）：
            # [0] = "第二条"(steer)，[1] = "第一条"(follow)
            assert [row.item.text for row in _rows(app)] == ["第二条", "第一条"]
            # 点 [1] 的 cancel 只删那一条
            row = _rows(app)[1]
            _edit_zone, cancel_zone = row._icon_zones()
            row.on_click(SimpleNamespace(x=cancel_zone[0] + 1, y=0))
            await pilot.pause()
            assert [row.item.text for row in _rows(app)] == ["第二条"]

    asyncio.run(case())


def test_edit_button_returns_the_item_to_the_editor(monkeypatch):
    """`✎` = 取回编辑：出队 + 文本进输入框 + 光标到末尾，**不碰当前任务**。

    这是它存在的理由：改排队里的一个错别字不该付"中断当前任务"的代价（那是 Esc）。
    """
    agent = _make_agent(monkeypatch)

    async def case():
        app = SmithTUI(agent)
        async with app.run_test() as pilot:
            editor = app.query_one("#input", ChatInput)
            editor.text = "已有一半草稿"
            app._busy = True
            app.start_task("排错的那句话")
            agent.steer("第二条")
            await pilot.pause()
            assert agent.pending_message_count == 2

            # 面板里 steer 组在前：[0] = "第二条"
            row = _rows(app)[0]
            assert row.item.text == "第二条"
            # 点在 ✎ 上：命中区由 QueueRow 自己给出（测试里不再复写一份坐标算术）
            edit_zone, _cancel_zone = row._icon_zones()
            row.on_click(SimpleNamespace(x=edit_zone[0] + 1, y=0))
            await pilot.pause()

            assert agent.pending_message_count == 1              # 该条出队
            assert [r.item.text for r in _rows(app)] == ["排错的那句话"]
            assert editor.text == "已有一半草稿\n第二条"          # 拼在草稿之后
            assert editor.cursor_location == (1, len("第二条"))   # 光标到末尾
            assert app._busy is True                              # 当前任务没被打断

    asyncio.run(case())
