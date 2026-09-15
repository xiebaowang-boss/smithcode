"""子代理斜杠命令：/agents 查看类型目录、/agents refresh 重新发现。

列出内置与用户/项目定义的子代理类型（来源、只读性、模型、迭代预算），
refresh 重扫磁盘并同步 task 工具 schema（与 Agent.start 同一路径）。
"""
from __future__ import annotations

from .. import config, subagents
from .base import KIND_BLOCK, CommandResult, register


@register(
    "agents",
    "查看子代理类型（refresh 重新发现）",
    usage="/agents [list|refresh]",
    accepts_args=True,
    immediate=True,
)
def cmd_agents(ctx) -> CommandResult:
    if ctx.args and ctx.args[0] == "refresh":
        diagnostics = ctx.agent.refresh_subagents()
        text = render_catalog()
        if diagnostics:
            text += "\n\n本次扫描产生诊断：\n" + "\n".join(f"- {d}" for d in diagnostics)
        return CommandResult(text=text, kind=KIND_BLOCK)
    if ctx.args and ctx.args[0] != "list":
        return CommandResult(text="用法: /agents [list|refresh]", style="yellow")
    return CommandResult(text=render_catalog(), kind=KIND_BLOCK)


def render_catalog() -> str:
    """类型目录的文本列表（/agents 与诊断共用）。"""
    if not config.SUBAGENTS.enabled:
        return "子代理功能已禁用（[subagents].enabled = false）。"
    specs = subagents.all_specs()
    if not specs:
        return "没有可用的子代理类型。"
    scope_labels = {"builtin": "内置", "user": "用户", "project": "项目", "path": "附加"}
    lines = [f"子代理类型（{len(specs)} 个）:"]
    for spec in specs:
        if spec.tools is None:
            tools = "全部工具（含写能力）"
        elif spec.read_only():
            tools = "只读"
        else:
            tools = f"{len(spec.tools)} 个工具"
        model = spec.model or "默认模型"
        turns = spec.max_turns or config.SUBAGENTS.max_turns or "继承"
        lines.append(
            f"  {spec.name}  [{scope_labels.get(spec.source, spec.source)}]"
            f"  {tools} · {model} · ≤{turns} 轮"
        )
        lines.append(f"    {spec.description}")
    return "\n".join(lines)
