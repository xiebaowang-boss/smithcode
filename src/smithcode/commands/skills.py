"""技能斜杠命令：`/skills` 展示/查看/刷新（技能名本身即命令，见下）。

`/skills` 无参数弹出技能展示面板（TUI SelectionPanel，只读：Enter 不确认，
仅 ↑↓ 查看、Esc 关闭；加载技能请用 `/<技能名> [任务]` 直达）；
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
    "查看技能列表（list 文本列表 / refresh 重新扫描）",
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

    首次加载**保持静默**：界面上的命令回显与随后的模型回应已说明发生了什么，
    再加一行回执只是噪音。载荷作为一条 user 消息注入会话历史，有任务时随后再
    发起任务消息，无任务时载荷本身就是本轮 user 消息（即加载后立即开跑一回）。

    重复加载同样**静默开跑、不向用户打印**：不重复注入正文（幂等），改为注入
    一句历史回找引导（`render.recall_notice`，`is_payload` 为假、不占上下文），
    让模型先在历史中找到此前的完整载荷、再按其中步骤执行；有任务时引导先进
    历史、任务文本随后发起，无任务时引导本身就是本轮 user 消息。

    载荷只进 `inject_history` / `start_task`，由宿主在会话就绪时写入——命令层
    不直接碰 session（运行中整体跳过，不留孤儿消息）。
    """
    text = skills.activate(name, by="user")
    if text.startswith("错误:"):
        return CommandResult(text=text, style="red")
    if skills.render.is_payload(text):  # 首次加载：正文进对话历史
        if task:
            return CommandResult(
                inject_history=[("user", text)], start_task=task, echo_input=True,
            )
        return CommandResult(start_task=text, echo_input=True)
    # 重复加载：activate 回 `__already_loaded__:<name>` 哨兵（非载荷）；静默开跑
    skill = skills.get(name)
    notice = skills.render.recall_notice(skill) if skill is not None else text
    if task:
        return CommandResult(
            inject_history=[("user", notice)], start_task=task, echo_input=True,
        )
    return CommandResult(start_task=notice, echo_input=True)


def _skill_picker() -> CommandResult:
    """技能展示意图：列出可手动加载的技能，仅展示、不确认加载。

    readonly 展示面板：Enter（含数字键）不确认，Esc 关闭；加载技能请用
    `/<技能名> [任务]` 直达。与内置命令重名的技能不进列表（内置命令优先）；
    没有可用技能时同样返回空展示面板（不打印创建路径等提示文字）。
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
    return CommandResult(select=CommandSelect(title="技能列表", items=items, readonly=True))
