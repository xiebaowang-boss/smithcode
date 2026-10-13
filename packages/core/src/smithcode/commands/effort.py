"""思考强度切换命令：`/effort [档位]`。

交互与 `/model` 一致：无参返回选择意图（TUI 居中弹窗 / REPL 提示），
带参直接切换。候选来自本地默认档位列表 `models.DEFAULT_EFFORTS`，
不从远端获取；未配置时的默认档位为 `config.DEFAULT_EFFORT`（high）。
"""

from .. import config
from ..llm.models import DEFAULT_EFFORTS
from .base import CommandChoice, CommandResult, CommandSelect, register


def _current_effort(agent) -> str:
    """本会话有效的思考强度：Agent 有会话覆盖时读它，否则回退全局。"""
    effective = getattr(agent, "effective_effort", None)
    if callable(effective):
        return effective()
    return config.REASONING_EFFORT or config.DEFAULT_EFFORT


@register("effort", "调整思考强度", usage="/effort [档位]", accepts_args=True, immediate=True)
def _effort(ctx):
    if ctx.args:
        value = ctx.args[0]
        setter = getattr(ctx.agent, "set_session_effort", None)
        if callable(setter):
            # 会话级切换：只改本会话的覆盖（事件由 Agent 发，含日志）
            setter(value)
        else:
            config.set_effort(value)
        # 静默切换：反馈由底栏「思考强度」刷新承担，不再打印提示行
        return CommandResult(refresh_status=True)

    current = _current_effort(ctx.agent)
    choices = [
        CommandChoice(label=level, value=level, current=(level == current))
        for level in DEFAULT_EFFORTS
    ]
    return CommandResult(
        select=CommandSelect(title="选择思考强度", command="effort", items=choices)
    )
