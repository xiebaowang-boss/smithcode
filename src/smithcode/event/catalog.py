"""事件目录：全部领域事件的载荷、词汇与声明。

**这是唯一允许定义事件类的地方**（守卫测试强制）。理由：事件是核心与前端之间
唯一的契约面，散落在各模块里就无法回答「一共有哪些事件」「哪些要落盘」
「按哪个字段做聚合根」。

分层：本模块属于 L0（事件基础设施），只依赖标准库。因此它必须自带事件词汇
（消息形状、增量类别、通知级别、排队项…）——这些形状由事件描述，owner 就是事件层；
`agent/` 侧的类型别名与 `QueueItem` 都从这里取。

对齐说明（opencode）：
- 类型名形如 `session.<域>.<动作>`，是**写进日志的稳定契约**；改名要升 `version`。
- 阶段 A 只落「信封 + 单一出口」：`durable` 暂全为 False、`aggregate` 暂为 None，
  由阶段 B（会话身份/执行语义）与阶段 E（持久日志）填实——在那之前不声称任何
  持久化能力。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from .registry import declare

# --------------------------------------------------------------------------
# 事件词汇（被事件描述的形状，owner 在事件层）
# --------------------------------------------------------------------------

#: 与 provider 交互的消息（OpenAI 形状）。smithcode 全程使用该形状，
#: 不引入 pi 的 `AgentMessage` / `convertToLlm` 转换层。
AgentMessage: TypeAlias = dict[str, Any]

#: 流式增量的类别：正文 / 思考内容。思考内容只展示、不入库。
StreamKind = Literal["content", "reasoning"]

#: 面向用户的状态文本级别。
NoticeLevel = Literal["info", "success", "warning", "error"]

#: 阻塞提问的来源（与 5 个阻塞点一一对应）。
PromptKind = Literal["permission", "outside_access", "skill_trust", "ask_user", "confirm"]

#: 用户作答的结果分类。answered 之外一律视为「没有拿到可用答案」，
#: 调用方按各自既有语义兜底（拒绝 / 取消 / 跳过）。
PromptOutcome = Literal["answered", "cancelled", "denied", "error"]

#: 忙碌行的状态类别。新增类别时同步检查前端的呈现与终端标题的前缀规则。
StatusKind = Literal["working", "retry", "compaction", "branch_summary", "stopping"]

QueueItemKind = Literal["steer", "follow_up"]


@dataclass(frozen=True)
class QueueItem:
    """一条排队输入。

    `images` 沿用会话消息里同一形状（`type: image_url` 的 provider dict），
    与 `Session.add` 的图片参数一致；当前 UI 不产生图片，字段先留着，
    避免以后加图片时改事件与 UI 的形状。
    """

    id: str
    text: str
    kind: QueueItemKind
    images: tuple[Any, ...] | None = None
    created_at: float = 0.0


# --------------------------------------------------------------------------
# 消息生命周期
# --------------------------------------------------------------------------


@declare("session.message.started")
@dataclass(frozen=True)
class MessageStart:
    """一条消息开始（system / user / assistant / tool）。"""

    message: AgentMessage


@declare("session.message.delta")
@dataclass(frozen=True)
class MessageUpdate:
    """assistant 流式增量（对齐 pi 的 `message_update`，仅 assistant 会发）。"""

    message: AgentMessage
    delta: str
    kind: StreamKind


@declare("session.message.ended")
@dataclass(frozen=True)
class MessageEnd:
    """一条消息完成。"""

    message: AgentMessage


# --------------------------------------------------------------------------
# 工具执行生命周期
# --------------------------------------------------------------------------


@declare("session.tool.started")
@dataclass(frozen=True)
class ToolStart:
    """工具调用开始（摘要行先上屏，结果为 pending 态）。"""

    tool_call_id: str | None
    name: str
    line: str
    display: str = "inline"


@declare("session.tool.preview")
@dataclass(frozen=True)
class ToolPreview:
    """执行前的变更预览（diff）。必须在真正执行前发出，否则文件已变更。"""

    tool_call_id: str | None
    detail: str


@declare("session.tool.ended")
@dataclass(frozen=True)
class ToolEnd:
    """工具执行结束。expand 标记写/编辑类工具（前端默认展开详情）。"""

    tool_call_id: str | None
    result: str
    is_error: bool = False
    expand: bool = False


# --------------------------------------------------------------------------
# 任务计划与提示
# --------------------------------------------------------------------------


@declare("session.plan.updated")
@dataclass(frozen=True)
class PlanUpdate:
    """步骤清单更新。created=True 表示本次是新建清单（只有此时前端展示整份详情）。"""

    summary: str
    rendered: str
    created: bool = False
    tool_call_id: str | None = None


@declare("session.notice")
@dataclass(frozen=True)
class Notice:
    """面向用户的状态文本，按级别呈现。"""

    text: str
    level: NoticeLevel = "info"


# --------------------------------------------------------------------------
# 排队
# --------------------------------------------------------------------------


@declare("session.inbox.changed")
@dataclass(frozen=True)
class QueueChanged:
    """排队内容变化：增 / 删 / 清 / 投递四个动作各发一次。

    带完整项（id + 文本）而不是纯文本列表：UI 的行尾「✕」要按 id 撤销，
    同文重复时不能靠文本匹配。两个队列一起发，UI 一次刷新即可。
    """

    steering: tuple[QueueItem, ...] = ()
    follow_up: tuple[QueueItem, ...] = ()


@declare("session.inbox.delivered")
@dataclass(frozen=True)
class QueuedPromptDelivered:
    """排队输入**被投递**：已作为 user 消息进入会话历史。

    区分它和 `QueueChanged` 的必要性：入队时这条文本只存在于排队面板里，**不在**
    对话区；投递（本轮跑完 / 工具批之间抽水）后才成为历史的一部分。前端要在这个
    时刻把它落到对话区，否则用户看到自己排队的消息从面板消失、对话区却没有出现，
    而模型已经开始回应一条"看不见的"用户消息。
    """

    text: str
    steering: bool = False


# --------------------------------------------------------------------------
# 标题 / 回合 / 终结
# --------------------------------------------------------------------------


@declare("session.title.changed")
@dataclass(frozen=True)
class TitleChanged:
    """会话标题变化（自动生成 / `/rename` / 新会话清空）。空串表示回退默认标题。"""

    title: str


@declare("session.turn.started")
@dataclass(frozen=True)
class TurnStart:
    """一轮开始（一次模型调用 + 其工具执行）。"""


@declare("session.turn.ended")
@dataclass(frozen=True)
class TurnEnd:
    """一轮结束，status 取 `RunResult.status` 的取值。"""

    status: str


@declare("session.run.ended")
@dataclass(frozen=True)
class AgentEnd:
    """终结事件：本次 run 的结果。流到此结束，终结值即 `result`。"""

    result: Any


# --------------------------------------------------------------------------
# 运行状态
# --------------------------------------------------------------------------


@declare("session.status.changed")
@dataclass(frozen=True)
class StatusChanged:
    """进入某种忙碌状态；同 kind 重复收到即覆盖文本。

    owner 用于同一 kind 下区分发起方（如前台任务与后台标题各自的重试），
    消费方按 (kind, owner) 判定是否属于自己。为空表示无归属。

    payload 是 kind 专属的结构化数据，供需要细节的消费方使用（不解析则为 None）：
    `retry` 带 `llm.retry.RetryState`（TUI 靠它渲染尝试序号与倒计时），
    `text` 只是给纯文本后端看的摘要。
    """

    kind: StatusKind
    text: str
    owner: str | None = None
    payload: Any = None


@declare("session.status.cleared")
@dataclass(frozen=True)
class StatusCleared:
    """退出某种忙碌状态；消费方按 kind（+ owner）匹配后清除。"""

    kind: StatusKind
    owner: str | None = None


# --------------------------------------------------------------------------
# 阻塞询问（请求-应答的事件对）
# --------------------------------------------------------------------------


@declare("session.prompt.started")
@dataclass(frozen=True)
class PromptStarted:
    """开始等待用户输入。`blocking=True` 表示此事件期间 agent 不会推进。"""

    id: str
    kind: PromptKind
    title: str
    blocking: bool = True
    detail: tuple[str, ...] = ()
    options: tuple[str, ...] = ()
    #: kind 专属载荷：permission 带 tool_call_id / outside_access 带路径 /
    #: ask_user 带问题列表。消费方按 kind 解释，不做跨 kind 解析。
    payload: Mapping[str, Any] | None = None


@declare("session.prompt.finished")
@dataclass(frozen=True)
class PromptFinished:
    """等待结束。与 `PromptStarted` 同 id 严格配对。"""

    id: str
    kind: PromptKind
    outcome: PromptOutcome
    #: 用户选择（已脱敏）；denied / cancelled 为空。
    value: str | None = None
    error: str | None = None


# --------------------------------------------------------------------------
# 事件联合
# --------------------------------------------------------------------------

AgentEvent: TypeAlias = (
    MessageStart
    | MessageUpdate
    | MessageEnd
    | ToolStart
    | ToolPreview
    | ToolEnd
    | PlanUpdate
    | Notice
    | TitleChanged
    | QueueChanged
    | QueuedPromptDelivered
    | TurnStart
    | TurnEnd
    | StatusChanged
    | StatusCleared
    | PromptStarted
    | PromptFinished
    | AgentEnd
)
