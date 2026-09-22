"""事件目录：全部领域事件的载荷、词汇与声明。

**这是唯一允许定义事件类的地方**（守卫测试强制）。理由：事件是核心与前端之间
唯一的契约面，散落在各模块里就无法回答「一共有哪些事件」「哪些要落盘」
「按哪个字段做聚合根」。

分层：本模块属于 L0（事件基础设施），只依赖标准库。因此它必须自带事件词汇
（消息形状、增量类别、通知级别、排队项…）——这些形状由事件描述，owner 就是事件层；
`agent/` 侧的类型别名与 `QueueItem` 都从这里取。

对齐说明（opencode）：
- 类型名形如 `session.<域>.<动作>`，是**写进日志的稳定契约**；改名要升 `version`。
- `aggregate="session_id"`：会话类事件都按会话聚合（前端按它路由、日志按它分段）。
- `durable=True`：**边界类**事件（消息起止、工具起止、计划、投递、标题、询问结果、
  执行/步骤起止）——它们是可回放的骨架；流式增量、通知、忙碌态是易失的，
  重连时丢掉即可（重放骨架就能重建视图）。阶段 E 的持久日志据此落盘。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
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

#: 执行（一次 run）为何停下。对齐 opencode 的 `interrupted.reason`：
#: user = 用户中断 / shutdown = 进程退出 / superseded = 被新任务取代 / inactivity = 空闲超时。
InterruptReason = Literal["user", "shutdown", "superseded", "inactivity"]

#: 一步（一次模型往返）为何结束。对齐 opencode 的 step finish：
#: stop = 模型给出最终回复 / tool_calls = 还要调工具（回灌成下一步）/
#: interrupted = 中途取消 / error = 流或订阅者故障。
StepFinish = Literal["stop", "tool_calls", "interrupted", "error"]


@dataclass(frozen=True)
class StepUsage:
    """一步的 token 用量（可 JSON 化：持久日志与跨进程传输都要它）。

    只留三个数字，不留 provider 原始结构——原结构是各家私有形状，落进事件契约
    就再也改不动了。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_raw(cls, usage) -> StepUsage:
        """从 provider 原始用量字典归一化（缺字段按 0，非映射一律 0）。"""
        if not isinstance(usage, Mapping):
            return cls()
        def pick(*names) -> int:
            for name in names:
                value = usage.get(name)
                if isinstance(value, (int, float)):
                    return int(value)
            return 0
        return cls(
            input_tokens=pick("prompt_tokens", "input_tokens"),
            output_tokens=pick("completion_tokens", "output_tokens"),
            total_tokens=pick("total_tokens"),
        )


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
# 会话生命周期与日志骨架
# --------------------------------------------------------------------------


@declare("session.created", durable=True, aggregate="session_id")
@dataclass(frozen=True)
class SessionCreated:
    """会话建立（日志的首条事件）：转录文件的一切元数据都在这里。

    对齐 opencode 的 `session.created`：会话的"出生"是一条事件，而不是文件头里
    的一段元数据——这样日志从头到尾只有"事件"一种东西，重放规则也就只剩一条。
    """

    cwd: str = ""
    model: str = ""
    effort: str = ""
    app: str = ""
    oneshot: bool = False


@declare("session.model.selected", durable=True, aggregate="session_id")
@dataclass(frozen=True)
class ModelSelected:
    """本轮使用的模型与思考强度（`/model` 切换、恢复会话都会发）。

    `store` 侧按 (model, effort) 去重：连续相同不重复落盘（与既有行为一致）。
    """

    model: str
    effort: str = ""


@declare("session.checkpointed", durable=True, aggregate="session_id")
@dataclass(frozen=True)
class SessionCheckpoint:
    """会话状态**检查点**：goal / plan / skills 等会话状态的快照。

    与"事件"的区别要认清：它描述的是**状态**而不是"发生了什么"，所以它只为
    重放省时间（折叠到最后一个检查点，再往后叠事件）。之所以也走事件通道，
    是为了让"写日志"只有一条路径（`EventLog.append`）。
    """

    state: Mapping[str, Any] = field(default_factory=dict)


@declare("session.history.compacted", durable=True, aggregate="session_id")
@dataclass(frozen=True)
class HistoryCompacted:
    """历史被压缩：`summary` + `tail` 成为此后重放的**新基线**。

    有它之前，压缩是转录里的一种特殊记录（`t=compact`），重放时要特判；现在它
    就是一条事件：折叠到它时把消息换成 `summary + tail`，规则与其它事件一样。
    """

    summary: AgentMessage = field(default_factory=dict)
    tail: tuple[AgentMessage, ...] = ()
    before_tokens: int = 0
    after_tokens: int = 0


# --------------------------------------------------------------------------
# 消息生命周期
# --------------------------------------------------------------------------


@declare("session.message.started", durable=True, aggregate="session_id")
@dataclass(frozen=True)
class MessageStart:
    """一条消息开始（system / user / assistant / tool）。"""

    message: AgentMessage


@declare("session.message.delta", aggregate="session_id")
@dataclass(frozen=True)
class MessageUpdate:
    """assistant 流式增量（对齐 pi 的 `message_update`，仅 assistant 会发）。"""

    message: AgentMessage
    delta: str
    kind: StreamKind


@declare("session.message.ended", durable=True, aggregate="session_id")
@dataclass(frozen=True)
class MessageEnd:
    """一条消息**进入历史**（会话日志里的消息事实就是它）。

    注意与 `StreamEnded` 的区别：本事件是"消息成为历史的一部分"（持久、可回放，
    折叠进会话视图）；流式输出的收口是另一件事（易失、只是呈现）。
    """

    message: AgentMessage


@declare("session.stream.ended", aggregate="session_id")
@dataclass(frozen=True)
class StreamEnded:
    """本轮流式输出收口：前端据此结束当前正文/思考块。

    易失：它是"屏幕上的这一段写完了"，不是历史事实——历史由 `MessageEnd` 承担。
    """

    message: AgentMessage = field(default_factory=dict)


# --------------------------------------------------------------------------
# 工具执行生命周期
# --------------------------------------------------------------------------


@declare("session.tool.started", durable=True, aggregate="session_id")
@dataclass(frozen=True)
class ToolStart:
    """工具调用开始（摘要行先上屏，结果为 pending 态）。"""

    tool_call_id: str | None
    name: str
    line: str
    display: str = "inline"


@declare("session.tool.preview", aggregate="session_id")
@dataclass(frozen=True)
class ToolPreview:
    """执行前的变更预览（diff）。必须在真正执行前发出，否则文件已变更。"""

    tool_call_id: str | None
    detail: str


@declare("session.tool.ended", durable=True, aggregate="session_id")
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


@declare("session.plan.updated", durable=True, aggregate="session_id")
@dataclass(frozen=True)
class PlanUpdate:
    """步骤清单更新。created=True 表示本次是新建清单（只有此时前端展示整份详情）。

    两种渲染形态都在载荷里（**前端不得再去读会话状态**）：
    - `rendered`：完整清单（含状态标记），新建时展示在对话区；
    - `titles`：只列步骤标题，常驻侧边栏用（每步更新都刷新）。
    """

    summary: str
    rendered: str
    titles: str = ""
    created: bool = False
    tool_call_id: str | None = None


@declare("session.notice", aggregate="session_id")
@dataclass(frozen=True)
class Notice:
    """面向用户的状态文本，按级别呈现。"""

    text: str
    level: NoticeLevel = "info"


# --------------------------------------------------------------------------
# 排队
# --------------------------------------------------------------------------


@declare("session.inbox.enqueued", aggregate="session_id")
@dataclass(frozen=True)
class InboxEnqueued:
    """排队输入入队：作为 `user` 消息进入历史，并把这一条落进对话区。

    只在**投递**时才是历史的一部分（见 `InboxDelivered`）；入队这一刻它只在排队
    面板里，所以前端要在这两个时刻分别处理。
    """

    item: QueueItem


@declare("session.inbox.delivered", aggregate="session_id")
@dataclass(frozen=True)
class InboxDelivered:
    """排队输入被投递（本轮跑完 / 工具批之间抽水）：已成为历史的一部分。

    区分它和 `InboxEnqueued` 的必要性：入队时这条文本**不在**对话区；投递后才
    成为历史。前端要在这个时刻把它落到对话区，否则用户看到自己排队的消息从面板
    消失、对话区却没有出现，而模型已经开始回应一条"看不见的"用户消息。
    """

    item: QueueItem


@declare("session.inbox.cancelled", aggregate="session_id")
@dataclass(frozen=True)
class InboxCancelled:
    """一条排队输入离队（逐条撤销 / 取回编辑）：前端从面板移除它。"""

    item_id: str


@declare("session.inbox.cleared", aggregate="session_id")
@dataclass(frozen=True)
class InboxCleared:
    """排队全部清空（Esc 中止时取回编辑器）。"""


# --------------------------------------------------------------------------
# 标题 / 回合 / 终结
# --------------------------------------------------------------------------


@declare("session.title.changed", durable=True, aggregate="session_id")
@dataclass(frozen=True)
class TitleChanged:
    """会话标题变化（自动生成 / `/rename` / 新会话清空）。空串表示回退默认标题。"""

    title: str
    #: 来源（`user` = 显式命名、`auto` = 后台自动命名）；空串表示未声明，
    #: 重放时保留上一个来源（见 `sessions/project.py` 的折叠规则）。
    source: str = ""


@declare("session.execution.started", durable=True, aggregate="session_id")
@dataclass(frozen=True)
class ExecutionStarted:
    """执行开始：一次 run（一次用户任务，含 /goal 续跑的多步）。

    与 step 的关系：execution 包含 N 个 step（一次模型往返 = 一步）。
    """


@declare("session.execution.succeeded", durable=True, aggregate="session_id")
@dataclass(frozen=True)
class ExecutionSucceeded:
    """执行正常结束（模型给出最终回复）。status 取 `RunResult.status`（通常 "ok"）。"""

    status: str
    text: str = ""


@declare("session.execution.failed", durable=True, aggregate="session_id")
@dataclass(frozen=True)
class ExecutionFailed:
    """执行以失败告终：权限被拒 / 达到迭代上限 / 响应流断开。

    `status` 区分是哪一种（取 `RunResult.status`），`reason` 是给排查看的原始信息。
    """

    status: str
    reason: str = ""


@declare("session.execution.interrupted", durable=True, aggregate="session_id")
@dataclass(frozen=True)
class ExecutionInterrupted:
    """执行被中断。`reason` 说明是谁停的——用户按 Esc、进程退出、被新任务取代、
    还是空闲超时；前端据此决定提示文案（"已中断" vs "已取代"）。"""

    reason: InterruptReason = "user"
    partial: bool = False


@declare("session.step.started", durable=True, aggregate="session_id")
@dataclass(frozen=True)
class StepStarted:
    """一步开始：一次模型往返（`index` 从 1 起，`step_id` 与结束事件配对）。"""

    step_id: str
    index: int


@declare("session.step.ended", durable=True, aggregate="session_id")
@dataclass(frozen=True)
class StepEnded:
    """一步结束：`finish` 说明为何结束，`usage` 是这一步的 token 用量。"""

    step_id: str
    index: int
    finish: StepFinish
    usage: StepUsage = StepUsage()


@declare("session.run.ended", aggregate="session_id")
@dataclass(frozen=True)
class AgentEnd:
    """终结事件：本次 run 的结果。流到此结束，终结值即 `result`。"""

    result: Any


# --------------------------------------------------------------------------
# 运行状态
# --------------------------------------------------------------------------


@declare("session.status.changed", aggregate="session_id")
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


@declare("session.status.cleared", aggregate="session_id")
@dataclass(frozen=True)
class StatusCleared:
    """退出某种忙碌状态；消费方按 kind（+ owner）匹配后清除。"""

    kind: StatusKind
    owner: str | None = None


# --------------------------------------------------------------------------
# 空闲与用量
# --------------------------------------------------------------------------


@declare("session.idle", aggregate="session_id")
@dataclass(frozen=True)
class Idle:
    """会话回到空闲：没有任何执行在跑、队列也空了。

    前端据此收起"运行中"提示（与 `ExecutionStarted` 配对）；也是将来服务端
    判断"这个会话可以安全断开"的依据。
    """


@declare("session.usage.updated", durable=True, aggregate="session_id")
@dataclass(frozen=True)
class UsageChanged:
    """会话用量变化（每次模型调用后一次）：宿主**订阅**它，而不是去读会话内部状态。

    带会话累计口径（`calls` + 三个 token 数）与本次增量（`step`）：
    只给累计值的话，前端想显示"这次花了多少"还得自己存上一份。
    """

    calls: int = 0
    total_input: int = 0
    total_output: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    step: StepUsage = StepUsage()


@declare("session.compaction.started", aggregate="session_id")
@dataclass(frozen=True)
class CompactionStarted:
    """上下文压缩开始（要发一次摘要请求，数秒到数十秒）。"""

    before_tokens: int = 0


@declare("session.compaction.ended", aggregate="session_id")
@dataclass(frozen=True)
class CompactionEnded:
    """压缩完成：`before` → `after` 的 token 数。"""

    before_tokens: int = 0
    after_tokens: int = 0


@declare("session.compaction.failed", aggregate="session_id")
@dataclass(frozen=True)
class CompactionFailed:
    """压缩放弃（摘要未按模板生成 / 中断）：原上下文原样继续。"""

    reason: str = ""


# --------------------------------------------------------------------------
# 阻塞询问（请求-应答的事件对）
# --------------------------------------------------------------------------


@declare("session.prompt.started", aggregate="session_id")
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


@declare("session.prompt.finished", durable=True, aggregate="session_id")
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
    SessionCreated
    | ModelSelected
    | SessionCheckpoint
    | HistoryCompacted
    | MessageStart
    | MessageUpdate
    | MessageEnd
    | StreamEnded
    | ToolStart
    | ToolPreview
    | ToolEnd
    | PlanUpdate
    | Notice
    | TitleChanged
    | InboxEnqueued
    | InboxDelivered
    | InboxCancelled
    | InboxCleared
    | ExecutionStarted
    | ExecutionSucceeded
    | ExecutionFailed
    | ExecutionInterrupted
    | StepStarted
    | StepEnded
    | StatusChanged
    | StatusCleared
    | Idle
    | UsageChanged
    | CompactionStarted
    | CompactionEnded
    | CompactionFailed
    | PromptStarted
    | PromptFinished
    | AgentEnd
)
