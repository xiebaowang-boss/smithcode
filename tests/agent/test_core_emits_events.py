"""守卫：核心（agent/*.py）只发事件，不直接调渲染后端。

这是方案的核心不变量：前端要么订阅事件，要么由「迁移桥」（`renderer_bridge.py`）
把事件翻回旧渲染器调用；一旦核心又开始直接调渲染器，前端就被两套渠道同时驱动，
"只发事件"的承诺名存实亡（而且这类回归不会让任何功能测试变红）。

静态扫描而不是运行时断言：漏掉的那处可能只在特定分支才走到。
"""

from __future__ import annotations

import re
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[2] / "src" / "smithcode" / "agent"
BRIDGE = "renderer_bridge.py"

# 视觉输出方法：正文流 / 工具块 / 计划 / 通知 / 标题 / 回合与重试态
VISUAL_METHODS = (
    "stream", "stream_done", "tool_call", "tool_preview", "tool_result", "plan",
    "info", "warn", "error", "success", "title_changed", "turn_started",
    "turn_finished", "turn_waiting_started", "turn_waiting_finished",
    "retry_started", "retry_finished",
)
CALL = re.compile(
    r"renderer\.current\(\)\.(" + "|".join(sorted(VISUAL_METHODS, key=len, reverse=True)) + r")\b"
)


def test_core_does_not_call_the_renderer_directly():
    offenders: dict[str, list[str]] = {}
    for path in sorted(AGENT_DIR.rglob("*.py")):
        if path.name == BRIDGE:
            continue  # 桥就是干这个的
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):  # 注释里提到方法名不算
                continue
            match = CALL.search(line)
            if match:
                offenders.setdefault(str(path.relative_to(AGENT_DIR)), []).append(
                    f"{number}: {match.group(1)}"
                )

    assert not offenders, (
        f"核心不得直接调渲染后端（改为发事件，见 events.py / renderer_bridge.py）：{offenders}"
    )


def test_bridge_covers_every_visual_event():
    """桥要认得核心发的事件：漏一种就会在终端上无声消失。"""
    source = (AGENT_DIR / BRIDGE).read_text(encoding="utf-8")
    for event in ("MessageUpdate", "MessageEnd", "ToolStart", "ToolPreview",
                  "ToolEnd", "PlanUpdate", "Notice", "TitleChanged",
                  "TurnStart", "TurnEnd"):
        assert f"case {event}(" in source, f"RendererBridge 没有处理 {event}"
