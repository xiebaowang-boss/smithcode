"""task 工具：把子任务派发给隔离子代理执行，只把最终报告回传主对话。

真正的执行由 `Agent._preflight` 特判接管（需要父 Agent 引用构造子 Agent 并
走统一的预检/调度流程）；本模块只负责 schema 注册、动态同步与兜底。
schema 同步对齐 `use_skill` 的手法：类型集合变化时原地改写 enum 与描述，
无可用类型（或 [subagents].enabled=false）时隐藏工具。
"""
from __future__ import annotations

from .. import subagents
from .base import all_schemas, register, set_hidden

_BASE_DESCRIPTION = (
    "派发一个隔离子任务给专用子代理执行：子代理在独立上下文中自主调用工具完成任务，"
    "只把最终报告回传，中间过程不占用主对话上下文。"
    "何时使用：开放式多轮搜索且只需要结论（如「找出 X 的实现位置并总结」）；"
    "两个以上互相独立的只读调查（同一回复里多次调用本工具会并发执行）；"
    "预计产生大量工具输出、只留结论更划算的任务。"
    "何时不用：已知目标文件的直接读取；单步小改动；需要用户决策的事——"
    "自己几轮工具就能完成的任务不值得多一层开销。"
    "prompt 必须自包含（目标、范围、已知线索、期望返回的格式）——子代理看不到主对话；"
    "报告是它唯一的产出，不要重复执行同样的调查。"
)


def sync_schema() -> None:
    """按当前子代理类型集合同步 task 工具的 schema（enum + 类型说明 + 可用性）。"""
    specs = subagents.all_specs()
    names = [s.name for s in specs]
    for schema in all_schemas():
        if schema.get("name") != "task":
            continue
        schema["parameters"]["properties"]["subagent_type"]["enum"] = names
        hints = "；".join(f"{s.name}: {s.description}" for s in specs)
        schema["description"] = _BASE_DESCRIPTION + (f" 可用类型：{hints}。" if hints else "")
        break
    set_hidden("task", not names)


@register(
    {
        "name": "task",
        "pattern_arg": "subagent_type",  # 权限规则可按类型配置（task = { explore = "allow" }）
        "describe": lambda args: (
            f"task {args.get('subagent_type', 'general')}: "
            f"{args.get('description', '') or args.get('prompt', '')}"
        ),
        "display": "block",
        "serial": False,  # 静态默认；串行/并行由 _preflight 按类型只读性动态判定
        "description": _BASE_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "任务的 3-5 个词的短名，用于终端展示",
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "完整、自包含的任务说明：目标、范围、已知线索、"
                        "期望的报告格式（子代理看不到主对话，信息不足它只能基于假设推进）"
                    ),
                },
                "subagent_type": {
                    "type": "string",
                    "enum": ["explore", "general"],  # 注册后由 sync_schema 动态同步
                    "description": "子代理类型；只读侦察用 explore，需要写文件的独立子任务用 general",
                },
            },
            "required": ["description", "prompt"],
        },
    }
)
def task(description: str = "", prompt: str = "", subagent_type: str = "general") -> str:
    # 防御性兜底：正常路径由 Agent._preflight 特判接管（这里拿不到父 Agent）
    return "错误: task 工具只能由 Agent 调度器执行。"


# 导入即同步一次（内置类型；Agent.start() 会在发现自定义定义后再同步）
sync_schema()
