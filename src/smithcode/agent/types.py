"""Agent 的公共类型别名（事件与消息的形状）。

方案原本还要求这里放 `AgentTool` / `AgentContext` / `AgentState` 与 `StreamFn`
协议，实测后**都不建**：

- `AgentTool` / `AgentContext`：工具 schema 与会话上下文在 smithcode 里就是
  provider 形状的 dict（`tools/base.py` / `Session`），再包一层只是空转；
- `AgentState`（轮询快照）：要有消费方（TUI 状态栏轮询）才有意义，现在没有——
  建出来就是个没人读的空壳；
- `StreamFn` 协议：真正的模型接缝是 `LLMClient.chat_stream`（60 个测试假客户端
  与扩展都在实现它），再加一层等价协议等于两套接缝同时维护（见 `stream_fn.py`）。

留下的是**确实被多处引用**的类型别名：消息形状、流式增量类别、通知级别。
"""

from __future__ import annotations

from typing import Any, Literal, TypeAlias

# 与 provider 交互的消息（OpenAI 形状）。smithcode 全程使用该形状，
# 不引入 pi 的 `AgentMessage` / `convertToLlm` 转换层。
AgentMessage: TypeAlias = dict[str, Any]

# 流式增量的类别：正文 / 思考内容。思考内容只展示、不入库。
StreamKind = Literal["content", "reasoning"]

# 面向用户的状态文本级别（对应 Renderer 的 info/success/warn/error 四个方法）
NoticeLevel = Literal["info", "success", "warning", "error"]
