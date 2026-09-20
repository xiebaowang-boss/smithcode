"""终端窗口标题测试：合成 / 净化 / 事件 → 写入 / 压栈出栈 / 退出钩子 / 装配与订阅。

不触摸真实终端：sink 一律换成记录器，退出钩子的 atexit / signal 注册在本文件里
统一打桩（避免测试进程真装信号处理器）。
"""
import asyncio
import signal

import pytest

from smithcode import title
from smithcode.agent import Agent
from smithcode.event.asks import AskAnswer, AskRequest
from smithcode.event.catalog import PromptFinished, PromptStarted
from smithcode.session import Session


def _env(payload):
    """把载荷装进信封：订阅者收到的是信封（会话标识由总线注入）。"""
    from smithcode.event.envelope import wrap

    return wrap(payload)


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
    p.on_agent_event(_env(_prompt("outer")))
    p.on_agent_event(_env(_prompt("inner")))
    assert sink.writes[-1] == "\x1b]0;! Smith · smithcode\x07"
    p.on_agent_event(_env(_finished("inner")))  # 内层先结束
    assert sink.writes[-1] == "\x1b]0;! Smith · smithcode\x07"  # 外层还在等，不熄灭
    p.on_agent_event(_env(_finished("outer")))
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


def test_presenter_ignores_events_it_does_not_handle():
    """不认识的事件不得抛异常（回归：新增事件曾让每轮都误报「输出中断」）。

    现在按类型分派，未知事件天然是空操作——这条锁住该性质，顺带确认已知事件仍
    照常处理。订阅者收到的是**信封**，所以这里包一层 `wrap`；连信封都不是的对象
    也不得抛（订阅者可能被别处直接调用）。
    """
    from smithcode.event.catalog import StatusChanged
    from smithcode.event.envelope import wrap

    sink = Recorder()
    p = _make_presenter(sink)
    p.enable()

    p.on_agent_event(wrap(StatusChanged(kind="retry", text="重试 1/3")))  # 不关心，不报错
    p.on_agent_event(object())  # 连信封都不是：getattr 兜底，不得抛
    p.on_turn_started()

    assert sink.writes[-1] == "\x1b]0;◐ Smith · smithcode\x07"


# ---------- 阻塞（等待用户输入）：询问事件对 → 标题 ----------


def _ask_with_presenter(presenter, *, run):
    """在一条总线上提问一次，让呈现器看到 started / finished 事件对。

    提问经询问端口（asked → 前端作答）；`run` 是替身前端的作答（或抛错）。
    """
    from smithcode import frontend
    from smithcode.event import Bus, activate, reset
    from smithcode.event.asks import AskPort
    from smithcode.event.asks import activate as activate_port
    from smithcode.event.asks import reset as reset_port

    class _Asker:
        async def ask(self, request):
            return run()

    bus = Bus(session_id="s")
    bus.subscribe(presenter.on_agent_event)
    token = activate(bus)
    asker_token = frontend.activate(_Asker())
    port_token = activate_port(AskPort(session_id="s"))
    try:
        return asyncio.run(AskPort(session_id="s").ask(
            AskRequest(kind="permission", title="允许执行 x?")
        ))
    finally:
        reset_port(port_token)
        frontend.reset(asker_token)
        reset(token)


def test_prompt_events_mark_waiting_in_title():
    """提问期间标题打 `!`，作答后回到运行态——切走窗口再回来能看出卡在等人。

    等待态由询问事件对（`PromptStarted` / `PromptFinished`，id 配对）驱动：
    呈现器订阅这两个事件，不再经 Relay 拦截 ask。
    """
    sink = Recorder()
    p = _make_presenter(sink)
    p.enable()
    p.on_turn_started()

    answer = _ask_with_presenter(p, run=lambda: AskAnswer(outcome="answered", value="y"))

    assert answer.value == "y"
    assert sink.writes == [
        "\x1b[22;2t",
        "\x1b]0;Smith · smithcode\x07",
        "\x1b]0;◐ Smith · smithcode\x07",
        "\x1b]0;! Smith · smithcode\x07",
        "\x1b]0;◐ Smith · smithcode\x07",
    ]


def test_prompt_waiting_cleared_when_ask_raises():
    """确认框异常退出也要摘掉等待态——否则标题永远停在 `!`。"""
    sink = Recorder()
    p = _make_presenter(sink)
    p.enable()
    p.on_turn_started()  # 任务在跑：等待态摘掉后应回到 ◐

    def boom():
        raise RuntimeError("面板挂了")

    with pytest.raises(RuntimeError):
        _ask_with_presenter(p, run=boom)

    assert sink.writes[-1] == "\x1b]0;◐ Smith · smithcode\x07"


def test_enable_title_is_separable_and_idempotent():
    """`enable_title()` 独立可用（幂等：重复调用不重复压栈）。"""
    sink = Recorder()
    first = title.enable_title(sink=sink, workspace="proj")
    second = title.enable_title(sink=sink, workspace="proj")
    assert first is second is title.presenter()
    assert sink.writes == ["\x1b[22;2t", "\x1b]0;Smith · proj\x07"]


def test_frontend_attach_subscribes_presenter_and_activates_asker():
    """装配入口 `frontend.attach`：订阅事件（含标题呈现器）+ 挂上询问端口。

    这是宿主的**唯一**装配方式（`cli.main` / `SmithTUI.on_mount` 都走它），
    所以这里锁住三件事：事件到达订阅者、询问落到该前端、detach 后复位。
    """
    from smithcode import frontend
    from smithcode.event.catalog import TitleChanged

    class FakeAsker:
        def on_event(self, env): pass  # 订阅事件：本用例只关心装配与询问端口

        async def ask(self, request): return AskAnswer(outcome="answered", value="y")

    sink = Recorder()
    presenter = _make_presenter(sink)
    presenter.enable()
    agent = Agent(session=Session(), persist=False)
    attached = frontend.attach(
        agent.events, FakeAsker(), extra_subscribers=(presenter.on_agent_event,)
    )
    try:
        agent.emit(TitleChanged("新标题"))
        assert sink.writes[-1] == "\x1b]0;Smith · 新标题\x07"
        answer = asyncio.run(frontend.current().ask(AskRequest(kind="permission", title="允许?")))
        assert answer.value == "y"
    finally:
        attached.detach()
    # detach 后回到**终端兜底**（与旧 renderer.current() 一致）；fail-closed 由调用点的
    # confirmations_available() 保证，所以这里断言的是"兜底是终端前端"而不是"一律拒绝"
    from smithcode.frontend.console import ConsoleFrontend

    assert isinstance(frontend.current(), ConsoleFrontend)


# ---------- Agent 事件发射 ----------


class FakeLLM:
    """单轮即返回最终回复的假 LLM。"""

    def chat_stream(self, messages, tools=None):
        yield ("message", {"role": "assistant", "content": "最终回复"})


class RecordingSubscriber:
    """订阅者替身：记下收到的载荷类型（事件只有一个通道）。"""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, env) -> None:
        from smithcode.event.catalog import (
            ExecutionStarted,
            ExecutionSucceeded,
            TitleChanged,
        )

        data = env.data
        if isinstance(data, TitleChanged):
            self.calls.append(("TitleChanged", data.title))
        elif isinstance(data, ExecutionStarted):
            self.calls.append(("ExecutionStarted",))
        elif isinstance(data, ExecutionSucceeded):
            self.calls.append(("ExecutionSucceeded", data.status))


def _install_presenter():
    """启用标题呈现器（+ 一个记录订阅者），返回 (recorder, sink, presenter)。"""
    recorder = RecordingSubscriber()
    sink = Recorder()
    presenter = _make_presenter(sink)
    presenter.enable()
    return recorder, sink, presenter


def _wire(agent, presenter, recorder=None) -> None:
    """装配：呈现器与记录器都订阅 agent 的事件总线（唯一的通道）。"""
    agent.events.subscribe(presenter.on_agent_event)
    if recorder is not None:
        agent.events.subscribe(recorder)


def test_agent_run_emits_execution_events(monkeypatch):
    monkeypatch.setattr("smithcode.agent.LLMClient", FakeLLM)
    recorder, sink, presenter = _install_presenter()
    agent = Agent(session=Session(), persist=False)
    _wire(agent, presenter, recorder)
    asyncio.run(agent.run("你好"))
    assert recorder.calls == [("ExecutionStarted",), ("ExecutionSucceeded", "ok")]
    # 收尾回到空闲态（回退名取自工作区目录名，故不断言具体字符串）
    assert sink.writes[-1].startswith("\x1b]0;Smith · ") and sink.writes[-1].endswith("\x07")


def test_agent_new_session_emits_empty_title(monkeypatch):
    recorder, sink, presenter = _install_presenter()
    agent = Agent(session=Session(), persist=False)
    _wire(agent, presenter, recorder)
    agent.rename_session("旧标题")
    agent.new_session()
    assert ("TitleChanged", "") in recorder.calls
    assert sink.writes[-1].startswith("\x1b]0;Smith · ")  # 回退默认标题


def test_agent_rename_pushes_title_to_terminal(monkeypatch):
    _recorder, sink, presenter = _install_presenter()
    agent = Agent(session=Session(), persist=False)
    _wire(agent, presenter)
    assert agent.rename_session("数据库迁移") is True
    assert sink.writes[-1] == "\x1b]0;Smith · 数据库迁移\x07"
