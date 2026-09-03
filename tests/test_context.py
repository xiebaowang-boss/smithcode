"""上下文计量测试：token 估算、分桶报告与锚点/提醒逻辑，不依赖真实 API。"""

from codeagent.agent import Agent
from codeagent.context import (
    ContextMeter,
    breakdown,
    estimate_message,
    estimate_text,
    report,
)
from codeagent.session import Session


# ---------- 估算启发式 ----------

def test_estimate_text_empty():
    assert estimate_text("") == 0


def test_estimate_text_ascii_four_chars_per_token():
    assert estimate_text("a" * 400) == 100


def test_estimate_text_cjk_one_and_half_chars_per_token():
    assert estimate_text("中" * 30) == 20


def test_estimate_message_counts_tool_call_arguments():
    plain = estimate_message({"role": "user", "content": ""})
    assert plain == 8  # 无正文时只剩固定结构开销

    msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
            }
        ],
    }
    assert estimate_message(msg) > plain


# ---------- 分桶与报告 ----------

def test_breakdown_buckets_by_role():
    messages = [
        {"role": "system", "content": "a" * 400},  # 100 + 8
        {"role": "user", "content": "a" * 200},  # 50 + 8
        {"role": "assistant", "content": "a" * 100},  # 25 + 8
        {"role": "tool", "content": "a" * 400, "tool_call_id": "1"},  # 100 + 8
    ]
    assert breakdown(messages) == {
        "system": 108,
        "user": 58,
        "assistant": 33,
        "tool": 108,
    }


def test_report_shows_total_buckets_and_threshold():
    messages = [
        {"role": "system", "content": "a" * 400},
        {"role": "tool", "content": "a" * 400, "tool_call_id": "1"},
    ]
    text = report(messages, budget=400, trigger=0.5, actual=216)
    assert "216 / 400 tokens" in text
    assert "系统提示词" in text
    assert "工具结果" in text
    assert "已越过" in text  # 216 >= 400 * 50%
    assert "prompt_tokens: 216" in text


def test_report_without_actual_omits_anchor_line():
    text = report([{"role": "user", "content": "hi"}], budget=1000, trigger=0.8)
    assert "prompt_tokens" not in text
    assert "还差" in text


# ---------- 锚点记录与阈值提醒 ----------

def test_context_meter_records_last_actual_and_tolerates_missing():
    meter = ContextMeter()
    assert meter.last_actual is None
    meter.record({"prompt_tokens": 123})
    meter.record(None)
    meter.record({"completion_tokens": 5})
    assert meter.last_actual == 123


def test_warn_once_fires_only_once_per_run(capsys):
    meter = ContextMeter()
    messages = [{"role": "user", "content": "a" * 40000}]  # 估算约 10,008 tokens
    budget, trigger = 11000, 0.8  # 阈值 8,800，提醒线为其 90%

    meter.warn_once(messages, budget, trigger)
    meter.warn_once(messages, budget, trigger)
    assert capsys.readouterr().out.count("接近压缩阈值") == 1

    meter.new_run()  # 新任务重新武装
    meter.warn_once(messages, budget, trigger)
    assert capsys.readouterr().out.count("接近压缩阈值") == 1


def test_warn_once_silent_below_line(capsys):
    meter = ContextMeter()
    meter.warn_once([{"role": "user", "content": "a" * 40}], budget=11000, trigger=0.8)
    assert capsys.readouterr().out == ""


# ---------- 与 Agent 循环的接线 ----------

def test_agent_run_records_anchor(monkeypatch):
    """run() 应把每次请求的真实 prompt_tokens 记为锚点，供 /context 校准展示。"""

    class UsageLLM:
        def chat_stream(self, messages, tools=None):
            yield (
                "usage",
                {"prompt_tokens": 77, "completion_tokens": 3, "total_tokens": 80},
            )
            yield ("message", {"role": "assistant", "content": "好的"})

    monkeypatch.setattr("codeagent.agent.LLMClient", UsageLLM)
    agent = Agent(session=Session())

    agent.run("你好")

    assert agent.context.last_actual == 77
