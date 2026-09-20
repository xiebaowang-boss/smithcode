"""守卫：唯一事件通道的**仓库级**不变量（`tests/agent/test_core_emits_events.py` 管核心侧）。

这六条是「架构不零散」的执法线——它们约束的不是某个功能，而是**改动落在哪里**。
每条都配一个理由：破坏了它，问题不会让功能测试变红，只会在某个场景下静默出错。

1. **一个事件目录**：事件类只在 `event/catalog.py` 声明（否则答不出"一共有哪些事件"，
   也就无法枚举落盘/路由规则）。
2. **一个发布口**：扇出只在 `event/bus.py` 实现；其余模块只能 `publish()`。
3. **一个订阅口 + 前端不读会话状态**：前端只认事件与 `Asker` 端口——它一旦 import
   `agent` / `plan` / `goal` / `skills`，就又能绕过事件直接读进程内状态，
   多客户端下必错。
4. **一个会话标识来源**：会话 id 只能由 `session.py` 采用（`config.use_session_id`
   是 `{$session}` 占位符的解析点，不是"当前会话"的真相）。
5. **信封只在事件层构造**：别处 `wrap()` 之外手工拼 `Envelope` 会绕过类型名/版本。
6. **删除即删除**：被删掉的旧架构模块不得复活（没有兼容层、没有转发壳）。
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "smithcode"
FRONTEND_FILES = (SRC / "frontend" / "console.py", SRC / "tui" / "frontend.py")
FRONTEND_DIRS = (SRC / "frontend", SRC / "tui")


def _python_files(root: Path = SRC) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _lines(path: Path) -> list[tuple[int, str]]:
    return [
        (number, line)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if not line.strip().startswith("#")
    ]


# ---------- 1. 一个事件目录 ----------


def test_events_are_declared_only_in_the_catalog():
    offenders = []
    for path in _python_files():
        if path.parent.name == "event":
            continue
        text = path.read_text(encoding="utf-8")
        if "@declare(" in text or "registry.declare(" in text:
            offenders.append(str(path.relative_to(SRC)))
    assert not offenders, f"事件声明只能写在 event/catalog.py：{offenders}"


# ---------- 2. 一个发布口 ----------


def test_fan_out_lives_only_in_the_bus():
    """扇出（遍历订阅者表）只允许在 `event/bus.py`；核心只多一处喂本轮流。

    注意排除项：`AbortSignal` 自己的回调表（取消通知，与事件无关）、
    `extra_subscribers` 这个**参数名**、以及前端对 `bus.subscribe` 的调用。
    """
    # agent/agent.py 例外：它同时喂本轮的事件流；agent/signal.py 例外：那是
    # AbortSignal 自己的取消回调表（取消原语，与事件总线无关）
    allowed = {
        SRC / "event" / "bus.py",
        SRC / "agent" / "agent.py",
        SRC / "agent" / "signal.py",
    }
    offenders = []
    for path in _python_files():
        if path in allowed:
            continue
        for number, line in _lines(path):
            if re.search(r"\.deliver\(|\._listeners\b", line):
                offenders.append(f"{path.relative_to(SRC)}:{number}")
    assert not offenders, (
        "扇出只能在 event/bus.py（发布用 event.publish / bus.publish）：" + str(offenders)
    )


def test_core_publishes_through_the_single_entry():
    """核心不得自己装信封投递：`Agent._emit` 是唯一例外（它还要喂本轮流）。"""
    offenders = []
    for path in _python_files():
        if path.parent.name == "event" or path == SRC / "agent" / "agent.py":
            continue
        for number, line in _lines(path):
            if re.search(r"\bEnvelope\(|_envelope\.wrap\(|event\.wrap\(", line):
                offenders.append(f"{path.relative_to(SRC)}:{number}")
    assert not offenders, f"信封只能在事件层装：{offenders}"


# ---------- 3. 一个订阅口 + 前端不读会话状态 ----------


def test_frontends_only_know_events_and_the_asker_port():
    """前端不得 import 核心或会话状态模块——它们只认事件与询问端口。"""
    forbidden = re.compile(r"from \.\.(agent|plan|goal|skills)\b|from \.\. import .*\b(plan|goal|skills)\b")
    offenders = []
    for path in FRONTEND_FILES:
        for number, line in _lines(path):
            if forbidden.search(line):
                offenders.append(f"{path.relative_to(SRC)}:{number}: {line.strip()}")
    assert not offenders, (
        "前端只认事件与 Asker 端口（呈现所需的一切都该由载荷带来）：" + str(offenders)
    )


#: 入口/宿主层：它们**就是**装配前端的地方，当然可以认识前端实现
HOST_LAYER = ("frontend", "tui", "commands")


def test_sessions_and_agent_do_not_import_frontends():
    """反向依赖同样禁止：核心 / 持久化 / 权限 / 工具不认识任何具体前端实现。"""
    offenders = []
    for path in _python_files():
        rel = path.relative_to(SRC)
        if rel.parts[0] in HOST_LAYER or rel.name in ("cli.py", "wizard.py", "welcome.py"):
            continue
        for number, line in _lines(path):
            if re.search(r"from \.\.?(tui|frontend)\b|import smithcode\.(tui|frontend)", line):
                offenders.append(f"{rel}:{number}")
    assert not offenders, f"核心不得依赖前端实现：{offenders}"


# ---------- 4. 一个会话标识来源 ----------


def test_session_id_is_adopted_in_one_place():
    """`config.use_session_id` 只能由 `session.py` 调用（它才是采纳会话 id 的地方）。"""
    offenders = []
    for path in _python_files():
        # session.py 是唯一采纳点；config.py 只是定义处
        if path.name in ("session.py", "config.py") or path.parent.name == "event":
            continue
        for number, line in _lines(path):
            if "use_session_id(" in line:
                offenders.append(f"{path.relative_to(SRC)}:{number}")
    assert not offenders, f"会话 id 只能由 session.py 采纳：{offenders}"


# ---------- 6. 删除即删除（无兼容层、无转发壳）----------


def test_removed_architecture_modules_stay_removed():
    """旧渲染后端 / 迁移桥 / 事件旧家 / 类型别名 / 状态重名模块不得复活。"""
    gone = (
        SRC / "renderer.py",
        SRC / "agent" / "renderer_bridge.py",
        SRC / "agent" / "events.py",
        SRC / "agent" / "interactions.py",
        SRC / "agent" / "types.py",
        SRC / "agent" / "status.py",
        SRC / "tui" / "bridge.py",
    )
    alive = [str(path.relative_to(SRC)) for path in gone if path.exists()]
    assert not alive, f"这些模块已被架构取代，不得复活：{alive}"

    # 引用也不得留下（导入即失败，属于运行期炸弹）
    pattern = re.compile(
        r"smithcode\.renderer\b|smithcode\.agent\.(events|interactions|types|status|renderer_bridge)\b"
        r"|from \.bridge import|from \.renderer_bridge import"
    )
    offenders = []
    for root in (SRC, SRC.parents[2] / "tests"):
        for path in _python_files(root):
            for number, line in _lines(path):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(root.parent)}:{number}")
    assert not offenders, f"旧模块的引用必须清零：{offenders}"
