"""协作式取消：Esc 中断当前任务的统一原语。

同步线程模型下的取消不走异常传播（运行中的线程不可强杀），而是令牌 +
检查点：发起方 cancel()，各层在自然检查点（流数据块、工具批边界、轮次
边界）查询令牌并尽快收尾。令牌经 ContextVar 沿调用链隐式传播——
agent.run() 全程运行在同一线程（含其驱动的一切生成器），LLM 流层与
工具调度层按需读取，接口零侵入；跨线程入口（TUI / REPL 主线程）只调
agent.interrupt()，不接触令牌本身。
"""
from __future__ import annotations

import threading
from contextvars import ContextVar
from dataclasses import dataclass


class Cancelled(Exception):
    """取消令牌触发时由检查点抛出（预留：供未来需要异常式传播的场景）。"""

    def __init__(self, reason: str = "用户中断"):
        super().__init__(reason)
        self.reason = reason


class CancellationToken:
    """线程安全的取消令牌：幂等 cancel、任意线程查询。"""

    def __init__(self):
        self._event = threading.Event()
        self._reason: str | None = None
        self._listeners: list = []

    def cancel(self, reason: str = "用户中断") -> None:
        """请求取消（幂等，线程安全）；已登记的监听回调立即收到通知。"""
        if self._event.is_set():
            return
        self._reason = reason
        self._event.set()
        for callback in list(self._listeners):
            try:
                callback()
            except Exception:  # noqa: BLE001, S110 通知失败不影响取消本身
                pass

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    def subscribe(self, callback) -> None:
        """登记取消通知回调（如 UI 显示「正在停止…」）。"""
        self._listeners.append(callback)


# 当前轮次的活跃令牌：agent.run() 激活，llm 流层 / 工具调度层按需读取
_current: ContextVar = ContextVar("smithcode_cancel_token", default=None)


def current_token() -> CancellationToken | None:
    """当前线程活跃轮次的取消令牌；无任务运行时为 None。"""
    return _current.get()


def activate_token(token: CancellationToken | None):
    """激活令牌并返回复位函数（run() 在 finally 中调用，恢复外层状态）。"""
    ticket = _current.set(token)

    def reset() -> None:
        _current.reset(ticket)

    return reset


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
