"""会话级命令：/new /save /compact /exit。"""

from .base import CommandResult, register


@register("exit", "退出")
def _exit(ctx):
    # 无输出：告别语由宿主自行处理（REPL 打印「再见!」，TUI 直接结束应用）
    return CommandResult(exit=True)


@register("new", "开启新会话")
def _new(ctx):
    # 重置动作集中在 Agent.new_session（会话级状态一处清空）；命令层只负责反馈与标记
    ctx.agent.new_session()
    return CommandResult(
        text="已开启新会话。",  # 仅 REPL 展示；TUI 以清空聊天区代替，不再追加文本
        style="yellow",
        session_reset=True,   # TUI 清空聊天区与计划侧栏
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
