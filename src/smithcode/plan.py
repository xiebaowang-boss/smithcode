"""任务拆分与分步骤执行：todo_write / todo_read 维护的步骤清单（会话级状态）。

借鉴 opencode 的 TodoWrite：模型用 todo_write 一次性提交全量最新清单（非增量），
状态为 pending / in_progress / completed / cancelled。每项有服务端分配的稳定 id：
标题（title）不可变（避免侧边栏看到内容跳动），描述（description）/ 状态 /
reason 可变。清单存于本模块进程内状态（会话口径，/new 时 reset），由 Agent 实时
渲染到终端，REPL 的 /plan 命令随时查看。清单只是追踪工具，不是指令。
"""
from __future__ import annotations

import uuid

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


_current = TodoList()


def current() -> TodoList:
    return _current


def reset() -> None:
    """清空当前会话的步骤清单（/new 时调用）。"""
    global _current
    _current = TodoList()


def snapshot() -> dict:
    """会话级步骤清单快照（持久化投影缓存用）。"""
    return {"items": [dict(item) for item in _current.items]}


def restore(data) -> None:
    """从快照恢复清单（id 与标题不可变语义保留；非法数据清空）。"""
    global _current
    items = data.get("items") if isinstance(data, dict) else None
    _current = TodoList(items if isinstance(items, list) else [])


def has_active() -> bool:
    """是否存在未完结步骤（pending / in_progress）——侧边栏任务区是否展示的依据。

    opencode 式：无任务或全部完成 / 取消时不展示任务区，有进行中或待办步骤才展示。
    """
    return any(i["status"] in ("pending", "in_progress") for i in _current.items)


def summary() -> str:
    """一行状态速览，如「共 3 步 · 已完成 1 · 进行中 1」。"""
    items = _current.items
    if not items:
        return "暂无任务计划"
    parts = [f"共 {len(items)} 步"]
    done = _current.count("completed")
    if done:
        parts.append(f"已完成 {done}")
    prog = _current.count("in_progress")
    if prog:
        parts.append(f"进行中 {prog}")
    return " · ".join(parts)


def render_current(color: bool = False, status: str | None = None) -> str:
    """当前清单的完整渲染（标题 + 描述 + reason）；status 非空时只返回该状态。"""
    if not _current.items:
        return _PLACEHOLDER
    if status is None:
        return _current.render(color=color)
    return _current.render_status(status, color=color) or f"（无 {status} 状态的步骤）"


def render_titles(color: bool = False) -> str:
    """仅标题的紧凑渲染（TUI 侧边栏用），不含描述与 reason。"""
    if not _current.items:
        return "（暂无任务计划）"
    return _current.render(color=color, titles_only=True)
