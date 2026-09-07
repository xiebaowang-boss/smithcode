"""任务拆分与分步骤执行：todo_write 维护的步骤清单（会话级状态）。

借鉴 opencode 的 TodoWrite：模型用 todo_write 工具一次性提交全量最新
清单（非增量），状态为 pending / in_progress / completed / cancelled。
清单存于本模块进程内状态（会话口径，/new 时 reset），由 Agent 实时渲染
到终端，REPL 的 /plan 命令随时查看。清单只是追踪工具，不是指令。
"""
from __future__ import annotations

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


class TodoList:
    """步骤清单：每次 replace 整体替换，保持模型提交的顺序。"""

    def __init__(self, items: list[dict] | None = None):
        self.items: list[dict] = []
        if items:
            self.replace(items)

    def replace(self, todos: list[dict]) -> None:
        """清洗并整体替换清单：忽略空内容，非法状态降级为 pending。"""
        cleaned = []
        for t in todos[:MAX_ITEMS]:
            content = str(t.get("content", "")).strip()
            if not content:
                continue
            status = t.get("status", "pending")
            if status not in STATUSES:
                status = "pending"
            cleaned.append(
                {
                    "content": content,
                    "status": status,
                    "reason": str(t.get("reason", "")).strip(),
                }
            )
        self.items = cleaned

    def count(self, status: str) -> int:
        return sum(1 for i in self.items if i["status"] == status)

    def render(self, color: bool = False) -> str:
        """渲染清单；color=True 时终端加色（in_progress 高亮、完成/取消置灰）。"""
        lines = []
        for idx, item in enumerate(self.items, 1):
            icon = _STATUS_ICON.get(item["status"], "○")
            line = f"  {icon} {idx}. {item['content']}"
            if item["reason"]:
                line += f"  — {item['reason']}"
            if color:
                if item["status"] == "in_progress":
                    line = f"{_BOLD}{line}{_RESET}"
                elif item["status"] in ("completed", "cancelled"):
                    line = f"{_DIM}{line}{_RESET}"
            lines.append(line)
        return "\n".join(lines)


_current = TodoList()


def current() -> TodoList:
    return _current


def reset() -> None:
    """清空当前会话的步骤清单（/new 时调用）。"""
    global _current
    _current = TodoList()


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


def render_current(color: bool = False) -> str:
    """当前清单的渲染结果；空清单返回占位提示。"""
    if not _current.items:
        return "（暂无任务计划，模型可在多步任务开始时用 todo_write 建立清单）"
    return _current.render(color=color)