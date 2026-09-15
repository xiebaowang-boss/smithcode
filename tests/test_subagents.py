"""子代理子系统测试：类型目录、隔离、工具白名单、并发与取消级联。

沿用 test_agent_parallel 的脚本化假 LLM：父子共享同一个假客户端，按调用序回放；
pytest 环境 stdin 非 TTY，显式放行交互确认，假工具不在权限规则表内时统一放行。
"""
import json
import threading
import time
from dataclasses import replace

import pytest

from smithcode import config, subagents
from smithcode.agent import Agent
from smithcode.cancel import current_token
from smithcode.session import Session
from smithcode.tools.task import sync_schema

TASK_ARGS = {"description": "侦察", "prompt": "找出认证模块的实现位置", "subagent_type": "explore"}


@pytest.fixture(autouse=True)
def enable_prompting(monkeypatch):
    monkeypatch.setattr("smithcode.permission.engine.confirmations_available", lambda: True)


@pytest.fixture(autouse=True)
def _restore():
    before = config.SUBAGENTS
    yield
    config.SUBAGENTS = before
    subagents.reset()
    subagents.reset_session_trust()
    sync_schema()


def _tc(name, args=None, call_id="1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args or {})},
    }


def _make_agent(monkeypatch, llm_cls):
    monkeypatch.setattr("smithcode.agent.LLMClient", llm_cls)
    agent = Agent(session=Session())
    monkeypatch.setattr(agent.permission, "check", lambda name, args, content=None: True)
    return agent


def _tool_messages(agent):
    return [m for m in agent.session.messages if m["role"] == "tool"]


def _pairs_ok(messages) -> bool:
    calls, results = [], []
    for message in messages:
        if message.get("role") == "assistant":
            calls.extend(tc["id"] for tc in message.get("tool_calls") or [])
        elif message.get("role") == "tool":
            results.append(message.get("tool_call_id"))
    return sorted(calls) == sorted(results)


# ---------- 类型目录 ----------

def test_builtin_specs_and_tool_scope():
    explore = subagents.get_spec("explore")
    general = subagents.get_spec("general")
    assert explore is not None and general is not None
    assert explore.read_only() is True
    assert general.read_only() is False
    assert explore.allowed_tool("read_file") and explore.allowed_tool("webfetch")
    assert not explore.allowed_tool("write_file")
    assert not explore.allowed_tool("run_command")
    # 强制排除项对任何类型都不开放（递归 / 交互 / 会话级单例状态）
    for name in ("task", "ask_user", "todo_write", "goal_read", "use_skill"):
        assert not general.allowed_tool(name), name
    # MCP 默认关闭，可显式打开
    assert not general.allowed_tool("mcp__docs__search")
    assert general.allowed_tool("mcp__docs__search", allow_mcp=True)


def test_refresh_loads_user_definition(monkeypatch, tmp_path):
    monkeypatch.setenv("SMITHCODE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    agents_dir = tmp_path / "home" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "reviewer.md").write_text(
        "---\n"
        "name: reviewer\n"
        "description: 代码审查\n"
        "tools: read_file, grep\n"
        "model: some-model\n"
        "max_turns: 7\n"
        "---\n"
        "你是审查子代理。",
        encoding="utf-8",
    )
    subagents.refresh()
    spec = subagents.get_spec("reviewer")
    assert spec is not None
    assert spec.tools == ("read_file", "grep")
    assert spec.model == "some-model"
    assert spec.max_turns == 7
    assert spec.read_only() is True
    assert spec.system_prompt == "你是审查子代理。"
    assert "reviewer" in [s.name for s in subagents.all_specs()]


def test_refresh_skips_invalid_definition(monkeypatch, tmp_path):
    monkeypatch.setenv("SMITHCODE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    agents_dir = tmp_path / "home" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "broken.md").write_text("---\nname: broken\n---\n正文", encoding="utf-8")
    subagents.refresh()
    assert subagents.get_spec("broken") is None
    assert any("缺少 description" in d for d in subagents.diagnostics())


def test_disabled_pattern_removes_builtin(monkeypatch, tmp_path):
    monkeypatch.setenv("SMITHCODE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "config.toml").write_text(
        '[subagents]\ndisabled = ["explore"]\n', encoding="utf-8"
    )
    subagents.refresh()
    names = [s.name for s in subagents.all_specs()]
    assert "explore" not in names and "general" in names


def test_project_definitions_skipped_when_untrusted(monkeypatch, tmp_path):
    monkeypatch.setenv("SMITHCODE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "home").mkdir()
    project_dir = tmp_path / ".smithcode" / "agents"
    project_dir.mkdir(parents=True)
    (project_dir / "evil.md").write_text(
        "---\nname: evil\ndescription: 来自未信任仓库\n---\n正文", encoding="utf-8"
    )
    monkeypatch.setattr("smithcode.utils.terminal.confirmations_available", lambda: False)
    subagents.refresh()
    assert subagents.get_spec("evil") is None
    assert any("已跳过项目子代理定义" in d for d in subagents.diagnostics())


def test_enabled_false_yields_empty_catalog(monkeypatch):
    monkeypatch.setattr(config, "SUBAGENTS", replace(config.SUBAGENTS, enabled=False))
    assert subagents.all_specs() == []
    assert subagents.get_spec("explore") is None


# ---------- 派生与隔离 ----------

def test_child_tool_visibility_and_hard_check(monkeypatch):
    class DummyLLM:
        pass

    agent = _make_agent(monkeypatch, DummyLLM)

    child = agent.fork_subagent(subagents.get_spec("explore"))
    names = {s["name"] for s in child._tool_schemas()}
    assert "read_file" in names and "grep" in names
    assert "write_file" not in names and "run_command" not in names and "task" not in names
    assert child._tool_forbidden("run_command") and child._tool_forbidden("task")
    assert not child._tool_forbidden("read_file")
    plan, denied = child._preflight_safe(_tc("run_command", {"command": "ls"}))
    assert denied is False
    assert "不在允许的工具范围内" in plan.run()

    general = agent.fork_subagent(subagents.get_spec("general"))
    names = {s["name"] for s in general._tool_schemas()}
    assert "write_file" in names and "run_command" in names
    for excluded in ("task", "ask_user", "todo_write", "goal_update", "use_skill"):
        assert excluded not in names, excluded
    # 共享进程级服务、隔离会话
    assert general.permission is agent.permission
    assert general.llm is agent.llm
    assert general.session is not agent.session


def test_child_iteration_budget_precedence(monkeypatch):
    class DummyLLM:
        pass

    agent = _make_agent(monkeypatch, DummyLLM)
    agent.max_iterations = 3
    explore = subagents.get_spec("explore")

    # 类型未声明时用 [subagents].max_turns（默认 25）
    child = agent.fork_subagent(explore)
    assert child.max_iterations == 25
    # 配置为 0 时继承父级
    monkeypatch.setattr(config, "SUBAGENTS", replace(config.SUBAGENTS, max_turns=0))
    assert agent.fork_subagent(explore).max_iterations == 3
    # 类型显式声明优先
    custom = replace(explore, max_turns=5)
    assert agent.fork_subagent(custom).max_iterations == 5


# ---------- 端到端：报告、隔离与用量合并 ----------

def test_task_runs_isolated_child_and_returns_report(monkeypatch, tmp_path):
    class ScriptLLM:
        def __init__(self):
            self.calls = 0

        def chat_stream(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                yield ("message", {"role": "assistant", "content": "",
                                   "tool_calls": [_tc("task", TASK_ARGS)]})
                yield ("usage", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
            elif self.calls == 2:
                yield ("message", {"role": "assistant", "content": "报告：auth 在 auth.py:12"})
                yield ("usage", {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28})
            else:
                yield ("message", {"role": "assistant", "content": "主代理总结"})
                yield ("usage", {"prompt_tokens": 30, "completion_tokens": 9, "total_tokens": 39})

    created = {}
    original = Agent.fork_subagent

    def spy(self, spec):
        child = original(self, spec)
        created["child"] = child
        return child

    monkeypatch.setattr(Agent, "fork_subagent", spy)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    agent = _make_agent(monkeypatch, ScriptLLM)
    result = agent.run("调查认证流程")

    assert result.status == "ok" and result.text == "主代理总结"
    tool_messages = _tool_messages(agent)
    assert len(tool_messages) == 1
    assert tool_messages[0]["content"].startswith("[子代理 explore（侦察） 完成")
    assert "auth.py:12" in tool_messages[0]["content"]
    assert _pairs_ok(agent.session.messages)

    child = created["child"]
    assert child is not agent and child.session is not agent.session
    system = child.session.messages[0]["content"]
    assert "子代理约束" in system
    assert "持久目标" not in system and "可用技能" not in system
    assert child.depth == 1

    # 用量合并：父 2 次 + 子 1 次；子账本清空不重复累计
    assert agent.session.usage.current_session.calls == 3
    assert child.session.usage.current_session.calls == 0


def test_bad_task_args_return_actionable_errors(monkeypatch):
    class OneShotLLM:
        def __init__(self):
            self.calls = 0

        def chat_stream(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                yield ("message", {"role": "assistant", "content": "", "tool_calls": [
                    _tc("task", {"description": "x", "prompt": "p", "subagent_type": "nope"}, "1"),
                    _tc("task", {"description": "x", "prompt": "", "subagent_type": "explore"}, "2"),
                ]})
            else:
                yield ("message", {"role": "assistant", "content": "已改用其他方式"})

    agent = _make_agent(monkeypatch, OneShotLLM)
    agent.run("试试")
    contents = {m["tool_call_id"]: m["content"] for m in _tool_messages(agent)}
    assert "未知子代理类型" in contents["1"]
    assert "prompt 不能为空" in contents["2"]
    assert contents["1"].startswith("错误:") and contents["2"].startswith("错误:")
    assert _pairs_ok(agent.session.messages)


# ---------- 调度：只读并行 / 写能力串行 ----------

def test_task_serial_follows_readonly_and_config(monkeypatch):
    class DummyLLM:
        pass

    agent = _make_agent(monkeypatch, DummyLLM)

    def plan_for(kind):
        args = {"description": "d", "prompt": "p", "subagent_type": kind}
        plan, denied = agent._preflight(_tc("task", args))
        assert denied is False
        return plan

    assert plan_for("explore").serial is False
    assert plan_for("general").serial is True
    monkeypatch.setattr(config, "SUBAGENTS", replace(config.SUBAGENTS, parallel=False))
    assert plan_for("explore").serial is True


def test_read_only_tasks_run_in_parallel(monkeypatch):
    barrier = threading.Barrier(2, timeout=10)

    class ParallelLLM:
        def __init__(self):
            self.parent_calls = 0

        def chat_stream(self, messages, tools=None):
            system = str(messages[0].get("content", ""))
            if "子代理约束" in system:  # 子代理调用：两个必须同时在场
                barrier.wait()
                yield ("message", {"role": "assistant", "content": "子报告"})
                return
            self.parent_calls += 1
            if self.parent_calls == 1:
                yield ("message", {"role": "assistant", "content": "", "tool_calls": [
                    _tc("task", TASK_ARGS, "1"),
                    _tc("task", TASK_ARGS, "2"),
                ]})
            else:
                yield ("message", {"role": "assistant", "content": "汇总"})

    agent = _make_agent(monkeypatch, ParallelLLM)
    result = agent.run("并行侦察")
    assert result.status == "ok"
    contents = [m["content"] for m in _tool_messages(agent)]
    assert len(contents) == 2
    assert all("子报告" in c and "错误" not in c for c in contents)
    assert _pairs_ok(agent.session.messages)


# ---------- /agents 命令 ----------

def test_agents_command_lists_and_refreshes():
    from smithcode import commands

    class StubAgent:
        def __init__(self):
            self.refreshed = False

        def refresh_subagents(self):
            self.refreshed = True
            return ["测试诊断"]

    agent = StubAgent()
    outcome = commands.dispatch(agent, "/agents")
    assert outcome.kind == "block"
    assert "explore" in outcome.text and "general" in outcome.text
    assert "只读" in outcome.text

    outcome = commands.dispatch(agent, "/agents refresh")
    assert agent.refreshed
    assert "测试诊断" in outcome.text

    assert "用法" in commands.dispatch(agent, "/agents nope").text


# ---------- 取消级联 ----------

def test_parent_interrupt_cascades_to_child(monkeypatch):
    class BlockingChildLLM:
        def __init__(self):
            self.calls = 0
            self.child_started = threading.Event()

        def chat_stream(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                yield ("message", {"role": "assistant", "content": "",
                                   "tool_calls": [_tc("task", TASK_ARGS)]})
                return
            if self.calls == 2:  # 子代理在流中等待被取消
                self.child_started.set()
                token = current_token()
                while token is not None and not token.cancelled:
                    time.sleep(0.005)
                return
            yield ("message", {"role": "assistant", "content": "总结"})

    agent = _make_agent(monkeypatch, BlockingChildLLM)
    box = {}
    thread = threading.Thread(
        target=lambda: box.setdefault("result", agent.run("长任务")), daemon=True
    )
    thread.start()
    assert agent.llm.child_started.wait(timeout=5)
    agent.interrupt()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert box["result"].status == "interrupted"
    tool_messages = _tool_messages(agent)
    assert tool_messages and "已中断" in tool_messages[0]["content"]
    assert _pairs_ok(agent.session.messages)


def test_subagent_timeout_cancels_child(monkeypatch):
    """[subagents].timeout 看门狗：子代理挂起超时后以中断报告返回，父级正常收尾。"""

    class HangingChildLLM:
        def __init__(self):
            self.calls = 0

        def chat_stream(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                yield ("message", {"role": "assistant", "content": "",
                                   "tool_calls": [_tc("task", TASK_ARGS)]})
                return
            if self.calls == 2:  # 子代理挂起，等待看门狗取消子令牌
                token = current_token()
                while token is not None and not token.cancelled:
                    time.sleep(0.005)
                return
            yield ("message", {"role": "assistant", "content": "已基于超时报告收尾"})

    monkeypatch.setattr(config, "SUBAGENTS", replace(config.SUBAGENTS, timeout=0.2))
    agent = _make_agent(monkeypatch, HangingChildLLM)
    result = agent.run("会超时的任务")

    assert result.status == "ok" and result.text == "已基于超时报告收尾"
    tool_messages = _tool_messages(agent)
    assert tool_messages and "已中断" in tool_messages[0]["content"]
    assert _pairs_ok(agent.session.messages)
