"""守卫：唯一事件通道的两条不变量。

1. **核心只发事件，不直接调任何前端**——一旦核心又直接调前端，前端就被两套渠道
   同时驱动，"只发事件"的承诺名存实亡（而且这类回归**不会**让任何功能测试变红）。
2. **每种已声明的事件都有前端认得**——新增事件时忘了在某一端处理，它就在那个
   前端上**无声消失**（终端上看不到、TUI 里也看不到），同样不会让测试变红。

静态扫描而不是运行时断言：漏掉的那处可能只在特定分支才走到。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

SRC = Path(__file__).resolve().parents[2] / "src" / "smithcode"
AGENT_DIR = SRC / "agent"

# 旧渲染后端的方法名（正文流 / 工具块 / 计划 / 通知 / 标题 / 回合与重试态）。
# 这些名字不该再出现在核心或任何非前端模块里——呈现只走事件。
VISUAL_METHODS = (
    "stream", "stream_done", "tool_call", "tool_preview", "tool_result", "plan",
    "info", "warn", "error", "success", "title_changed", "turn_started",
    "turn_finished", "turn_waiting_started", "turn_waiting_finished",
    "retry_started", "retry_finished",
)
# 历史形态：`renderer.current().X(...)`（进程级渲染后端，架构上已删除）
LEGACY_CALL = re.compile(
    r"renderer\.current\(\)\.(" + "|".join(sorted(VISUAL_METHODS, key=len, reverse=True)) + r")\b"
)
# 前端层自身（呈现实现）当然要"打印"，不在此列
FRONTEND_DIRS = ("frontend", "tui")


def test_core_does_not_call_a_frontend_directly():
    offenders: dict[str, list[str]] = {}
    for path in sorted(AGENT_DIR.rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):  # 注释里提到方法名不算
                continue
            match = LEGACY_CALL.search(line)
            if match:
                offenders.setdefault(str(path.relative_to(AGENT_DIR)), []).append(
                    f"{number}: {match.group(1)}"
                )

    assert not offenders, (
        f"核心不得直接调前端（改为发事件，见 event/catalog.py）：{offenders}"
    )


def test_no_module_reaches_for_a_global_renderer():
    """全仓不得再有进程级渲染后端：那是多会话/多客户端的硬阻塞。"""
    offenders: list[str] = []
    pattern = re.compile(r"renderer\.(current|set_renderer)|_renderer\b|renderer_bridge")
    for path in sorted(SRC.rglob("*.py")):
        if path.relative_to(SRC).parts[0] in FRONTEND_DIRS:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            if pattern.search(line):
                offenders.append(f"{path.relative_to(SRC)}:{number}")
    assert not offenders, f"不得再有全局渲染后端：{offenders}"


def test_events_are_only_declared_in_the_catalog():
    """事件类只能在 `event/catalog.py` 声明：否则无法回答"一共有哪些事件"。"""
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name in ("catalog.py", "registry.py") or path.parent.name == "event":
            continue
        text = path.read_text(encoding="utf-8")
        if "@declare(" in text or "@event(" in text:
            offenders.append(str(path.relative_to(SRC)))
    assert not offenders, f"事件声明只能写在 event/catalog.py：{offenders}"


def test_both_frontends_handle_every_declared_event():
    """每种已声明的事件都要被两个前端认得——漏一种就在那个前端上无声消失。

    这里只要求"认得"（分派里出现该类型或在 `case _` 之外的显式分支），不要求
    一定有视觉表现：队列/回合事件在终端前端上本就是空操作，但有显式分支意味着
    作者**想过**它该怎么呈现。
    """
    from smithcode.event import catalog, registry

    console = (SRC / "frontend" / "console.py").read_text(encoding="utf-8")
    tui = (SRC / "tui" / "frontend.py").read_text(encoding="utf-8")

    # 事件类名 → 声明（排除纯词汇：类型别名与 QueueItem 不是事件）
    names = [
        cls.__name__ for cls in registry.declared_classes()
    ]
    missing = {
        "console": [n for n in names if f"case {n}(" not in console],
        "tui": [n for n in names if f"case {n}(" not in tui],
    }
    # 允许存在刻意不处理的类型：两侧都必须显式列出，避免"悄悄漏掉"
    intentionally_ignored = {
        # 询问事件对：前端自己就是提问方（弹窗/读 stdin），不需要再呈现一次
        "PromptStarted": ("console", "tui"),
        "PromptFinished": ("console", "tui"),
        # 消息开始/终结、回合边界：前端按内容与状态渲染，不需要单独分支
        "MessageStart": ("console", "tui"),
        "AgentEnd": ("console", "tui"),
        "TurnStart": ("console", "tui"),
        "TurnEnd": ("console", "tui"),
        "QueueChanged": ("console",),
        "QueuedPromptDelivered": ("console",),
        "TitleChanged": ("console",),
        "PlanUpdate": ("tui",),
        "StatusCleared": ("console",),
    }
    for name, sides in intentionally_ignored.items():
        for side in sides:
            if name in missing[side]:
                missing[side].remove(name)
    assert not missing["console"], f"终端前端漏了事件：{missing['console']}"
    assert not missing["tui"], f"TUI 前端漏了事件：{missing['tui']}"
    # 联合与声明必须一致：漏 declare（无法落盘/路由）或漏进联合（类型检查看不见）
    union_members = {cls.__name__ for cls in get_args(catalog.AgentEvent)}
    assert union_members == set(names), (
        f"事件联合与声明不一致：联合多出 {union_members - set(names)}，"
        f"声明多出 {set(names) - union_members}"
    )
