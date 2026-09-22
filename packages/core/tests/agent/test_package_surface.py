"""包门面与 monkeypatch 接缝（agent/__init__.py）。

包化把「一个模块」变成「包 + 子模块」两个命名空间，最危险的失效模式是
**静默的**：`monkeypatch.setattr("smithcode.agent.LLMClient", fake)` 只写了包属性，
实现模块若仍读自己的 `globals()`，测试就会用**真客户端**跑——不报错，只是打真接口。
本文件把这类接缝锁住。
"""

from __future__ import annotations

import sys

import smithcode.agent
from smithcode import agent as agent_mod
from smithcode.agent import (
    INTERRUPTED_CONTEXT,
    INTERRUPTED_NOTE,
    MAX_ITERATIONS_WRAPUP,
    STREAM_INTERRUPTED_CONTEXT,
    STREAM_INTERRUPTED_NOTE,
    TITLE_MAX_ATTEMPTS,
    TITLE_RETRY_ROUNDS,
    Agent,
    ResumeReport,
    SubscriberError,
    _diff_preview,
    format_stream_interrupted,
)
from smithcode.agent import agent as implementation


def test_frozen_surface_is_importable_from_package():
    """冻结面（见 docs/rebuild-plan.md）必须能从包直接导入且类型没走样。"""
    assert callable(Agent)
    assert callable(_diff_preview)
    assert callable(format_stream_interrupted)
    assert isinstance(SubscriberError, type)
    assert isinstance(ResumeReport, type)
    assert (TITLE_MAX_ATTEMPTS, TITLE_RETRY_ROUNDS) == (3, 5)
    assert INTERRUPTED_NOTE and STREAM_INTERRUPTED_NOTE
    assert INTERRUPTED_CONTEXT and STREAM_INTERRUPTED_CONTEXT and MAX_ITERATIONS_WRAPUP


def test_unknown_attributes_forward_to_implementation_module():
    """包级未列举的名字转发到实现模块（如 `agent_mod.emitter`）。"""
    assert agent_mod.emitter is implementation.emitter
    assert agent_mod.Agent is implementation.Agent


def test_llm_client_seam_reads_the_package_namespace(monkeypatch):
    """改写包属性 `smithcode.agent.LLMClient` 后，默认客户端构造必须换掉。

    否则 60 处 `monkeypatch.setattr("smithcode.agent.LLMClient", …)` 全部静默失效，
    测试会去打真实接口。
    """

    class FakeClient:
        pass

    monkeypatch.setattr("smithcode.agent.LLMClient", FakeClient)
    assert isinstance(implementation._default_llm(), FakeClient)
    assert implementation._default_llm.__module__ == "smithcode.agent.agent"


def test_implementation_module_lives_under_the_package():
    """实现模块的包名必须是 `smithcode.agent`——接缝靠它定位包命名空间。"""
    assert implementation.__package__ == "smithcode.agent"
    assert sys.modules["smithcode.agent"] is smithcode.agent
