"""信息查询命令：/help /plan /usage /context。"""

from .. import config, context, plan
from .base import CommandResult, help_text, register


@register("help", "显示帮助")
def _help(ctx):
    return CommandResult(text=help_text(), kind="block")


@register("plan", "显示当前任务计划（步骤清单）")
def _plan(ctx):
    # render_current(color=True) 内嵌 ANSI 颜色码：REPL 原样打印可着色，
    # TUI 的 ui_block 会解析 ANSI，两端表现一致
    return CommandResult(
        text=f"[计划] {plan.summary()}\n{plan.render_current(color=True)}",
        kind="block",
    )


@register("usage", "显示 token 用量统计")
def _usage(ctx):
    return CommandResult(
        text=ctx.agent.session.usage.summary(),
        kind="block",
        refresh_status=True,
    )


@register("context", "显示上下文占用分布")
def _context(ctx):
    agent = ctx.agent
    return CommandResult(
        text=context.report(
            agent.session.messages,
            config.CONTEXT_TOKEN_BUDGET,
            config.COMPACT_TRIGGER,
            agent.context.last_actual,
            agent.context.compact_count,
        ),
        kind="block",
        refresh_status=True,
    )
