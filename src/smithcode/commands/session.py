"""会话级命令：/new /save /compact /exit。"""

from .. import config, plan
from .base import CommandResult, register


@register("exit", "退出")
def _exit(ctx):
    # 无输出：告别语由宿主自行处理（REPL 打印「再见!」，TUI 直接结束应用）
    return CommandResult(exit=True)


@register("new", "开启新会话")
def _new(ctx):
    agent = ctx.agent
    agent.session.reset()
    agent.permission.session_rules.clear()
    config.SESSION_EXTRA_ROOTS.clear()
    agent.context.compact_count = 0  # 压缩计数是会话口径，随 /new 清零
    plan.reset()  # 步骤清单是会话口径，随 /new 清零
    return CommandResult(
        text="已开启新会话。",
        style="yellow",
        session_reset=True,   # TUI 清空计划侧栏
        refresh_status=True,  # TUI 刷新状态栏
    )


@register("save", "保存会话记录")
def _save(ctx):
    path = ctx.agent.session.save()
    return CommandResult(text=f"会话已保存到 {path}", style="green")


@register("compact", "手动压缩上下文")
def _compact(ctx):
    if ctx.agent.compact():
        return CommandResult(text="已压缩上下文。", style="green", refresh_status=True)
    return CommandResult(
        text="没有可压缩的上下文（历史太短或摘要未生成）。",
        style="yellow",
        refresh_status=True,
    )
