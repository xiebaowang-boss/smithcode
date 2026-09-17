"""技能斜杠命令：`/skills` 选择/查看/刷新（技能名本身即命令，见下）。

`/skills` 无参数直接弹出技能选择框（TUI SelectionPanel，选中即加载并开跑）；
`/skills list` 输出文本列表与扫描诊断，`/skills refresh` 重扫磁盘。
技能名作为动态命令直达：`/技能名 [任务]` 由 commands.dispatch 的兜底分发进来，
载荷作为一条 user 消息进会话历史（无任务时它本身就是本轮 user 消息，加载后
立即开跑一回），带任务时随后再发起任务；技能名同时并入 `/` 输入补全。与内置
命令重名的技能不进命令面（分发时内置命令优先），只能由模型用 use_skill 加载。
"""
from __future__ import annotations

from .. import skills
from .base import (
    KIND_BLOCK,
    CommandChoice,
    CommandResult,
    CommandSelect,
    get_command,
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


def load_skill(name: str, task: str) -> CommandResult:
    """加载技能并决定载荷的投递方式（`/技能名 [任务]` 直达的实现）。

    首次加载：载荷作为一条 user 消息注入会话历史；有任务时随后再发起任务消息，
    无任务时载荷本身就是本轮 user 消息（即加载后立即开跑一回）。已加载：不重复
    注入（幂等），有任务直接开跑，无任务只提示。

    载荷只进 `inject_history` / `start_task`，由宿主在会话就绪时写入——命令层
    不直接碰 session（运行中整体跳过，不留孤儿消息）。
    """
    text = skills.activate(name, by="user")
    if text.startswith("错误:"):
        return CommandResult(text=text, style="red")
    if skills.render.is_payload(text):  # 首次加载：正文进对话历史
        notice = f"已加载技能 {name}"
        if task:
            return CommandResult(
                text=notice, style="green",
                inject_history=[("user", text)], start_task=task, echo_input=True,
            )
        return CommandResult(text=notice, style="green", start_task=text, echo_input=True)
    if task:  # 已加载：不重复注入
        return CommandResult(text=text, start_task=task, echo_input=True)
    return CommandResult(text=text, style="yellow")


def _skill_picker() -> CommandResult:
    """技能选择意图：列出可手动加载的技能，选中后按技能名直达（`/<技能名>`）。

    command 留空 = 宿主按 `/<value>` 重新分发（技能名即命令）。与内置命令重名的
    技能不进列表——分发时内置命令优先，选中只会执行内置命令；没有可用技能时
    同样返回空选择框（不打印创建路径等提示文字）。
    """
    active = set(skills.active_names())
    items = []
    for skill in skills.all_skills():
        if skill.disabled or get_command(skill.name) is not None:
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
    return CommandResult(select=CommandSelect(title="选择技能", items=items))
