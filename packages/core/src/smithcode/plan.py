"""任务拆分与分步骤执行：todo_write / todo_read 维护的步骤清单（会话级状态）。

借鉴 opencode 的 TodoWrite：模型用 todo_write 一次性提交全量最新清单（非增量），
状态为 pending / in_progress / completed / cancelled。每项有服务端分配的稳定 id：
标题（title）不可变（避免侧边栏看到内容跳动），描述（description）/ 状态 /
reason 可变。清单存于本模块进程内状态（会话口径，/new 时 reset），由 Agent 实时
渲染到终端，REPL 的 /plan 命令随时查看。清单只是追踪工具，不是指令。
"""
from __future__ import annotations

import uuid
from contextvars import ContextVar

STATUSES = ("pending", "in_progress", "completed", "cancelled")
MAX_ITEMS = 50  # 单份清单上限，防止模型一次提交超大清单撑爆上下文

_STATUS_ICON = {
    "pending": "○",
    "in_progress": "●",
    "completed": "✓",
    "cancelled": "✕",
}

# ANSI 颜色与 agent.py 一致：in_progress 加粗高亮，completed/cancelled 置灰
_BOLD = "\033[1m"
_DIM = "\033[90m"
_RESET = "\033[0m"

_PLACEHOLDER = "（暂无任务计划，模型可在多步任务开始时用 todo_write 建立清单）"


class TodoList:
    """步骤清单：每次 replace 整体替换；按 id 维持标题不可变、其余字段可更新。"""

    def __init__(self, items: list[dict] | None = None):
        self.items: list[dict] = []
        if items:
            self.replace(items)

    def replace(self, todos: list) -> None:
        """清洗并整体替换清单：既有项保留原标题，描述/状态/reason 可更新。

        - 空标题忽略；非法状态降级 pending；单份上限 MAX_ITEMS。
        - 带 id 的项按 id 精确匹配（标题不可变，其余字段更新）。
        - 无 id 时按标题匹配既有项（全量重写不带 id 的常见情形），保住标题不变；
          匹配不到视为新项，分配新 id。
        """
        prev = list(self.items)
        cleaned = []
        for t in todos[:MAX_ITEMS]:
            title = str(t.get("title", "")).strip()
            status = t.get("status", "pending")
            if status not in STATUSES:
                status = "pending"
            description = str(t.get("description", "")).strip()
            reason = str(t.get("reason", "")).strip()
            if not title:
                continue
            item = self._match(prev, t.get("id"), title)
            if item is not None:
                prev.remove(item)
                item["status"] = status
                item["description"] = description
                item["reason"] = reason
                cleaned.append(item)
            else:
                cleaned.append(
                    {
                        "id": uuid.uuid4().hex[:8],
                        "title": title,
                        "status": status,
                        "description": description,
                        "reason": reason,
                    }
                )
        self.items = cleaned

    @staticmethod
    def _match(prev: list, tid, title) -> dict | None:
        """在既有项里找同一条：优先 id，其次标题。"""
        if tid:
            for p in prev:
                if p.get("id") == tid:
                    return p
        for p in prev:
            if p.get("title") == title:
                return p
        return None

    def count(self, status: str) -> int:
        return sum(1 for i in self.items if i["status"] == status)

    def render(self, color: bool = False, titles_only: bool = False) -> str:
        """渲染清单；color=True 加色（in_progress 加粗、完成/取消置灰）。
        titles_only=True 只显示标题（TUI 侧边栏用），否则含描述与 reason。"""
        return self._render_list(self.items, color, titles_only)

    def render_status(self, status: str, color: bool = False) -> str:
        """只渲染指定状态的项（todo_read 的 status 过滤用）。"""
        return self._render_list(
            [i for i in self.items if i["status"] == status], color, False
        )

    @staticmethod
    def _render_list(items: list, color: bool, titles_only: bool) -> str:
        lines = []
        for idx, item in enumerate(items, 1):
            icon = _STATUS_ICON.get(item["status"], "○")
            line = f"  {icon} {idx}. {item['title']}"
            if not titles_only and item.get("reason"):
                line += f"  — {item['reason']}"
            if color:
                if item["status"] == "in_progress":
                    line = f"{_BOLD}{line}{_RESET}"
                elif item["status"] in ("completed", "cancelled"):
                    line = f"{_DIM}{line}{_RESET}"
            lines.append(line)
            if not titles_only and item.get("description"):
                lines.append(f"      {item['description']}")
        return "\n".join(lines)


class PlanState:
    """一个会话的步骤清单状态（原模块级单例 `_current`）。

    为什么要有实例：清单属于**会话**（`/new` 清空、恢复时按 `t=state` 读回），
    而模块单例在"恢复另一个会话"时会串味。实例由 `AgentSession` 持有，见
    `bind()`。
    """

    def __init__(self) -> None:
        self.current = TodoList()

    def inherit(self, other: PlanState) -> None:
        """接管另一个实例的当前清单（会话建立时从默认实例接过来，见 GoalState.inherit）。

        **复制**而不是别名：`self.current = other.current` 会让两个会话共用同一个
        TodoList——一个会话改清单会改到另一个的（探针
        `tests/guard/test_multi_session_isolation.py` 抓的就是它）。
        """
        self.current = TodoList([dict(item) for item in other.current.items])


    def reset(self) -> None:
        """清空当前会话的步骤清单（/new 时调用）。"""
        self.current = TodoList()

    def snapshot(self) -> dict:
        """会话级步骤清单快照（持久化投影缓存用）。"""
        return {"items": [dict(item) for item in self.current.items]}

    def restore(self, data) -> None:
        """从快照恢复清单（id 与标题不可变语义保留；非法数据清空）。"""
        items = data.get("items") if isinstance(data, dict) else None
        self.current = TodoList(items if isinstance(items, list) else [])

# 进程级默认实例：没有会话绑定时的落点（直接调本模块的测试、无 Agent 的路径）
_default_state = PlanState()
# 活动状态：**按上下文**解析（不是进程级指针）。
# 同进程并发两个会话时，各自的 goal/plan/skills 互不覆盖——这正是把
# "活动实例"从模块全局换成 ContextVar 要解决的问题（事件总线/取消信号同款做法）。
_active_state_var: ContextVar[PlanState] = ContextVar("smithcode_plan_state", default=_default_state)


def bind(state: PlanState | None) -> None:
    """切换本模块函数作用的状态实例（`None` = 回到默认实例）。

    与 `event.activate()` / `signal.activate_token()` 同一套"活动实例"模式：
    调用点太多（session.py、commands/*、tui/app.py、tui/sidebar），与其逐个改成
    `session.plan_state.xxx()`，不如让既有函数指向当前实例。**局限**：同一进程
    同时跑两个会话会互相覆盖——这与改造前的单例行为一致，不会更糟；TUI/REPL
    都是一个进程一个会话（阶段 D 会把它收敛到会话对象上）。
    """
    _active_state_var.set(state if state is not None else _default_state)



def current() -> TodoList:
    return _active_state_var.get().current





def has_active() -> bool:
    """是否存在未完结步骤（pending / in_progress）——侧边栏任务区是否展示的依据。

    opencode 式：无任务或全部完成 / 取消时不展示任务区，有进行中或待办步骤才展示。
    """
    return any(i["status"] in ("pending", "in_progress") for i in _active_state_var.get().current.items)


def summary() -> str:
    """一行状态速览，如「共 3 步 · 已完成 1 · 进行中 1」。"""
    items = _active_state_var.get().current.items
    if not items:
        return "暂无任务计划"
    parts = [f"共 {len(items)} 步"]
    done = _active_state_var.get().current.count("completed")
    if done:
        parts.append(f"已完成 {done}")
    prog = _active_state_var.get().current.count("in_progress")
    if prog:
        parts.append(f"进行中 {prog}")
    return " · ".join(parts)


def render_current(color: bool = False, status: str | None = None) -> str:
    """当前清单的完整渲染（标题 + 描述 + reason）；status 非空时只返回该状态。"""
    if not _active_state_var.get().current.items:
        return _PLACEHOLDER
    if status is None:
        return _active_state_var.get().current.render(color=color)
    return _active_state_var.get().current.render_status(status, color=color) or f"（无 {status} 状态的步骤）"


def render_titles(color: bool = False) -> str:
    """仅标题的紧凑渲染（TUI 侧边栏用），不含描述与 reason。"""
    if not _active_state_var.get().current.items:
        return "（暂无任务计划）"
    return _active_state_var.get().current.render(color=color, titles_only=True)


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


def default_state() -> PlanState:
    """进程级默认实例（无会话绑定时的作用对象，会话建立时从其继承）。"""
    return _default_state


def active_state() -> PlanState:
    """当前生效的实例（会话建立时从它接管状态，见 AgentSession）。

    按**上下文**解析：并发会话各拿各的（同进程多会话不会互相覆盖）。
    """
    return _active_state_var.get()
