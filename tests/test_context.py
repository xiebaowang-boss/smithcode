"""上下文计量测试：token 估算、分桶报告与锚点/提醒逻辑，不依赖真实 API。"""

import asyncio

from smithcode import config
from smithcode.agent import Agent
from smithcode.context import (
    ContextMeter,
    assemble,
    breakdown,
    build_summary_request,
    estimate_message,
    estimate_text,
    is_context_overflow,
    pick_tail,
    report,
    validate_summary,
)
from smithcode.session import Session

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


# ---------- 锚点记录 ----------

def test_context_meter_records_last_actual_and_tolerates_missing():
    meter = ContextMeter()
    assert meter.last_actual is None
    meter.record({"prompt_tokens": 123})
    meter.record(None)
    meter.record({"completion_tokens": 5})
    assert meter.last_actual == 123
    assert meter.compact_count == 0


# ---------- 压缩纯逻辑 ----------

def _turn(label: str, tool_chars: int = 0) -> list[dict]:
    """构造一个完整轮次：user + assistant(tool_calls) + tool，配对完整。"""
    return [
        {"role": "user", "content": label},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                }
            ],
        },
        {"role": "tool", "content": "x" * tool_chars, "tool_call_id": "1"},
    ]


def test_pick_tail_keeps_recent_turns_within_budget():
    msgs = (
        [{"role": "system", "content": "s"}]
        + _turn("一", 400)
        + _turn("二", 400)
        + _turn("三", 400)
    )
    # 每轮约 130 tokens（tool 400 字符 + 结构开销）；预算 300 只装得下最后两轮
    assert pick_tail(msgs, 300) == 4
    # 预算 400 连第一轮也装得下（三轮共约 390），尾部覆盖全部轮次
    assert pick_tail(msgs, 400) == 1


def test_pick_tail_keeps_whole_oversized_turn():
    msgs = [{"role": "system", "content": "s"}] + _turn("一", 40000)
    assert pick_tail(msgs, 150) == 1  # 宁可尾部超预算，也不把 tool 配对拆开


def test_build_summary_request_serializes_history():
    old = [{"role": "user", "content": "你好"}] + _turn("帮我", 10)[1:]
    req = build_summary_request(old)
    assert req[0]["role"] == "system" and "上下文压缩器" in req[0]["content"]
    body = req[1]["content"]
    assert "用户: 你好" in body
    assert "[调用工具] read_file" in body


def test_validate_summary_requires_headings():
    assert validate_summary("## 目标\n做 X\n## 下一步\n\"继续 Y\"")
    assert not validate_summary("随便写两点，没有模板标题")


def test_assemble_structure_and_tail_truncation():
    tail = _turn("最近一轮", 3000)
    result = assemble("SYS", "## 目标\n做 X\n## 下一步\n\"继续\"", tail)
    assert result[0] == {"role": "system", "content": "SYS"}
    assert "<context-summary>" in result[1]["content"]
    # 配对完整：assistant 的 tool_calls 和对应 tool 结果都在尾部
    assert any(m.get("tool_calls") for m in result)
    assert result[-1]["role"] == "tool"
    assert len(result[-1]["content"]) < 3000 and "已省略" in result[-1]["content"]


def test_is_context_overflow_matches_provider_wording():
    assert is_context_overflow(
        RuntimeError("This model's maximum context length is 8192 tokens")
    )
    assert is_context_overflow(RuntimeError("输入的上下文长度超过限制"))
    assert not is_context_overflow(RuntimeError("Incorrect API key provided"))


# ---------- 压缩与 Agent 循环的接线 ----------

SUMMARY = "## 目标\n重构 agent 循环\n## 下一步\n\"跑测试确认\""


class CompactAwareLLM:
    """摘要请求（system 以"你是上下文压缩器"开头）返回固定摘要，其余返回普通回复。"""

    def __init__(self):
        self.calls = 0

    def chat_stream(self, messages, tools=None):
        self.calls += 1
        if messages[0].get("content", "").startswith("你是上下文压缩器"):
            yield ("message", {"role": "assistant", "content": SUMMARY})
        else:
            yield ("message", {"role": "assistant", "content": "最终回复"})


def _over_threshold_agent(monkeypatch):
    monkeypatch.setattr(config, "CONTEXT_TOKEN_BUDGET", 500)  # 阈值 400
    monkeypatch.setattr(config, "COMPACT_KEEP_TOKENS", 150)
    monkeypatch.setattr("smithcode.agent.LLMClient", CompactAwareLLM)
    session = Session()
    # 历史：system + 一轮千 token 的工具结果，越过阈值
    session.messages = [{"role": "system", "content": "SYS"}] + _turn("上次任务", 4000)
    return Agent(session=session)


def test_run_compacts_when_over_threshold(monkeypatch, capsys):
    agent = _over_threshold_agent(monkeypatch)

    asyncio.run(agent.run("继续"))

    msgs = agent.session.messages
    assert agent.context.compact_count == 1
    # run() 发请求前同步系统提示词（含当前持久目标段），压缩保留 messages[0]
    assert msgs[0]["role"] == "system"
    assert "Smith Code" in msgs[0]["content"]
    assert msgs[1]["content"].startswith("<context-summary>")
    assert "## 目标" in msgs[1]["content"]
    # 尾部只剩本轮（user + 最终回复），旧的千 token 工具结果已不在上下文里
    assert [m["role"] for m in msgs] == ["system", "user", "user", "assistant"]
    assert "已压缩" in capsys.readouterr().out


def test_compact_prunes_skills_lost_from_context(monkeypatch, capsys, tmp_path):
    """技能正文随中段被摘要掉：从加载集合剔除，并注入一行提示告知可重新加载。"""
    from smithcode import skills

    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    (home / "config.toml").write_text('[skills]\nproject = "on"\n', encoding="utf-8")
    directory = workspace / ".agents" / "skills" / "proj"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: proj\ndescription: d\n---\n技能正文标记\n", encoding="utf-8"
    )

    skills.clear()
    try:
        skills.refresh()
        payload = skills.activate("proj")

        monkeypatch.setattr(config, "CONTEXT_TOKEN_BUDGET", 500)
        monkeypatch.setattr(config, "COMPACT_KEEP_TOKENS", 150)
        monkeypatch.setattr("smithcode.agent.LLMClient", CompactAwareLLM)
        session = Session()
        # 中段：技能载荷（用户侧加载注入的那条 user 消息）→ 会被摘要回收
        session.messages = (
            [{"role": "system", "content": "SYS"},
             {"role": "user", "content": payload},
             {"role": "assistant", "content": "收到"}]
            + _turn("上次任务", 4000)
        )
        agent = Agent(session=session)

        assert asyncio.run(agent.compact()) is True

        assert skills.active_names() == []  # 正文已不在上下文：剔除
        notice = agent.session.messages[-1]
        assert notice["role"] == "user"
        assert "技能 proj" in notice["content"]
        assert "use_skill" in notice["content"]
    finally:
        skills.clear()


def test_compact_aborts_silently_when_cancelled(monkeypatch, capsys):
    """任务已中断时不再发起摘要请求，静默放弃压缩（不打印失败噪音）。"""
    from smithcode.cancel import CancellationToken, activate_token

    agent = _over_threshold_agent(monkeypatch)
    called = []
    monkeypatch.setattr(agent, "_complete", lambda req: called.append(1) or "")

    token = CancellationToken()
    reset = activate_token(token)
    token.cancel()
    try:
        assert asyncio.run(agent.compact()) is False
    finally:
        reset()

    assert called == []  # 未发起任何摘要请求
    assert "摘要未按模板生成" not in capsys.readouterr().out


def test_compact_aborts_when_summary_invalid_twice(monkeypatch, capsys):
    class BadSummaryLLM(CompactAwareLLM):
        def chat_stream(self, messages, tools=None):
            if messages[0].get("content", "").startswith("你是上下文压缩器"):
                yield ("message", {"role": "assistant", "content": "不合格：没有模板标题"})
            else:
                yield ("message", {"role": "assistant", "content": "最终回复"})

    monkeypatch.setattr(config, "CONTEXT_TOKEN_BUDGET", 500)
    monkeypatch.setattr(config, "COMPACT_KEEP_TOKENS", 150)
    monkeypatch.setattr("smithcode.agent.LLMClient", BadSummaryLLM)
    session = Session()
    session.messages = [{"role": "system", "content": "SYS"}] + _turn("上次任务", 4000)
    agent = Agent(session=session)

    assert asyncio.run(agent.run("继续")).text == "最终回复"  # 压缩失败不中断任务

    assert agent.context.compact_count == 0
    assert len(agent.session.messages) == 6  # 原 4 条 + 本轮 user + assistant，原样保留
    assert "放弃本次压缩" in capsys.readouterr().out


def test_run_recovers_from_context_overflow(monkeypatch, capsys):
    class OverflowOnceLLM(CompactAwareLLM):
        def chat_stream(self, messages, tools=None):
            if messages[0].get("content", "").startswith("你是上下文压缩器"):
                yield ("message", {"role": "assistant", "content": SUMMARY})
                return
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("This model's maximum context length is 4096 tokens")
            yield ("message", {"role": "assistant", "content": "重试后回复"})

    monkeypatch.setattr(config, "CONTEXT_TOKEN_BUDGET", 5000)  # 不触发预检，纯溢出恢复
    monkeypatch.setattr(config, "COMPACT_KEEP_TOKENS", 150)
    monkeypatch.setattr("smithcode.agent.LLMClient", OverflowOnceLLM)
    session = Session()
    session.messages = [{"role": "system", "content": "SYS"}] + _turn("上次任务", 4000)
    agent = Agent(session=session)

    assert asyncio.run(agent.run("继续")).text == "重试后回复"

    assert agent.context.compact_count == 1
    assert "上下文溢出" in capsys.readouterr().out


# ---------- 手动压缩（/compact 的宿主入口） ----------

def _compactable_agent(monkeypatch):
    """可压历史：system + 一轮旧历史（待压）+ 一轮近期（尾部保留）。"""
    agent = _over_threshold_agent(monkeypatch)
    agent.session.messages = agent.session.messages + _turn("最近一轮", 50)
    return agent


def test_compact_manual_compacts_and_reports_ok(monkeypatch):
    """compact_manual 自建令牌执行压缩，成功后返回 ok。"""
    agent = _compactable_agent(monkeypatch)

    assert asyncio.run(agent.compact_manual()) == "ok"
    assert agent.context.compact_count == 1
    assert agent._token is None  # 令牌已复位，不残留到下一次任务


def test_compact_manual_reports_empty_when_nothing_to_compact(monkeypatch):
    """历史太短（无中段可压）时返回 empty，不改动消息。"""
    monkeypatch.setattr("smithcode.agent.LLMClient", CompactAwareLLM)
    session = Session()
    session.messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "你好"},
    ]
    agent = Agent(session=session)

    assert asyncio.run(agent.compact_manual()) == "empty"
    assert len(agent.session.messages) == 2


def test_compact_manual_reports_cancelled_when_interrupted(monkeypatch):
    """压缩途中按 Esc 触发 interrupt()：令牌截停摘要重试，返回 cancelled。"""
    agent = _compactable_agent(monkeypatch)
    calls: list = []

    def on_complete(request, model=None):
        calls.append(1)
        agent.interrupt()  # 模拟摘要请求期间用户中断
        return ""

    monkeypatch.setattr(agent, "_complete", on_complete)

    assert asyncio.run(agent.compact_manual()) == "cancelled"
    assert calls == [1]  # 第二次重试前查令牌，不再发请求
    assert agent.context.compact_count == 0


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

    monkeypatch.setattr("smithcode.agent.LLMClient", UsageLLM)
    agent = Agent(session=Session())

    asyncio.run(agent.run("你好"))

    assert agent.context.last_actual == 77
