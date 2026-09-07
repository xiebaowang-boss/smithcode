"""todo_write 工具：任务拆分与分步骤执行的状态机。

模型收到多步任务时先调用它提交全量步骤清单，此后每完成一步更新一次。
清单写入 plan 模块的会话级状态，由 Agent 渲染到终端，/plan 命令随时查看。
"""
from .. import plan
from .base import register


@register(
    {
        "name": "todo_write",
        "describe": lambda args: f"plan ({len(args.get('todos') or [])} 步)",
        "description": "创建并维护任务拆分后的步骤清单（分步骤执行的状态机）。"
        "参数传全量最新清单（不是增量），状态取 pending（未开始）/ in_progress（进行中，"
        "同一时刻仅一个）/ completed（已完成）/ cancelled（不再需要）。"
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
                            "content": {
                                "type": "string",
                                "description": "一步任务的简短描述",
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
                        "required": ["content", "status"],
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