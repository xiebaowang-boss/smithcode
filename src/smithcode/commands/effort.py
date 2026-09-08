"""思考强度切换命令：`/effort [档位]`。

交互与 `/model` 一致：无参返回选择意图（TUI 居中弹窗 / REPL 提示），
带参直接切换。候选来自本地默认档位列表 `models.DEFAULT_EFFORTS`，
不从远端获取；未配置时的默认档位为 `config.DEFAULT_EFFORT`（high）。
"""

from .. import config
from ..models import DEFAULT_EFFORTS
from .base import CommandChoice, CommandResult, CommandSelect, register


@register("effort", "调整思考强度", usage="/effort [档位]", accepts_args=True, immediate=True)
def _effort(ctx):
    if ctx.args:
        value = ctx.args[0]
        config.REASONING_EFFORT = value
        return CommandResult(text=f"思考强度已切换: {value}", style="green", refresh_status=True)

    current = config.REASONING_EFFORT or config.DEFAULT_EFFORT
    choices = [
        CommandChoice(label=level, value=level, current=(level == current))
        for level in DEFAULT_EFFORTS
    ]
    return CommandResult(
        select=CommandSelect(title="选择思考强度", command="effort", items=choices)
    )
