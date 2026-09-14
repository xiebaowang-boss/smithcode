"""TUI MCP 向导测试：面板推进、输入步骤、取消无副作用与宿主接线。"""

import asyncio

from textual.widgets import Input, Static

from smithcode import config
from smithcode.agent import Agent
from smithcode.mcp.config import ServerConfig
from smithcode.mcp.wizard import McpWizard, WizardPlan
from smithcode.session import Session
from smithcode.tui.app import SmithTUI
from smithcode.tui.panels import (
    McpWizardPanel,
    McpWizardScreen,
    SelectionItem,
    SelectionPanel,
    SelectionScreen,
)


class FakeLLM:
    def chat_stream(self, messages, tools=None):
        yield ("message", {"role": "assistant", "content": "ok"})


def _agent(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr("smithcode.agent.LLMClient", lambda: FakeLLM())
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    return Agent(session=Session())


def test_wizard_template_flow(monkeypatch, tmp_path):
    app = SmithTUI(_agent(monkeypatch, tmp_path))
    results = []

    async def _case():
        async with app.run_test() as pilot:
            wizard = McpWizard(workspace=str(tmp_path))
            panel = McpWizardPanel(wizard, lambda plan: app.screen.dismiss(plan))
            app.push_screen(McpWizardScreen(panel), callback=results.append)
            await pilot.pause()
            for _ in range(5):  # source / template / name / scope / review 全默认
                await pilot.press("enter")
                await pilot.pause()

    asyncio.run(_case())
    assert results and results[0] is not None
    plan = results[0]
    assert plan.config.name == "filesystem"
    assert str(tmp_path) in " ".join(plan.config.command)


def test_wizard_manual_flow_with_secret(monkeypatch, tmp_path):
    app = SmithTUI(_agent(monkeypatch, tmp_path))
    results = []

    async def _case():
        async with app.run_test() as pilot:
            wizard = McpWizard(workspace=str(tmp_path))
            panel = McpWizardPanel(wizard, lambda plan: app.screen.dismiss(plan))
            app.push_screen(McpWizardScreen(panel), callback=results.append)
            await pilot.pause()

            await pilot.press("down")   # 选择「手动输入启动命令」
            await pilot.press("enter")
            panel.query_one(Input).value = "npx -y @example/svc"
            await pilot.press("enter")  # 命令
            panel.query_one(Input).value = "TOKEN"
            await pilot.press("enter")  # 环境变量名
            panel.query_one(Input).value = "mine"
            await pilot.press("enter")  # 名称
            await pilot.press("enter")  # 作用域（默认用户级）
            await pilot.press("enter")  # 密钥方式（默认存凭据库）
            panel.query_one(Input).value = "hunter2"
            await pilot.press("enter")  # 密钥值
            await pilot.press("enter")  # 确认

    asyncio.run(_case())
    plan = results[0]
    assert isinstance(plan, WizardPlan)
    assert plan.config.name == "mine"
    assert plan.config.command == ["npx", "-y", "@example/svc"]
    assert plan.config.env == {"TOKEN": "${TOKEN}"}
    assert plan.secrets == [("TOKEN", "hunter2")]


def test_wizard_cancel_has_no_side_effects(monkeypatch, tmp_path):
    app = SmithTUI(_agent(monkeypatch, tmp_path))
    results = []

    async def _case():
        async with app.run_test() as pilot:
            wizard = McpWizard(workspace=str(tmp_path))
            panel = McpWizardPanel(wizard, lambda plan: app.screen.dismiss(plan))
            app.push_screen(McpWizardScreen(panel), callback=results.append)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

    asyncio.run(_case())
    assert results == [None]
    home = tmp_path / "home"
    assert not (home / "config.toml").exists()
    assert not (tmp_path / ".smithcode").exists()


def test_mcp_add_command_opens_wizard(monkeypatch, tmp_path):
    app = SmithTUI(_agent(monkeypatch, tmp_path))

    async def _case():
        async with app.run_test() as pilot:
            app.handle_command("/mcp add")
            await pilot.pause()
            assert isinstance(app.screen, McpWizardScreen)

    asyncio.run(_case())


def test_selection_panel_skips_separator(monkeypatch, tmp_path):
    app = SmithTUI(_agent(monkeypatch, tmp_path))

    async def _case():
        async with app.run_test() as pilot:
            items = [
                SelectionItem("添加 MCP", "add"),
                SelectionItem("", "", separator=True),
                SelectionItem("fake", "fake", trailing="已连接",
                              trailing_style="#23d18b"),
            ]
            results = []
            panel = SelectionPanel("MCP", items, lambda value: app.screen.dismiss(value))
            app.push_screen(SelectionScreen(panel), callback=results.append)
            await pilot.pause()
            assert panel._selected == 0

            await pilot.press("down")   # 跳过间隔行，落到 fake
            assert panel._selected == 2
            await pilot.press("up")     # 回到第一项
            assert panel._selected == 0
            await pilot.press("enter")
            await pilot.pause()

    asyncio.run(_case())


class _Status:
    name = "fake"
    scope = "user"
    state = "connected"
    tool_count = 2
    error = ""
    state_label = "已连接"


class _StubMcp:
    """只提供命令层用到的读取/动作，供二级菜单测试。"""

    def __init__(self):
        self.reconnected = []

    def status(self):
        return [_Status()]

    def reconnect(self, name):
        self.reconnected.append(name)
        return True


def test_mcp_second_level_escape_returns_to_overview(monkeypatch, tmp_path):
    app = SmithTUI(_agent(monkeypatch, tmp_path))
    app.agent.mcp = _StubMcp()

    async def _case():
        async with app.run_test() as pilot:
            app.handle_command("/mcp")
            await pilot.pause()
            assert isinstance(app.screen, SelectionScreen)
            overview = app.screen.query_one("SelectionPanel")
            assert overview._selected == 0  # 「添加 MCP」

            await pilot.press("down")   # 跳过间隔行，落到 fake
            assert overview._selected == 2
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, SelectionScreen)
            child = app.screen.query_one("SelectionPanel")
            assert "fake" in child._title

            await pilot.press("escape")  # 二级 Esc：返回一级
            await pilot.pause()
            assert isinstance(app.screen, SelectionScreen)
            parent = app.screen.query_one("SelectionPanel")
            assert parent is not overview
            # 光标锚点落回刚才进入的那台服务器，但不给该行加「(当前)」
            anchored = parent._items[parent._selected]
            assert anchored.value == "fake"
            assert anchored.current is False
            row = parent.query(".selection-row")[parent._selected]
            assert "当前" not in str(row.query_one(".selection-label", Static).content)

            await pilot.press("escape")  # 根级 Esc：关闭
            await pilot.pause()
            assert not isinstance(app.screen, SelectionScreen)
            assert app._select_stack == []

    asyncio.run(_case())


def test_mcp_second_level_action_executes_and_clears_stack(monkeypatch, tmp_path):
    app = SmithTUI(_agent(monkeypatch, tmp_path))
    stub = _StubMcp()
    app.agent.mcp = stub

    async def _case():
        async with app.run_test() as pilot:
            app.handle_command("/mcp")
            await pilot.pause()
            await pilot.press("down")   # fake
            await pilot.press("enter")  # 进入二级
            await pilot.pause()
            assert isinstance(app.screen, SelectionScreen)

            await pilot.press("down")   # 查看工具 → 重连
            await pilot.press("enter")
            await pilot.pause()

            assert stub.reconnected == ["fake"]
            assert not isinstance(app.screen, SelectionScreen)
            assert app._select_stack == []

    asyncio.run(_case())


def test_selection_panel_hides_scrollbar(monkeypatch, tmp_path):
    """选择面板不绘制滚动条（与聊天区/命令菜单一致），超长列表仍可滚动。"""
    app = SmithTUI(_agent(monkeypatch, tmp_path))

    async def _case():
        async with app.run_test() as pilot:
            items = [SelectionItem(f"item {i}", str(i)) for i in range(40)]
            panel = SelectionPanel("test", items, lambda value: app.screen.dismiss(value))
            app.push_screen(SelectionScreen(panel), callback=lambda value: None)
            await pilot.pause()
            scroller = panel.query_one(".selection-scroll")
            assert scroller.styles.scrollbar_size_vertical == 0

    asyncio.run(_case())


def test_selection_panel_grouping_headers(monkeypatch, tmp_path):
    """分组：按 category 出现顺序插入表头（不可选中），导航跳过表头与空行。"""
    app = SmithTUI(_agent(monkeypatch, tmp_path))

    async def _case():
        async with app.run_test() as pilot:
            items = [
                SelectionItem("m1", "m1", category="Favorites"),
                SelectionItem("m2", "m2", category="Favorites"),
                SelectionItem("m3", "m3", category="Provider A"),
                SelectionItem("plain", "plain"),  # 无分类
            ]
            results = []
            panel = SelectionPanel("models", items, lambda value: app.screen.dismiss(value))
            app.push_screen(SelectionScreen(panel), callback=results.append)
            await pilot.pause()

            headers = [str(w.content) for w in panel.query(".selection-header")]
            assert len(headers) == 2
            assert any("Favorites" in text for text in headers)
            assert any("Provider A" in text for text in headers)

            # 可选项导航 0→1→2→3：表头与分组间空行都跳过
            assert panel._selected == 0
            for expected in (1, 2, 3):
                await pilot.press("down")
                assert panel._selected == expected
            # item 索引 2 的真实行号因表头/空行而后移
            assert panel._row_of_item[2] > 2

            await pilot.press("enter")
            await pilot.pause()
            assert results == ["plain"]

    asyncio.run(_case())


def test_after_mcp_wizard_applies_plan(monkeypatch, tmp_path):
    app = SmithTUI(_agent(monkeypatch, tmp_path))
    recorded = []
    monkeypatch.setattr("smithcode.tui.app.apply_plan", lambda service, plan: recorded.append(plan))

    plan = WizardPlan(config=ServerConfig(name="svc", command=["npx", "x"]), scope="user")

    async def _case():
        async with app.run_test():
            app._after_mcp_wizard(plan)
            app._after_mcp_wizard(None)  # 取消分支：不调用 apply_plan

    asyncio.run(_case())
    assert recorded == [plan]
