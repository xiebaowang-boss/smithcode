"""斜杠命令：导入各命令模块即完成注册，此处统一导出给 REPL / TUI 使用。

新增命令的流程：在 commands/ 下新建文件，用 @register 声明，再到本文件的
导入行加上模块名——分发、/help 文案、两端宿主渲染全部自动生效。
"""

from . import (  # noqa: F401  导入即注册
    base,
    effort,
    goal,
    info,
    mcp,
    model,
    session,
    skills,
)
from .base import (  # noqa: F401
    KIND_BLOCK,
    KIND_LINE,
    Command,
    CommandChoice,
    CommandContext,
    CommandResult,
    CommandSelect,
    CommandWizard,
    all_commands,
    complete_commands,
    get_command,
    help_text,
)
from .session import COMPACT_RUNNING, compact_report  # noqa: F401  宿主共用的压缩文案


def dispatch(agent, text: str, interactive: bool = True) -> CommandResult:
    """解析并执行一条斜杠命令，返回 CommandResult 交由宿主渲染。

    REPL / TUI 共用这一个入口：未知命令、参数误用、命令内异常都收敛为
    友好的中文提示（异常不得拖垮 REPL / TUI 主循环）。注册命令未命中时
    兜底查技能名（`/技能名 [任务]` 直达，技能名即命令）。
    """
    parts = text[1:].split()
    name = parts[0] if parts else ""
    args = parts[1:]
    cmd = base.get_command(name)
    if cmd is None:
        outcome = _dispatch_skill(name, args)
        if outcome is not None:
            return outcome
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


def is_skill_command(text: str) -> bool:
    """`/技能名 …` 是否为技能直达（非注册命令、技能已装载且存在）。

    与 `_dispatch_skill` 同一套判定，供宿主在不分发的前提下预判——TUI 的 busy
    守卫据此拦下运行中的技能加载：加载在分发期就登记集合、正文却要等宿主投递，
    运行中放行会留下「已加载但正文没进对话」的坏状态。
    """
    tokens = text.strip().split()
    if not tokens or not tokens[0].startswith("/"):
        return False
    name = tokens[0][1:]
    if not name or base.get_command(name) is not None:
        return False
    from .. import skills as registry  # 延迟导入：本包 skills 是命令模块，避免重名

    return registry.is_loaded() and registry.get(name) is not None


def _dispatch_skill(name: str, args: list):
    """未知命令兜底：命中技能名则按技能名加载（`/技能名 [任务]`），否则返回 None。

    注册命令始终优先（与内置命令重名的技能不进命令面，只能由模型用 use_skill
    加载）；技能未装载时不触发磁盘扫描，直接按未知命令处理。
    """
    from .. import skills as registry  # 延迟导入：本包 skills 是命令模块，避免重名
    from .skills import load_skill

    if not name or not registry.is_loaded():
        return None
    if registry.get(name) is None:
        return None
    return load_skill(name, " ".join(args).strip())
