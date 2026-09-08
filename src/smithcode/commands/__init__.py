"""斜杠命令：导入各命令模块即完成注册，此处统一导出给 REPL / TUI 使用。

新增命令的流程：在 commands/ 下新建文件，用 @register 声明，再到本文件的
导入行加上模块名——分发、/help 文案、两端宿主渲染全部自动生效。
"""

from . import base, effort, info, model, session  # noqa: F401  导入即注册
from .base import (  # noqa: F401
    KIND_BLOCK,
    KIND_LINE,
    Command,
    CommandChoice,
    CommandContext,
    CommandResult,
    CommandSelect,
    all_commands,
    complete_commands,
    get_command,
    help_text,
)


def dispatch(agent, text: str, interactive: bool = True) -> CommandResult:
    """解析并执行一条斜杠命令，返回 CommandResult 交由宿主渲染。

    REPL / TUI 共用这一个入口：未知命令、参数误用、命令内异常都收敛为
    友好的中文提示（异常不得拖垮 REPL / TUI 主循环）。
    """
    parts = text[1:].split()
    name = parts[0] if parts else ""
    args = parts[1:]
    cmd = base.get_command(name)
    if cmd is None:
        return CommandResult(text=f"未知命令: {text}（/help 查看）", style="red")
    if not cmd.accepts_args and args:
        return CommandResult(
            text=f"用法: {cmd.usage or '/' + cmd.name}（此命令不接收参数）",
            style="yellow",
        )
    ctx = CommandContext(agent=agent, raw=text, args=args, interactive=interactive)
    try:
        return cmd.handler(ctx)
    except Exception as e:  # noqa: BLE001  命令异常统一兜底，保持主循环存活
        return CommandResult(text=f"[命令出错] {type(e).__name__}: {e}", style="red")
