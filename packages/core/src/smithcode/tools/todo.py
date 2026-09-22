"""todo_write / todo_read 工具：任务拆分与分步骤执行的状态机。

模型收到多步任务时先调用 todo_write 提交全量步骤清单，此后每完成一步更新一次。
每项由服务端分配稳定 id：标题（title）不可变，描述/状态/reason 可变。
todo_read 随时拉取当前权威快照（含 id），供模型在更新前确认现状、保住标题不可变。
清单写入 plan 模块的会话级状态，由 Agent 渲染到终端，/plan 命令随时查看。
"""
from __future__ import annotations

from .. import plan
from .base import register


@register(
    {
        "name": "todo_write",
        "describe": lambda args: f"plan ({len(args.get('todos') or [])} 步)",
        "display": "block",
        "serial": True,
        "description": "创建并维护任务拆分后的步骤清单（分步骤执行的状态机）。"
        "参数传全量最新清单（不是增量），状态取 pending（未开始）/ in_progress（进行中，"
        "同一时刻仅一个）/ completed（已完成）/ cancelled（不再需要）。"
        "每项 title 为标题（创建后不可修改，侧边栏只显示它），description 为可选详情（可改）。"
        "更新既有项时务必带上 id（先用 todo_read 获取）以保住标题不可变，无 id 的新项才填 title。"
        "多步任务动手前先调用它列出完整计划，此后每完成一步更新一次。",
        "parameters": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "全量步骤清单，按执行顺序排列",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "服务端分配；更新既有项时务必带上（否则视为新项，标题将重置）",
                            },
                            "title": {
                                "type": "string",
                                "description": "标题，创建后不可修改，仅体现该步骤需要做的事情，5-10个字",
                            },
                            "description": {
                                "type": "string",
                                "description": "可选详情，可修改",
                            },
                            "status": {
                                "type": "string",
                                "enum": list(plan.STATUSES),
                                "description": "该步当前状态",
                            },
                            "reason": {
                                "type": "string",
                                "description": "状态变更原因（标记 completed / cancelled 时建议说明）",
                            },
                        },
                        "required": ["title", "status"],
                    },
                },
            },
            "required": ["todos"],
        },
    }
)
def todo_write(todos: list) -> str:
    todo = plan.current()
    todo.replace(todos)
    return todo.render() or "(空计划)"


@register(
    {
        "name": "todo_read",
        "describe": lambda args: f"plan ({plan.summary()})",
        "family": "todo_write",
        "description": "读取当前任务步骤清单的权威快照（含每项 id）。"
        "多步任务开始前、长时间工具调用后、或计划可能漂移时调用，确认现状后再决定"
        "继续还是用 todo_write 调整。可用 status 过滤只回某状态，或 summary_only 只要进度摘要。",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": list(plan.STATUSES),
                    "description": "只返回该状态的步骤（默认全部）",
                },
                "summary_only": {
                    "type": "boolean",
                    "description": "为 true 时只返回一行进度摘要，不返回清单明细",
                },
            },
        },
    }
)
def todo_read(status: str | None = None, summary_only: bool = False) -> str:
    if summary_only:
        return plan.summary()
    if status:
        return plan.render_current(status=status)
    return plan.render_current()
