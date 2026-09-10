"""外部命令执行的唯一出口：创建、超时、取消与进程树终止。

所有需要执行外部命令的代码都走 `run()`，不要直接调用 `subprocess`——
超时与中断（Esc / Ctrl+C）语义集中在此一处，避免各工具各写一套。工具层
只负责组装命令与把 `ProcessResult` 翻译成给模型的文案。

取消接入：`run()` 默认读取当前线程的取消令牌（`cancel.current_token()`），
因此运行在 Agent 的 run 线程上的 serial 工具（如 `run_command`）天然获得
中断能力，工具侧零接线；并行 worker 线程读不到令牌（返回 None），安全
降级为不响应取消。若未来并行工具需要取消，可显式传入捕获到的 `token`。
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass

from .cancel import CancellationToken, current_token

# 轮询步长：取消 / 超时判定的最大延迟
_POLL_INTERVAL = 0.2
# POSIX 先 SIGTERM 的宽限期，超过则 SIGKILL
_TERM_GRACE = 2.0


@dataclass
class ProcessResult:
    """一次命令执行的结构化结果：状态由 status 区分，输出原样带回。"""

    returncode: int | None
    stdout: str
    stderr: str
    status: str  # "ok" | "timeout" | "interrupted"


def run(command: str, *, timeout: float, cwd=None, env=None,
        token: CancellationToken | None = None) -> ProcessResult:
    """执行命令直到结束 / 超时 / 被取消；超时与取消都会终止整个进程树。

    token 缺省时读取当前线程令牌（current_token()）。轮询用
    `communicate(timeout=…)`：官方保证捕获 TimeoutExpired 后重试不丢已缓冲
    输出，因此无需跨线程关闭管道即可实现取消。
    """
    if token is None:
        token = current_token()
    proc = _spawn(command, cwd, env)
    deadline = time.monotonic() + timeout
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=_POLL_INTERVAL)
            return ProcessResult(proc.returncode, stdout or "", stderr or "", "ok")
        except subprocess.TimeoutExpired:
            if token is not None and token.cancelled:
                _terminate_tree(proc)
                out, err = _reap(proc)
                return ProcessResult(proc.returncode, out, err, "interrupted")
            if time.monotonic() >= deadline:
                _terminate_tree(proc)
                out, err = _reap(proc)
                return ProcessResult(proc.returncode, out, err, "timeout")


def _spawn(command: str, cwd, env) -> subprocess.Popen:
    """启动 shell 命令；POSIX 下放入独立进程组，便于整组终止。"""
    return subprocess.Popen(
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        cwd=cwd,
        env=env,
        start_new_session=(os.name != "nt"),
    )


def _terminate_tree(proc: subprocess.Popen) -> None:
    """终止进程及其全部后代：Windows 用 taskkill /T，POSIX 用进程组信号升级。"""
    if proc.poll() is not None:
        return  # 已退出，无需处理
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
        try:
            proc.wait(timeout=_TERM_GRACE)
        except subprocess.TimeoutExpired:
            os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _reap(proc: subprocess.Popen) -> tuple[str, str]:
    """终止后尽力回收输出与退出码；管道若被存活后代占住也不无限阻塞。"""
    try:
        return proc.communicate(timeout=_TERM_GRACE)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            return proc.communicate(timeout=_TERM_GRACE)
        except subprocess.TimeoutExpired:
            return "", ""
