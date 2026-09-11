"""技能斜杠命令：/skills 选择/查看/刷新、/skill 选择/加载。

`/skills` 无参数直接弹出技能选择框（TUI SelectionPanel，选中自动加载）；
`/skills list` 输出文本列表与扫描诊断，`/skills refresh` 重扫磁盘。
`/skill <名称> [任务]` 直接加载或加载后立即开跑（脚本/非交互场景）。
技能名还会作为动态条目并入 `/` 输入补全（commands/base.complete_commands），
`/技能名 [任务]` 直达与 /skill 等价（见 commands.dispatch 的技能兜底）。
"""
from __future__ import annotations

from .. import skills
from .base import (
    KIND_BLOCK,
    CommandChoice,
    CommandResult,
    CommandSelect,
    register,
)


@register(
    "skills",
    "打开技能选择框（list 文本列表 / refresh 重新扫描）",
    usage="/skills [list|refresh]",
    accepts_args=True,
    immediate=True,
)
def cmd_skills(ctx) -> CommandResult:
    if not ctx.args:
        return _skill_picker()
    if ctx.args[0] == "list":
        return CommandResult(text=skills.status_text(), kind=KIND_BLOCK)
    if ctx.args[0] == "refresh":
        diagnostics = ctx.agent.refresh_skills()
        text = skills.status_text()
        if diagnostics:
            text += f"\n\n本次扫描产生 {len(diagnostics)} 条诊断。"
        return CommandResult(text=text, kind=KIND_BLOCK)
    return CommandResult(text="用法: /skills [list|refresh]", style="yellow")


@register(
    "skill",
    "加载指定技能（无参数弹出选择框）",
    usage="/skill [名称] [任务]",
    accepts_args=True,
)
def cmd_skill(ctx) -> CommandResult:
    if not ctx.args:
        return _skill_picker()
    name = ctx.args[0]
    message = skills.activate(name, by="user")
    if message.startswith("错误:"):
        return CommandResult(text=message, style="red")
    # 激活成功保持静默（不打印"已加载技能"提示）；带任务时由宿主把用户输入
    # 原文（含技能指令）回显为消息，聊天区看到的与用户实际输入一致
    task = " ".join(ctx.args[1:]).strip()
    if task:
        return CommandResult(start_task=task, echo_input=True)
    return CommandResult()


def _skill_picker() -> CommandResult:
    """技能选择意图：列出全部可用技能（含仅手动加载的），选中后重分发 /skill <名称>。

    没有可用技能时同样返回空选择框（不打印创建路径等提示文字）。
    """
    active = set(skills.active_names())
    items = []
    for skill in skills.all_skills():
        if skill.disabled:
            continue
        tags = []
        if skill.name in active:
            tags.append("已激活")
        if not skill.model_invocable:
            tags.append("仅手动")
        label = skill.name + (f"（{'，'.join(tags)}）" if tags else "")
        items.append(
            CommandChoice(
                label=label,
                value=skill.name,
                description=skill.description[:80],
                current=skill.name in active,
            )
        )
    return CommandResult(select=CommandSelect(title="选择技能", command="skill", items=items))
