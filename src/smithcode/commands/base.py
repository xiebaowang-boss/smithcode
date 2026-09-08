"""斜杠命令注册表：新增命令只需在实现文件里用 @register 装饰器声明，
无需再修改分发处（与 tools 注册表同款机制，导入即注册）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# 渲染形态：line=一行式提示（REPL 直接 print / TUI 着色单行）；
#           block=多行文本块（TUI 按块渲染并解析内嵌 ANSI 转义）。
KIND_LINE = "line"
KIND_BLOCK = "block"


@dataclass
class CommandChoice:
    """选择器的一个候选项：label 展示、value 回传、current 标记当前值。"""

    label: str
    value: str
    description: str = ""
    current: bool = False


@dataclass
class CommandSelect:
    """命令要求宿主弹出的选择意图：选中后按 `/<command> <value>` 重新分发。

    command 是被调用的命令名（如 "model"），宿主不关心选项语义，只负责
    展示并回填参数——命令处理器保持同步、纯函数。
    """

    title: str
    command: str
    items: list  # CommandChoice 列表


@dataclass
class CommandResult:
    """命令执行结果：宿主（REPL / TUI）据此渲染输出并做后续动作。"""

    text: str | None = None       # 展示给用户的文本；None 表示无输出
    kind: str = KIND_LINE         # line 一行式 / block 多行块
    style: str | None = None      # TUI 着色提示（REPL 忽略），如 "green"
    exit: bool = False            # 要求退出（REPL 跳出循环 / TUI 结束应用）
    session_reset: bool = False   # 会话已重置（TUI 需清空计划侧栏）
    refresh_status: bool = False  # 会话状态可能变化（TUI 需刷新状态栏）
    select: CommandSelect | None = None  # 非空时宿主弹出选择器


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
    """按前缀过滤命令，供输入补全菜单用（REPL 与 TUI 共用同一份数据）。

    prefix 为 "/" 后已敲出的字符（可为空串 = 全部命令）；别名不进菜单。
    """
    return [cmd for cmd in all_commands() if cmd.name.startswith(prefix)]


# /help 尾部的输入操作提示（REPL 与 TUI 通用）
HELP_FOOTER = "输入: Enter 发送，Ctrl+Enter 换行；↑↓ 翻历史，Ctrl+W 删词。"


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
