"""斜杠命令 /mcp：服务器状态、添加向导与生命周期管理。

命令层保持纯函数：选择/向导只返回意图（CommandSelect / CommandWizard），
宿主负责弹面板并把向导计划交回 `mcp.wizard.apply_plan` 落盘。

mcp 包在函数内延迟导入：本模块经 commands → utils.terminal 的导入链被
renderer 间接加载，顶层导入会形成环（mcp.service → tools.ask → terminal）。
"""
from __future__ import annotations

from .base import (
    KIND_BLOCK,
    CommandChoice,
    CommandResult,
    CommandSelect,
    CommandWizard,
    register,
)

_USAGE = (
    "用法:\n"
    "  /mcp                        查看服务器状态\n"
    "  /mcp list                   文本列表\n"
    "  /mcp add                    打开添加向导\n"
    "  /mcp add <名称> -- <命令...> [-e KEY=VALUE] [--scope user|project]\n"
    "  /mcp tools|logs|reconnect|enable|disable|remove <名称>"
)

# 状态 → 选择面板右侧状态文字的颜色（已连接绿；其余按语义区分）
_STATE_STYLES = {
    "connected": "#23d18b",     # 绿：已连接
    "connecting": "#e0af68",    # 黄：连接中
    "pending": "#565f89",       # 灰：等待连接
    "failed": "#f7768e",        # 红：连接失败
    "missing_env": "#fab283",   # 橙：缺少密钥
    "disabled": "#565f89",      # 灰：已停用
    "disconnected": "#565f89",  # 灰：已断开
}


def _scope_text(scope: str) -> str:
    return "项目" if scope == "project" else "全局"


@register(
    "mcp", "管理 MCP 服务器",
    usage="/mcp [add|list|tools|logs|reconnect|enable|disable|remove] ...",
    accepts_args=True, immediate=True,
)
def _mcp(ctx):
    service = getattr(ctx.agent, "mcp", None)
    if service is None:
        return CommandResult(text="MCP 服务不可用。", style="red")
    args = ctx.args
    if not args:
        return _overview(service)
    head = args[0].lower()
    if head == "add":
        return _add(ctx, service, args[1:])
    if head == "list":
        return CommandResult(text=_status_text(service), kind=KIND_BLOCK)
    if head == "tools":
        return _tools(service, args[1:])
    if head == "logs":
        return _logs(service, args[1:])
    if head in ("reconnect", "enable", "disable", "remove"):
        return _lifecycle(service, head, args[1:])
    # 无子命令时按服务器名处理：弹出该服务器的操作菜单
    if _find(service, args[0]) is not None:
        return _server_menu(service, args[0])
    return CommandResult(text=_USAGE, style="yellow")


def _overview(service) -> CommandResult:
    """无参 `/mcp`：始终弹出选择面板（无服务器时只有「添加 MCP」一项）。"""
    items = [CommandChoice("添加 MCP", "add", description="打开添加向导")]
    statuses = service.status()
    if statuses:
        items.append(CommandChoice("", "", separator=True))
    for status in statuses:
        items.append(_server_choice(status))
    return CommandResult(
        select=CommandSelect(title="MCP", command="mcp", items=items, size="large")
    )


def _server_choice(status) -> CommandChoice:
    """一行服务器：名称 + 工具数量 + 级别（左），状态文字（右、按状态着色）。"""
    scope = _scope_text(status.scope)
    return CommandChoice(
        label=status.name,
        value=status.name,
        description=f"{status.tool_count} 工具 · {scope}",
        trailing=status.state_label,
        trailing_style=_STATE_STYLES.get(status.state, "#565f89"),
    )


def _server_menu(service, name: str) -> CommandResult:
    status = _find(service, name)
    if status is None:
        return CommandResult(text=f"未找到 MCP 服务器 {name!r}。", style="yellow")
    toggle = ("启用", f"enable {name}") if status.state == "disabled" else ("停用", f"disable {name}")
    items = [
        CommandChoice("查看工具", f"tools {name}"),
        CommandChoice("重连", f"reconnect {name}"),
        CommandChoice(*toggle),
        CommandChoice("查看日志", f"logs {name}"),
        CommandChoice("删除", f"remove {name}", description="同时从配置文件移除"),
    ]
    title = f"{status.name} · {_scope_text(status.scope)} · {status.state_label}"
    return CommandResult(
        select=CommandSelect(title=title, command="mcp", items=items, size="medium")
    )


def _add(ctx, service, rest: list) -> CommandResult:
    from ..mcp import config as mcp_config
    from ..mcp.errors import McpConfigError
    from ..mcp.secrets import store_secret

    if not rest:
        if not ctx.interactive:
            return CommandResult(text=_USAGE, style="yellow")
        return CommandResult(wizard=CommandWizard("mcp.add"))
    name, scope, env_pairs, command = _parse_add(rest)
    if not name or not command:
        return CommandResult(text=_USAGE, style="yellow")
    if scope not in ("user", "project"):
        return CommandResult(text="--scope 只支持 user 或 project。", style="yellow")

    cfg = mcp_config.ServerConfig(name=name, command=command)
    for key, value in env_pairs.items():
        if value.startswith("${") and value.endswith("}"):
            cfg.env[key] = value
        else:
            store_secret(name, key, value)
            cfg.env[key] = "${" + key + "}"
    try:
        service.add(cfg, scope)
    except McpConfigError as e:
        return CommandResult(text=f"添加失败: {e}", style="red")
    label = _scope_text(scope)
    return CommandResult(
        text=f"已保存 MCP 服务器 {name}（{label}），正在后台连接…用 /mcp 查看状态。",
        style="retry",
    )


def _parse_add(rest: list):
    """解析 `/mcp add <名称> -- <命令...> [-e K=V] [--scope ...]`。"""
    name = None
    scope = "user"
    env_pairs = {}
    command = []
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--":
            command = list(rest[index + 1:])
            break
        if token == "--scope" and index + 1 < len(rest):
            scope = rest[index + 1].lower()
            index += 2
            continue
        if token in ("-e", "--env") and index + 1 < len(rest):
            pair = rest[index + 1]
            if "=" in pair:
                key, value = pair.split("=", 1)
                env_pairs[key] = value
            index += 2
            continue
        if name is None:
            name = token
        else:
            command.append(token)  # 容错：省略 -- 时名称之后的都是命令
        index += 1
    return name, scope, env_pairs, command


def _tools(service, rest: list) -> CommandResult:
    if not rest:
        return CommandResult(text="用法: /mcp tools <名称>", style="yellow")
    name = rest[0]
    if _find(service, name) is None:
        return CommandResult(text=f"未找到 MCP 服务器 {name!r}。", style="yellow")
    tools = service.tools(name)
    if not tools:
        return CommandResult(text=f"{name} 暂无已暴露的工具（可能未连接或未启用）。")
    lines = [f"{name} 的工具（{len(tools)}）:"]
    for tool in tools:
        suffix = " · 只读" if tool["read_only"] else ""
        lines.append(f"  {tool['original']}{suffix}")
        if tool["description"]:
            lines.append(f"    {tool['description']}")
    return CommandResult(text="\n".join(lines), kind=KIND_BLOCK)


def _logs(service, rest: list) -> CommandResult:
    if not rest:
        return CommandResult(text="用法: /mcp logs <名称>", style="yellow")
    name = rest[0]
    if _find(service, name) is None:
        return CommandResult(text=f"未找到 MCP 服务器 {name!r}。", style="yellow")
    text = service.logs(name, lines=30)
    if not text:
        return CommandResult(text=f"{name} 暂无日志输出。")
    return CommandResult(text=f"{name} 最近日志:\n{text}", kind=KIND_BLOCK)


def _lifecycle(service, action: str, rest: list) -> CommandResult:
    if not rest:
        return CommandResult(text=f"用法: /mcp {action} <名称>", style="yellow")
    name = rest[0]
    if _find(service, name) is None:
        return CommandResult(text=f"未找到 MCP 服务器 {name!r}。", style="yellow")
    if action == "reconnect":
        service.reconnect(name)
        return CommandResult(text=f"MCP 服务器 {name} 正在重连…", style="retry")
    if action == "enable":
        service.set_enabled(name, True)
        return CommandResult(text=f"已启用 MCP 服务器 {name}，正在连接…", style="retry")
    if action == "disable":
        service.set_enabled(name, False)
        return CommandResult(text=f"已停用 MCP 服务器 {name}。")
    service.remove(name)
    return CommandResult(text=f"已删除 MCP 服务器 {name}（已从配置移除）。")


def _find(service, name: str):
    for status in service.status():
        if status.name == name:
            return status
    return None


def _status_text(service) -> str:
    statuses = service.status()
    lines = ["MCP 服务器:"]
    if not statuses:
        lines.append("  （无）用 /mcp add 添加。")
    for status in statuses:
        line = f"  {status.name}  [{status.scope_label} · {status.state_label}]"
        if status.tool_count:
            line += f"  {status.tool_count} 工具"
        if status.error:
            line += f"  {status.error}"
        lines.append(line)
    for message in getattr(service, "diagnostics", []) or []:
        lines.append(f"  [警告] {message}")
    return "\n".join(lines)
