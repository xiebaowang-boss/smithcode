"""重试策略层测试：分类、预算、退避、状态文案与可取消退避。

这一层是"是否重试 / 等多久 / 怎么显示"的唯一权威，client 与 Agent 都只调用它，
所以这里的判定必须钉死——它同时决定"该重试的别放弃"和"不该重试的别浪费预算"。
"""
import time

import httpx2
import pytest
from openai import AuthenticationError, BadRequestError, RateLimitError

from smithcode import config
from smithcode.cancel import CancellationToken, activate_token
from smithcode.llm.retry import (
    RetryPolicy,
    RetryState,
    build_retry_state,
    classify,
    describe,
    retryable,
    stream_with_retry,
    wait,
)


def _status_error(code: int, exc=BadRequestError):
    response = httpx2.Response(code, request=httpx2.Request("POST", "http://x"))
    return exc("boom", response=response, body=None)


# ---------- 分类 ----------

@pytest.mark.parametrize("exc, expected", [
    (httpx2.ReadTimeout("read timed out"), "读取超时"),
    (httpx2.ConnectTimeout("connect"), "连接超时"),
    (httpx2.ConnectError("refused"), "连接失败"),
    (httpx2.ReadError("reset"), "连接被重置"),
    (httpx2.RemoteProtocolError("incomplete chunked read"), "连接中断"),
    (_status_error(429, RateLimitError), "请求过于频繁"),
    (_status_error(503, BadRequestError), "服务端错误(503)"),
    (ValueError("别的"), "ValueError"),
])
def test_classify_maps_to_chinese_reason(exc, expected):
    assert classify(exc) == expected


def test_describe_keeps_original_message():
    """状态行要短，终端提示要能定位问题——两者共用同一分类。"""
    text = describe(httpx2.ReadTimeout("The read operation timed out"))
    assert text.startswith("读取超时: ")
    assert "read operation timed out" in text


# ---------- 可重试判定 ----------

@pytest.mark.parametrize("exc", [
    httpx2.ReadTimeout("x"),
    httpx2.ConnectError("x"),
    httpx2.RemoteProtocolError("x"),
    httpx2.ReadError("x"),
    _status_error(429, RateLimitError),
    _status_error(500, BadRequestError),   # 5xx 即使 SDK 未标 isRetryable 也重试
])
def test_retryable_true_for_transient(exc):
    assert retryable(exc) is True


@pytest.mark.parametrize("exc", [
    _status_error(400),
    _status_error(401, AuthenticationError),
    _status_error(404),
    ValueError("逻辑错误"),
])
def test_retryable_false_for_permanent(exc):
    assert retryable(exc) is False


def test_retryable_false_after_cancel():
    """取消引发的读错误不是流故障：重试等于违背用户意图。"""
    token = CancellationToken()
    reset = activate_token(token)
    try:
        assert retryable(httpx2.ReadTimeout("x")) is True
        token.cancel()
        assert retryable(httpx2.ReadTimeout("x")) is False
    finally:
        reset()


# ---------- 预算与退避 ----------

def test_budget_counts_attempts():
    policy = RetryPolicy(max_attempts=3)
    err = httpx2.ReadTimeout("x")
    assert policy.should_retry(err, 1) is True
    assert policy.should_retry(err, 2) is True
    assert policy.should_retry(err, 3) is False  # 第 3 次失败即用尽
    assert policy.should_retry(_status_error(400), 1) is False


def test_delay_is_exponential_with_jitter_and_cap():
    policy = RetryPolicy(max_attempts=5, cap=100.0)
    # 抖动注入固定值以断言确定性：base * factor^(n-1) * (1 + jitter)
    assert policy.delay(1, None, rng=lambda: 0.0) == 2.0
    assert policy.delay(2, None, rng=lambda: 0.0) == 4.0
    assert policy.delay(3, None, rng=lambda: 0.0) == 8.0
    assert policy.delay(1, None, rng=lambda: 1.0) == 2.5  # +25%
    assert policy.delay(4, None, rng=lambda: 0.0) == 16.0
    assert policy.delay(9, None, rng=lambda: 1.0) == 100.0  # 撞 cap


def test_delay_prefers_server_retry_after():
    """服务端给了 Retry-After 就照办，不猜本地退避。"""
    response = httpx2.Response(
        429, request=httpx2.Request("POST", "http://x"), headers={"retry-after": "7"},
    )
    exc = RateLimitError("slow down", response=response, body=None)
    assert RetryPolicy().delay(1, exc, rng=lambda: 0.0) == 7.0


def test_delay_caps_server_retry_after_at_wide_limit():
    """服务端指示只受宽上限约束（300s），不受本地退避 cap（30s）约束。"""
    response = httpx2.Response(
        429, request=httpx2.Request("POST", "http://x"), headers={"retry-after": "9999"},
    )
    exc = RateLimitError("slow down", response=response, body=None)
    assert RetryPolicy(cap=30.0).delay(1, exc) == 300.0


# ---------- 状态文案 ----------

def test_state_text_has_live_countdown():
    state = RetryState(attempt=2, total=3, reason="读取超时", wait=8.0,
                       next_at=time.monotonic() + 8.0)
    assert "正在重试 2/3" in state.text()
    assert "8s 后" in state.text()
    # 倒计时随时间递减（UI 每帧自己算，不靠客户端推事件）
    assert state.remaining(state.next_at - 3.0) == pytest.approx(3.0)
    assert state.remaining(state.next_at + 1.0) == 0.0
    assert "即将恢复" in state.text(state.next_at - 0.2)


def test_state_summary_reads_like_a_sentence():
    state = RetryState(attempt=2, total=3, reason="读取超时", wait=8.0, next_at=0.0)
    text = state.summary()
    assert "第 1 次尝试中断" in text and "读取超时" in text and "重试（2/3）" in text


# ---------- 执行器与等待 ----------

def test_stream_retries_then_succeeds():
    """流式执行器：失败尝试的增量照常透出，终值事件只认最后一次成功尝试。"""
    calls = []
    states = []

    def attempt():
        calls.append(1)
        if len(calls) < 3:
            yield ("content", f"第{len(calls)}次半截")
            yield ("message", {"role": "assistant", "content": "坏的终值"})
            raise httpx2.ReadTimeout("x")
        yield ("content", "最终正文")
        yield ("message", {"role": "assistant", "content": ""})
        yield ("usage", {"prompt_tokens": 1})

    events = list(stream_with_retry(
        attempt, RetryPolicy(max_attempts=5),
        on_retry=states.append, waiting=lambda s: None,
    ))

    assert len(calls) == 3
    assert [s.attempt for s in states] == [2, 3]
    kinds = [kind for kind, _ in events]
    assert kinds.count("content") == 3  # 两次半截 + 一次最终，都实时透出
    messages = [payload for kind, payload in events if kind == "message"]
    assert messages == [{"role": "assistant", "content": ""}]  # 失败尝试的终值不污染
    assert [payload for kind, payload in events if kind == "usage"] == [
        {"prompt_tokens": 1}]


def test_stream_settles_on_both_paths():
    settled = []

    def fail():
        yield from ()
        raise ValueError("不可重试")

    with pytest.raises(ValueError):
        list(stream_with_retry(fail, RetryPolicy(),
                               on_settled=lambda: settled.append("err"),
                               waiting=lambda s: None))
    assert settled == ["err"]

    settled.clear()
    assert list(stream_with_retry(lambda: iter([("content", "好")]), RetryPolicy(),
                                  on_settled=lambda: settled.append("ok"),
                                  waiting=lambda s: None)) == [("content", "好")]
    assert settled == ["ok"]


def test_stream_does_not_retry_system_exit():
    """系统退出透传：不重试、不上报、不等待。"""
    states = []

    def attempt():
        yield from ()
        raise SystemExit(1)

    with pytest.raises(SystemExit):
        list(stream_with_retry(attempt, RetryPolicy(max_attempts=3),
                               on_retry=states.append, waiting=lambda s: None))
    assert states == []


def test_build_retry_state_matches_runner_semantics():
    """状态构造与旧 Runner 一致：attempt 为下一次序号，total 为预算。"""
    state = build_retry_state(RetryPolicy(max_attempts=5), 2,
                              httpx2.ReadTimeout("x"), rng=lambda: 0.0)
    assert (state.attempt, state.total) == (3, 5)
    assert state.reason == "读取超时"
    assert state.wait == 4.0


def test_wait_returns_immediately_when_cancelled():
    """Esc 中断退避等待：不能傻等满 8 秒。"""
    token = CancellationToken()
    reset = activate_token(token)
    try:
        token.cancel()
        state = RetryState(attempt=2, total=3, reason="读取超时", wait=8.0,
                           next_at=time.monotonic() + 8.0)
        started = time.monotonic()
        wait(state)
        assert time.monotonic() - started < 1.0
    finally:
        reset()


def test_default_policy_follows_config_budget(monkeypatch):
    """默认预算与 `[limits].max_retries` 对齐（客户端用它构造策略）。"""
    monkeypatch.setattr(config, "MAX_RETRIES", 4)
    policy = RetryPolicy(max_attempts=config.MAX_RETRIES + 1)
    assert policy.should_retry(httpx2.ReadTimeout("x"), 4) is True
    assert policy.should_retry(httpx2.ReadTimeout("x"), 5) is False


# ---------- 错误文本兜底（对齐 opencode 的 RETRYABLE_MESSAGE_PATTERNS） ----------

@pytest.mark.parametrize("message", [
    "stream timeout",
    "The read operation timed out",
    "connection reset by peer",
    "socket hang up",
    "upstream connect error or disconnect/reset before headers",
    "terminated",
    "fetch failed",
    "ECONNRESET",
    "503 Service Unavailable",
    "provider returned error: internal server error",
    "rate limit exceeded, please try your request again later",
    "incomplete chunked read",
])
def test_retryable_by_text_when_provider_wraps_plain_exception(message):
    """服务商把瞬时故障包成普通异常（类型/状态码都判不出来）时按文案兜底。"""
    assert retryable(RuntimeError(message)) is True


@pytest.mark.parametrize("message", [
    "invalid api key",
    "invalid_request_error: unknown parameter 'foo'",
    "model not found",
    "insufficient quota for this billing period",
])
def test_text_fallback_does_not_overreach(message):
    """兜底不能把普通业务错误一起放进来。"""
    assert retryable(RuntimeError(message)) is False


def test_known_permanent_error_wins_over_text():
    """4xx 永久错误优先于文本兜底：响应体里恰好含 timeout 也不重试。"""
    response = httpx2.Response(
        400, request=httpx2.Request("POST", "http://x"),
        content=b'{"error": "invalid parameter: response timeout"}',
    )
    exc = BadRequestError("invalid parameter: response timeout", response=response,
                          body='{"error": "invalid parameter: response timeout"}')
    assert retryable(exc) is False


def test_provider_body_participates_in_fallback():
    """响应体里的文案也算（部分 SDK 把正文单独放在 body 上）。"""
    exc = RuntimeError("provider error")
    exc.body = '{"error": {"message": "The server is overloaded"}}'
    assert retryable(exc) is True


def test_retry_after_read_from_error_text():
    """服务商只把 Retry-After 写进错误文案时同样照办。"""
    exc = RuntimeError("429 Too Many Requests, retry_after: 12")
    assert RetryPolicy().delay(1, exc, rng=lambda: 0.0) == 12.0


def test_retry_after_not_reduced_to_backoff_cap():
    """服务端明确要求等 120s：不能砍到 30s 提前重试（会再撞一次限流）。"""
    response = httpx2.Response(
        429, request=httpx2.Request("POST", "http://x"), headers={"retry-after": "120"},
    )
    exc = RateLimitError("slow down", response=response, body=None)
    assert RetryPolicy().delay(1, exc) == 120.0          # 不是 30.0
    assert RetryPolicy().delay(1, exc) == 120.0
    # 也没到宽上限：300s 只是保底不疯等
    assert RetryPolicy().delay(1, exc) <= RetryPolicy().retry_after_cap
