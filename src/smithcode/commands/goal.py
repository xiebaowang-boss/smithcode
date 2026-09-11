"""持久目标命令：`/goal <目标>` 设定并自动推进，无参查看状态。

生命周期由 goal 模块的会话级单例承载；命令只负责解析用户意图、反馈与
返回 start_task 让宿主立即开跑。目标跨回合存活：每次任务结束后 Agent
的 run_with_goal 会自动注入续跑提示词，直到模型核验证据后声明完成、被
用户暂停/清除或回合预算用尽。

子命令：
    /goal <目标>        设定（替换）目标并立即开始推进
    /goal               查看当前目标状态
    /goal pause         暂停自动推进（目标保留）
    /goal resume        恢复推进并立即接续一轮
    /goal budget <N>    调整当前目标的回合预算
    /goal clear         清除目标（别名：stop / off / cancel / reset）
"""

from .. import goal
from .base import CommandResult, register

_CLEAR_VERBS = ("clear", "stop", "off", "cancel", "reset")


def _tokens_now(ctx) -> int:
    """当前会话已累计的 token 数（目标起点的差值基准）；取不到按 0。"""
    usage = getattr(getattr(ctx.agent, "session", None), "usage", None)
    accumulator = getattr(usage, "current_session", None)
    return accumulator.get("total_tokens") if accumulator is not None else 0


def _start(outcome: CommandResult, prompt: str) -> CommandResult:
    outcome.start_task = prompt
    return outcome


@register(
    "goal",
    "设定持久目标并自动推进",
    usage="/goal <目标> | /goal [pause|resume|clear|budget <N>]",
    accepts_args=True,
)
def _goal(ctx):
    args = ctx.args

    # 无参：查看状态
    if not args:
        if not goal.is_set():
            return CommandResult(
                text=(
                    "当前没有持久目标。用 /goal <目标描述> 设定，例如：\n"
                    "  /goal 修复所有失败的测试，直到 pytest 全部通过且不修改测试文件"
                ),
                kind="block",
            )
        return CommandResult(text=goal.render_status(), kind="block")

    head = args[0].lower()

    # 生命周期动词仅在恰为单个 token 时识别——"/goal clear the failures" 是目标描述，
    # 不是清除命令（对齐 Codex 的子命令解析，避免动词吃掉正常目标文本）
    if len(args) == 1 and head in _CLEAR_VERBS:
        if goal.clear():
            return CommandResult(
                text="目标已清除。", style="yellow", refresh_status=True
            )
        return CommandResult(text="当前没有持久目标。", style="yellow")

    if len(args) == 1 and head == "pause":
        if goal.pause("用户暂停"):
            return CommandResult(
                text="目标已暂停自动推进；/goal resume 继续，/goal clear 清除。",
                style="yellow",
                refresh_status=True,
            )
        return CommandResult(text="当前没有进行中的目标。", style="yellow")

    if len(args) == 1 and head == "resume":
        if not goal.is_set():
            return CommandResult(text="当前没有持久目标，无法恢复。", style="yellow")
        if not goal.resume():
            # 已是 active（如被中断后循环已停）：按用户意图再踢一轮续跑
            current = goal.current()
            return _start(
                CommandResult(
                    text="目标正在推进中，继续接续一轮。",
                    style="green",
                    refresh_status=True,
                ),
                current.continuation_prompt(),
            )
        current = goal.current()
        return _start(
            CommandResult(
                text="目标已恢复，继续推进。", style="green", refresh_status=True
            ),
            current.continuation_prompt(),
        )

    # 调整预算
    if head == "budget":
        if not goal.is_set():
            return CommandResult(text="当前没有持久目标，无法设置预算。", style="yellow")
        if len(args) != 2 or not args[1].isdigit() or int(args[1]) <= 0:
            return CommandResult(
                text="用法: /goal budget <N>（N 为正整数，表示最多自动推进的回合数）",
                style="yellow",
            )
        goal.set_budget(int(args[1]))
        current = goal.current()
        return CommandResult(
            text=f"目标回合预算已设为 {current.max_turns}（当前第 {current.turns} 回合）。",
            style="green",
            refresh_status=True,
        )

    # 其余情况：把参数整体当作目标描述
    objective = " ".join(args).strip()
    if not objective:
        return CommandResult(
            text="用法: /goal <目标描述>（如 /goal 修复 lint 问题直到 ruff check 通过）",
            style="yellow",
        )
    if len(objective) > goal.MAX_OBJECTIVE_LEN:
        return CommandResult(
            text=(
                f"目标描述过长（{len(objective)} 字符，上限 {goal.MAX_OBJECTIVE_LEN}）："
                "请精简为可核验的完成条件。"
            ),
            style="yellow",
        )
    current = goal.set(objective, tokens_at_start=_tokens_now(ctx))
    return _start(
        CommandResult(
            text=(
                f"已设定目标（回合预算 {current.max_turns}），开始推进。\n"
                f"目标: {objective}\n"
                "自动接续中：/goal 查看状态，/goal pause 暂停，/goal clear 清除。"
            ),
            kind="block",
            style="green",
            refresh_status=True,
        ),
        current.start_prompt(),
    )
