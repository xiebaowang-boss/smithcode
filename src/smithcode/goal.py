"""持久目标（/goal）：跨回合使命的会话级状态机与续跑提示词。

借鉴 Codex CLI 的 /goal：目标在多个回合间存活，模型围绕目标推进，直到逐条
核验真实证据后调用 goal_update 声明完成（或同一阻碍连续多回合后声明受阻），
或（配置了回合预算时）预算用尽由系统收尾。目标存于本模块的进程内单例（会话口径，/new 时
reset()）；AgentSession.run_with_goal() 在每轮任务结束后检查状态，必要时注入续跑
提示词自动开启下一回合——续跑只在宿主空闲、目标 active 且上一轮正常结束
且有实际推进（用过工具）时发生，避免空转。

与 plan.py 的分工：plan 是模型用 todo_write 维护的步骤清单（怎么做），goal
是用户用 /goal 设定的使命与完成标准（做到什么算完）。两者独立，可组合使用。
"""
from __future__ import annotations

import copy
import time
from contextvars import ContextVar

from . import config

# 生命周期状态：active（推进中）/ paused（用户或系统暂停）/ complete（证据核验后
# 由模型声明完成）/ blocked（同一阻碍连续多回合且无用户输入无法继续）/ budget_limited
# （回合预算用尽，系统收尾）
ACTIVE = "active"
PAUSED = "paused"
COMPLETE = "complete"
BLOCKED = "blocked"
BUDGET_LIMITED = "budget_limited"
STATUSES = (ACTIVE, PAUSED, COMPLETE, BLOCKED, BUDGET_LIMITED)

STATUS_LABELS = {
    ACTIVE: "进行中",
    PAUSED: "已暂停",
    COMPLETE: "已完成",
    BLOCKED: "受阻",
    BUDGET_LIMITED: "预算用尽",
}

STATUS_ICONS = {
    ACTIVE: "◎",
    PAUSED: "⏸",
    COMPLETE: "✓",
    BLOCKED: "✕",
    BUDGET_LIMITED: "◷",
}

# 目标文本上限（对齐 Claude Code 的 4000 字符条件），防止把整份文档塞进目标
MAX_OBJECTIVE_LEN = 4000

# 同一阻碍连续申明达到此回合数才接受 blocked（对齐 Codex 的 blocked audit）
BLOCKED_THRESHOLD = 3

# 不视为"推进动作"的工具：只有目标工具被调用时，阻碍连击才继续累计
GOAL_TOOLS = frozenset({"goal_update", "goal_read"})

# 渲染状态时目标文本的最大展示宽度，超出截断（完整目标用 /goal 查看）
MAX_RENDER_LEN = 200


def _format_elapsed(seconds: float) -> str:
    """时长分级格式（与 TUI 页脚同款语义，此处避免依赖 tui 包）：42s / 2m 10s / 1h 2m。"""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s" if sec else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def _clip(text: str, limit: int = MAX_RENDER_LEN) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _normalize_reason(reason: str) -> str:
    """阻碍文本归一化：折叠空白，用于判断"同一阻碍"。"""
    return " ".join(str(reason or "").split())


class Goal:
    """一份持久目标的状态机实例（模块级单例，目标不存在时为 None）。"""

    def __init__(self, objective: str, max_turns: int, tokens_at_start: int = 0):
        self.objective = objective
        self.status = ACTIVE
        self.created_at = time.time()
        self.ended_at: float | None = None
        self.turns = 0            # 目标存续期间已执行的回合数（含用户发起的回合）
        self.max_turns = max_turns
        self.tokens_at_start = tokens_at_start
        self.tokens_used = 0      # 按会话用量差值累计（note_run 更新）
        self.evidence = ""        # complete 的证据 / blocked 的阻碍说明
        self.note = ""            # 暂停、预算等系统侧原因
        self._blocked_key = ""    # 最近一次阻碍文本（归一化）
        self._blocked_streak = 0  # 同一阻碍连续出现的回合数
        self._blocked_turn = 0    # 最近一次阻碍计数发生的回合（同回合重复调用不重复计数）

    # ---------- 生命周期 ----------

    def begin_turn(self) -> int:
        """开始一个目标回合（用户发起或自动续跑），返回新的回合序号。"""
        if self.status == ACTIVE:
            self.turns += 1
        return self.turns

    def pause(self, note: str = "") -> None:
        self.status = PAUSED
        self.note = note
        self.ended_at = time.time()

    def resume(self) -> bool:
        """恢复推进（paused / blocked / budget_limited / complete 均可重新激活）。

        预算用尽的目标恢复时重置回合计数，等于开启一个新的预算窗口——
        用户已明确要求继续，不应刚跑一轮就再次触发收尾。
        """
        if self.status == ACTIVE:
            return False
        if self.status == BUDGET_LIMITED:
            self.turns = 0
        self.status = ACTIVE
        self.note = ""
        self.evidence = ""
        self.ended_at = None
        self._blocked_key = ""
        self._blocked_streak = 0
        self._blocked_turn = 0
        return True

    def complete(self, evidence: str = "") -> None:
        self.status = COMPLETE
        self.evidence = evidence
        self.note = ""
        self.ended_at = time.time()

    def budget_limited(self) -> None:
        self.status = BUDGET_LIMITED
        self.note = f"已达到回合预算（{self.max_turns} 回合），系统停止自动推进"
        self.ended_at = time.time()

    def try_block(self, reason: str) -> tuple[bool, str]:
        """申明受阻：同一阻碍连续达到 BLOCKED_THRESHOLD 个回合才接受。

        返回 (是否已接受, 面向模型的结果文本)。未达标时不改变状态，
        只记录连击次数并回传可操作的引导。
        """
        key = _normalize_reason(reason)
        if not key:
            return False, "错误: goal_update(status=\"blocked\") 需要 summary 说明具体阻碍与所需输入。"
        if key != self._blocked_key:
            self._blocked_key = key
            self._blocked_streak = 1
            self._blocked_turn = self.turns
        elif self._blocked_turn != self.turns:
            self._blocked_streak += 1
            self._blocked_turn = self.turns
        # 同一回合内重复申明不重复计数（阻碍按"连续回合"判定，不按调用次数）
        if self._blocked_streak >= BLOCKED_THRESHOLD:
            self.status = BLOCKED
            self.evidence = key
            self.note = "同一阻碍连续多回合无法推进"
            self.ended_at = time.time()
            return True, (
                f"目标已标记为受阻（同一阻碍已连续出现 {self._blocked_streak} 个回合）。"
                "请向用户说明阻碍、已尝试的做法与所需输入，然后等待用户指示。"
            )
        return False, (
            f"阻碍申明已记录（{self._blocked_streak}/{BLOCKED_THRESHOLD}）："
            "同一阻碍需连续出现多个回合、且确实无法继续时才会接受 blocked。"
            "请继续尝试不同做法；若已无可行路径，向用户说明所需输入。"
        )

    def note_run(self, tools_used=(), total_tokens: int | None = None) -> None:
        """记录一轮结束：有推进动作则重置阻碍连击，并同步累计 token 用量。"""
        if any(name not in GOAL_TOOLS for name in tools_used):
            self._blocked_key = ""
            self._blocked_streak = 0
            self._blocked_turn = 0
        if total_tokens is not None:
            self.tokens_used = max(0, int(total_tokens) - self.tokens_at_start)

    @property
    def blocked_streak(self) -> int:
        return self._blocked_streak

    # ---------- 查询与渲染 ----------

    @property
    def elapsed(self) -> float:
        return (self.ended_at or time.time()) - self.created_at

    @property
    def unlimited(self) -> bool:
        """回合预算是否为「不限」（`max_turns <= 0`，默认 -1）。"""
        return self.max_turns <= 0

    @property
    def turns_left(self) -> int:
        return max(0, self.max_turns - self.turns)

    def turn_label(self) -> str:
        """回合进度文案：有预算显示 `N/M`，不限则只显示 `N`。"""
        return f"{self.turns}" if self.unlimited else f"{self.turns}/{self.max_turns}"

    def marker(self) -> str:
        """TUI 底栏指示文本；无目标由调用方处理（这里总返回非空）。"""
        if self.status == ACTIVE:
            return f"{STATUS_ICONS[self.status]} 目标 {self.turn_label()}"
        return f"{STATUS_ICONS[self.status]} 目标{STATUS_LABELS[self.status]}"

    def sidebar(self) -> tuple:
        """TUI 侧边栏目标卡片内容：(标题, 正文)。

        标题为「目标 · 进度/状态」，正文为截断后的目标、进度明细与证据/原因；
        由宿主着色与展示，本模块不依赖 TUI。
        """
        if self.status == ACTIVE:
            title = f"目标 · {self.turn_label()}"
            detail = (
                f"{STATUS_ICONS[self.status]} 进行中 · 约 {self.tokens_used:,} tokens"
                f" · 用时 {_format_elapsed(self.elapsed)}"
            )
        else:
            title = f"目标 · {STATUS_LABELS[self.status]}"
            detail = (
                f"{STATUS_ICONS[self.status]} 第 {self.turn_label()} 回合"
                f" · 用时 {_format_elapsed(self.elapsed)}"
            )
        lines = [_clip(self.objective, 120), detail]
        if self.evidence:
            label = "证据" if self.status == COMPLETE else "阻碍"
            lines.append(f"{label}: {_clip(self.evidence, 80)}")
        if self.note and self.status != ACTIVE:
            lines.append(_clip(self.note, 80))
        return title, "\n".join(lines)

    def render_status(self) -> str:
        """面向用户/模型的完整状态块（/goal 与 goal_read 共用）。"""
        head = (
            f"[目标] {STATUS_LABELS[self.status]} · 第 {self.turn_label()} 回合"
            f" · 用时 {_format_elapsed(self.elapsed)} · 约 {self.tokens_used:,} tokens"
        )
        lines = [head, f"目标: {self.objective}"]
        if self.evidence:
            label = "证据" if self.status == COMPLETE else "阻碍"
            lines.append(f"{label}: {_clip(self.evidence)}")
        if self.note:
            lines.append(f"说明: {self.note}")
        return "\n".join(lines)

    def render_section(self) -> str:
        """注入系统提示词的「当前持久目标」段。

        只含稳定信息（状态 / 目标 / 证据），不含回合与 token 计数——那些每回合
        变化，放进续跑提示词；此段在状态变更前保持逐字节不变，避免系统提示词
        频繁重建破坏服务商的前缀缓存。
        """
        lines = ["## 当前持久目标", f"状态: {STATUS_LABELS[self.status]}", f"目标: {self.objective}"]
        if self.evidence:
            lines.append(f"记录: {_clip(self.evidence)}")
        if self.status == ACTIVE:
            lines.append(
                "推进与完成标准见「持久目标」一节的规则；系统会在回合间自动接续，"
                "被暂停或清除后立即停止推进。"
            )
        elif self.status in (COMPLETE, BLOCKED):
            lines.append("该目标已结束，不要继续推进；等待用户的新指示。")
        else:
            lines.append("该目标当前不推进；恢复后按「持久目标」规则继续。")
        return "\n".join(lines)

    # ---------- 提示词 ----------

    def _objective_block(self) -> str:
        return f"<objective>\n{self.objective}\n</objective>"

    def _budget_line(self) -> str:
        """提示词里的回合预算说明：有预算给上限，不限则说明持续自动推进。"""
        if self.unlimited:
            return (
                "回合预算：不限。系统会在回合间持续自动接续，直到你核验证据后声明完成/受阻，"
                "或用户暂停、清除、中断。"
            )
        return f"回合预算：最多 {self.max_turns} 个回合自动推进（当前为第 1 回合）。"

    def start_prompt(self) -> str:
        """设定目标后的首轮指令（由宿主的 start_task 发出）。"""
        return (
            "开始执行以下持久目标。该目标跨多个回合存活，系统会在回合之间自动接续，"
            "直到你逐条核验证据后声明完成、或回合预算用尽。\n\n"
            f"{self._objective_block()}\n\n"
            "工作方式：\n"
            "1. 先把目标拆解为可核验的交付物与成功标准；需要多步时用 todo_write 建立步骤清单；\n"
            "2. 从当前实际状态出发，逐步推进并做最小必要的验证；不要重复已完成的工作；\n"
            "3. 决定完成前做一次完成审计：把目标重述为具体交付物，逐条建立"
            "「要求 → 证据」清单，检查真实证据（文件内容、命令输出、测试结果）是否覆盖"
            "每一条要求；测试通过、清单全勾、工作量很大都只是证据的一部分；\n"
            "4. 只有证据证明全部要求达成、无剩余必需工作时，才调用 "
            "goal_update(status=\"complete\", summary=\"核验过的证据\")；"
            "不要因为回合将尽、工作量大或\"打算完成\"就标记完成；\n"
            "5. 同一阻碍连续出现且无用户输入无法继续时，才调用 "
            "goal_update(status=\"blocked\", summary=\"阻碍与所需输入\")。\n\n"
            f"{self._budget_line()}"
        )

    def continuation_prompt(self) -> str:
        """自动续跑轮的提示词（Codex continuation.md 的中文化）。"""
        if self.unlimited:
            head = (
                f"继续推进当前持久目标（第 {self.turns} 回合，预算不限，"
                f"已用约 {self.tokens_used:,} tokens）。\n"
            )
        else:
            head = (
                f"继续推进当前持久目标（第 {self.turns}/{self.max_turns} 回合，"
                f"剩余 {self.turns_left} 回合，已用约 {self.tokens_used:,} tokens）。\n"
            )
        return (
            f"{head}"
            "目标跨回合存活：本回合做不完不代表要把目标缩小；保持完整目标不变，"
            "围绕真正的交付物推进。\n\n"
            f"{self._objective_block()}\n\n"
            "避免重复已完成的工作，从当前实际状态选择下一个具体动作（文件与命令输出是"
            "权威依据；对话记忆只用于定位）。\n"
            "在决定目标是否达成前，必须完成一次基于证据的完成审计：\n"
            "1. 把目标重述为具体的交付物或成功标准；\n"
            "2. 逐条建立「要求 → 证据」清单：每个明确要求、编号项、文件、命令、测试、"
            "门禁、交付物都要对应到真实证据；\n"
            "3. 检查相关文件、命令输出、测试结果等真实证据；测试通过、清单全勾、"
            "验证脚本成功，只有在覆盖全部要求时才算证据；\n"
            "4. 不要用意图、部分进展、花费的精力或\"看起来快完成了\"当作完成证明；\n"
            "5. 任何要求缺失、未完成或未被验证，都视为未达成，继续工作。\n\n"
            "只有证据证明目标全部达成、无剩余必需工作时，才调用 "
            "goal_update(status=\"complete\", summary=\"核验过的证据\")，并在最终回复里向用户总结。\n"
            "若同一阻碍连续出现、且没有用户输入就无法继续，可按规则调用 "
            "goal_update(status=\"blocked\", summary=\"阻碍与所需输入\")；工作困难、耗时或"
            "只是不完整，都不算受阻。\n"
            "不要因为预算将尽或准备停止就标记完成。"
        )

    def wrapup_prompt(self) -> str:
        """预算用尽后的收尾轮提示词（Codex budget_limit.md 的中文化）。"""
        return (
            f"当前持久目标的回合预算（{self.max_turns} 回合）已用尽，系统已停止自动推进，"
            "不要开始新的实质工作。\n\n"
            f"{self._objective_block()}\n\n"
            "尽快收尾本回合：总结已取得的进展（附关键文件与验证结果）、剩余工作或阻碍，"
            "给用户清晰的下一步建议。\n"
            "只有目标确实已经全部完成时，才调用 goal_update(status=\"complete\", "
            "summary=\"核验过的证据\")；不要仅仅因为预算用尽就标记完成。"
        )


# ---------- 会话级状态（/new 时 reset） ----------


class GoalState:
    """一个会话的持久目标状态（原模块级单例 `_current`）。

    目标明确属于会话（`/new` 清空、恢复时按 `t=state` 读回），所以状态要能随会话
    存续——模块单例做不到"同时存在两个会话"（恢复/切换时会串味）。实例由
    `AgentSession` 持有并通过 `bind()` 生效。
    """

    def __init__(self) -> None:
        self.current: Goal | None = None

    def inherit(self, other: GoalState) -> None:
        """接管另一个实例的当前目标（会话建立时从默认实例接过来）。

        为什么需要：`/goal set` 之类的调用可能发生在会话对象建立之前（命令层直接
        调 `goal.set`，那时绑定的是默认实例）。新会话若从这里从空开始，用户刚设的
        目标就凭空消失了。接过来是"会话建立前设的状态属于随后建立的这个会话"。

        **复制**而不是别名：`self.current = other.current` 会让两个会话共用同一个
        目标对象——一个会话 `/goal clear` 会把另一个的目标也清掉（同进程多会话时
        的静默串味，探针 `tests/guard/test_multi_session_isolation.py` 抓的就是它）。
        """
        self.current = copy.deepcopy(other.current)


    def reset(self) -> None:
        """清空当前会话的目标（/new 时调用）。"""
        self.current = None

    def snapshot(self) -> dict | None:
        """会话级目标快照（持久化投影缓存用；不含回合计数与 token 基线）。"""
        if self.current is None:
            return None
        return {
            "objective": self.current.objective,
            "status": self.current.status,
            "max_turns": self.current.max_turns,
            "evidence": self.current.evidence,
            "note": self.current.note,
        }

    def restore(self, data) -> None:
        """从快照恢复目标（原地重建单例）；回合计数与 token 基线有意重置。
    
        对齐 Claude Code：活跃目标跨进程存活，但回合数/计时/用量从零重新开始，
        避免恢复后立即触发预算收尾。非法数据等同于清空。
        """
        if not isinstance(data, dict) or not str(data.get("objective") or "").strip():
            self.current = None
            return
        try:
            budget = int(data.get("max_turns"))
        except (TypeError, ValueError):
            budget = config.GOAL_MAX_TURNS
        restored = Goal(
            objective=str(data["objective"]).strip(),
            max_turns=budget,
        )
        status = data.get("status")
        restored.status = status if status in STATUSES else ACTIVE
        restored.evidence = str(data.get("evidence") or "")
        restored.note = str(data.get("note") or "")
        if restored.status != ACTIVE:
            restored.ended_at = time.time()
        self.current = restored

# 进程级默认实例：没有会话绑定时的落点（直接调本模块的测试、无 Agent 的路径）
_default_state = GoalState()
# 活动状态：**按上下文**解析（不是进程级指针）。
# 同进程并发两个会话时，各自的 goal/plan/skills 互不覆盖——这正是把
# "活动实例"从模块全局换成 ContextVar 要解决的问题（事件总线/取消信号同款做法）。
_active_state_var: ContextVar[GoalState] = ContextVar("smithcode_goal_state", default=_default_state)


def bind(state: GoalState | None) -> None:
    """切换本模块函数作用的状态实例（`None` = 回到默认实例）。

    调用点太多（session.py、commands/*、agent/*、tui/*），逐个改成
    `session.goal_state.xxx()` 不如让既有函数指向"当前实例"。**局限**：同一进程
    同时跑两个会话会互相覆盖——与改造前的单例行为一致；TUI/REPL 是一个进程一个会话。
    """
    _active_state_var.set(state if state is not None else _default_state)


def current() -> Goal | None:
    return _active_state_var.get().current


def is_set() -> bool:
    return _active_state_var.get().current is not None


def is_active() -> bool:
    return _active_state_var.get().current is not None and _active_state_var.get().current.status == ACTIVE


def set(objective: str, max_turns: int | None = None, tokens_at_start: int = 0) -> Goal:
    """设定（或替换）当前目标；max_turns 缺省取配置 GOAL_MAX_TURNS（默认 -1 不限）。"""
    _active_state_var.get().current = Goal(
        objective=str(objective).strip(),
        max_turns=config.GOAL_MAX_TURNS if max_turns is None else int(max_turns),
        tokens_at_start=int(tokens_at_start or 0),
    )
    return _active_state_var.get().current


def pause(note: str = "") -> bool:
    if _active_state_var.get().current is None or _active_state_var.get().current.status != ACTIVE:
        return False
    _active_state_var.get().current.pause(note)
    return True


def resume() -> bool:
    return _active_state_var.get().current.resume() if _active_state_var.get().current is not None else False


def clear() -> bool:
    if _active_state_var.get().current is None:
        return False
    _active_state_var.get().current = None
    return True


def complete(evidence: str = "") -> bool:
    if _active_state_var.get().current is None:
        return False
    _active_state_var.get().current.complete(evidence)
    return True


def budget_limited() -> bool:
    if _active_state_var.get().current is None or _active_state_var.get().current.status != ACTIVE:
        return False
    _active_state_var.get().current.budget_limited()
    return True


def try_block(reason: str) -> tuple[bool, str]:
    if _active_state_var.get().current is None:
        return False, "错误: 当前没有持久目标，不要调用 goal_update。"
    return _active_state_var.get().current.try_block(reason)


def begin_turn() -> int:
    if _active_state_var.get().current is None:
        return 0
    return _active_state_var.get().current.begin_turn()


def note_run(tools_used=(), total_tokens: int | None = None) -> None:
    if _active_state_var.get().current is not None:
        _active_state_var.get().current.note_run(tools_used, total_tokens)


def set_budget(max_turns: int) -> bool:
    if _active_state_var.get().current is None:
        return False
    _active_state_var.get().current.max_turns = int(max_turns)
    return True


def render_status() -> str:
    return _active_state_var.get().current.render_status() if _active_state_var.get().current is not None else "当前没有持久目标。"


def render_section() -> str:
    return _active_state_var.get().current.render_section() if _active_state_var.get().current is not None else ""


def marker() -> str:
    return _active_state_var.get().current.marker() if _active_state_var.get().current is not None else ""


def sidebar() -> tuple | None:
    """侧边栏目标卡片内容；无目标返回 None（宿主隐藏该卡片）。"""
    return _active_state_var.get().current.sidebar() if _active_state_var.get().current is not None else None





def reset(*args, **kwargs):
    """对**当前绑定的实例**做 reset（见 `bind`）；会话内的等价调用用
    `AgentSession` 持有的实例，避免依赖绑定状态。"""
    return _active_state_var.get().reset(*args, **kwargs)


def snapshot(*args, **kwargs):
    """对**当前绑定的实例**做 snapshot（见 `bind`）；会话内的等价调用用
    `AgentSession` 持有的实例，避免依赖绑定状态。"""
    return _active_state_var.get().snapshot(*args, **kwargs)


def restore(*args, **kwargs):
    """对**当前绑定的实例**做 restore（见 `bind`）；会话内的等价调用用
    `AgentSession` 持有的实例，避免依赖绑定状态。"""
    return _active_state_var.get().restore(*args, **kwargs)


def default_state() -> GoalState:
    """进程级默认实例（无会话绑定时的作用对象，会话建立时从其继承）。"""
    return _default_state


def active_state() -> GoalState:
    """当前生效的实例（会话建立时从它接管状态，见 AgentSession）。

    按**上下文**解析：并发会话各拿各的（同进程多会话不会互相覆盖）。
    """
    return _active_state_var.get()
