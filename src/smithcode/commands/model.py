"""模型切换命令：`/model [名称]`。

无参数时返回选择意图，由宿主弹出候选列表（TUI 居中弹窗 / REPL 提示）；
带参数时直接切换。候选来自 Agent 持有的模型目录 `agent.models`
（外部配置优先，未配置时由启动期远端 `/models` 拉取填充）。
"""

from .. import config
from .base import CommandChoice, CommandResult, CommandSelect, register


@register("model", "切换模型", usage="/model [名称]", accepts_args=True, immediate=True)
def _model(ctx):
    if ctx.args:
        name = ctx.args[0]
        config.MODEL = name
        # 标题失败常与当前模型相关（不按 JSON 输出、拒标题请求）：换模型后把
        # 耗尽的计数归零，下一轮正常结束即再试，不再继续沉默
        reset = getattr(ctx.agent, "reset_title_attempts", None)
        if callable(reset):
            reset()
        return CommandResult(text=f"已切换模型: {name}", style="green", refresh_status=True)

    choices = [
        CommandChoice(label=name, value=name, current=(name == config.MODEL))
        for name in ctx.agent.models.list()
    ]
    if len(choices) <= 1:
        return CommandResult(
            text=(
                "没有可切换的候选模型。\n"
                "可在 config.toml 的 [provider] 下添加 models = [\"模型A\", \"模型B\"]，"
                "或确认接口支持 /models 后重试；也可直接 /model <名称> 切换。"
            ),
            style="yellow",
        )
    return CommandResult(
        select=CommandSelect(title="选择模型", command="model", items=choices)
    )
