"""共享 asyncio 事件循环线程：MCP 子系统的异步宿主。

官方 SDK v2 的 `Client` / 传输 / OAuth 全部是 async；本项目其余部分保持
同步线程模型。`AsyncRuntime` 把**唯一的** asyncio 事件循环关在专用线程里，
对外只暴露同步的 `run` / `submit` / `call_soon`：所有 MCP 连接（stdio /
Streamable HTTP / SSE）与 OAuth 流程都跑在这个 loop 上，Agent、工具、渲染、
权限层完全无感知。

约定：loop 线程内禁止阻塞调用。需要文件 / 浏览器等待等阻塞操作时，协程内用
`anyio.to_thread` / `asyncio.to_thread` 下放，避免卡住整个事件循环。
"""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future


class AsyncRuntime:
    """一个专用线程 + 一个事件循环；生命周期由 McpService 托管。"""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """启动 loop 线程（幂等）；返回时事件循环已就绪。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._run, name="smithcode-mcp-loop", daemon=True
            )
            self._thread.start()
        self._ready.wait()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            finally:
                loop.close()

    def stop(self, timeout: float = 5.0) -> None:
        """停止事件循环并回收线程（幂等）。遗留任务在收尾时统一取消。"""
        with self._lock:
            loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return
        if not loop.is_closed():
            loop.call_soon_threadsafe(loop.stop)
        if thread.is_alive():
            thread.join(timeout)
        with self._lock:
            self._loop = None
            self._thread = None

    # ---------- 跨线程调度 ----------

    @property
    def running(self) -> bool:
        loop = self._loop
        return loop is not None and not loop.is_closed()

    def submit(self, coro) -> Future:
        """把协程投递到 loop 线程，返回 concurrent Future（不等待）。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            coro.close()  # 避免 "coroutine was never awaited" 警告
            raise RuntimeError("MCP 事件循环未启动")
        return asyncio.run_coroutine_threadsafe(coro, loop)

    def run(self, coro, timeout: float | None = None):
        """同步等待一个协程完成（从非 loop 线程调用）。"""
        return self.submit(coro).result(timeout)

    def call_soon(self, fn, *args) -> None:
        """在 loop 线程上尽快执行一个普通回调（线程安全）。"""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(fn, *args)
