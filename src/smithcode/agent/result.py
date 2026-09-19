"""一次任务的结束状态。

从 `cancel.py` 移入 `agent/`：`cancel.py` 将改为对外的兼容别名层，而
`agent/` 内部（`agent.py` / `events.py`）需要 `RunResult`——若仍从
`cancel.py` 取就会形成 `cancel → agent.signal` 与 `agent.* → cancel` 的
导入环（`agent` 包初始化期间会先执行 `agent/agent.py`，此时 `cancel` 尚未
定义出任何名字）。
"""

from __future__ import annotations

from dataclasses import dataclass

RESULT_STATUSES = frozenset({
    "ok",            # 正常结束（模型给出最终回复）
    "interrupted",   # 用户中断 / 取消
    "denied",        # 权限被拒（任务停止）
    "max_iterations",  # 达到迭代上限后的收尾轮
    "stream_error",  # 响应流中途断开（部分正文已入库）
})
"""`RunResult.status` 的**全部**取值。

宿主按它分支渲染，所以取值集合是冻结的：新增取值必须同时改这里与各个宿主，
否则前端会走进"未知状态"的兜底分支。`tests/agent/test_result_status.py` 静态
扫描源码里的 `RunResult("<字面量>"`，保证没有绕过这个集合的新状态。
"""


@dataclass
class RunResult:
    """一次任务的结束状态：status 区分终止原因，text 为回显文本。

    partial 表示截停于流中（部分正文已入库）；宿主层（REPL / TUI）按
    status 决定提示文案与渲染，agent 层不再产出面向用户的哨兵字符串。
    `stream_error` 是响应流中途断开（读完超时 / 对端掐断连接）：部分正文
    已入库，`text` 即那部分内容，`reason` 是失败原因（`分类: 原始信息`），
    宿主应提示"输出中断"并带上原因而非当作正常结束——只报中断不报原因，
    用户无从判断是超时、限流还是对端掐断。
    tools_used 为本次任务实际执行过的工具名（按调用顺序去重保序），供
    /goal 的续跑裁决使用：续跑轮没有任何工具调用视为空转、有推进动作
    则重置阻碍连击。
    """

    status: str  # "ok" | "interrupted" | "denied" | "max_iterations" | "stream_error"
    text: str = ""
    partial: bool = False
    tools_used: tuple = ()
    reason: str = ""  # stream_error：失败原因（`分类: 原始信息`），其余状态为空
