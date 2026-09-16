"""自动标题生成测试：失败补试、用户标题优先、到顶提示与停止重试。

标题请求在后台 daemon 线程里跑，这里用同步假 `_complete` + join 消除不确定性。
"""
import threading

import pytest

from smithcode import config, renderer
from smithcode.agent import TITLE_MAX_ATTEMPTS, Agent
from smithcode.session import Session


class FakeLLM:
    """普通对话用的假模型：单轮直接给最终回复。"""

    def chat_stream(self, messages, tools=None):
        yield ("message", {"role": "assistant", "content": "好的"})


@pytest.fixture
def agent(tmp_path, monkeypatch):
    """隔离的家目录 + 工作区，自动标题开启（其余用例默认关闭，避免后台线程）。"""
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(workspace))
    (home / "config.toml").write_text(
        "[sessions]\nauto_title = true\ncleanup_days = 0\n", encoding="utf-8"
    )
    monkeypatch.setattr("smithcode.agent.LLMClient", lambda: FakeLLM())
    backup = renderer._current
    renderer.set_renderer(renderer.ConsoleRenderer())
    monkeypatch.setattr(
        "smithcode.permission.engine.confirmations_available", lambda: True
    )
    instance = Agent(session=Session(), persist=True)
    instance.session.add("user", "帮我重构会话管理模块")
    yield instance
    renderer.set_renderer(backup)


def _wait_title_threads() -> None:
    """等待所有标题后台线程收尾（daemon 线程，join 带超时保护）。"""
    for thread in threading.enumerate():
        if thread.name == "smithcode-title":
            thread.join(timeout=5)


def _attempt(instance: Agent) -> None:
    """发起一次标题生成并等后台线程收尾。"""
    before = {t for t in threading.enumerate() if t.name == "smithcode-title"}
    instance._maybe_generate_title()
    for thread in threading.enumerate():
        if thread.name == "smithcode-title" and thread not in before:
            thread.join(timeout=5)


def _patch_complete(instance: Agent, monkeypatch, replies):
    """把 `_complete` 换成按序返回的假实现；元素为异常实例时抛出。"""
    calls = []

    def fake(request, model=None):
        calls.append(model)
        item = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(instance, "_complete", fake)
    return calls


def test_title_success_sets_title_once(agent, monkeypatch):
    calls = _patch_complete(agent, monkeypatch, ['{"title": "会话管理重构"}'])
    _attempt(agent)
    _attempt(agent)  # 已有标题：不再发起
    assert agent.session.title == "会话管理重构"
    assert len(calls) == 1


def test_failed_title_is_retried_next_turn(agent, monkeypatch, capsys):
    """回归：一次失败曾被永久放弃——现在下一轮补试，成功后不再发起。"""
    calls = _patch_complete(
        agent, monkeypatch, [RuntimeError("boom"), '{"title": "第二次成功"}']
    )
    _attempt(agent)
    assert agent.session.title == ""
    assert "自动命名失败" in capsys.readouterr().out  # 失败可见，不再静默
    _attempt(agent)
    assert agent.session.title == "第二次成功"
    assert len(calls) == 2
    assert agent._title_attempts == 2


def test_title_stops_after_max_attempts(agent, monkeypatch, capsys):
    """到顶后不再发起，并提示改用 /rename。"""
    calls = _patch_complete(agent, monkeypatch, [RuntimeError("boom")])
    for _ in range(TITLE_MAX_ATTEMPTS + 2):
        _attempt(agent)
    assert len(calls) == TITLE_MAX_ATTEMPTS
    assert agent._title_attempts == TITLE_MAX_ATTEMPTS
    out = capsys.readouterr().out
    assert "将在下一轮结束后重试" in out
    assert "已停止重试" in out and "/rename" in out


def test_user_title_never_attempted(agent, monkeypatch):
    """用户已命名（/rename、--name、恢复的会话）：不消耗尝试次数。"""
    agent.session.set_title("手动命名", source="user")
    calls = _patch_complete(agent, monkeypatch, ['{"title": "不该出现"}'])
    _attempt(agent)
    assert calls == []
    assert agent._title_attempts == 0


def test_unusable_output_counts_as_failure(agent, monkeypatch, capsys):
    """模型输出解析不出标题（非 JSON / 错误句式 / 空）同样算失败并提示。"""
    calls = _patch_complete(agent, monkeypatch, ["抱歉，我无法生成标题"])
    _attempt(agent)
    assert agent.session.title == ""
    assert "模型未返回可用标题" in capsys.readouterr().out
    assert len(calls) == 1


def test_new_session_resets_attempts(agent, monkeypatch):
    """`/new` 后重新计数：新会话仍能拿到标题。"""
    _patch_complete(agent, monkeypatch, [RuntimeError("boom")])
    _attempt(agent)
    assert agent._title_attempts == 1
    agent.new_session()
    assert agent._title_attempts == 0


def test_run_triggers_title_retry_across_turns(agent, monkeypatch, capsys):
    """端到端：两轮任务——第一轮标题请求失败，第二轮结束后补试成功。"""
    replies = [RuntimeError("网络抖动"), '{"title": "跨轮补试"}']
    _patch_complete(agent, monkeypatch, replies)

    agent.run("第一轮")
    _wait_title_threads()
    assert agent.session.title == ""

    agent.run("第二轮")
    _wait_title_threads()
    assert agent.session.title == "跨轮补试"
    assert agent.session.title_source == "auto"


def test_title_max_attempts_default():
    """上限是常量且大于 1：单次抖动不至于丢掉标题。"""
    assert TITLE_MAX_ATTEMPTS > 1
