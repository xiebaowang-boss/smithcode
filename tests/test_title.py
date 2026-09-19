"""终端窗口标题测试：合成 / 净化 / 事件 → 写入 / 压栈出栈 / 退出钩子 / 装配与订阅。

不触摸真实终端：sink 一律换成记录器，退出钩子的 atexit / signal 注册在本文件里
统一打桩（避免测试进程真装信号处理器）。
"""
import asyncio
import signal

import pytest

import smithcode.renderer as renderer_module
from smithcode import title
from smithcode.agent import Agent
from smithcode.agent.interactions import (
    InteractionBridge,
    PromptFinished,
    PromptRequest,
    PromptStarted,
)
from smithcode.renderer import Renderer
from smithcode.session import Session


def _prompt(prompt_id: str, kind: str = "permission", title: str = "允许执行 x?"):
    return PromptStarted(id=prompt_id, kind=kind, title=title)


def _finished(prompt_id: str, outcome: str = "answered"):
    return PromptFinished(id=prompt_id, kind="permission", outcome=outcome)


class Recorder:
    """假 sink：记录写出的控制序列。"""

    def __init__(self):
        self.writes = []

    def __call__(self, seq: str) -> None:
        self.writes.append(seq)


class FakeRenderer(Renderer):
    """记录收到的调用，用于断言事件经迁移桥到达后端。"""

    def __init__(self):
        super().__init__()
        self.calls = []

    def title_changed(self, title_text: str) -> None:
        self.calls.append(("title", title_text))

    def turn_started(self) -> None:
        self.calls.append(("started",))

    def turn_finished(self, status: str = "ok") -> None:
        self.calls.append(("finished", status))

    def turn_waiting_started(self) -> None:
        self.calls.append(("waiting_started",))

    def turn_waiting_finished(self) -> None:
        self.calls.append(("waiting_finished",))

    def warn(self, text: str) -> None:
        self.calls.append(("warn", text))

    def info(self, text: str, scope=None) -> None:
        self.calls.append(("info", text))

    def confirm_choice(self, prompt: str, valid: str, hint: str, detail=None,
                       descriptions=None, content=None, scope=None) -> str:
        self.calls.append(("confirm", prompt))
        return "y"

    def ask_form(self, questions: list[dict], scope=None) -> list[str]:
        self.calls.append(("ask_form",))
        return [""] * len(questions)


class BrokenAskRenderer(FakeRenderer):
    """confirm_choice 抛异常的假后端（模拟面板装配失败等）。"""

    def confirm_choice(self, prompt: str, valid: str, hint: str, detail=None,
                       descriptions=None, content=None, scope=None) -> str:
        raise RuntimeError("面板炸了")


@pytest.fixture(autouse=True)
def _isolate_title(monkeypatch):
    """复位单例、放行开关与 tty 判定，并把副作用挡在测试进程之外。"""
    title.reset()
    monkeypatch.setattr(title.config, "load_terminal_title", lambda: True)
    monkeypatch.setattr(title, "stdout_is_tty", lambda: True)
    monkeypatch.setattr(title, "write_terminal_control", lambda seq: None)  # 不写真实终端
    monkeypatch.setattr(title.atexit, "register", lambda *args, **kwargs: None)
    monkeypatch.setattr(title.signal, "signal", lambda *args, **kwargs: None)
    monkeypatch.setattr(title.signal, "getsignal", lambda sig: signal.SIG_DFL)
    yield
    title.reset()


def _make_presenter(sink=None, workspace="smithcode"):
    p = title.TerminalTitlePresenter(workspace=workspace)
    if sink is not None:
        p.bind_sink(sink)
    return p


# ---------- 状态与净化 ----------


def test_compose_forms():
    state = title.TitleState(workspace="smithcode")
    assert state.compose() == "Smith · smithcode"
    state.title = "重构会话管理"
    assert state.compose() == "Smith · 重构会话管理"
    state.busy = 1
    assert state.compose() == "◐ Smith · 重构会话管理"
    state.busy = 0
    state.workspace = ""
    state.title = ""
    assert state.compose() == "Smith"


def test_compose_waiting_beats_busy():
    """等待用户输入优先于运行中：任务是停住的，`!` 要盖住 `◐`。"""
    state = title.TitleState(workspace="smithcode")
    state.busy = 1
    state.open_prompts["p1"] = _prompt("p1")
    assert state.waiting == 1
    assert state.compose() == "! Smith · smithcode"
    state.open_prompts.clear()
    assert state.compose() == "◐ Smith · smithcode"


def test_sanitize_title_strips_control_chars():
    """标题可能来自模型生成：ESC / BEL 必须剥掉，否则等于往终端注入转义序列。"""
    cleaned = title.sanitize_title("a\x07b\x1b]0;evil\x07")
    assert "\x07" not in cleaned and "\x1b" not in cleaned
    assert cleaned == "a b ]0;evil"


def test_sanitize_title_truncates():
    assert len(title.sanitize_title("x" * 200)) == title.TITLE_MAX


# ---------- 事件 → 写入 ----------


def test_enable_writes_default_title_and_pushes_stack():
    sink = Recorder()
    p = _make_presenter(sink)
    p.on_title_changed("重构会话管理")  # 早于 enable：只暂存，不写
    assert sink.writes == []
    p.enable()
    assert sink.writes == ["\x1b[22;2t", "\x1b]0;Smith · 重构会话管理\x07"]


def test_title_changes_dedupe_and_track_busy():
    sink = Recorder()
    p = _make_presenter(sink)
    p.enable()
    p.on_title_changed("X")
    p.on_title_changed("X")  # 相同标题不重复写
    p.on_turn_started()
    p.on_turn_finished()
    assert sink.writes == [
        "\x1b[22;2t",
        "\x1b]0;Smith · smithcode\x07",
        "\x1b]0;Smith · X\x07",
        "\x1b]0;◐ Smith · X\x07",
        "\x1b]0;Smith · X\x07",
    ]


def test_busy_counter_survives_nested_turns():
    """run_with_goal 的多回合：内层结束、外层仍在跑时标题不能熄灭。"""
    sink = Recorder()
    p = _make_presenter(sink)
    p.enable()
    p.on_turn_started()
    p.on_turn_started()
    p.on_turn_finished()  # 内层 run() 结束
    assert sink.writes[-1] == "\x1b]0;◐ Smith · smithcode\x07"
    p.on_turn_finished()
    assert sink.writes[-1] == "\x1b]0;Smith · smithcode\x07"


def test_waiting_survives_overlapping_prompts():
    """提问可能重叠：先结束的那次不能提前摘掉 `!`（按 id 配对，而不是计数）。"""
    sink = Recorder()
    p = _make_presenter(sink)
    p.enable()
    p.on_turn_started()
    p.on_agent_event(_prompt("outer"))
    p.on_agent_event(_prompt("inner"))
    assert sink.writes[-1] == "\x1b]0;! Smith · smithcode\x07"
    p.on_agent_event(_finished("inner"))  # 内层先结束
    assert sink.writes[-1] == "\x1b]0;! Smith · smithcode\x07"  # 外层还在等，不熄灭
    p.on_agent_event(_finished("outer"))
    assert sink.writes[-1] == "\x1b]0;◐ Smith · smithcode\x07"  # 回到运行态
    p.on_turn_finished()
    assert sink.writes[-1] == "\x1b]0;Smith · smithcode\x07"


def test_prompt_finished_with_unknown_id_is_ignored():
    """没见过的 id 结束事件：忽略即可（不再有"计数被压成负数"这种状态）。"""
    sink = Recorder()
    p = _make_presenter(sink)
    p.enable()
    p.on_agent_event(_finished("不存在"))
    assert sink.writes[-1] == "\x1b]0;Smith · smithcode\x07"


def test_empty_title_falls_back_to_workspace():
    """`/new` 清空标题后回退到工作区目录名（agent 发的 title_changed("")）。"""
    sink = Recorder()
    p = _make_presenter(sink)
    p.enable()
    p.on_title_changed("旧标题")
    p.on_title_changed("")
    assert sink.writes[-1] == "\x1b]0;Smith · smithcode\x07"


# ---------- 开关与生命周期 ----------


def test_enable_is_idempotent():
    sink = Recorder()
    p = _make_presenter(sink)
    p.enable()
    p.enable()  # 组合根 + 宿主各接一次：不得重复压栈
    assert sink.writes.count("\x1b[22;2t") == 1


def test_disabled_by_config_writes_nothing(monkeypatch):
    monkeypatch.setattr(title.config, "load_terminal_title", lambda: False)
    sink = Recorder()
    p = _make_presenter(sink)
    p.enable()
    p.on_title_changed("X")
    p.on_turn_started()
    p.release()
    assert sink.writes == []


def test_disabled_without_tty_writes_nothing(monkeypatch):
    monkeypatch.setattr(title, "stdout_is_tty", lambda: False)
    sink = Recorder()
    p = _make_presenter(sink)
    p.enable()
    p.on_title_changed("X")
    p.release()
    assert sink.writes == []


def test_release_pops_stack_once_via_raw_sink(monkeypatch):
    """退出恢复：只出栈、只弹一次；且走真实终端通道（atexit 阶段 driver 已停）。"""
    raw = Recorder()
    monkeypatch.setattr(title, "write_terminal_control", raw)
    sink = Recorder()
    p = _make_presenter(sink)
    p.enable()
    p.release()
    p.release()
    assert raw.writes == ["\x1b[23;2t"]
    # 注入通道里只有压栈与标题，没有空标题（清空会把原标题抹掉）
    assert sink.writes == ["\x1b[22;2t", "\x1b]0;Smith · smithcode\x07"]


def test_release_without_enable_is_noop():
    sink = Recorder()
    p = _make_presenter(sink)
    p.release()
    assert sink.writes == []


# ---------- 退出钩子 ----------


def test_exit_hooks_register_atexit_and_skip_sigint(monkeypatch):
    registered, seen = [], []
    monkeypatch.setattr(title.atexit, "register", registered.append)
    monkeypatch.setattr(title.signal, "signal", lambda sig, handler: seen.append(sig))
    title._install_exit_hooks()
    assert registered == [title._release_at_exit]
    # SIGINT 必须留给 cli._wait_for_task 的「第一次取消、第二次退出」语义
    assert signal.SIGINT not in seen
    expected = {signal.SIGTERM, getattr(signal, "SIGHUP", signal.SIGTERM)}
    assert set(seen) == expected


def test_exit_hooks_install_once(monkeypatch):
    registered = []
    monkeypatch.setattr(title.atexit, "register", registered.append)
    title._install_exit_hooks()
    title._install_exit_hooks()
    assert len(registered) == 1


def test_chain_handler_calls_previous(monkeypatch):
    calls = []
    monkeypatch.setattr(title, "_release_at_exit", lambda: calls.append("release"))
    handler = title._chain_handler(
        signal.SIGTERM, lambda signum, frame: calls.append(("prev", signum))
    )
    handler(signal.SIGTERM, None)
    assert calls == ["release", ("prev", signal.SIGTERM)]


def test_chain_handler_reraises_default_signal(monkeypatch):
    """原处理器是默认行为时：复位为默认并重新发信号，保住退出码语义。"""
    calls = []
    monkeypatch.setattr(title, "_release_at_exit", lambda: calls.append("release"))
    monkeypatch.setattr(title.signal, "signal", lambda sig, h: calls.append(("set", sig, h)))
    monkeypatch.setattr(title.os, "kill", lambda pid, sig: calls.append(("kill", sig)))
    title._chain_handler(signal.SIGTERM, signal.SIG_DFL)(signal.SIGTERM, None)
    assert calls == [
        "release",
        ("set", signal.SIGTERM, signal.SIG_DFL),
        ("kill", signal.SIGTERM),
    ]


def test_chain_handler_respects_ignored_signal(monkeypatch):
    calls = []
    monkeypatch.setattr(title, "_release_at_exit", lambda: calls.append("release"))
    title._chain_handler(signal.SIGTERM, signal.SIG_IGN)(signal.SIGTERM, None)
    assert calls == ["release"]


# ---------- 装配：attach 只做「接管标题 + 订阅事件」 ----------


def test_attach_returns_the_same_backend_and_enables_title(monkeypatch):
    """attach 不再包一层装饰器：返回原后端对象，同时接管窗口标题。

    （原来返回的是 `Relay(inner, presenter)`；等待/标题事件都有正式事件通道后，
    中间层被删除——调用方拿到的就是自己传进去的那个渲染后端。）
    """
    sink = Recorder()
    inner = FakeRenderer()
    returned = title.attach(inner, sink=sink, workspace="proj")
    assert returned is inner
    assert sink.writes == ["\x1b[22;2t", "\x1b]0;Smith · proj\x07"]


def test_attach_subscribes_presenter_to_agent_events(monkeypatch):
    """传了 agent 就订阅：标题/忙闲/等待三类事件都能驱动标题。"""
    from smithcode.agent.events import TitleChanged

    sink = Recorder()
    inner = FakeRenderer()
    monkeypatch.setattr(renderer_module, "_current", None, raising=False)
    renderer_module.set_renderer(inner)  # 迁移桥送达的是「当前后端」
    agent = Agent(session=Session(), persist=False)
    title.attach(inner, sink=sink, workspace="proj", agent=agent)

    agent.emit(TitleChanged("新标题"))

    assert sink.writes[-1] == "\x1b]0;Smith · 新标题\x07"
    assert inner.calls == [("title", "新标题")]  # 迁移桥照常送达后端


def test_presenter_ignores_events_it_does_not_handle():
    """不认识的事件不得抛异常（回归：新增事件曾让每轮都误报「输出中断」）。

    旧实现用 `getattr(self._presenter, "on_" + name)` 转发，未实现的方法直接抛
    `AttributeError`。现在按类型分派，未知事件天然是空操作——这条锁住该性质，
    顺带确认已知事件仍照常处理。
    """
    from smithcode.agent.status import StatusChanged

    sink = Recorder()
    p = _make_presenter(sink)
    p.enable()

    p.on_agent_event(StatusChanged(kind="retry", text="重试 1/3"))  # 不关心，不报错
    p.on_agent_event(object())  # 完全未知
    p.on_turn_started()

    assert sink.writes[-1] == "\x1b]0;◐ Smith · smithcode\x07"


# ---------- 阻塞（等待用户输入）：交互桥事件对 → 标题 ----------


def test_prompt_events_mark_waiting_in_title():
    """提问期间标题打 `!`，作答后回到运行态——切走窗口再回来能看出卡在等人。

    等待态由 `InteractionBridge` 在提问进出两侧发 `PromptStarted` /
    `PromptFinished`（id 配对），呈现器订阅这两个事件（不再经 Relay 拦截 ask）。
    """
    sink = Recorder()
    p = _make_presenter(sink)
    p.enable()
    inner = FakeRenderer()
    bridge = InteractionBridge(p.on_agent_event)
    p.on_turn_started()

    answer = bridge.request(
        PromptRequest(kind="permission", title="允许执行 x?"),
        lambda: inner.confirm_choice("允许执行 x?", "yn", "y / n"),
    )

    assert answer == "y"
    assert sink.writes == [
        "\x1b[22;2t",
        "\x1b]0;Smith · smithcode\x07",
        "\x1b]0;◐ Smith · smithcode\x07",
        "\x1b]0;! Smith · smithcode\x07",
        "\x1b]0;◐ Smith · smithcode\x07",
    ]
    assert bridge.open_prompts == {}  # 成对收口
    assert inner.calls == [("confirm", "允许执行 x?")]


def test_prompt_waiting_cleared_when_ask_raises():
    """确认框异常退出也要摘掉等待态——否则标题永远停在 `!`。"""
    sink = Recorder()
    p = _make_presenter(sink)
    p.enable()
    inner = BrokenAskRenderer()
    bridge = InteractionBridge(p.on_agent_event)
    p.on_turn_started()  # 任务在跑：等待态摘掉后应回到 `◐`

    with pytest.raises(RuntimeError):
        bridge.request(
            PromptRequest(kind="permission", title="允许执行 x?"),
            lambda: inner.confirm_choice("允许执行 x?", "yn", "y / n"),
        )

    assert sink.writes[-1] == "\x1b]0;◐ Smith · smithcode\x07"
    assert bridge.open_prompts == {}


def test_enable_title_is_separable_and_idempotent():
    """拆出的 `enable_title()` 独立可用（幂等：重复调用不重复压栈）。"""
    sink = Recorder()
    first = title.enable_title(sink=sink, workspace="proj")
    second = title.enable_title(sink=sink, workspace="proj")
    assert first is second is title.presenter()
    assert sink.writes == ["\x1b[22;2t", "\x1b]0;Smith · proj\x07"]


def test_attach_equals_enable_title_for_side_effects():
    """`attach` 的副作用就是 `enable_title()`：写入序列逐字节一致。"""
    sink_a, sink_b = Recorder(), Recorder()
    inner_a, inner_b = FakeRenderer(), FakeRenderer()
    combined = title.attach(inner_a, sink=sink_a, workspace="proj")
    title.reset()  # 复位单例，手工装配同一场景
    manual = title.enable_title(sink=sink_b, workspace="proj")
    assert combined is inner_a  # 不再包装后端
    assert isinstance(manual, title.TerminalTitlePresenter)
    assert sink_a.writes == sink_b.writes
    assert inner_a.calls == inner_b.calls == []


# ---------- Agent 事件发射 ----------


class FakeLLM:
    """单轮即返回最终回复的假 LLM。"""

    def chat_stream(self, messages, tools=None):
        yield ("message", {"role": "assistant", "content": "最终回复"})


def _install_recording_backend(monkeypatch):
    """把全局渲染后端换成记录器并启用标题呈现器；返回 (inner, sink, presenter)。

    呈现器与 Agent 的连接由调用方用 `_wire()` 建立——这就是 Relay 删除后的装配
    方式：订阅事件（而不是经渲染后端转发）。
    """
    inner = FakeRenderer()
    sink = Recorder()
    presenter = _make_presenter(sink)
    presenter.enable()
    monkeypatch.setattr(renderer_module, "_current", None, raising=False)
    renderer_module.set_renderer(inner)
    return inner, sink, presenter


def _wire(agent, presenter) -> None:
    agent.subscribe(presenter.on_agent_event)


def test_agent_run_emits_turn_events(monkeypatch):
    monkeypatch.setattr("smithcode.agent.LLMClient", FakeLLM)
    inner, sink, presenter = _install_recording_backend(monkeypatch)
    agent = Agent(session=Session(), persist=False)
    _wire(agent, presenter)
    asyncio.run(agent.run("你好"))
    assert inner.calls == [("started",), ("finished", "ok")]
    # 收尾回到空闲态（回退名取自工作区目录名，故不断言具体字符串）
    assert sink.writes[-1].startswith("\x1b]0;Smith · ") and sink.writes[-1].endswith("\x07")


def test_agent_new_session_emits_empty_title(monkeypatch):
    inner, sink, presenter = _install_recording_backend(monkeypatch)
    agent = Agent(session=Session(), persist=False)
    _wire(agent, presenter)
    agent.rename_session("旧标题")
    agent.new_session()
    assert ("title", "") in inner.calls
    assert sink.writes[-1].startswith("\x1b]0;Smith · ")  # 回退默认标题


def test_agent_rename_pushes_title_to_terminal(monkeypatch):
    _inner, sink, presenter = _install_recording_backend(monkeypatch)
    agent = Agent(session=Session(), persist=False)
    _wire(agent, presenter)
    assert agent.rename_session("数据库迁移") is True
    assert sink.writes[-1] == "\x1b]0;Smith · 数据库迁移\x07"
