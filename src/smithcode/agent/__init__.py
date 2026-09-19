"""Agent 核心包：Agent 循环编排与对外公开面。

原 `smithcode/agent.py` 在本目录转为包——Python 不允许 `agent.py` 与 `agent/`
并存，所以这是**替换**而非新增。实现仍集中在 `agent/agent.py`，本模块只做转出，
保证 `from smithcode.agent import ...` 与 `from smithcode import agent; agent.<name>`
两种既有用法一字不改。

**属性解析是惰性的**（PEP 562）：本模块不急切导入 `agent/agent.py`。原因是导入环——
`cancel.py` 单向转出 `agent.signal` / `agent.result`，而 `agent/agent.py` 会经
`..renderer → utils.terminal → commands → llm.client` 走回 `cancel`；若包初始化时
就急切拉进这一切，`cancel` 会在尚未定义出任何名字时被重入（`ImportError: partially
initialized module`）。惰性解析让「导入 packages 的轻量模块」不牵动重链。

后续按 `docs/rebuild-plan.md` 的阶段 3 把 `agent/agent.py` 拆进
`loop.py` / `tools_run.py` 等模块，转出面保持不变。
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

# 从各自模块**显式**转出（不依赖 __getattr__ 惰性转发）：这些名字被 cli/tui/扩展
# 直接 import，而它们并不都活在实现模块 agent/agent.py 里——错误类型在 errors.py、
# 中断文案在 loop.py、占位结果在 tools_run.py。显式转出还避免"实现模块里只被 import
# 没被使用，被 lint 当 F401 删掉后包门面转发不到"这种脆弱耦合。
from .errors import RendererError, StreamInterrupted
from .loop import (
    INTERRUPTED_CONTEXT,
    INTERRUPTED_NOTE,
    MAX_ITERATIONS_WRAPUP,
    STREAM_INTERRUPTED_CONTEXT,
    STREAM_INTERRUPTED_NOTE,
    format_stream_interrupted,
    stream_interrupted_context,
)
from .tools_run import DENIED_RESULT, INTERRUPTED_RESULT

__all__ = [
    "DEFAULT_EXPAND_TOOLS",
    "DENIED_RESULT",
    "INTERRUPTED_CONTEXT",
    "INTERRUPTED_NOTE",
    "INTERRUPTED_RESULT",
    "MAX_ITERATIONS_WRAPUP",
    "MAX_PREVIEW_LINES",
    "MAX_SUMMARY_LEN",
    "STREAM_INTERRUPTED_CONTEXT",
    "STREAM_INTERRUPTED_NOTE",
    "TIMEOUT_HINT",
    "TITLE_MAX_ATTEMPTS",
    "TITLE_RETRY_ROUNDS",
    "Agent",
    "AgentSession",
    "LLMClient",
    "RendererError",
    "ResumeReport",
    "StreamInterrupted",
    "_diff_preview",
    "format_stream_interrupted",
    "stream_interrupted_context",
]


def __getattr__(name: str) -> Any:
    """把属性解析转发到实现模块 `agent/agent.py`（首次访问时才导入它）。

    包化把一个模块拆成了「包 + 子模块」两个命名空间，而旧代码与测试会把
    `smithcode.agent` 当模块用（如 `agent_mod.renderer`、`agent_mod.config`、
    内部私有类）。惰性转发既保持旧模块的完整属性面，又不会在包初始化阶段
    把整条重链拉进来（见模块 docstring 的导入环说明）。

    `LLMClient` 也走这里解析，但一旦被 `monkeypatch.setattr` 写入包命名空间，
    后续读取就直接命中包属性——`_default_llm` 依赖这一行为，见
    `agent/agent.py` 里该接缝的注释与 `tests/agent/test_package_surface.py`。

    用 `import_module` 而非 `from . import agent`：后者经 `_handle_fromlist`
    会对本模块做属性查找，从而再次进入 `__getattr__`，形成无限递归。
    """
    module = import_module(f"{__name__}.agent")
    return getattr(module, name)


def __dir__() -> list[str]:
    """让 `dir(smithcode.agent)` 至少列出转出面（源码发现与补全用）。"""
    return sorted(set(__all__) | set(globals()))
