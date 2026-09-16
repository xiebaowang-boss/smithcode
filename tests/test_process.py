"""外部进程执行测试：正常 / 超时 / 取消与进程树终止。"""
import sys
import threading
import time

from smithcode.cancel import CancellationToken
from smithcode.process import run


def _sleep_command(seconds: int) -> str:
    """构造一条长时间睡眠的命令（跨平台，用当前解释器执行）。"""
    return f'"{sys.executable}" -c "import time; time.sleep({seconds})"'


def test_run_captures_output():
    result = run("echo hello", timeout=10)
    assert result.status == "ok"
    assert result.returncode == 0
    assert "hello" in result.stdout


def test_run_reports_exit_code():
    result = run("exit 3", timeout=10)
    assert result.status == "ok"
    assert result.returncode == 3


def test_run_timeout_terminates_promptly():
    """超时即终止进程，不等命令自然结束。"""
    start = time.monotonic()
    result = run(_sleep_command(30), timeout=1)
    elapsed = time.monotonic() - start
    assert result.status == "timeout"
    assert elapsed < 6  # 远小于 30s，证明进程被终止


def test_run_cancel_terminates_promptly():
    """取消令牌触发时终止进程，无需等下一块输出或超时。"""
    token = CancellationToken()
    threading.Thread(
        target=lambda: (time.sleep(0.2), token.cancel()), daemon=True
    ).start()

    start = time.monotonic()
    result = run(_sleep_command(30), timeout=30, token=token)
    elapsed = time.monotonic() - start
    assert result.status == "interrupted"
    assert elapsed < 6


def test_run_without_token_completes_normally():
    """无取消令牌（并行 worker 场景）时行为不变，正常收尾。"""
    result = run("echo ok", timeout=10)
    assert result.status == "ok"
    assert "ok" in result.stdout


def test_run_without_capture_output_returns_promptly():
    """capture_output=False 不等后台子进程：适用于 fork 后长驻的命令。

    这类命令（如剪贴板工具 wl-copy）的子进程会继承输出管道并持有到命令
    真正被替换，捕获管道时 communicate 一直等不到 EOF，会挂到超时才返回。
    用例用 `sleep &` 复现同样的管道继承形态；含 stdin 输入也不受影响。
    """
    if sys.platform == "win32":
        return  # 依赖 POSIX shell 的后台子进程与管道继承语义
    start = time.monotonic()
    result = run("sleep 5 &", timeout=2, input="喂给子进程的数据", capture_output=False)
    elapsed = time.monotonic() - start
    assert result.status == "ok"
    assert result.returncode == 0
    assert elapsed < 1.5  # 远小于 5s，证明没有等后台子进程
