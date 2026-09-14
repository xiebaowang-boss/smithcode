"""斜杠命令注册表：新增命令只需在实现文件里用 @register 装饰器声明，
无需再修改分发处（与 tools 注册表同款机制，导入即注册）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

# 渲染形态：line=一行式提示（REPL 直接 print / TUI 着色单行）；
#           block=多行文本块（TUI 按块渲染并解析内嵌 ANSI 转义）。
KIND_LINE = "line"
KIND_BLOCK = "block"


@dataclass
class CommandChoice:
    """选择器的一个候选项：label 展示、value 回传、current 标记当前值。

    description 跟在标题后（同一侧），trailing 贴行尾右对齐（如状态），
    trailing_style 为 trailing 的颜色（选中行仍反白，由面板决定）；
    separator=True 表示纯间隔行（不可选中）。两者都是纯展示、可留空；
    TUI 选择面板按此排版，REPL 降级只列 label。
    """

    label: str
    value: str
    description: str = ""
    current: bool = False
    trailing: str = ""
    trailing_style: str = ""
    separator: bool = False


@dataclass
class CommandSelect:
    """命令要求宿主弹出的选择意图：选中后按 `/<command> <value>` 重新分发。

    command 是被调用的命令名（如 "model"），宿主不关心选项语义，只负责
    展示并回填参数——命令处理器保持同步、纯函数。

    size 是弹窗宽度档位（small / medium / large / xlarge），由调用方按内容
    长度声明；缺省 medium，宿主不测量内容、只按档位取宽度。
    """

    title: str
    command: str
    items: list  # CommandChoice 列表
    size: str = "medium"


@dataclass
class CommandWizard:
    """命令要求宿主启动的向导意图（如 /mcp add）。

    name 标识向导类型（宿主据此选择面板/行式流程），payload 为初始参数；
    向导本身不产生副作用，完成后由宿主调用服务层写盘与连接。
    """

    name: str
    payload: dict = field(default_factory=dict)


@dataclass
class CommandResult:
    """命令执行结果：宿主（REPL / TUI）据此渲染输出并做后续动作。"""

    text: str | None = None       # 展示给用户的文本；None 表示无输出
    kind: str = KIND_LINE         # line 一行式 / block 多行块
    style: str | None = None      # TUI 着色提示（REPL 忽略），如 "green"
    exit: bool = False            # 要求退出（REPL 跳出循环 / TUI 结束应用）
    session_reset: bool = False   # 会话已重置（TUI 需清空计划侧栏）
    session_resume: bool = False  # 会话已切换/恢复（TUI 清聊天区后回放历史）
    refresh_status: bool = False  # 会话状态可能变化（TUI 需刷新状态栏）
    select: CommandSelect | None = None  # 非空时宿主弹出选择器
    start_task: str | None = None  # 非空时宿主立即以此文本发起一次任务（如 /goal 开跑）
    echo_input: bool = False      # 与 start_task 搭配：宿主先把用户输入原文回显为消息
    wizard: CommandWizard | None = None  # 非空时宿主启动向导（如 /mcp add）


@dataclass
class CommandContext:
    """命令运行上下文：命令只依赖它，不直接感知 REPL / TUI 宿主差异。"""

    agent: object                 # Agent 实例（松耦合，避免循环导入）
    raw: str                      # 用户输入原文（含 "/xxx" 前缀）
    args: list                    # 按空白切分的参数（不含命令名）
    interactive: bool = True      # 预留：非交互 fail-closed 场景的开关


@dataclass
class Command:
    name: str                     # 命令名（不含斜杠），如 "help"
    description: str              # 一句话中文描述（/help 文案自动生成用）
    handler: Callable             # (ctx) -> CommandResult
    usage: str | None = None      # 用法示例（含参数形态），缺省为 /<name>
    aliases: tuple = ()           # 别名，注册后同样指向本命令
    accepts_args: bool = False    # False 时携带参数直接回用法提示
    immediate: bool = False       # TUI 菜单选中后立即执行（而非填入输入框）


COMMANDS: dict = {}  # 命令名 / 别名 -> Command


def register(name: str, description: str, usage: str | None = None,
             aliases: tuple = (), accepts_args: bool = False,
             immediate: bool = False):
    """把一个函数注册为斜杠命令。

    name 为命令名（不含斜杠）；description 进 /help 文案；
    usage 为含参数形态的用法示例（如 "/model [名称]"，缺省 /<name>）；
    aliases 为可选别名；accepts_args 为 False 时携带参数会收到用法提示；
    immediate 为 True 时 TUI 命令菜单选中即执行（不填入输入框）。
    """

    def decorator(func):
        cmd = Command(
            name=name, description=description, handler=func,
            usage=usage, aliases=tuple(aliases), accepts_args=accepts_args,
            immediate=immediate,
        )
        COMMANDS[name] = cmd
        for alias in cmd.aliases:
            COMMANDS[alias] = cmd
        return func

    return decorator


def get_command(name: str):
    """按命令名或别名取命令，未注册返回 None。"""
    return COMMANDS.get(name)


def all_commands() -> list:
    """按命令名排序去重后的全部命令（别名不重复出现）。"""
    seen, out = set(), []
    for name in sorted(COMMANDS):
        cmd = COMMANDS[name]
        if cmd.name not in seen:
            seen.add(cmd.name)
            out.append(cmd)
    return out


def complete_commands(prefix: str) -> list:
    """按前缀过滤命令与技能，供输入补全菜单用（REPL 与 TUI 共用同一份数据）。

    prefix 为 "/" 后已敲出的字符（可为空串 = 全部命令）；别名不进菜单。
    排序：功能命令按名称在前，技能条目按名称在后；同名技能不重复出现
    （仍可用 /skill 加载）；技能未装载时不并入（补全路径不做磁盘扫描/信任确认）。
    """
    registered = [cmd for cmd in all_commands() if cmd.name.startswith(prefix)]
    taken = {cmd.name for cmd in registered}
    skills = sorted(_skill_commands(prefix, taken), key=lambda cmd: cmd.name)
    return registered + skills


def _skill_commands(prefix: str, taken: set) -> list:
    """技能补全条目：选中后填入 `/技能名 `（immediate=False），可继续补任务。"""
    from .. import skills  # 延迟导入：commands -> utils.terminal -> commands 存在导入环

    if not skills.is_loaded():
        return []
    out = []
    for skill in skills.all_skills():
        if skill.disabled or skill.name in taken or not skill.name.startswith(prefix):
            continue
        out.append(
            Command(
                name=skill.name,
                description=f"技能 · {skill.description}",
                handler=None,
                usage=f"/{skill.name} [任务]",
                accepts_args=True,
            )
        )
    return out


# /help 尾部的输入操作提示（REPL 与 TUI 通用）
HELP_FOOTER = (
    "技能: /skills 打开选择框，/<技能名> [任务] 直达，/skill <名称> [任务] 直接加载。\n"
    "输入: Enter 发送，Ctrl+Enter 换行；↑↓ 翻历史，Ctrl+W 删词。"
)


def help_text() -> str:
    """由注册表自动生成 /help 文案，新增命令无需改这里。"""
    entries = [(cmd.usage or "/" + cmd.name, cmd.description) for cmd in all_commands()]
    width = max(len(label) for label, _ in entries)
    lines = ["命令:"]
    for label, desc in entries:
        lines.append(f"  {label.ljust(width + 2)}{desc}")
    lines.append("")
    lines.append(HELP_FOOTER)
    return "\n".join(lines)
