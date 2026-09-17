"""会话级命令：/new /sessions /rename /save /compact /exit。

`/sessions` 是会话切换与管理的统一入口：无参弹选择框（选中即切换）、
带 id/序号直接切换、`list` 文本列表、`delete <id>` 删除。启动恢复走
CLI 参数 `-c` / `--resume`（见 cli.py），会话内不再有单独的 /resume。
"""

import time

from .. import config, sessions
from .base import KIND_BLOCK, CommandChoice, CommandResult, CommandSelect, register


def _format_time(timestamp: float) -> str:
    """列表展示用的本地时间（月-日 时:分）。"""
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(timestamp)))
    except (TypeError, ValueError, OSError):
        return "?"


def _entry_label(item) -> str:
    return item.title or item.first_prompt or "（无标题）"


def _render_list(items) -> CommandResult:
    """文本列表（`/sessions list` 与非交互降级共用展示形态）。"""
    if not items:
        return CommandResult(text="当前项目还没有历史会话。")
    lines = [f"最近会话（{len(items)} 条）:"]
    for index, item in enumerate(items, 1):
        mark = "（当前）" if item.id == config.SESSION_ID else ""
        lines.append(
            f"  {index}. {item.short_id}  {_format_time(item.updated)}  {_entry_label(item)}{mark}"
        )
    lines.append(
        "直接 /sessions 打开选择框；也可 /sessions <id|序号> 切换，"
        "/sessions delete <id> 删除。"
    )
    return CommandResult(text="\n".join(lines), kind=KIND_BLOCK)


def _select_items(items):
    """选择框候选：标题后跟短 id，更新时间贴行尾右对齐（列表已按时间倒序）。"""
    return [
        CommandChoice(
            label=_entry_label(item),
            value=item.short_id,
            description=item.short_id,
            trailing=_format_time(item.updated),
            current=item.id == config.SESSION_ID,
        )
        for item in items
    ]


def _switch(ctx, items, token: str) -> CommandResult:
    """把 id / 唯一前缀 / 序号解析为目标会话并原地切换。"""
    summary = None
    if token.isdigit():  # 支持 /sessions list 里的序号
        index = int(token) - 1
        if 0 <= index < len(items):
            summary = items[index]
    if summary is None:
        try:
            summary = sessions.find(token)
        except sessions.StoreError as exc:
            return CommandResult(text=str(exc), style="yellow")
    if summary is None:
        return CommandResult(text=f"未找到会话：{token}", style="yellow")

    try:
        report = ctx.agent.resume(summary)
    except sessions.StoreError as exc:
        return CommandResult(text=f"切换失败：{exc}", style="yellow")

    text = f"已切换到会话 {report.session_id[:8]}（{report.message_count} 条消息）"
    if report.title:
        text += f"：{report.title}"
    if report.repair == "appended":
        text += "；已修复上次中断留下的未完成工具调用"
    elif report.repair == "truncated":
        text += "；检测到历史损坏，已截断修复"
    if report.bad_lines:
        text += f"；跳过了 {report.bad_lines} 行损坏记录"
    return CommandResult(
        text=text, style="green", session_resume=True, refresh_status=True
    )


@register("exit", "退出")
def _exit(ctx):
    # 无输出：告别语由宿主自行处理（REPL 打印「再见!」，TUI 直接结束应用）
    return CommandResult(exit=True)


@register("new", "开启新会话（可带名称）", usage="/new [名称]", accepts_args=True)
def _new(ctx):
    # 重置动作集中在 Agent.new_session（会话级状态一处清空）；命令层只负责反馈与标记
    ctx.agent.new_session()
    name = " ".join(ctx.args).strip()
    if name:
        ctx.agent.rename_session(name)
    text = f"已开启新会话：{name}" if name else "已开启新会话。"
    return CommandResult(
        text=text,  # 仅 REPL 展示；TUI 以清空聊天区代替，不再追加文本
        style="yellow",
        session_reset=True,   # TUI 清空聊天区与计划侧栏
        refresh_status=True,  # TUI 刷新状态栏
    )


@register(
    "sessions",
    "查看 / 切换历史会话",
    usage="/sessions [list [数量] | delete <id> | <id|序号>]",
    accepts_args=True,
    immediate=True,  # 菜单里选中即弹选择框（与 /model /skills 一致）
)
def _sessions(ctx):
    args = ctx.args
    limit = config.load_sessions_config().list_limit

    if args and args[0] in ("delete", "rm"):
        if len(args) < 2:
            return CommandResult(text="用法: /sessions delete <id>", style="yellow")
        try:
            removed = sessions.delete(args[1])
        except sessions.StoreError as exc:
            return CommandResult(text=str(exc), style="yellow")
        return CommandResult(
            text="已删除该会话。" if removed else "未找到该会话。",
            style="green" if removed else "yellow",
        )

    if args and args[0] in ("list", "ls"):
        count = limit
        if len(args) > 1 and args[1].isdigit():
            count = max(1, int(args[1]))
        return _render_list(sessions.list_sessions(limit=count))

    items = sessions.list_sessions(limit=limit)
    if not args:
        if not items:
            return CommandResult(text="当前项目还没有历史会话。")
        return CommandResult(select=CommandSelect(
            title="切换会话",
            command="sessions",
            items=_select_items(items),
            size="large",  # 会话标题 + 时间/短 id/模型说明比其他选择框长
        ))
    return _switch(ctx, items, args[0])


@register("rename", "重命名当前会话", usage="/rename <名称>", accepts_args=True)
def _rename(ctx):
    title = " ".join(ctx.args).strip()
    if not title:
        return CommandResult(text="用法: /rename <名称>", style="yellow")
    ctx.agent.rename_session(title)
    return CommandResult(
        text=f"会话已重命名为「{title}」。", style="green", refresh_status=True
    )


@register("save", "保存会话记录")
def _save(ctx):
    path = ctx.agent.session.save()
    return CommandResult(text=f"会话已保存到 {path}", style="green")


@register("compact", "手动压缩上下文")
def _compact(ctx):
    # 压缩要发多次摘要请求，同步执行会阻塞宿主主循环（TUI 直接卡死）：
    # 命令层只声明意图，由宿主在后台线程执行并反馈进度与结果。
    return CommandResult(start_compact=True)


# 手动压缩的宿主文案：开始提示与结果映射集中在此，REPL / TUI 共用同一份措辞
COMPACT_RUNNING = "正在压缩上下文…"


def compact_report(status: str) -> tuple[str, str]:
    """把 Agent.compact_manual() 的状态映射为宿主文案 (文本, 命令层 style)。"""
    if status == "ok":
        return "上下文压缩完成。", "green"
    if status == "cancelled":
        return "已取消压缩。", "yellow"
    return "没有可压缩的上下文（历史太短或摘要未生成）。", "yellow"
