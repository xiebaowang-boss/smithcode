"""迁移桥（agent/renderer_bridge.py）：事件 → 既有 Renderer 调用的映射完备性。

这里的核心断言不是「映射对不对」（那是各条用例的事），而是**没有漏项**：
阶段 3 起核心只发事件，任何未被映射的 Renderer 输出方法都会在前端静默消失。
新增 Renderer 输出方法或新增事件类型却忘记接入时，本文件会失败。
"""

from __future__ import annotations

from smithcode.agent.events import (
    AGENT_EVENT_TYPES,
    AgentEnd,
    MessageEnd,
    MessageStart,
    MessageUpdate,
    Notice,
    PlanUpdate,
    TitleChanged,
    ToolEnd,
    ToolPreview,
    ToolStart,
    TurnEnd,
    TurnStart,
)
from smithcode.agent.interactions import PromptFinished, PromptStarted
from smithcode.agent.renderer_bridge import (
    RENDERER_INPUT_METHODS,
    UNMAPPED_EVENTS,
    RendererBridge,
)
from smithcode.agent.status import StatusChanged, StatusCleared
from smithcode.cancel import RunResult
from smithcode.renderer import Renderer

MESSAGE = {"role": "user", "content": "x"}


class RecordingRenderer(Renderer):
    """记录被调用的方法名，不产出任何输出。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, name: str, *args, **kwargs) -> None:
        self.calls.append((name, args, kwargs))

    @property
    def called_methods(self) -> set[str]:
        return {name for name, _args, _kwargs in self.calls}

    def stream(self, kind, chunk):
        self._record("stream", kind, chunk)

    def stream_done(self):
        self._record("stream_done")

    def tool_call(self, line, display="inline", name=""):
        self._record("tool_call", line, display, name)
        return self._next_tool_id()

    def tool_preview(self, tool_id, detail):
        self._record("tool_preview", tool_id, detail)

    def tool_result(self, result, tool_id=None, expand=False):
        self._record("tool_result", result, tool_id, expand)

    def plan(self, summary, rendered, *, created=False, tool_id=None):
        self._record("plan", summary, rendered, created=created, tool_id=tool_id)

    def info(self, text):
        self._record("info", text)

    def success(self, text):
        self._record("success", text)

    def warn(self, text):
        self._record("warn", text)

    def error(self, text):
        self._record("error", text)

    def title_changed(self, title):
        self._record("title_changed", title)

    def retry_started(self, state, owner=None):
        self._record("retry_started", state, owner)

    def retry_finished(self, owner=None):
        self._record("retry_finished", owner)

    def turn_started(self):
        self._record("turn_started")

    def turn_finished(self, status="ok"):
        self._record("turn_finished", status)

    def turn_waiting_started(self):
        self._record("turn_waiting_started")

    def turn_waiting_finished(self):
        self._record("turn_waiting_finished")


def samples() -> list:
    """每个事件类型至少一个样本；Notice 四个级别各一个（各自映射一个方法）。"""
    return [
        MessageStart(message=MESSAGE),
        MessageUpdate(message=MESSAGE, delta="a", kind="content"),
        MessageEnd(message=MESSAGE),
        ToolStart(tool_call_id="t1", name="read_file", line="read a.txt"),
        ToolPreview(tool_call_id="t1", detail="+x"),
        ToolEnd(tool_call_id="t1", result="ok"),
        PlanUpdate(summary="2 步", rendered="1. a", created=True),
        Notice(text="i", level="info"),
        Notice(text="s", level="success"),
        Notice(text="w", level="warning"),
        Notice(text="e", level="error"),
        TitleChanged(title="标题"),
        TurnStart(),
        TurnEnd(status="ok"),
        StatusChanged(kind="retry", text="重试 1/3", payload=object()),
        StatusCleared(kind="retry"),
        PromptStarted(id="p1", kind="permission", title="允许执行？"),
        PromptFinished(id="p1", kind="permission", outcome="answered", value="y"),
        AgentEnd(result=RunResult("ok")),
    ]


def test_bridge_covers_every_renderer_output_method():
    """完备性：Renderer 的公开方法 = 桥覆盖到的 + 明确的输入方法。"""
    renderer = RecordingRenderer()
    bridge = RendererBridge(renderer)
    for event in samples():
        bridge.emit(event)

    public_methods = {
        name
        for name, member in vars(Renderer).items()
        if not name.startswith("_") and callable(member)
    }
    assert public_methods - RENDERER_INPUT_METHODS == renderer.called_methods


def test_bridge_never_calls_input_methods():
    """输入方法（提问 / 确认）方向相反，桥不得触发。"""
    renderer = RecordingRenderer()
    bridge = RendererBridge(renderer)
    for event in samples():
        bridge.emit(event)

    assert renderer.called_methods & RENDERER_INPUT_METHODS == set()


def test_every_event_type_is_exercised_or_declared_unmapped():
    """新增事件类型时必须在本文件给出样本，或显式登记为无渲染映射。"""
    covered = {type(event) for event in samples()}
    assert covered | set(UNMAPPED_EVENTS) == set(AGENT_EVENT_TYPES)


def test_tool_events_reuse_the_legacy_tool_id():
    """ToolStart 拿到的旧式整数 id 必须被 ToolPreview / ToolEnd 复用。"""
    renderer = RecordingRenderer()
    bridge = RendererBridge(renderer)
    bridge.emit(ToolStart(tool_call_id="t1", name="edit_file", line="edit a.txt"))
    assert renderer.calls[-1][0] == "tool_call"

    bridge.emit(ToolPreview(tool_call_id="t1", detail="+x"))
    bridge.emit(ToolEnd(tool_call_id="t1", result="ok"))

    preview_args = next(args for name, args, _kwargs in renderer.calls if name == "tool_preview")
    result_args = next(args for name, args, _kwargs in renderer.calls if name == "tool_result")
    assert preview_args[0] == 1  # 第一个工具调用的旧式 id
    assert result_args[1] == 1


def test_tool_id_mapping_is_released_after_tool_end():
    """配对在 ToolEnd 时释放：已结束的工具再收到预览时不再持有旧 id。"""
    renderer = RecordingRenderer()
    bridge = RendererBridge(renderer)
    for index in range(50):
        bridge.emit(ToolStart(tool_call_id=f"t{index}", name="read_file", line="read"))
        bridge.emit(ToolEnd(tool_call_id=f"t{index}", result="ok"))

    renderer.calls.clear()
    bridge.emit(ToolPreview(tool_call_id="t0", detail="+x"))

    preview_args = next(args for name, args, _kwargs in renderer.calls if name == "tool_preview")
    assert preview_args[0] is None


def test_notice_levels_map_to_distinct_methods():
    renderer = RecordingRenderer()
    bridge = RendererBridge(renderer)
    for level in ("info", "success", "warning", "error"):
        bridge.emit(Notice(text=level, level=level))

    assert renderer.called_methods == {"info", "success", "warn", "error"}


def test_non_retry_status_events_are_noops_for_legacy_renderer():
    """kind="working" 等由阶段 4 接入；当下不得误触任何 Renderer 方法。"""
    renderer = RecordingRenderer()
    bridge = RendererBridge(renderer)
    bridge.emit(StatusChanged(kind="working", text="运行中"))
    bridge.emit(StatusCleared(kind="working"))

    assert renderer.calls == []


def test_retry_status_carries_structured_state_through():
    """retry 的 payload 必须原样透传：TUI 靠 RetryState 渲染编号与倒计时。"""
    state = object()
    renderer = RecordingRenderer()
    RendererBridge(renderer).emit(StatusChanged(kind="retry", text="重试", payload=state))

    _name, args, _kwargs = renderer.calls[0]
    assert args[0] is state


def test_unmapped_events_produce_no_renderer_calls():
    renderer = RecordingRenderer()
    bridge = RendererBridge(renderer)
    bridge.emit(MessageStart(message=MESSAGE))
    bridge.emit(AgentEnd(result=RunResult("ok")))

    assert renderer.calls == []
