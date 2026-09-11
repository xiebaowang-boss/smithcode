"""goal_update / goal_read 工具：持久目标（/goal）的模型侧闭环。

目标由用户用 /goal 设定，跨回合存活；模型围绕目标推进，只有逐条核验真实
证据后才允许声明完成（goal_update complete），同一阻碍连续多回合无法推进
时才允许声明受阻（blocked）。goal_read 随时拉取权威快照，供上下文压缩后
或不确定现状时使用。目标状态存于 goal 模块的会话级单例，由 Agent 渲染到
终端，/goal 命令随时查看。
"""
from __future__ import annotations

from .. import goal
from .base import register


@register(
    {
        "name": "goal_update",
        "describe": lambda args: f"goal {args.get('status', '?')}",
        "serial": True,
        "description": "更新当前持久目标的状态（仅当系统提示词存在「当前持久目标」时可用）。"
        "status=\"complete\"：仅当逐条核验真实证据（文件内容、命令输出、测试结果）后，"
        "确认目标全部要求已满足、无剩余必需工作时使用；summary 写明核验过的证据。"
        "status=\"blocked\"：仅当同一阻碍连续出现多个回合、且无用户输入无法继续时使用；"
        "summary 写明阻碍与所需输入。目标进行中不要调用本工具；不要因预算将尽、"
        "工作量大或意图完成而标记完成。",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["complete", "blocked"],
                    "description": "目标的新状态：complete（已达成）或 blocked（受阻）",
                },
                "summary": {
                    "type": "string",
                    "description": "核验过的证据（complete）或阻碍与所需输入（blocked）",
                },
            },
            "required": ["status", "summary"],
        },
    }
)
def goal_update(status: str, summary: str = "") -> str:
    if not goal.is_set():
        return "错误: 当前没有持久目标，不要调用 goal_update。"
    if status == "complete":
        current = goal.current()
        goal.complete(summary)
        return (
            f"目标已标记为完成（第 {current.turns}/{current.max_turns} 回合，"
            f"累计约 {current.tokens_used:,} tokens）。"
            "请在最终回复里向用户简要总结成果与核验过的证据。"
        )
    if status == "blocked":
        _, message = goal.try_block(summary)
        return message
    return f"错误: 未知状态 {status!r}（可选 complete / blocked）。"


@register(
    {
        "name": "goal_read",
        "family": "goal_update",
        "describe": lambda args: "goal status",
        "description": "读取当前持久目标的权威快照（目标、状态、回合/预算、证据）。"
        "上下文被压缩后、或不确定目标现状时调用。",
        "parameters": {"type": "object", "properties": {}},
    }
)
def goal_read() -> str:
    return goal.render_status()
