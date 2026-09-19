"""运行状态事件：带 `kind` 判别的「忙碌行」状态。

对齐 pi 的 `StatusIndicator`（`interactive-mode.ts:145-150` 的
`Working` / `Retry` / `Compaction` / `BranchSummary`）与
`clearStatusIndicator(kind)` 语义：**消费方按 kind 清除，各自只清自己那一条**，
不会互相误清。

替换 smithcode 现有的三套分散机制：
- `Renderer.turn_started` / `turn_finished`（忙碌态）；
- `Renderer.retry_started` / `retry_finished`（重试态，现靠 `owner` 手工避免互相误清）；
- TUI 里「正在停止…」（`tui/app.py:973`）。

状态快照（供轮询）见 `AgentState.status`；事件只负责变化通知。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# 忙碌行的状态类别。新增类别时同步检查 TUI 的呈现与 `title.py` 的前缀规则。
StatusKind = Literal["working", "retry", "compaction", "branch_summary", "stopping"]


@dataclass(frozen=True)
class StatusChanged:
    """进入某种忙碌状态；同 kind 重复收到即覆盖文本。

    owner 用于同一 kind 下区分发起方（如前台任务与后台标题各自的重试），
    消费方按 (kind, owner) 判定是否属于自己。为空表示无归属。

    payload 是 kind 专属的结构化数据，供需要细节的消费方使用（不解析则为 None）：
    `retry` 必须带 `llm.retry.RetryState`（现 `Renderer.retry_started` 的入参，
    TUI 靠它渲染尝试序号与倒计时），`text` 只是给纯文本后端看的摘要。
    """

    kind: StatusKind
    text: str
    owner: str | None = None
    # kind 专属对象，消费方按 kind 收窄类型（retry → llm.retry.RetryState）。
    payload: object | None = None


@dataclass(frozen=True)
class StatusCleared:
    """退出某种忙碌状态；消费方按 kind（+ owner）匹配后清除。"""

    kind: StatusKind
    owner: str | None = None
