"""终端窗口标题测试：合成 / 净化 / 事件 → 写入 / 压栈出栈 / 退出钩子 / Relay 转发。

不触摸真实终端：sink 一律换成记录器，退出钩子的 atexit / signal 注册在本文件里
统一打桩（避免测试进程真装信号处理器）。
"""
import signal

import pytest

import smithcode.renderer as renderer_module
from smithcode import title
from smithcode.agent import Agent
from smithcode.llm import RetryState
from smithcode.renderer import Renderer
from smithcode.session import Session


class Recorder:
    """假 sink：记录写出的控制序列。"""

    def __init__(self):
        self.writes = []

    def __call__(self, seq: str) -> None:
        self.writes.append(seq)


class FakeRenderer(Renderer):
    """记录收到的调用，用于断言 Relay 的拦截与透传。"""

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
    state.waiting = 1
    assert state.compose() == "! Smith · smithcode"
    state.waiting = 0
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


def test_waiting_counter_survives_overlapping_asks():
    """确认可能嵌套：先结束的那次不能提前摘掉 `!`。"""
    sink = Recorder()
    p = _make_presenter(sink)
    p.enable()
    p.on_turn_started()
    p.on_waiting_started()
    p.on_waiting_started()
    assert sink.writes[-1] == "\x1b]0;! Smith · smithcode\x07"
    p.on_waiting_finished()
    assert sink.writes[-1] == "\x1b]0;! Smith · smithcode\x07"  # 仍在等，不熄灭
    p.on_waiting_finished()
    assert sink.writes[-1] == "\x1b]0;◐ Smith · smithcode\x07"  # 回到运行态
    p.on_turn_finished()
    assert sink.writes[-1] == "\x1b]0;Smith · smithcode\x07"


def test_waiting_never_goes_negative():
    sink = Recorder()
    p = _make_presenter(sink)
    p.enable()
    p.on_waiting_finished()  # 多余的一次结束不得把计数压成负数
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


# ---------- Relay：解耦与转发 ----------


def test_relay_covers_renderer_api():
    """转发清单必须覆盖基类全部公开方法：以后新增事件方法不会静默漏转发。"""
    public = {name for name in dir(Renderer) if not name.startswith("_")}
    forwarded = set(title.Relay.__dict__)
    assert public, "基类公开方法集合不应为空"
    assert public - forwarded == set()


def test_relay_intercepts_title_events_and_forwards_rest():
    sink = Recorder()
    p = _make_presenter(sink)
    p.enable()
    inner = FakeRenderer()
    relay = title.Relay(inner, p)
    relay.title_changed("新标题")
    relay.turn_started()
    relay.turn_finished("ok")
    relay.info("hi")
    assert "\x1b]0;Smith · 新标题\x07" in sink.writes
    assert "\x1b]0;◐ Smith · 新标题\x07" in sink.writes
    assert inner.calls == [
        ("title", "新标题"),
        ("started",),
        ("finished", "ok"),
        ("info", "hi"),
    ]


def test_attach_returns_relay_and_enables(monkeypatch):
    sink = Recorder()
    inner = FakeRenderer()
    relay = title.attach(inner, sink=sink, workspace="proj")
    assert isinstance(relay, title.Relay)
    relay.turn_started()
    assert sink.writes == [
        "\x1b[22;2t",
        "\x1b]0;Smith · proj\x07",
        "\x1b]0;◐ Smith · proj\x07",
    ]


# ---------- Relay：阻塞（等待用户输入）上报 ----------


def test_relay_marks_waiting_during_confirm():
    """确认期间标题打 `!`，作答后回到运行态——切走窗口再回来能看出卡在等人。"""
    sink = Recorder()
    p = _make_presenter(sink)
    p.enable()
    inner = FakeRenderer()
    relay = title.Relay(inner, p)
    relay.turn_started()
    assert relay.confirm_choice("允许执行 x?", "yn", "y / n") == "y"
    assert sink.writes == [
        "\x1b[22;2t",
        "\x1b]0;Smith · smithcode\x07",
        "\x1b]0;◐ Smith · smithcode\x07",
        "\x1b]0;! Smith · smithcode\x07",
        "\x1b]0;◐ Smith · smithcode\x07",
    ]
    assert inner.calls == [
        ("started",),
        ("waiting_started",),
        ("confirm", "允许执行 x?"),
        ("waiting_finished",),
    ]


def test_relay_marks_waiting_during_ask_form():
    sink = Recorder()
    p = _make_presenter(sink)
    p.enable()
    inner = FakeRenderer()
    relay = title.Relay(inner, p)
    relay.turn_started()
    relay.ask_form([{"question": "选哪个?"}])
    assert "\x1b]0;! Smith · smithcode\x07" in sink.writes
    assert sink.writes[-1] == "\x1b]0;◐ Smith · smithcode\x07"


def test_relay_waiting_cleared_when_ask_raises():
    """确认框异常退出也要摘掉等待态——否则标题永远停在 `!`。"""
    sink = Recorder()
    p = _make_presenter(sink)
    p.enable()
    inner = BrokenAskRenderer()
    relay = title.Relay(inner, p)
    relay.turn_started()
    with pytest.raises(RuntimeError):
        relay.confirm_choice("允许执行 x?", "yn", "y / n")
    assert sink.writes[-1] == "\x1b]0;◐ Smith · smithcode\x07"


# ---------- 纯总线装配（GUI 前端路径） ----------


def test_bus_forwards_to_inner_without_creating_presenter():
    """`bus()` 只建总线：事件照进内层后端，但不创建标题呈现器。"""
    inner = FakeRenderer()
    relay = title.bus(inner)
    relay.title_changed("X")
    relay.turn_started()
    relay.turn_waiting_started()
    relay.turn_waiting_finished()
    relay.turn_finished("ok")
    relay.confirm_choice("允许执行 x?", "yn", "y / n")
    assert inner.calls == [
        ("title", "X"),
        ("started",),
        ("waiting_started",),
        ("waiting_finished",),
        ("finished", "ok"),
        ("waiting_started",),  # confirm_choice 自身的一对
        ("confirm", "允许执行 x?"),
        ("waiting_finished",),
    ]
    assert title._presenter is None  # 没碰标题单例


def test_bus_has_no_terminal_side_effects(monkeypatch):
    """纯总线模式一个字节都不写、也不装退出钩子（GUI 宿主无终端）。"""
    writes, hooks = [], []
    monkeypatch.setattr(title, "write_terminal_control", writes.append)
    monkeypatch.setattr(title.atexit, "register", hooks.append)
    relay = title.bus(FakeRenderer())
    relay.turn_started()
    relay.turn_waiting_started()
    relay.turn_waiting_finished()
    relay.turn_finished()
    assert writes == []
    assert hooks == []


def test_relay_without_presenter_does_not_crash():
    """省略订阅者且 report_title=True（最易踩的组合）：五处喂事件都要短路。"""
    inner = FakeRenderer()
    relay = title.Relay(inner)
    relay.title_changed("X")
    relay.turn_started()
    relay.turn_finished()
    relay.turn_waiting_started()
    relay.turn_waiting_finished()
    relay.confirm_choice("允许执行 x?", "yn", "y / n")
    relay.ask_form([{"question": "选哪个?"}])
    # 显式一对 + confirm_choice 与 ask_form 各自的一对
    assert inner.calls.count(("waiting_started",)) == 3
    assert inner.calls.count(("waiting_finished",)) == 3
    assert ("confirm", "允许执行 x?") in inner.calls
    assert ("ask_form",) in inner.calls


def test_enable_title_is_separable_and_idempotent():
    """拆出的 `enable_title()` 独立可用（幂等：重复调用不重复压栈）。"""
    sink = Recorder()
    first = title.enable_title(sink=sink, workspace="proj")
    second = title.enable_title(sink=sink, workspace="proj")
    assert first is second is title.presenter()
    assert sink.writes == ["\x1b[22;2t", "\x1b]0;Smith · proj\x07"]


def test_attach_equals_bus_plus_enable_title():
    """`attach` 就是两者的组合：写入序列与手工拼装逐字节一致。"""
    sink_a, sink_b = Recorder(), Recorder()
    inner_a, inner_b = FakeRenderer(), FakeRenderer()
    combined = title.attach(inner_a, sink=sink_a, workspace="proj")
    title.reset()  # 复位单例，手工拼装同一场景
    manual = title.Relay(inner_b, title.enable_title(sink=sink_b, workspace="proj"))
    for relay in (combined, manual):
        relay.turn_started()
        relay.turn_waiting_started()
        relay.turn_waiting_finished()
        relay.turn_finished()
    assert sink_a.writes == sink_b.writes
    assert inner_a.calls == inner_b.calls


# ---------- Agent 事件发射 ----------


class FakeLLM:
    """单轮即返回最终回复的假 LLM。"""

    def chat_stream(self, messages, tools=None):
        yield ("message", {"role": "assistant", "content": "最终回复"})


def _install_recording_backend(monkeypatch):
    """把全局渲染后端换成「记录器 + 呈现器」，并返回两者。"""
    inner = FakeRenderer()
    sink = Recorder()
    presenter = _make_presenter(sink)
    presenter.enable()
    monkeypatch.setattr(renderer_module, "_current", None, raising=False)
    renderer_module.set_renderer(title.Relay(inner, presenter))
    return inner, sink


def test_agent_run_emits_turn_events(monkeypatch):
    monkeypatch.setattr("smithcode.agent.LLMClient", FakeLLM)
    inner, sink = _install_recording_backend(monkeypatch)
    agent = Agent(session=Session(), persist=False)
    agent.run("你好")
    assert inner.calls == [("started",), ("finished", "ok")]
    # 收尾回到空闲态（回退名取自工作区目录名，故不断言具体字符串）
    assert sink.writes[-1].startswith("\x1b]0;Smith · ") and sink.writes[-1].endswith("\x07")


def test_agent_new_session_emits_empty_title(monkeypatch):
    inner, sink = _install_recording_backend(monkeypatch)
    agent = Agent(session=Session(), persist=False)
    agent.rename_session("旧标题")
    agent.new_session()
    assert ("title", "") in inner.calls
    assert sink.writes[-1].startswith("\x1b]0;Smith · ")  # 回退默认标题


def test_agent_rename_pushes_title_to_terminal(monkeypatch):
    _inner, sink = _install_recording_backend(monkeypatch)
    agent = Agent(session=Session(), persist=False)
    assert agent.rename_session("数据库迁移") is True
    assert sink.writes[-1] == "\x1b]0;Smith · 数据库迁移\x07"


def test_relay_tolerates_presenter_without_new_event_callbacks():
    """订阅者没有某个事件的回调时不能抛异常——新增事件不得打断任务。

    回归：`Relay.retry_finished` 广播 `on_retry_finished`，而
    `TerminalTitlePresenter` 只实现标题相关回调，`getattr(...)` 直接抛
    `AttributeError`；该异常被 Agent 的流异常处理捕获后，每一轮都被误判成
    `stream_error`（用户侧：每次回复结尾都显示「输出中断」）。
    """
    sink = Recorder()
    p = _make_presenter(sink)
    p.enable()
    inner = FakeRenderer()
    relay = title.Relay(inner, p)
    presenter_api = {name for name in dir(p) if name.startswith("on_")}
    assert "on_retry_started" not in presenter_api  # 前提：标题呈现器不关心重试
    assert "on_retry_finished" not in presenter_api

    state = RetryState(attempt=2, total=3, reason="读取超时", wait=2.0, next_at=0.0)
    relay.retry_started(state, "owner")  # 不得抛 AttributeError
    relay.retry_finished("owner")
    relay.info("继续")

    kinds = [call[0] for call in inner.calls]
    assert "warn" in kinds  # retry_started 照常透传（基类默认降级为一行 warn）
    assert inner.calls[-1] == ("info", "继续")  # 后续事件不受影响
