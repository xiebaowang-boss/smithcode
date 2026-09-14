"""use_skill 工具：把技能的完整指令注入系统提示词的「已激活技能」段。

技能只提供文本指令与资源清单；技能目录中的脚本执行仍走 run_command
正常权限确认，不做任何捷径（frontmatter 的 allowed-tools 也不参与授权）。
"""
from __future__ import annotations

from .. import skills
from .base import all_schemas, register, set_hidden


def sync_schema() -> None:
    """按当前技能集合同步 use_skill 的工具 schema（enum 与可用性）。

    无可用技能时隐藏工具（对齐 agentskills.io 指南：没有技能就不要注册空工具）。
    """
    names = [s.name for s in skills.model_skills()]
    for schema in all_schemas():
        if schema.get("name") == "use_skill":
            schema["parameters"]["properties"]["name"]["enum"] = names
            break
    set_hidden("use_skill", not names)


@register(
    {
        "name": "use_skill",
        "pattern_arg": "name",
        "describe": lambda args: f"skill {args.get('name', '?')}",
        "serial": True,
        "description": "加载某个技能的完整指令。当任务与系统提示词「可用技能」中某个技能的"
        "描述相符时，先调用本工具加载其完整指令，再按指令执行；name 必须是可用技能名之一。"
        "已激活的技能无需重复加载。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "技能名（可用技能列表中的 name）"},
            },
            "required": ["name"],
        },
    }
)
def use_skill(name: str) -> str:
    return skills.activate(name, by="model")
