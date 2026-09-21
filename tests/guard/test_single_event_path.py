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
        # 只认**装饰器用法**（行首 `@declare(` / `registry.declare(`）：文档与注释里
        # 提到这两个名字不算声明（否则讲规则的文档会把守卫自己触发了）
        if re.search(r"^\s*@?(declare|registry\.declare)\(", text, re.MULTILINE):
            offenders.append(str(path.relative_to(SRC)))
    assert not offenders, f"事件声明只能写在 event/catalog.py：{offenders}"


def test_session_events_declare_an_aggregate():
    """会话事件必须声明 `aggregate="session_id"`：没有它就无法按会话路由/回放。

    多客户端要靠它把事件分给正确的连接，持久日志要靠它分段——漏声明的后果
    不会立刻显形（单会话跑得好好的），所以必须静态锁住。
    """
    from smithcode.event import registry

    offenders = []
    for cls in registry.declared_classes():
        if cls.__module__ != "smithcode.event.catalog":
            continue
        info = registry.meta(cls)
        if info.type.startswith("session.") and info.aggregate != "session_id":
            offenders.append(f"{cls.__name__}（{info.type}）")
    assert not offenders, f"会话事件必须声明 aggregate=session_id：{offenders}"


def test_durable_events_are_serializable():
    """持久事件的载荷必须能 JSON 化（阶段 E 要落盘、将来要跨进程）。

    易失事件允许携带进程内对象（如 RetryState）；持久事件不允许——一旦带上，
    写日志时才会炸，而那时数据已经半写。
    """
    import json
    from dataclasses import fields

    from smithcode.event import catalog, registry
    from smithcode.event.envelope import payload_to_dict

    # 检查点事件例外：它的载荷**就是**投影快照（goal/plan/skills 的字典），
    # 天然是"任意 JSON 形状"——它不是领域事实，不吃这条约束。
    EXEMPT = {"session.checkpointed"}
    offenders = []
    for cls in registry.declared_classes():
        info = registry.meta(cls)
        if cls.__module__ != "smithcode.event.catalog" or not info.durable:
            continue
        if info.type in EXEMPT:
            continue
        for field in fields(cls):
            hint = str(field.type)
            if "Mapping" in hint or "Any" in hint or "object" in hint:
                offenders.append(f"{cls.__name__}.{field.name}: {hint}")
    assert not offenders, f"持久事件的字段必须是可序列化的具体形状：{offenders}"
    # 顺带证明现有持久载荷真的能 JSON 化
    json.dumps(payload_to_dict(catalog.ExecutionSucceeded(status="ok", text="好")))


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


# ---------- 7. 询问只经端口（asked → replied） ----------


def test_asking_goes_through_the_port_only():
    """`frontend.current()` 只能在询问端口里出现：别处提问必须走端口。

    端口负责发 `PromptStarted` / `PromptFinished`、绑定会话标识、以及在收尾时
    统一取消挂起询问。绕过它直接调前端，这三件事就全丢了（远程客户端也收不到）。
    """
    offenders = []
    for path in _python_files():
        if path.parent.name == "event" or path.relative_to(SRC).parts[0] in FRONTEND_DIRS:
            continue
        for number, line in _lines(path):
            if "frontend.current()" in line or "frontend.current().ask" in line:
                offenders.append(f"{path.relative_to(SRC)}:{number}")
    assert not offenders, f"询问只能经 event.asks 端口：{offenders}"


def test_old_blocking_ask_methods_are_gone():
    """旧的四方法（ask_text/ask_choice/ask_form/confirm_choice）不得复活。

    它们是"同步阻塞 + 每个调用点自己拼呈现参数"的形态：前端已被统一成
    `async ask(AskRequest) -> AskAnswer`，复活它们等于同时复活两套询问语义。
    """
    legacy = re.compile(r"(?<![_\w])(ask_text|ask_choice|ask_form|confirm_choice)\(")
    offenders = []
    for path in _python_files():
        if path.name == "test_single_event_path.py":
            continue
        for number, line in _lines(path):
            if line.strip().startswith(("assert", "#", '"', "'")):
                continue
            found = legacy.search(line)
            if found:
                offenders.append(f"{path.relative_to(SRC)}:{number}: {found.group(1)}")
    assert not offenders, f"旧的阻塞询问方法不得复活：{offenders}"


def test_preflight_is_not_pushed_to_a_thread():
    """权限预检不得再下放线程：提问要 await 前端作答（面板必须在循环上）。

    下放线程的后果不只是慢：等待方会卡在非 daemon 池线程里，收尾 join 它 → 进程
    回不到 shell（退出卡死故障的形态）。读文件的 diff 快照仍是例外。
    """
    tools_run = (SRC / "agent" / "tools_run.py").read_text(encoding="utf-8")
    assert "to_thread(self._agent._preflight_safe" not in tools_run
    assert "await self._agent._preflight_safe(" in tools_run


# ---------- 8. 会话级可变状态不得留在模块全局 ----------


def test_config_holds_no_session_state():
    """`config` 只留**启动期只读默认值**：会话级可变状态必须归会话（或沙箱）。

    留在 config 的不是"难看"，是并发下会算错：信任目录会被另一个会话看见、
    临时放行会被交错删掉。已迁移的三项不得复活。
    """
    text = (SRC / "config.py").read_text(encoding="utf-8")
    legacy = ("SESSION_EXTRA_ROOTS", "_WIDENED_ROOTS", "SKILL_READ_ROOTS")
    offenders = [name for name in legacy if name in text]
    assert not offenders, f"会话级状态不得回到 config：{offenders}（见 sandbox.py）"


def test_state_modules_use_contextvars_not_process_pointers():
    """goal / plan / skills 的"活动实例"必须是 ContextVar，不是进程级指针。

    进程级指针会让同进程的两个会话互相覆盖（谁后 bind 谁生效），而这类错**不会**
    报错：只会把 A 的目标显示给 B。探针见 `test_multi_session_isolation.py`。
    """
    targets = {
        "goal.py": "smithcode_goal_state",
        "plan.py": "smithcode_plan_state",
        "skills/state.py": "smithcode_skills_state",
    }
    offenders = []
    for rel, var in targets.items():
        text = (SRC / rel).read_text(encoding="utf-8")
        if f'"{var}"' not in text:
            offenders.append(f"{rel}: 没有 ContextVar {var}")
        if re.search(r"^_active_state\s*=", text, re.MULTILINE):
            offenders.append(f"{rel}: 仍存在进程级 _active_state 指针")
    assert not offenders, f"活动状态必须按上下文解析：{offenders}"


def test_session_scoped_roots_are_only_touched_through_the_sandbox():
    """会话沙箱的字段只能经 `sandbox.current()` 访问，且只有沙箱模块定义它们。"""
    offenders = []
    for path in _python_files():
        if path.name == "sandbox.py" or path.parent.name == "event":
            continue
        for number, line in _lines(path):
            if re.search(r"config\.(SESSION_EXTRA_ROOTS|SKILL_READ_ROOTS|_WIDENED_ROOTS)", line):
                offenders.append(f"{path.relative_to(SRC)}:{number}")
    assert not offenders, f"会话沙箱目录只能经 sandbox.current() 访问：{offenders}"


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
