"""重试策略与状态机：分类、预算、退避、状态文案的唯一权威。

此前重试逻辑散在三处、语义各不相同：`client.chat_stream` 内联的退避循环
（且"正文已上屏就不再重试"）、`Agent._chat_with_recovery` 的溢出恢复、
自动标题的计数与冷却。调用点各自 sleep / 各自计数 / 各自拼提示，导致
"重试"这件事没有统一状态可上报，UI 也无法显示进度。

现在收敛到这里，调用方只声明意图：

- **分类**：`retryable()` / `classify()` 判定一个异常是否值得重试、属于哪类；
- **预算与退避**：`RetryPolicy.should_retry()` / `delay()`；
- **状态**：`RetryState` 是"正在重试"的进行态（含恢复时刻），UI 据此画倒计时；
- **执行**：`RetryRunner.run()` 跑完整个重试过程，并在每次退避前上报状态。

禁止在调用点内联 `time.sleep` / 重试计数 / 提示文案——文案统一由
`RetryState` 生成，TUI 与终端共用同一份，避免两处漂移。
"""
from __future__ import annotations

import random
import re
import time as _time
from collections.abc import Callable
from dataclasses import dataclass

import httpx2
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)

from ..cancel import current_token

# 退避参数：2s 起步、×2 指数、25% 抖动、无服务端指示时上限 30s
# （对齐 opencode 的 RETRY_INITIAL_DELAY / BACKOFF_FACTOR / JITTER_FACTOR /
# MAX_DELAY_NO_HEADERS）。抖动避免多客户端同时被限流后同步重试再次撞墙。
RETRY_BASE = 2.0
RETRY_BACKOFF = 2.0
RETRY_JITTER = 0.25
RETRY_MAX_DELAY = 30.0
# 服务端**明确给出** Retry-After 时的上限（对齐 opencode：有响应头就不套用
# NO_HEADERS 的 30s）。限流窗口常有 60s 量级，砍到 30s 提前重试只会再撞一次。
RETRY_AFTER_CAP = 300.0
# 未显式给出策略时的默认尝试次数。客户端恒从 `[limits].max_retries` 构造策略，
# 这个默认只兜住直接使用 `RetryPolicy()` 的调用点（opencode 固定 5 次，不随配置）。
RETRY_DEFAULT_ATTEMPTS = 3

# 不做重试的错误：重试也不会成功，只会浪费预算并延迟用户看到真实原因。
# 上下文溢出也必须排除——它由 Agent 的压缩恢复处理，不属于传输层重试。
NON_RETRYABLE = (
    BadRequestError,          # 参数 / 请求体错误
    AuthenticationError,      # 鉴权失败
    PermissionDeniedError,    # 无权限
    NotFoundError,            # 模型名 / 端点不存在
)

# 传输层瞬时错误：限流、断网、超时、服务端 5xx，以及流中途的传输层中断
# （对端掐断连接、读流超时——OpenAI SDK 原样透传，不包装成 APIConnectionError）。
TRANSIENT_ERRORS = (
    RateLimitError,
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    httpx2.RemoteProtocolError,  # 对端中途断连（incomplete chunked read 等）
    httpx2.ReadError,            # 读取流时连接被重置
    httpx2.ReadTimeout,          # 模型长时间静默超过 llm_timeout
    httpx2.ConnectTimeout,
    httpx2.WriteTimeout,
    httpx2.ConnectError,
)

# 错误**文本**兜底（对齐 opencode 的 RETRYABLE_MESSAGE_PATTERNS）：有些服务商
# 把瞬时故障包成普通异常（`Exception("stream timeout")`），类型和状态码都判不出来，
# 只能看文案。只在"已排除永久错误之后"使用，避免 400 的响应体里恰好含 timeout
# 就被误判成可重试。
RETRYABLE_MESSAGE_RE = re.compile(
    r"rate[ _-]?limit|too many requests|resource[ _-]?exhausted"          # 限流
    r"|overloaded|service[ _-]?unavailable|provider returned error"        # 服务端过载
    r"|internal (?:server )?error|server error|bad gateway"                # 服务端错误
    r"|terminated|socket hang up|connection (?:error|lost|refused|reset)"  # 连接层
    r"|fetch failed|failed to fetch|network[ _-]?error|upstream connect"
    r"|econnrefused|econnreset|etimedout|enotfound|eai_again|getaddrinfo"
    r"|incomplete chunked read|unexpected eof|eof occurred"                # 流被掐断
    # 超时：两种语序都认（"stream timeout" 与 "The read operation timed out"）
    r"|\b(?:request|response|connection|network|stream|read|operation)[ _-]?"
    r"(?:timeout|timed out|time out)\b"
    r"|\b(?:timed out|timeout|time out)\b[^0-9\n]{0,30}?\b"
    r"(?:request|response|connection|network|stream|read|operation|server|provider)\b"
    r"|try (?:your request )?again|temporarily (?:at capacity|unavailable)",
    re.IGNORECASE,
)


def classify(exc: BaseException) -> str:
    """把一个异常归类为面向用户的中文短语（状态行与终端提示共用）。"""
    status = getattr(exc, "status_code", None)
    if isinstance(exc, RateLimitError) or status == 429:
        return "请求过于频繁"
    if isinstance(exc, (httpx2.ReadTimeout, APITimeoutError)):
        return "读取超时"
    if isinstance(exc, (httpx2.ConnectTimeout, httpx2.WriteTimeout)):
        return "连接超时"
    if isinstance(exc, (httpx2.ConnectError, APIConnectionError)):
        return "连接失败"
    if isinstance(exc, httpx2.ReadError):
        return "连接被重置"
    if isinstance(exc, httpx2.RemoteProtocolError):
        return "连接中断"
    if isinstance(exc, InternalServerError) or (status is not None and status >= 500):
        return f"服务端错误({status})" if status else "服务端错误"
    return type(exc).__name__


def describe(exc: BaseException) -> str:
    """`分类: 原始信息`——状态行短、终端提示要能定位问题，两者共用同一分类。"""
    detail = str(exc).strip().replace("\n", " ")
    return f"{classify(exc)}: {detail}" if detail else classify(exc)


def retryable(exc: BaseException) -> bool:
    """该异常是否值得重试。

    判定顺序（与 opencode 的 `retryable()` 同构，且把永久错误放在最前）：

    1. 取消 → 否（不是流故障，是用户按了 Esc，重试等于违背意图）
    2. 5xx 状态码 → 是（部分 SDK 把 5xx 包成 BadRequestError，只看类型会误判）
    3. 已知永久错误（参数 / 鉴权 / 无权限 / 404）→ 否
    4. 已知传输层瞬时类型 → 是
    5. 错误文本兜底 → 部分服务商把瞬时故障包成普通异常，只能看文案；
       放在第 3 步之后是刻意的——4xx 响应体里恰好含 `timeout` 不会被误判。
    """
    token = current_token()
    if token is not None and token.cancelled:
        return False
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and 500 <= status < 600:
        return True  # 5xx 恒为重试对象（部分 SDK 不标 isRetryable）
    if isinstance(exc, NON_RETRYABLE):
        return False
    if isinstance(exc, TRANSIENT_ERRORS):
        return True
    if isinstance(exc, httpx2.HTTPStatusError):
        return exc.response.status_code >= 500
    return RETRYABLE_MESSAGE_RE.search(_error_text(exc)) is not None


def _error_text(exc: BaseException) -> str:
    """异常里可判定可重试性的文本：str(exc) 加服务商原始响应体（若有）。"""
    text = str(exc)
    body = getattr(exc, "body", None) or getattr(exc, "response_body", None)
    if body:
        text = f"{text} {body}"
    return text


def _retry_after(exc: BaseException) -> tuple[float | None, bool]:
    """服务端 `Retry-After` 指示：返回 (秒数, 是否来自响应头)。

    响应头（最可靠）与错误文本里的 `retry_after: N` / `retry-after-ms: N` 都认，
    后者是部分服务商只把指示写进错误文案时的情况。秒数为 None 表示没有指示。
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        raw = headers.get("retry-after-ms") or headers.get("retry-after")
        if raw:
            try:
                value = float(str(raw).strip())
            except (TypeError, ValueError):
                value = None
            if value is not None:
                if headers.get("retry-after-ms"):
                    value /= 1000.0
                return max(0.0, value), True
    match = re.search(
        r"retry[ _-]?after(?:[ _-]?ms)?\D{0,4}(\d+(?:\.\d+)?)",
        _error_text(exc),
        re.IGNORECASE,
    )
    if match:
        return max(0.0, float(match.group(1))), True
    return None, False


@dataclass(frozen=True)
class RetryPolicy:
    """一次重试过程的策略：能不能重试、最多几次、下次等多久。"""

    max_attempts: int = RETRY_DEFAULT_ATTEMPTS
    base: float = RETRY_BASE
    factor: float = RETRY_BACKOFF
    jitter: float = RETRY_JITTER
    cap: float = RETRY_MAX_DELAY
    # 服务端明确给出 Retry-After 时的上限：比本地退避宽得多，因为那是服务端
    # 自己的限流窗口（60s 量级很常见），砍到 30s 提前重试只会再撞一次。
    retry_after_cap: float = RETRY_AFTER_CAP

    def should_retry(self, exc: BaseException, attempt: int) -> bool:
        """attempt 为已失败的尝试序号（1 起），判断是否还允许再试一次。"""
        return attempt < self.max_attempts and retryable(exc)

    def delay(self, attempt: int, exc: BaseException | None = None,
              rng: Callable[[], float] = random.random) -> float:
        """第 attempt 次失败后的退避时长（秒）：服务端指示优先，否则指数 + 抖动。"""
        if exc is not None:
            indicated, from_server = _retry_after(exc)
            if indicated is not None:
                # 服务端指示走自己的上限；本地推算的退避才套 cap
                limit = self.retry_after_cap if from_server else self.cap
                return min(indicated, limit)
        base = self.base * (self.factor ** (attempt - 1))
        return min(base + base * self.jitter * rng(), self.cap)


@dataclass
class RetryState:
    """"正在重试"的进行态：一次退避 + 即将进行的那次尝试。

    attempt 是即将进行的尝试序号（1 起，因此首次重试为 2）、total 是总预算。
    next_at 是恢复时刻（`time.monotonic` 口径），UI 据此画倒计时而不必被
    客户端的每次刷新推着走。
    """

    attempt: int
    total: int
    reason: str
    wait: float
    next_at: float

    def remaining(self, now: float | None = None) -> float:
        """距离恢复还有多久（秒，不小于 0）。"""
        current = _time.monotonic() if now is None else now
        return max(0.0, self.next_at - current)

    def text(self, now: float | None = None) -> str:
        """状态行后缀：「正在重试 2/3 · 8s 后」（带实时倒计时）。"""
        left = self.remaining(now)
        tail = f"{left:.0f}s 后" if left >= 1 else "即将恢复"
        return f"正在重试 {self.attempt}/{self.total} · {tail}"

    def summary(self) -> str:
        """面向终端的一行摘要：「第 1 次尝试中断（读取超时: …），8s 后重试（2/3）」。"""
        return (f"第 {self.attempt - 1} 次尝试中断（{self.reason}），"
                f"{self.wait:.0f}s 后重试（{self.attempt}/{self.total}）")


class RetryRunner:
    """按策略执行 `attempt()`，并在每次退避前上报重试状态。

    `attempt()` 每次调用都是一次**完整请求**（不是"续写半截"）：上游三家
    （opencode / Codex / Claude Code）都是整请求重发，重复的正文由上层按
    各自的模型处理，客户端不掺和。

    适用对象是**返回终值**的调用（如 `Agent._complete` 的摘要 / 标题补全）。
    流式消费不能走这里——生成器无法把控制权交给外层循环，`llm.client.chat_stream`
    自己持有循环，逐步调用 `RetryPolicy` 与 `wait()`，两者共用同一套策略与文案。

    - `on_retry(state)`：退避前回调，用于上报状态。
    - `on_settled()`：过程结束（成功或彻底失败）后回调一次。
    - `wait(state)`：退避方式，默认 `wait()`（可被取消打断）。
    """

    def __init__(self, policy: RetryPolicy | None = None) -> None:
        self.policy = policy or RetryPolicy()

    def run(self, attempt: Callable[[], object], *,
            on_retry: Callable[[RetryState], None] | None = None,
            on_settled: Callable[[], None] | None = None,
            waiting: Callable[[RetryState], None] | None = None,
            rng: Callable[[], float] = random.random) -> object:
        sleeper = waiting or wait
        try:
            for attempt_no in range(1, self.policy.max_attempts + 1):
                try:
                    return attempt()
                except BaseException as e:  # 交策略判定是否值得重试
                    if not self.policy.should_retry(e, attempt_no):
                        raise
                    state = self._state(attempt_no, e, rng)
                    if on_retry is not None:
                        on_retry(state)
                    sleeper(state)
            raise AssertionError("重试循环不应走到这里")  # pragma: no cover
        finally:
            if on_settled is not None:
                on_settled()

    def _state(self, attempt_no: int, exc: BaseException,
               rng: Callable[[], float]) -> RetryState:
        """构造第 attempt_no 次失败后的重试状态（文案在 `RetryState` 里）。"""
        delay = self.policy.delay(attempt_no, exc, rng)
        return RetryState(
            attempt=attempt_no + 1,
            total=self.policy.max_attempts,
            reason=classify(exc),
            wait=delay,
            next_at=_time.monotonic() + delay,
        )


def wait(state: RetryState) -> None:
    """分段 sleep，段间查取消令牌：Esc 能立刻打断退避等待，不必等满时长。"""
    deadline = state.next_at
    while True:
        token = current_token()
        if token is not None and token.cancelled:
            return
        left = deadline - _time.monotonic()
        if left <= 0:
            return
        _time.sleep(min(left, 0.2))
