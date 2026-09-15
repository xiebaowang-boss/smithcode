"""子代理执行编排：并发上限、取消级联、渲染作用域与结果契约。

父 Agent 的 `_preflight` 为 task 工具构造 runner 闭包并调用本模块。执行流程：
获取并发槽位 → 派生隔离的子 Agent → 绑定 scoped 渲染后端与子取消令牌（父令牌
级联）→ 子循环同步跑完 → 用量并入父账本 → 把结果包装成报告字符串回传。
子代理的中间过程只进子会话历史，主对话只看到报告。
"""
from __future__ import annotations

import threading
import time

from .. import config, renderer
from ..cancel import CancellationToken, current_token

_SLOT_LOCK = threading.Lock()
_SLOTS: threading.Semaphore | None = None
_SLOTS_SIZE = 0

_STATUS_LABELS = {
    "ok": "完成",
    "interrupted": "已中断",
    "denied": "因权限被拒而停止",
    "max_iterations": "到达轮次上限",
}


def run_task(parent, spec, prompt: str, label: str = "", tool_id: int | None = None) -> str:
    """在隔离上下文中执行一个子任务，返回回传给主代理的报告文本。"""
    cfg = config.SUBAGENTS
    if not cfg.enabled:
        return "错误: 子代理功能已被配置禁用（[subagents].enabled=false）。"
    slots = _slots()
    slots.acquire()
    try:
        child = parent.fork_subagent(spec)
        child_token = CancellationToken()
        parent_token = current_token()
        if parent_token is not None:
            if parent_token.cancelled:
                # 排队等槽位期间父级已被中断：新令牌不会收到既成事实的通知，需补一次
                child_token.cancel(parent_token.reason or "用户中断")
            parent_token.subscribe(child_token.cancel)  # Esc → 父子一起停
        scope = renderer.Scope(task_id=int(tool_id or 0), agent=spec.name)
        reset_renderer = renderer.activate(renderer.current().scoped(scope))
        watchdog = None
        if cfg.timeout and cfg.timeout > 0:
            watchdog = threading.Timer(float(cfg.timeout), lambda: child_token.cancel("子任务超时"))
            watchdog.daemon = True
            watchdog.start()
        start = time.monotonic()
        try:
            try:
                result = child.run(prompt, token=child_token)
            finally:
                if watchdog is not None:
                    watchdog.cancel()
                reset_renderer()
            _merge_usage(parent, child)
            return format_report(spec, result, time.monotonic() - start, label)
        except Exception as e:  # noqa: BLE001 子代理异常不破坏父级轮次，转为错误结果
            return f"错误: 子代理执行失败（{type(e).__name__}: {e}）"
    finally:
        slots.release()


def format_report(spec, result, elapsed: float, label: str = "") -> str:
    """把子代理的 RunResult 包装成主代理看到的报告（状态 + 任务短名 + 正文）。"""
    status = _STATUS_LABELS.get(result.status, result.status)
    seconds = f"{elapsed:.1f}s" if elapsed < 10 else f"{elapsed:.0f}s"
    tag = f"（{label}）" if label and label != spec.name else ""
    head = f"[子代理 {spec.name}{tag} {status} · {seconds}]"
    body = (result.text or "").strip()
    if result.status == "interrupted" and body:
        body = "（以下为中断前已产出的结论）\n" + body
    if not body:
        body = "（子代理没有产出文本结论）"
    return f"{head}\n{body}"


def _merge_usage(parent, child) -> None:
    """把子代理的 token 用量并入父会话账本（两个口径都记，界面可见真实开销）。"""
    source = child.session.usage.current_session
    if not source.calls:
        return
    for target in (parent.session.usage.since_start, parent.session.usage.current_session):
        target.calls += source.calls
        for field, value in source.totals.items():
            target.totals[field] = target.totals.get(field, 0) + value
    child.session.usage.reset_session()


def _slots() -> threading.Semaphore:
    """并发槽位信号量；配置变化（测试 monkeypatch）时按新值重建。"""
    global _SLOTS, _SLOTS_SIZE
    size = max(1, int(config.SUBAGENTS.max_concurrency))
    with _SLOT_LOCK:
        if _SLOTS is None or _SLOTS_SIZE != size:
            _SLOTS = threading.Semaphore(size)
            _SLOTS_SIZE = size
        return _SLOTS
