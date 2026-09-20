from __future__ import annotations

import asyncio
import inspect
import json
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from .. import config, goal, instructions, sessions, skills
from ..context import (
    ContextMeter,
    assemble,
    build_summary_request,
    is_context_overflow,
    pick_tail,
    total_tokens,
    truncate_output,
    validate_summary,
)
from ..event import Bus
from ..event.catalog import (
    AgentEnd,
    AgentEvent,
    MessageEnd,
    MessageUpdate,
    Notice,
    PlanUpdate,
    QueueChanged,
    QueuedPromptDelivered,
    QueueItem,
    StatusChanged,
    StatusCleared,
    TitleChanged,
    ToolEnd,
    ToolPreview,
    ToolStart,
    TurnEnd,
    TurnStart,
)
from ..event.envelope import wrap
from ..event.stream import EventStream, agent_event_stream
from ..llm import LLMClient as _RealLLMClient
from ..llm.models import (
    CachedModelSource,
    ConfiguredModelSource,
    ModelCache,
    ModelCatalog,
    RemoteModelSource,
)
from ..llm.request import TurnConfig
from ..llm.retry import describe as describe_error
from ..mcp import McpService
from ..permission import Permission
from ..plan import has_active, render_current, render_titles, summary
from ..session import Session
from ..tools import (
    DESCRIBERS,
    DISPLAY,
    FUNCTIONS,
    PATHS_EXTRACTORS,
    PREVIEWS,
    READ_ONLY_TOOLS,
    SERIAL,
    reset_read_tracking,
    visible_schemas,
)
from ..tools.skills import sync_schema
from . import emitter
from .agent_session import AgentSession
from .errors import StreamInterrupted, SubscriberError
from .hooks import (
    AfterToolCallContext,
    AfterToolCallResult,
    AgentHooks,
    BeforeToolCallContext,
    BeforeToolCallResult,
    TurnContext,
)
from .loop import (
    INTERRUPTED_CONTEXT,
    MAX_ITERATIONS_WRAPUP,
    stream_interrupted_context,
)
from .queues import MessageQueue
from .result import RunResult
from .signal import AbortSignal, activate_token, current_token
from .stream_fn import drain_sync_stream
from .tools_run import (
    DENIED_RESULT,
    INTERRUPTED_RESULT,
    BatchScheduler,
    ToolPlan,
)

# 工具调用短摘要行（如 `read src/agent.py`）的最大显示宽度，超出截断
MAX_SUMMARY_LEN = 80

# 变更预览（diff）最多展示的行数，超出截断
MAX_PREVIEW_LINES = 40

# 自动标题的最大尝试次数：单次失败（网络抖动、瞬时错误重试耗尽、模型输出不可用）
# 不永久放弃——下一轮任务正常结束后补试；到顶后进入冷却（TITLE_RETRY_ROUNDS
# 轮内静默），冷却一过自动再探一次，期间 /model 切换模型或 /rename 改名都会
# 立即重置计数——换模型往往意味着标题失败的原因已消除，不应继续沉默。
TITLE_MAX_ATTEMPTS = 3

# 到顶后的冷却轮数：冷却期内 _maybe_generate_title 直接返回（静默、不提示，
# 到顶时已提示过一次 /rename）；冷却一过自动再探，避免"3 次用完就永久沉默"。
TITLE_RETRY_ROUNDS = 5

# 结果详情默认展开的工具：写/编辑类的 diff 是本次改动的关键信息（apply_patch
# 与 edit_file 同族），ask_user 的结果就是用户回答（页面主体）；都直接可见、可收起。
DEFAULT_EXPAND_TOOLS = frozenset({
    "write_file", "edit_file", "apply_patch", "ask_user",
})


@dataclass
class ResumeReport:
    """恢复会话的结果摘要：宿主据此渲染（标题 / 条数 / 崩溃修复情况）。"""

    path: Path
    session_id: str
    title: str
    message_count: int
    repair: str  # none / appended / truncated
    bad_lines: int
    model: str = ""  # 该会话最后使用的模型（转录里的 model 记录，回退创建时模型）


class _StatePart(NamedTuple):
    """会话级状态的统一协议：快照 / 恢复 / 重置（注册表遍历执行）。"""

    name: str
    snapshot: Callable[[], object]
    restore: Callable[[object], None]
    reset: Callable[[], None]


# `LLMClient` 名字是测试替换点：monkeypatch 改写它时 `_default_llm`
# 同步换成假 LLM。真实现另存 `_RealLLMClient`，供类型注解与文档引用。
# 包化后这个接缝跨越两个命名空间：测试改写的是**包属性**
# `smithcode.agent.LLMClient`，而本模块是同名包下的子模块，`globals()` 与包
# 命名空间是两个对象。故先读包命名空间、再回退本模块 globals（详见
# docs/rebuild-plan.md「冻结的兼容面」）。
LLMClient = _RealLLMClient


def _default_llm():
    """按当前的 `LLMClient` 替换点构造默认客户端（`from_config` 优先）。

    经属性查找而非闭包直引：测试用 monkeypatch 替换
    `smithcode.agent.LLMClient`（包属性）时，这里的构造同步换成假 LLM。
    包命名空间优先，保证旧的 monkeypatch 入口继续生效；`globals()` 兜底，
    覆盖直接替换本子模块属性的用法。
    """
    package = sys.modules.get(__package__)
    cls = getattr(package, "LLMClient", None) or globals()["LLMClient"]
    factory = getattr(cls, "from_config", None)
    if callable(factory):
        return factory()
    return cls()


def _diff_preview(name: str, args: dict) -> str:
    """执行前生成工具的变更预览（diff）并按行截断。

    必须在工具执行前调用——执行后文件已变更，diff 恒为空。生成失败只
    影响展示，不影响执行。"""
    preview = PREVIEWS.get(name)
    if preview is None:
        return ""
    try:
        detail = str(preview(args) or "")
    except Exception:  # noqa: BLE001 预览失败不影响执行
        return ""
    lines = detail.splitlines()
    if len(lines) > MAX_PREVIEW_LINES:
        hidden = len(lines) - MAX_PREVIEW_LINES
        lines = lines[:MAX_PREVIEW_LINES]
        lines.append(f"……（diff 过长，省略 {hidden} 行）")
    return "\n".join(lines)


class Agent:
    def __init__(self, session: Session | None = None, max_iterations: int | None = None,
                 store=None, persist: bool = False, oneshot: bool = False,
                 llm=None, permission=None, mcp=None, models=None,
                 model: str | None = None, reset_globals: bool = True,
                 hooks: AgentHooks | None = None):
        """构造 Agent。

        进程级服务（llm / permission / mcp / models）可注入：复用已有实例
        （共享权限模式与会话规则、连接与客户端），而不是各自新建。model 覆盖
        模型；reset_globals=False 跳过「已读文件」等进程级状态的初始化
        （复用实例不得清空既有记录）。hooks 是四个可选决策点（见 hooks.py），
        不传时行为与没有钩子时逐字相同。
        """
        if reset_globals:
            reset_read_tracking()  # 新会话开始，「已读文件」记录从零开始
        self.llm = llm if llm is not None else _default_llm()
        self.session = session or Session()
        self.permission = permission or Permission()
        self.context = ContextMeter()  # 上下文快照计量：真实锚点 + 临近阈值提醒
        # MCP 会话级服务：配置加载 / 后台连接 / 动态工具注册（start/close 挂钩）
        self.mcp = mcp if mcp is not None else McpService()
        self._token: AbortSignal | None = None  # 当前轮次的取消令牌（run 期间非空）
        # 事件出口：核心只发类型化事件（见 _emit）。`events` 是本**会话**的事件总线，
        # 前端订阅它取事件、`event.publish` 从深层调用点发事件。
        # `_loop` 是「本轮 run 所在的事件循环」——UI 线程 / 后台线程发的事件要转回
        # 它，否则 push 会跨线程碰 EventStream 的内部状态。
        self.events = Bus()
        self._stream: EventStream | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # 运行中排队的两条队列（见 queues.py）：变更即发 QueueChanged。
        # 属性名带 `_queue` 后缀，避免与 steer()/follow_up() 方法同名互相遮蔽。
        self.queue_config = config.load_queue_config()
        self.steering_queue = MessageQueue(
            "steer", self.queue_config.steering_mode, on_change=self._on_queue_changed
        )
        self.follow_up_queue = MessageQueue(
            "follow_up", self.queue_config.follow_up_mode, on_change=self._on_queue_changed
        )
        # 四个可选决策点（见 hooks.py）：不传 = 现状行为，逐字不变
        self.hooks = hooks or AgentHooks()
        self._session_owner: AgentSession | None = None
        self._last_batch_results: list[str] = []  # 本批工具结果文本，供 TurnContext
        self._model = model  # 非空时覆盖 config.MODEL
        self._turn: TurnConfig | None = None  # 本轮请求快照：run() 开头 pin，轮内冻结
        # 迭代上限：None 取配置；<0（默认 -1）表示不限制，正整数表示上限轮数
        self.max_iterations = (
            config.MAX_ITERATIONS if max_iterations is None else int(max_iterations)
        )
        # 进程级服务留在 Agent（不随会话存取）：llm / permission / models / 令牌
        self.sessions_config = config.load_sessions_config()
        self._persist = bool(persist) and self.sessions_config.enabled
        self._last_state = None  # 最近一次写盘的 state 快照（去重）
        self._title_attempts = 0  # 自动标题已尝试次数（有限次补试，见 _maybe_generate_title）
        self._title_cooldown = 0  # 到顶后的冷却轮数（见 TITLE_RETRY_ROUNDS）
        if store is not None:
            self.session.bind_store(store)
        elif self._persist:
            self.session.bind_store(self._new_store(oneshot=oneshot))
        # 候选模型目录：命令层只读 `agent.models.list()`，不关心来源与装载时机
        # （假 LLM 没有 list_models 时 Remote 恒返回 None，目录退化为"当前模型兜底"）
        cache = ModelCache()
        self.models = models or ModelCatalog(
            configured=ConfiguredModelSource(),
            cached=CachedModelSource(cache),
            remote=RemoteModelSource(getattr(self.llm, "list_models", None), cache),
            current_model=lambda: config.MODEL,
        )

    @property
    def session_owner(self) -> AgentSession:
        """本 Agent 的会话对象（惰性创建，进程内一个 Agent 对应一个会话）。

        `agent_session.py` 是会话级入口（目标续跑 / 排队 / 会话边界），宿主与
        扩展应当经它使用；Agent 保留单轮编排（`run`）。
        """
        if self._session_owner is None:
            from .agent_session import AgentSession

            self._session_owner = AgentSession(self)
        return self._session_owner

    @property
    def last_turn(self) -> TurnConfig | None:
        """本轮（最近一轮）请求快照：页脚据此展示实际发出的模型与思考强度。

        `run_with_goal` 连跑多轮时每轮覆盖，读到的是最后一轮的。轮外调用
        （如后台标题）不经过 `run()`，此时为上一轮的值或 None（回退 config）。
        """
        return self._turn

    def _turn_kwargs(self) -> dict:
        """本轮请求参数：真客户端透传 pin 住的 model/effort，替身走旧逻辑。

        30 多个 FakeLLM 的签名只有 `(messages, tools)`，无条件传参会全炸——
        只有置了 `accepts_turn_params` 的真客户端才透传。
        """
        if getattr(self.llm, "accepts_turn_params", False):
            turn = self._turn or TurnConfig.capture(self._model)
            return {"model": turn.model, "effort": turn.effort}
        return {"model": self._model} if self._model else {}

    def _new_store(self, oneshot: bool = False):
        """新建一个会话转录（轮换 id，文件懒物化）。"""
        return sessions.SessionStore.create(
            model=config.MODEL, effort=config.REASONING_EFFORT, oneshot=oneshot,
        )

    # ---------- 事件出口（核心 → 订阅者） ----------

    @property
    def stream(self) -> EventStream | None:
        """最近一轮 run 的事件流（run 期间创建，此后保留供宿主导出）。"""
        return self._stream

    def emit(self, data: AgentEvent) -> None:
        """线程安全的事件入口：UI 线程改队列、后台标题线程都用它。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            self._emit(data)  # 非运行态（如后台标题线程）：直接发
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._emit(data)
            return
        loop.call_soon_threadsafe(self._emit, data)

    def _emit(self, data: AgentEvent) -> None:
        """事件出口（只在本轮事件循环线程上调用，见 `emit`）。

        装一次信封，喂本轮流 + 投递给总线订阅者：**同一条事件的两个消费者看到
        的是同一个信封**（同一个 id / 会话标识），不存在两份真相。
        """
        if self.events.session_id is None:
            self.events.session_id = self.session_id()
        env = wrap(data, session_id=self.events.session_id)
        stream = self._stream
        if stream is not None and not stream.done:
            stream.push(env)
        try:
            self.events.deliver(env)
        except Exception as e:
            # 订阅者（前端 / 标题 / 将来的远程客户端）自身的故障：单独成型，
            # 不落进 StreamInterrupted 的收尾语义——UI 坏了既不该重试，也不该
            # 被报成「输出中断」（那会把排查方向引到网络上）。
            raise SubscriberError(f"订阅者异常: {type(e).__name__}: {e}") from e

    def session_id(self) -> str | None:
        """本会话的标识（供事件信封注入）。

        会话 id 目前仍由转录存储持有（见 `sessions/store.py`）；阶段 B 会把它
        收敛到会话对象上，届时这里改为直接读会话字段。
        """
        return getattr(getattr(self.session, "store", None), "id", None)

    # ---------- 钩子调用（未配置时返回默认值，行为与没有钩子时一致） ----------

    def _hook_signal(self) -> AbortSignal:
        """钩子的 signal 入参：本轮令牌；轮外（直接调 _execute_batch）新建一个。"""
        return self._token or current_token() or AbortSignal()

    async def before_tool_call(self, assistant_message: dict,
                               tool_call: dict) -> BeforeToolCallResult | None:
        """预检之前的异步决策点。参数不是合法 JSON 对象时不调用（没有 args 可看）。

        **钩子抛异常 = 拦下本次调用**（fail-closed）：扩展点的 bug 不该让某个
        `tool_call_id` 失去配对结果——那会让下一次请求被服务商拒绝，而故障现场
        已经跑到几轮之后。拦下时把异常文本作为结果回传，模型与用户都看得见。
        """
        hook = self.hooks.before_tool_call
        if hook is None:
            return None
        try:
            args = json.loads(tool_call["function"]["arguments"] or "{}")
        except (KeyError, TypeError, json.JSONDecodeError):
            return None
        if not isinstance(args, dict):
            return None
        try:
            return await hook(
                BeforeToolCallContext(assistant_message, tool_call, args), self._hook_signal()
            )
        except Exception as e:  # noqa: BLE001
            return BeforeToolCallResult(
                block=True, reason=f"错误: before_tool_call 钩子失败: {type(e).__name__}: {e}"
            )

    async def after_tool_call(self, assistant_message: dict, plan: ToolPlan,
                              result: str) -> AfterToolCallResult | None:
        """结果收集前的决策点：可替换结果文本 / 投票提前结束。

        **钩子抛异常 = 保留原结果并附一行说明**（fail-open）：结果已经拿到了，
        格式化失败不该把它弄丢。
        """
        hook = self.hooks.after_tool_call
        if hook is None:
            return None
        try:
            return await hook(
                AfterToolCallContext(assistant_message, plan.tc, self._plan_args(plan), result),
                self._hook_signal(),
            )
        except Exception as e:  # noqa: BLE001
            return AfterToolCallResult(
                content=f"{result}\n（after_tool_call 钩子失败: {type(e).__name__}: {e}）"
            )

    @staticmethod
    def _plan_args(plan: ToolPlan) -> dict:
        """计划的目标参数（预检已解析过一次；失败退化为空）。"""
        try:
            args = json.loads(plan.tc["function"]["arguments"] or "{}")
        except (KeyError, TypeError, json.JSONDecodeError):
            return {}
        return args if isinstance(args, dict) else {}

    async def _hook_prepare_next_turn(self, turn: TurnContext) -> None:
        """默认准备（同步系统提示词 + 按需压缩）之后的附加调用点。"""
        hook = self.hooks.prepare_next_turn
        if hook is not None:
            await hook(turn, self._hook_signal())

    async def _hook_should_stop_after_turn(self, turn: TurnContext) -> bool:
        """回合结束后的裁决点：True 即结束本次 run（不再发起下一轮模型调用）。"""
        hook = self.hooks.should_stop_after_turn
        if hook is None:
            return False
        return bool(await hook(turn, self._hook_signal()))

    def _ensure_stream(self) -> bool:
        """确保存在一条未收口的事件流，返回「本次是否由调用方创建」。

        创建者拥有它：负责发终结事件并收口。`run_with_goal` 连跑多轮时只应有
        一条流（否则每次 `run()` 都终结一次，消费者会在第一轮就拿到结果）。
        """
        if self._stream is None or self._stream.done:
            self._stream = agent_event_stream()
            return True
        return False

    def _close_stream(self, error: BaseException | None, result: RunResult | None = None) -> None:
        """收口事件流：发终结事件（有结果时）并唤醒消费者；异常路径也必须收口。"""
        if result is not None:
            self._emit(AgentEnd(result))  # 终结事件：终结值即本次 RunResult
        if self._stream is not None:
            self._stream.end(error)
        self._loop = None

    # ---------- 运行中排队（见 queues.py） ----------

    async def prompt(self, text: str, *, images=None,
                     delivery: str = "auto") -> RunResult | None:
        """统一的任务入口：空闲即开跑，运行中按投递方式入队。

        `auto`（默认）读 `[queue] delivery`（默认 `follow`：等本轮跑完再送）；
        显式传 `"follow"` / `"steer"` 供扩展与 SDK 覆盖配置——UI 不提供这个选择，
        所以「怎么投递」是配置项而不是新按键。

        返回：真正跑了就返回本轮 `RunResult`；入队则返回 `None`（通知走
        `QueueChanged` 事件，宿主据此刷新队列面板）。
        """
        if self._token is None:
            return await self.session_owner.run_with_goal(text)
        self.enqueue(text, images, delivery)
        return None

    def enqueue(self, text: str, images=None, delivery: str = "auto") -> QueueItem:
        """按投递方式入队（同步，宿主在 UI 线程调用；`prompt` 也走这里）。

        `auto` 读 `[queue].delivery`（默认 `follow`）。返回入队的条目（带 id，
        UI 的逐条撤销据此定位）。
        """
        kind = self.queue_config.delivery if delivery == "auto" else delivery
        if kind == "steer":
            return self.steer(text, images)
        if kind == "follow":
            return self.follow_up(text, images)
        raise ValueError(f"未知的投递方式: {kind!r}（可选 auto / follow / steer）")

    @property
    def busy(self) -> bool:
        """是否正在跑任务（运行中提交的输入会入队而不是立刻开跑）。"""
        return self._token is not None

    def steer(self, text: str, images=None) -> QueueItem:
        """运行中插话：当前工具批结束后立即作为 user 消息送入本轮。"""
        return self.steering_queue.enqueue(text, images)

    def follow_up(self, text: str, images=None) -> QueueItem:
        """留到本轮跑完再送（默认投递方式）。"""
        return self.follow_up_queue.enqueue(text, images)

    def take_queued(self, item_id: str) -> QueueItem | None:
        """摘出某条排队项并返回（UI 的「取回编辑」）；不存在返回 None。

        与 `cancel_queued` 的差别：撤销只丢弃，取回要把文本还给调用方去填回输入框。
        """
        item = self.steering_queue.take(item_id)
        return item if item is not None else self.follow_up_queue.take(item_id)

    def cancel_queued(self, item_id: str) -> bool:
        """逐条撤销排队项（UI 行尾「✕」）。"""
        return self.steering_queue.remove(item_id) or self.follow_up_queue.remove(item_id)

    def clear_steering_queue(self) -> list[str]:
        return [item.text for item in self.steering_queue.clear()]

    def clear_follow_up_queue(self) -> list[str]:
        return [item.text for item in self.follow_up_queue.clear()]

    def clear_queue(self) -> tuple[list[str], list[str]]:
        """清空两条队列，返回 (steering, follow_up) 的文本。

        Esc 中止时宿主用它把排队内容取回输入框（对齐 pi 的 dequeue 行为）。
        """
        return self.clear_steering_queue(), self.clear_follow_up_queue()

    def _deliver(self, queue: MessageQueue) -> tuple[QueueItem, ...]:
        """按抽水策略取走待投递项：作为 user 消息进历史 + 发投递事件。

        投递即彻底出队（`drain` 会发 `QueueChanged`），所以同一项不会被投两次。
        发 `QueuedPromptDelivered` 是因为入队时这条文本只存在于排队面板里——前端
        要在**投递**这一刻才把它落到对话区。
        """
        items = tuple(queue.drain())
        for item in items:
            kwargs = {"images": list(item.images)} if item.images else {}
            self.session.add("user", item.text, **kwargs)
            self._emit(QueuedPromptDelivered(text=item.text, steering=queue.kind == "steer"))
        return items

    def _drain_queue(self, queue: MessageQueue) -> bool:
        """循环里的抽水点：有投递则返回 True（见 `_deliver`）。"""
        return bool(self._deliver(queue))

    def get_steering_messages(self) -> tuple[QueueItem, ...]:
        """投递点：取走待插话项（走 `_deliver`：进历史 + 上屏，不是裸 drain）。"""
        return self._deliver(self.steering_queue)

    def get_follow_up_messages(self) -> tuple[QueueItem, ...]:
        """投递点：取走待续跑项（走 `_deliver`：进历史 + 上屏，不是裸 drain）。"""
        return self._deliver(self.follow_up_queue)

    @property
    def pending_message_count(self) -> int:
        """两条队列的待投递总数（UI 的「排队中 N」）。"""
        return self.steering_queue.count + self.follow_up_queue.count

    def _on_queue_changed(self) -> None:
        """队列变更回调：可能在 UI 线程上触发，统一走线程安全的事件入口。"""
        self.emit(
            QueueChanged(
                steering=tuple(self.steering_queue.list()),
                follow_up=tuple(self.follow_up_queue.list()),
            )
        )

    def start(self) -> None:
        """启动期装载模型目录、技能目录与项目指令：模型未配置时后台拉取 `/models`。

        技能发现可能弹出项目信任确认（渲染后端此时为 ConsoleRenderer，TUI
        尚未接管，交互行为一致）。项目指令在会话边界装载（此处 / `/new` /
        恢复三处），会话中途不重载以保护提示前缀缓存；读取失败只警告、不阻断启动。
        """
        self.models.bootstrap()
        # 事件总线与询问端口由**宿主**装配（`frontend.attach`）：本类不认识任何前端，
        # 也不该往进程级全局里塞东西——多会话时那是两份真相。start() 之前装配好
        # 即可覆盖启动期与命令层的提问（如 /skills refresh 的项目信任确认）。
        self.refresh_skills() 
        instructions.refresh()
        self.mcp.start()  # 后台连接已配置的 MCP 服务器；失败隔离、不阻塞启动

    def refresh_skills(self) -> list:
        """重新发现技能并同步 use_skill 工具 schema（启动与 /skills refresh 共用）。"""
        diagnostics = skills.refresh()
        sync_schema()
        return diagnostics

    def new_session(self) -> None:
        """开启新会话（/new 的实际动作）：原地重置全部会话口径状态。

        覆盖：消息历史与会话 id、会话用量、权限会话规则、越界信任目录、
        上下文快照（压缩计数与真实 token 锚点）、工具侧「已读文件」记录、
        步骤清单、持久目标、技能激活集合，以及转录文件的轮换（旧会话保留
        在磁盘、仍可恢复）。新增会话级状态时注册进 `_state_registry`，
        命令层（commands）不感知重置细节。项目指令不属会话状态，但在
        `/new` 这一会话边界重新装载一次（读盘或去重，见 instructions.refresh）。
        """
        if self.session.store is not None:
            self.session.store.close()  # 旧转录闭合；从未物化则不产生文件
        self.session.reset()
        self.permission.new_session()
        config.SESSION_EXTRA_ROOTS.clear()
        self.context.new_session()
        reset_read_tracking()
        for part in self._state_registry():
            part.reset()
        self._last_state = None
        self._title_attempts = 0
        self._title_cooldown = 0
        self.clear_queue()  # 新会话不继承上一条会话的排队输入
        instructions.refresh()  # 会话边界：重新装载项目约定（中途修改在此生效）
        if self._persist:
            self.session.bind_store(self._new_store())
        else:
            self.session.bind_store(None)
        # 标题事件：Session.reset 已清空会话标题，宿主据此回退到默认标题
        self.emit(TitleChanged(""))

    # ---------- 会话恢复（原地装载，不换对象） ----------

    def resume(self, target) -> ResumeReport:
        """恢复一个已保存的会话。

        target 支持 SessionSummary / 会话 id / 唯一前缀 / `.jsonl` 路径 /
        旧 `.json` 路径。装载是原地的——`self.session` 对象身份不变，宿主
        对它的引用与渲染绑定始终有效。安全语义（§3.9）：权限会话规则、
        越界信任目录、已读记录一律重置不恢复。
        """
        summary = self._resolve_target(target)
        loaded = sessions.load(summary)

        if self.session.store is not None:
            self.session.store.close()
        # 沿用持久化 id：{$session} 请求头跨进程稳定
        store = sessions.SessionStore.open(
            loaded.path, loaded.id, cwd=loaded.cwd or None
        )
        self.session.bind_store(store)
        self.session.restore_state(
            loaded.messages, loaded.meta,
            title=loaded.title, title_source=loaded.title_source,
        )
        # 崩溃修复补的占位结果落盘：否则下次恢复会被误判成中段损坏
        for message in loaded.repaired:
            store.append_message(message)

        # 安全例外：跨进程不继承（对齐 Claude Code 的 fork 语义）
        self.permission.new_session()
        config.SESSION_EXTRA_ROOTS.clear()
        reset_read_tracking()

        # 计量重算：真实 token 锚点作废，压缩次数按转录里的检查点数恢复
        self.context.new_session()
        self.context.compact_count = loaded.compact_count

        # 投影缓存恢复：先全部重置，再按记录恢复（缺失时保持默认值）
        for part in self._state_registry():
            part.reset()
        if self.sessions_config.persist_state and loaded.state:
            for part in self._state_registry():
                if part.name in loaded.state:
                    part.restore(loaded.state[part.name])
        self._last_state = None
        self._title_attempts = 0
        self._title_cooldown = 0
        instructions.refresh()  # 会话边界：恢复即按磁盘最新内容重建项目约定段
        self.session.sync_system()  # system 段按当前提示词立即重建
        self.emit(TitleChanged(self.session.title))  # 标题随恢复的会话同步

        return ResumeReport(
            path=loaded.path,
            session_id=loaded.id,
            title=loaded.title,
            message_count=len(loaded.messages),
            repair=loaded.repair,
            bad_lines=loaded.bad_lines,
            model=loaded.model,
        )

    @staticmethod
    def _resolve_target(target):
        """定位恢复目标：摘要对象 / 文件路径 / id / 唯一前缀。"""
        if isinstance(target, sessions.SessionSummary):
            return target
        text = str(target or "").strip()
        if not text:
            raise sessions.StoreError("未指定要恢复的会话。")
        path = Path(text)
        if path.is_file():
            if path.suffix == ".json":
                return sessions.import_json(path)  # 旧格式：导入后继续
            return sessions.summary_from_path(path)
        summary = sessions.find(text)
        if summary is None:
            raise sessions.StoreError(f"未找到会话：{text}")
        return summary

    def _state_registry(self) -> tuple:
        """会话级状态注册表：new_session / resume / 快照共用同一张表。

        新增会话级状态时在此注册（测试断言注册表覆盖重置清单），否则
        恢复会静默漏项。进程级服务（llm / permission 引擎 / models）不在表内。
        """
        # 直接绑到**会话持有的实例**（而不是模块函数）：模块函数作用在"当前绑定
        # 的实例"上，取值正确性依赖绑定时机；这里显式取会话实例，任何调用顺序都
        # 得到同一个真相（会话状态只有一处）。
        owner = self.session_owner
        return (
            _StatePart("goal", owner.goal_state.snapshot, owner.goal_state.restore,
                       owner.goal_state.reset),
            _StatePart("plan", owner.plan_state.snapshot, owner.plan_state.restore,
                       owner.plan_state.reset),
            _StatePart("skills", owner.skills_state.snapshot, owner.skills_state.restore,
                       owner.skills_state.reset),
        )

    def _persist_turn(self) -> None:
        """一轮结束：把变化的会话级状态写入转录（投影缓存，序列化去重）。"""
        if self.session.store is None or not self.sessions_config.persist_state:
            return
        payload = {part.name: part.snapshot() for part in self._state_registry()}
        if payload == self._last_state:
            return
        self._last_state = payload
        self.session.store.append_state(payload)

    # ---------- 自动标题（后台，失败有限次补试） ----------

    def _maybe_generate_title(self) -> None:
        """一轮正常结束后自动生成标题：用户标题优先，失败不永久放弃。

        每次任务正常结束调用一次；标题请求本身失败（网络抖动、瞬时错误重试
        耗尽、模型输出不可用）时保留计数，下一轮结束再试，最多
        `TITLE_MAX_ATTEMPTS` 次——原来的"只试一次"会让一次抖动永久丢掉标题。
        到顶后不永久沉默：进入 `TITLE_RETRY_ROUNDS` 轮冷却，冷却一过自动再探
        （计数归零重来，期间只提示一次）；`reset_title_attempts`（/model 切换
        模型）同样重置计数——换模型往往意味着失败原因已消除。
        已生成标题或用户自行命名后不再发起（`should_generate` 判空）。
        """
        if self.session.store is None or not self.sessions_config.auto_title:
            return
        if self._title_attempts >= TITLE_MAX_ATTEMPTS:
            self._title_cooldown += 1
            if self._title_cooldown >= TITLE_RETRY_ROUNDS:
                self._title_attempts = 0  # 冷却结束：再给一轮机会
                self._title_cooldown = 0
            return
        if not sessions.should_generate(self.session.title, self.session.title_source):
            return
        self._title_attempts += 1
        # 在主线程取快照（后台线程不再读会话消息），再交给 daemon 线程
        request = sessions.build_title_request(self.session.messages)
        model = self.sessions_config.title_model or None
        threading.Thread(
            target=self._title_worker, args=(request, model, self._title_attempts),
            name="smithcode-title", daemon=True,
        ).start()

    def reset_title_attempts(self) -> None:
        """重置自动标题计数与冷却（如 /model 切换模型后调用）。

        标题失败常与当前模型相关（小模型不按 JSON 输出、4xx 拒标题请求）；
        换模型后沿用旧的耗尽计数不合理——直接归零，让下一轮正常结束即再试。
        """
        self._title_attempts = 0
        self._title_cooldown = 0

    def _title_worker(self, request, model, attempt: int = 1) -> None:
        """后台生成标题；失败只提示不抛，绝不影响主流程。"""
        try:
            text = self._complete(request, model=model)
        except Exception as e:  # noqa: BLE001 标题失败不打断任务
            self._note_title_failure(f"{type(e).__name__}: {e}", attempt)
            return
        title = sessions.clean_title(text, self.sessions_config.title_max_chars)
        if not title:
            self._note_title_failure("模型未返回可用标题", attempt)
            return
        self.session.set_title(title, source="auto")
        self.emit(TitleChanged(title))  # 后台线程：走线程安全入口

    def _note_title_failure(self, reason: str, attempt: int) -> None:
        """标题生成失败提示：说明是否还会补试，避免"静默失败"让人以为功能没生效。

        瞬时错误在 `llm/client.py` 内已重试多次（重试过程本身也会打印），这里
        只报最终结果；到顶后提示改用 /rename，避免继续无声等待。
        """
        if attempt < TITLE_MAX_ATTEMPTS:
            hint = f"将在下一轮结束后重试（{attempt}/{TITLE_MAX_ATTEMPTS}）"
        else:
            hint = (f"已停止重试（{TITLE_MAX_ATTEMPTS} 次均失败），可用 /rename 手动命名；"
                    f"{TITLE_RETRY_ROUNDS} 轮后会自动再试一次，切换模型会立即重试")
        # 后台线程：走线程安全的 emit
        self.emit(Notice(f"[标题] 自动命名失败：{reason}；{hint}", level="warning"))

    def rename_session(self, title: str) -> bool:
        """用户命名当前会话（/rename / --name）：刷新标题记录，自动标题不再覆盖。"""
        text = str(title or "").strip()
        if not text:
            return False
        self.session.set_title(text, source="user")
        self.emit(TitleChanged(text))  # 可能来自 UI 线程：走线程安全入口
        return True

    def close(self) -> None:
        """进程退出前收尾：关闭 MCP 连接 + flush + 关闭转录句柄。"""
        self.mcp.stop()
        if self.session.store is not None:
            self.session.store.close()

    def interrupt(self) -> None:
        """请求中断当前任务（线程安全：TUI / REPL 主线程调用，Agent 在后台线程运行）。

        取消是协作式的：LLM 流在下一块数据到达前截停，正在执行的工具让
        其跑完，未执行的工具调用补占位结果——会话历史始终保持合法。
        """
        if self._token is not None:
            self._token.cancel()

    async def run(self, user_input: str) -> RunResult:
        """执行一次任务直至模型给出最终回复（或中断 / 拒绝 / 迭代上限 / 流中断）。

        协程入口：宿主用一个事件循环驱动一次任务（REPL / TUI 各自在自己线程里
        用 `asyncio.run`；两者都保留「主线程做 Ctrl+C 通道」的既有结构）。

        每次调用激活一个取消令牌并经 ContextVar 沿调用链传播（llm 流层、
        工具调度层按需读取）；结束后复位，保证下一次任务不受残留取消状态
        影响。
        响应流中途断开（`StreamInterrupted`）不向上抛异常：部分正文已经实时
        上屏，这里把它与中断说明一并写进历史（`_note_stream_interrupted`），
        返回 `stream_error` 状态并保留部分正文作为 `text`——宿主据此提示"输出
        中断"，下一轮模型也能接着写，而不是从头重做。
        本轮请求快照（模型与思考强度）在入口 pin 住：`run()` 内所有 `_chat` /
        `_complete` 都用它，轮内切配置不影响本轮，下一轮自动用新的；页脚据此
        展示实际发出的值。`finally` 里不清——页脚在返回后才读，下一轮开头覆盖。
        """
        return await self._run_turn(user_input)

    async def continue_run(self) -> RunResult:
        """**不注入新 user 消息**，接着当前上下文再跑一轮（pi 的 `agentLoopContinue`）。

        与 `run()` 的区别只有一处：不追加 user 消息，模型看到的历史原样不变。
        约束（对齐 pi，都是为了让"最后一条"合法）：

        - 上下文里至少有一条非 system 消息，否则无从"接着"；
        - 最后一条不能是 assistant——否则等于让模型连续产出两条 assistant 消息，
          服务商侧通常直接报错。被中断的轮次末尾是我们回写的 user 注释，正好满足
          条件（`Agent._note_interrupted`），所以「中断后接着写」是它的典型用法。

        名字说明：Python 里 `continue` 是关键字，方法名只能写成 `continue_run`。
        """
        messages = self.session.messages
        if not any(message.get("role") != "system" for message in messages):
            raise ValueError("上下文为空，无法继续：请先用 run() 发一条消息")
        if messages and messages[-1].get("role") == "assistant":
            raise ValueError(
                "最后一条是 assistant 消息，无法继续：请用 run() 追加输入，"
                "或先由外部事件（工具结果 / 中断注释）把末尾变成非 assistant"
            )
        return await self._run_turn(None)

    async def _run_turn(self, user_input: str | None) -> RunResult:
        """一轮任务的主体：`user_input` 为 None 时表示"继续"（不追加 user 消息）。"""
        # 让 goal / plan / skills 的模块函数指向本会话的实例：宿主（命令层 / TUI）
        # 与 `session.sync_system()` 都按模块函数读取，绑定后它们自动落在同一份
        # 状态上。会话尚未建立时不建（`session_owner` 惰性创建有它自己的时机）。
        if self._session_owner is not None:
            self._session_owner.bind_state()
        self.session.sync_system()  # 发请求前同步系统提示词（含当前持久目标段）
        if user_input is not None:
            self.session.add("user", user_input)
        self._turn = TurnConfig.capture(self._model)
        token = AbortSignal()
        self._token = token
        owner = self._ensure_stream()  # 直接调用时本层拥有事件流；被目标续跑驱动时不是
        if owner:
            self._loop = asyncio.get_running_loop()
            self.events.bind_loop(self._loop)  # 跨线程发事件时跳回本循环
        reset_token = activate_token(token)
        emitter_token = emitter.activate(self.emit)  # 深层模块（llm 层）也能发事件
        status = "error"
        self._emit(TurnStart())
        try:
            result = await self._run_loop(token)
            status = result.status
        except StreamInterrupted as e:
            reason = describe_error(e.original)  # 分类: 原始信息（诊断用）
            self._note_stream_interrupted(e.partial, reason)
            result = RunResult("stream_error", e.partial, reason=reason)
            status = result.status
        except BaseException as exc:
            if owner:  # 未预期异常也要收口，否则消费者一直等下去
                self._close_stream(exc)
            raise
        finally:
            self._token = None
            reset_token()
            emitter.reset(emitter_token)
            self._emit(TurnEnd(status))
        if owner:
            self._close_stream(None, result)
        self._persist_turn()  # 状态投影缓存落盘（无变化不写）
        self._checkpoint()  # 每轮结束的 fsync 屏障：本轮消息与状态挺过断电
        if result.status == "ok":
            self._maybe_generate_title()  # 本轮结束后自动标题（后台，失败下轮补试）
        if result.status == "interrupted":
            self._note_interrupted()  # 回写上下文但不发请求，供下一轮模型看到
        return result

    def _note_interrupted(self) -> None:
        """把「用户中断」事件作为 user 消息写进会话历史（不触发新请求）。

        追加在所有占位结果之后，是本次轮次的最后一条消息；下一轮用户提问时
        模型即可看到上一轮被主动中止、任务未完成，不会把部分输出当作结果。"""
        self.session.add("user", INTERRUPTED_CONTEXT)

    def outer_turn_begin(self) -> bool:
        """外层回合入口（目标续跑的多轮包装）：返回「本层是否拥有事件流」。

        多轮只应有一条事件流（否则每轮 `run()` 都终结一次，消费者会在第一轮就
        拿到结果），所以由**最外层**创建并收口。这里同时钉住本轮的事件循环，
        供跨线程事件（UI 线程改队列、后台标题）转回。
        """
        owner = self._ensure_stream()
        if owner:
            self._loop = asyncio.get_running_loop()
            self.events.bind_loop(self._loop)  # 跨线程发事件时跳回本循环
        self._emit(TurnStart())
        return owner

    def outer_turn_end(self, owner: bool, result: RunResult | None,
                       exc: BaseException | None = None) -> None:
        """外层回合收尾：发 `TurnEnd`；异常路径也要收口事件流。"""
        self._emit(TurnEnd(result.status if result is not None else "error"))
        if exc is not None:
            if owner:
                self._close_stream(exc)
            return
        if owner:
            self._close_stream(None, result)

    def note_goal_run(self, result: RunResult) -> None:
        """把一轮结果同步给目标状态机：推进动作重置阻碍连击、累计 token 用量。"""
        goal.note_run(
            result.tools_used,
            self.session.usage.current_session.get("total_tokens"),
        )

    async def _run_loop(self, token: AbortSignal) -> RunResult:
        tools_used: list = []  # 本任务执行过的工具名（去重保序），供 /goal 续跑裁决
        iteration = 0  # 已执行的工具轮次（一轮 = 一次模型调用 + 执行其工具调用）
        while True:
            if token.cancelled:
                return RunResult("interrupted")
            # 抽水点 1/2：起点与每轮末。用户在上一次等待/上一轮期间提交的插话
            # 在这里作为 user 消息进入历史（对齐 pi 的 `agent-loop.ts:174,263`）
            self._drain_queue(self.steering_queue)
            # 每轮同步系统提示词：本轮加载的技能正文下一轮生效（内容不变时不重建）
            self.session.sync_system()
            await self._compact_if_needed()
            # 钩子：默认准备之后的附加调用点（见 hooks.py 的取舍说明）
            await self._hook_prepare_next_turn(
                TurnContext(tool_results=tuple(self._last_batch_results),
                            tools_used=tuple(tools_used), iteration=iteration)
            )
            msg, usage, interrupted = await self._chat_with_recovery()
            self._record_model_call(msg, usage)

            if interrupted:
                return RunResult("interrupted", partial=True, tools_used=tuple(tools_used))

            if not msg.get("tool_calls"):
                # 抽水点 3：本要停时取 follow-up（对齐 pi 的 `agent-loop.ts:267-272`）——
                # 还有排队输入就接着跑，而不是先把这轮结束掉
                if self._drain_queue(self.follow_up_queue):
                    continue
                return RunResult("ok", msg.get("content", ""), tools_used=tuple(tools_used))

            if token.cancelled:
                # 流刚好走完时才取消：这批 tool_calls 一个都未执行，补占位后停止
                self._interrupt_batch([], msg.get("tool_calls", []))
                return RunResult("interrupted", tools_used=tuple(tools_used))

            for tc in msg["tool_calls"]:
                name = tc.get("function", {}).get("name", "")
                if name and name not in tools_used:
                    tools_used.append(name)
            self._checkpoint()  # 副作用屏障：本批 tool_calls 先落盘，工具才会真正执行
            stopped = await self._execute_batch(msg["tool_calls"], msg)
            if stopped == "denied":
                return RunResult(
                    "denied", "任务已停止：权限请求被用户拒绝。", tools_used=tuple(tools_used)
                )
            if stopped == "interrupted":
                return RunResult("interrupted", tools_used=tuple(tools_used))
            if stopped == "terminate":
                # 整批钩子都投票提前结束：不再发起下一轮模型调用（正文为空）
                return RunResult("ok", "", tools_used=tuple(tools_used))

            iteration += 1
            # 钩子：回合结束后的裁决点（如目标续跑的刹车逻辑）
            turn = TurnContext(message=msg, tool_results=tuple(self._last_batch_results),
                               tools_used=tuple(tools_used), iteration=iteration)
            if await self._hook_should_stop_after_turn(turn):
                return RunResult("ok", "", tools_used=tuple(tools_used))
            # 仅当配置为正整数时封顶；达到上限后走收尾轮而非硬中止
            if self.max_iterations > 0 and iteration >= self.max_iterations:
                return await self._wrap_up(tools_used)

    def _record_model_call(self, msg: dict, usage: dict | None) -> None:
        """登记一次模型调用：会话用量 + 真实 token 锚点 + 转录用量 + 消息入库。"""
        self.session.usage.add(usage)
        self.context.record(usage)  # 记下真实 prompt_tokens 作估算锚点
        store = self.session.store
        if store is not None:
            # 记「谁生成的」：用本轮 pin 住的快照，而不是可能已被 /model 改掉的全局值
            turn = self._turn or TurnConfig.capture(self._model)
            store.append_model(turn.model, turn.effort)
            if usage:
                store.append_usage(usage)
        self.session.messages.append(msg)

    def _checkpoint(self) -> None:
        """崩溃持久化屏障：把已追加的记录 fsync 到磁盘（无持久化时无操作）。

        只在语义点调用——工具可能产生外部副作用之前，以及本轮结束之后。两次
        检查点之间的普通追加只 `flush()`（进程内可见即可），fsync 的成本留给
        "错了就没法挽回"的时刻。失败仍按既有策略降级为纯内存会话（fail-open）。
        """
        store = self.session.store
        if store is not None:
            store.sync()

    async def _wrap_up(self, tools_used: list) -> RunResult:
        """到达迭代上限后的收尾轮：不再暴露工具，要求模型用纯文本总结。

        对齐 opencode 的 max-steps 语义——最后一轮强制 text-only（`tools=None`），
        并把收尾提示词作为 user 消息入库，使总结成为本任务最后一条可见回复。
        若模型仍返回 tool_calls，一律剥离、不执行，避免历史里留下悬空
        `tool_call_id`（下一条请求会因此非法）。返回状态仍为 `max_iterations`，
        宿主据此提示「已达上限」；总结正文已在流式过程中展示。
        """
        self._emit(Notice(
            f"[迭代] 已达上限（{self.max_iterations} 轮），请求模型总结本次任务…",
            level="warning",
        ))
        self.session.add("user", MAX_ITERATIONS_WRAPUP)
        self.session.sync_system()
        await self._compact_if_needed()
        msg, usage, interrupted = await self._chat_with_recovery(use_tools=False)
        text = msg.get("content") or ""
        if msg.get("tool_calls"):
            # 收尾轮不执行工具：剥离残缺工具调用，只保留正文
            msg = {"role": "assistant", "content": text}
        self._record_model_call(msg, usage)
        if interrupted:
            return RunResult("interrupted", partial=True, tools_used=tuple(tools_used))
        return RunResult("max_iterations", text, tools_used=tuple(tools_used))

    async def _chat_with_recovery(self, use_tools: bool = True) -> tuple[dict, dict | None, bool]:
        """一次模型调用；上下文溢出时压缩后重试一次（opencode 的溢出恢复）。

        仅当错误文本命中溢出特征才走这条路，其他异常原样上抛；每步只重试
        一次，不反复烧钱。流中途断开（`StreamInterrupted`）不重试——正文已
        经实时上屏，重放只会重复打印；但已收到的部分要写进会话历史后继续
        上抛，保证「界面上看到的」与「历史里的」一致，否则下一轮模型看不见
        自己的输出，会从头重做一遍。首块之前就断开（部分正文为空）时历史
        没有任何残缺内容，纯溢出重试照常进行。
        `use_tools=False` 用于迭代上限的收尾轮（强制纯文本，不暴露工具）。
        返回 (消息, 用量, 是否被中断)。
        """
        try:
            return await self._chat(use_tools=use_tools)
        except StreamInterrupted as e:
            if e.partial.strip() or not is_context_overflow(e.original):
                raise
            # 首块之前就断且是上下文溢出：历史里没有残留正文，按溢出恢复重试
        except Exception as e:
            if not is_context_overflow(e):
                raise
        self._emit(Notice("[context] 上下文溢出，压缩后重试…"))
        await self.compact()
        return await self._chat(use_tools=use_tools)

    def _note_stream_interrupted(self, partial: str, reason: str | None = None) -> None:
        """把「流中断」事件写进会话历史：已上屏的部分正文 + 一行中断说明。

        部分正文按 assistant 消息落库（与屏幕上看到的一致）；正文为空（首块之前
        就断了）则只写说明，不留空 assistant 消息。说明是 user 消息、不触发新请求，
        下一轮模型据此续写而非重做；reason（`分类: 原始信息`）一并写入，让下一轮
        模型知道断在哪类故障上。
        """
        if partial.strip():
            self.session.messages.append({"role": "assistant", "content": partial})
        self.session.add("user", stream_interrupted_context(reason))

    async def _compact_if_needed(self) -> None:
        """每轮调用前的预检：估算越过阈值（预算 × COMPACT_TRIGGER）就先压缩。"""
        budget = config.CONTEXT_TOKEN_BUDGET
        if total_tokens(self.session.messages) > budget * config.COMPACT_TRIGGER:
            await self.compact()

    async def compact(self) -> bool:
        """LLM 摘要压缩：中段历史换成结构化摘要，保留 system 与近期尾部。

        返回是否实际压缩。失败（无中段可压、摘要两次不合格）一律保持消息
        原样并返回 False——压缩只是手段，绝不因此中断任务。
        """
        messages = self.session.messages
        tail_start = pick_tail(messages, config.COMPACT_KEEP_TOKENS)
        old = messages[1:tail_start]
        if not old:
            return False

        before = total_tokens(messages)
        summary = None
        # 压缩要发一次摘要请求（数秒到数十秒）：用状态事件告知前端「在压缩」，
        # 而不是混进对话区的一行 info（TUI 的忙碌行可以据此显示，见 status.py）
        self._emit(StatusChanged(kind="compaction", text="正在压缩上下文…"))
        for _ in range(2):  # 摘要缺必需标题时重试一次
            token = current_token()
            if token is not None and token.cancelled:
                return False  # 已中断：不再发起摘要请求，静默放弃（中断提示由收尾路径给出）
            text = await self._acomplete(build_summary_request(old))
            if validate_summary(text):
                summary = text
                break
        if summary is None:
            self._emit(StatusCleared(kind="compaction"))
            self._emit(Notice("[context] 摘要未按模板生成，放弃本次压缩，原样继续"))
            return False

        assembled = assemble(
            messages[0].get("content", ""), summary, messages[tail_start:]
        )
        self.session.set_compacted(
            assembled[1], assembled[2:], before=before,
            after=total_tokens(assembled),
        )
        dropped = skills.prune_active(self.session.messages)
        if dropped:
            # 技能正文在被压缩的中段里：剔除加载集合并显式告知模型，
            # 避免它以为手上还有一份看不见的指令（需要时重新 use_skill 加载）
            self.session.add("user", skills.render.compacted_notice(dropped))
            self._persist_turn()  # 剔除结果立即落进 t=state，恢复时不与转录打架
        self.context.compact_count += 1
        self._emit(StatusCleared(kind="compaction"))
        self._emit(Notice(
            f"[context] 已压缩: {before:,} → {total_tokens(self.session.messages):,} tokens"
        ))
        return True

    async def compact_manual(self) -> str:
        """手动 /compact 的宿主入口：在独立轮次令牌下压缩，返回结果状态。

        与 `compact()` 的差别只在取消语义：自动压缩跑在任务轮次里，令牌由
        `run()` 提供；手动压缩没有轮次，这里自建令牌并挂到 `_token` 上，
        使 Esc / Ctrl+C 能经 `interrupt()` 关流截停摘要请求（否则压缩一旦
        开始就只能等它跑完）。返回 "ok"（已压缩）/ "cancelled"（被中断）/
        "empty"（无中段可压或摘要两次不合格）。
        """
        token = AbortSignal()
        self._token = token
        reset_token = activate_token(token)
        try:
            changed = await self.compact()
        finally:
            self._token = None
            reset_token()
        if changed:
            return "ok"
        return "cancelled" if token.cancelled else "empty"

    async def _acomplete(self, request: list[dict], model: str | None = None) -> str:
        """`_complete` 的异步入口：阻塞式补全下放线程，别冻住事件循环。

        轮内调用（压缩摘要）走这里。标题生成走后台线程里的同步 `_complete`
        （`_title_worker` 本就是 daemon 线程，不需要再借事件循环）。
        """
        return await asyncio.to_thread(self._complete, request, model)

    def _complete(self, request: list[dict], model: str | None = None) -> str:
        """一次不带工具的补全，收集完整文本（摘要 / 标题生成专用）。

        同步实现：直接迭代同步客户端，供后台线程与 `_acomplete` 复用。
        显式 `model`（标题专用模型）优先；否则用本轮 pin 住的快照，保证轮内
        压缩摘要与主循环用同一份；轮外调用（`_turn` 为空）回退旧逻辑。
        """
        parts = []
        if model:
            kwargs = {"model": model}
        elif getattr(self.llm, "accepts_turn_params", False):
            turn = self._turn or TurnConfig.capture(self._model)
            kwargs = {"model": turn.model, "effort": turn.effort}
        else:
            kwargs = {"model": self._model} if self._model else {}
        for kind, payload in self.llm.chat_stream(request, tools=None, **kwargs):
            if kind == "content":
                parts.append(payload)
            elif kind == "message" and not parts:
                parts.append(payload.get("content") or "")
        return "".join(parts)

    async def _chat(self, use_tools: bool = True) -> tuple[dict, dict | None, bool]:
        """一次流式模型调用：思考与正文各占一行（均带 助手> 前缀）。

        `use_tools=False` 时不暴露任何工具（迭代上限收尾轮强制纯文本）。
        返回 (消息, 用量, 是否被中断)。用量由 llm 层从流中提取，服务商
        不提供时为 None。渲染交给 renderer（CLI 逐字打印 / TUI 进组件）。

        **正文按尝试累积**（对齐 opencode 在一条 assistant 消息里累积多个 text
        part）：llm 层每次尝试的正文都实时上屏，这里把它们按到达顺序拼进同一条
        消息——重试成功后，历史里是「第一次中断的那段 + 重试补完的那段」，与
        用户屏幕上看到的内容逐一对应，不会出现"屏幕上有、历史里没有"。
        内容一律以累积的 `parts` 为准重建：llm 层最后那条 message 只交付
        tool_calls 与归属，不再携带正文——"正文是什么"只有 `parts` 一个来源，
        两条链路不会再出现两套说法（message.content 为空是刻意的，不是缺数据）。

        任务被取消时流在下一块数据前截停（llm 层负责），已收到的正文拼成部分
        assistant 消息返回并标记 interrupted——残缺的工具调用不回传（无法解析），
        完整正文得以保留。
        重试预算用尽（读完超时 / 对端掐断连接）时正文同样已实时上屏，故异常也
        要携带已收到的部分上抛（`StreamInterrupted`），由恢复层写进会话历史。
        `r.stream_done()` 放 finally——否则 TUI 的正文块永不收口，下一轮的
        增量会直接追加进上一轮那个还没闭合的块里（两轮内容黏成一团）。
        思考内容（reasoning_content，仅部分模型返回）以灰色实时展示，
        但不写入会话——多数 OpenAI 兼容服务不接受它被回传。

        **同步客户端 + 异步抽取**：`chat_stream` 是同步生成器，经
        `drain_sync_stream` 逐块抽到事件循环上（阻塞读下放线程），因此本协程
        在等网络时不会冻住循环。取消语义不变：`llm/client.py` 登记的关流回调
        解除阻塞中的读，抽取层看到 `token.cancelled` 后停止产出。
        """
        msg: dict = {}
        usage = None
        parts: list[str] = []
        schemas = visible_schemas() if use_tools else None
        kwargs = self._turn_kwargs()
        token = current_token()

        def emit(kind: str, payload) -> None:
            """把流式增量发成事件（订阅者故障由 `_emit` 统一包成 SubscriberError）。

            订阅者里的 bug（如某个前端对未知事件抛 `AttributeError`）此前会被下面
            的流异常处理捕获，于是每一轮都报"输出中断"——把 UI 故障描述成网络故障，
            排查方向直接跑偏。`_emit` 把这类故障包成不参与流重试语义的独立异常。
            """
            self._emit(MessageUpdate(message=msg, delta=payload, kind=kind))

        try:
            source = self.llm.chat_stream(self.session.messages, tools=schemas, **kwargs)
            async for kind, payload in drain_sync_stream(source, token):
                if kind == "message":
                    msg = payload
                elif kind == "usage":
                    usage = payload
                else:
                    if kind == "content":
                        parts.append(payload)
                    emit(kind, payload)
        except (StreamInterrupted, SubscriberError):
            raise  # 流中断已成型；订阅者故障不参与流重试/收尾语义
        except Exception as e:
            if token is not None and token.cancelled:
                raise  # 取消引发的读错误：不是流故障，按取消语义上抛
            raise StreamInterrupted(e, "".join(parts)) from e
        finally:
            # 幂等；异常路径也必须收口，否则下一轮流会黏进本块
            self._emit(MessageEnd(message=msg))
        if token is not None and token.cancelled and not msg:
            msg = {"role": "assistant", "content": "".join(parts)}
            return msg, usage, True
        if msg and parts:
            msg = {**msg, "content": "".join(parts)}  # 多尝试累积的正文以 parts 为准
        return msg, usage, False

    def _preflight_safe(self, tc: dict) -> tuple[ToolPlan, bool]:
        """预检的兜底包装：预检自身的意外异常转为该工具的错误结果，不外抛。

        旧实现里权限确认与执行同处一个 try 块，交互层异常（如终端不可用）
        只体现为该工具的错误结果、循环继续；这里保持同样的容错边界。
        """
        try:
            return self._preflight(tc)
        except Exception as e:  # noqa: BLE001
            name = tc["function"]["name"]
            text = f"错误: {type(e).__name__}: {e}"
            return ToolPlan(tc, name, lambda: text, rendered=False), False

    def _preflight(self, tc: dict) -> tuple[ToolPlan, bool]:
        """预检一个工具调用：解析参数、渲染摘要、路径预检与权限检查。

        全部在主线程按接收顺序进行——交互确认与权限规则的会话级写入
        不容并发。返回 (计划, 是否权限被拒)；被拒时计划的 run() 返回
        DENIED_RESULT，由 _execute_batch 终止任务。解析失败、路径解析
        失败等错误不视为被拒，同样封装成计划（run() 直接返回错误文本），
        保证结果收集路径统一。
        """
        name = tc["function"]["name"]
        args_json = tc["function"]["arguments"]

        try:
            args = json.loads(args_json or "{}")
        except json.JSONDecodeError as e:
            text = f"错误: JSONDecodeError: {e}"
            self._emit(ToolStart(
                tool_call_id=tc.get("id"), name=name, line=f"[Tool] {name}({args_json[:80]})"
            ))
            return ToolPlan(tc, name, lambda: text), False

        # 摘要必须是单行：自定义 describe 会把命令原文等参数直接拼进来，多行命令
        # （heredoc 等）的换行会在终端撑成多行。压平放在截断之前，保证 80 字符
        # 上限全部花在可见内容上。
        line = " ".join(self._describe(name, args).split())
        display = DISPLAY.get(name, "inline")
        # 既有计划的更新不上屏工具行：todo_write 每完成一步就更新一次，若每次都
        # 生成工具块会往对话区反复打印进度；更新只静默刷新侧边栏，新建清单才展示
        todo_update = name == "todo_write" and has_active()
        if not todo_update:
            self._emit(ToolStart(
                tool_call_id=tc.get("id"),
                name=name,
                line=line[:MAX_SUMMARY_LEN] + ("..." if len(line) > MAX_SUMMARY_LEN else ""),
                display=display,
            ))
        denied_plan = ToolPlan(tc, name, lambda: DENIED_RESULT, rendered=not todo_update)

        # 多路径工具（如 apply_patch）：从参数提取目标路径，逐路径预检 + 聚合权限检查
        extractor = PATHS_EXTRACTORS.get(name)
        if extractor is not None:
            try:
                paths = [str(p) for p in extractor(args)]
            except Exception as e:  # noqa: BLE001
                text = f"错误: 无法解析目标路径: {type(e).__name__}: {e}"
                return ToolPlan(tc, name, lambda: text), False

            widened = []
            for raw in paths:
                pre = self._preflight_path(raw)
                if pre == "deny":
                    return denied_plan, True
                if isinstance(pre, Path):
                    widened.append(pre)
            if not self.permission.check_paths(name, paths, content=line):
                return denied_plan, True

            snapshot = _diff_preview(name, args)  # 执行前快照（apply_patch 暂无 preview，为空串）
            if snapshot:
                self._emit(ToolPreview(tool_call_id=tc.get("id"), detail=snapshot))

            # "仅本次"越界放行经 widen_roots 全局生效，并行窗口内其他线程会
            # 意外获得该目录的访问权——带临时放行目录的计划强制串行
            return ToolPlan(tc, name,
                             self._make_runner(name, args, widened), serial=bool(widened)), False

        if name == "todo_write":
            if not self.permission.check(name, args, content=line):
                return denied_plan, True
            created = not todo_update  # 此前无未完结步骤 → 本次新建清单

            def run_todo() -> str:
                try:
                    result = str(FUNCTIONS[name](**args))
                except Exception as e:  # noqa: BLE001
                    result = f"错误: {type(e).__name__}: {e}"
                    self._emit(ToolEnd(tool_call_id=tc.get("id"), result=result))
                    return result
                self._emit(PlanUpdate(
                    summary=summary(), rendered=render_current(color=True),
                    titles=render_titles(color=True),
                    created=created, tool_call_id=tc.get("id"),
                ))
                return result

            # todo_write 改写会话级状态机并渲染计划，必须独占主线程
            return ToolPlan(tc, name, run_todo, serial=True, display_result=False,
                             rendered=not todo_update), False

        # 单路径/无路径工具：变更预览（diff）在路径预检 / 权限确认 / 执行之前
        # 推送到工具调用块：审核时改动内容已经可见，权限框保持纯净
        snapshot = _diff_preview(name, args)
        if snapshot:
            self._emit(ToolPreview(tool_call_id=tc.get("id"), detail=snapshot))

        # 路径预检：目标在授权目录之外时先请用户确认（目录信任 → 操作权限，两道关卡有序）
        preflight = self._preflight_outside_path(args, read_only=name in READ_ONLY_TOOLS)
        if preflight == "deny":
            return denied_plan, True
        if not self.permission.check(name, args, content=line):
            return denied_plan, True

        # 注册为 serial 的工具（shell / 写文件 / 交互确认等）与需要临时放行的
        # 调用在主线程串行执行，其余进并发波次
        widen = [preflight] if isinstance(preflight, Path) else []
        return ToolPlan(tc, name,
                         self._make_runner(name, args, widen),
                         serial=bool(SERIAL.get(name)) or bool(widen)), False

    @staticmethod
    def _make_runner(name: str, args: dict, widen: list[Path]) -> Callable[[], object]:
        """生成工具执行闭包：临时放行（widen）+ 调用 + 异常转结果文本。

        闭包在预检完成后于主线程或线程池 worker 中调用；工具执行的任何
        失败都只作为结果回传给模型，不中断循环。widen 为空时 widen_roots
        直接放行，等价无放行调用。

        **两种签名都收**（方案 §7(2)）：工具函数返回 awaitable 时（`async def`
        工具）原样交给 `BatchScheduler._run_plan` 在事件循环上 await——这里**不能**
        先 `str()`，否则协程会被转成 `"<coroutine object …>"` 并且永不执行。

        异步工具与「临时放行」的边界：widen 随闭包返回即失效，而异步工具的协程体
        在之后才跑，所以**异步工具不得依赖越界放行**（它们本就是只读/网络类，
        走不到那条路径；真要支持得把 widen 移进 `_run_plan`）。
        """
        def run() -> object:
            try:
                with config.widen_roots(widen):
                    value = FUNCTIONS[name](**args)
                    if inspect.isawaitable(value):
                        return value  # 交给 _run_plan 在循环上 await
                    return str(value)
            except Exception as e:  # noqa: BLE001
                return f"错误: {type(e).__name__}: {e}"

        return run

    async def _execute_batch(self, tool_calls: list[dict],
                             assistant_message: dict | None = None) -> str | None:
        """执行同一条 assistant 消息里的全部工具调用（流式调度，见 BatchScheduler）。

        逐项预检、边预检边执行：可并行计划进波次缓冲，串行计划作为顺序屏障
        （先冲刷前面的并行波次再执行），因此串行工具在后续工具的权限确认之前
        就已执行完。结果严格按提交顺序回传；被拒 / 中断时未执行的项补占位结果，
        保证每个 tool_call_id 成对。返回 None / "denied" / "interrupted" /
        "terminate"（整批钩子都投票提前结束）。
        """
        limit = max(1, int(config.MAX_TOOL_CONCURRENCY))
        scheduler = BatchScheduler(self, current_token(), limit, assistant_message)
        status = await scheduler.run(tool_calls)
        self._last_batch_results = scheduler.collected
        if status is None and scheduler.terminate_all:
            return "terminate"
        return status

    def _interrupt_batch(self, pending_plans: list[ToolPlan],
                         remaining_tcs: list[dict]) -> str:
        """中断收尾：已预检未执行 / 尚未预检的 tool_calls 一律补占位结果。

        会话不变量：每条 assistant 消息的每个 tool_call_id 都必须有配对
        的 tool 结果，否则下一次请求会被服务商拒绝。返回 "interrupted"。
        """
        for p in pending_plans:
            self._placeholder(INTERRUPTED_RESULT, p.tc.get("id"), rendered=p.rendered)
        for tc in remaining_tcs:
            self._placeholder(INTERRUPTED_RESULT, tc.get("id"))
        return "interrupted"

    def _placeholder(self, content: str, tool_call_id, rendered: bool = False) -> None:
        """为未执行的 tool_call 补占位结果（拒绝 / 中断的会话修复共用）。

        `rendered` 表示该调用是否已经开出界面上的工具块（预检时发过 ToolStart）：
        开过的要补一条结果收尾，否则 TUI 停在 pending 态；没开过的不该凭空多出
        一个结果块。"""
        self.session.messages.append(
            {"role": "tool", "content": content, "tool_call_id": tool_call_id}
        )
        if rendered:
            self._emit(ToolEnd(tool_call_id=tool_call_id, result=content))

    def _collect(self, plan: ToolPlan, result: str) -> str:
        """收集一个执行完的计划：截断、终端展示、按序追加进会话。

        只在主线程调用（渲染不进 worker）；todo_write 的计划展示已由 run()
        自行渲染，display_result=False 时跳过 tool_result 展示与截断。
        """
        if plan.display_result:
            result = self._finish(result, plan.tc.get("id"), plan.name)
        self.session.messages.append(
            {"role": "tool", "content": result, "tool_call_id": plan.tc.get("id")}
        )
        return result

    def _finish(self, result: str, tool_call_id: str | None = None,
                name: str | None = None) -> str:
        """回传前截断超长输出；终端展示交给 renderer（summary 模式只有
        执行前那行短摘要，detail 模式追加结果内容）。失败信息无论何种模式
        都原样展示——失败的细节比格式化摘要更重要。

        name 非空且属写/编辑类时，TUI 中调用详情默认展开（diff 已在
        tool_preview 阶段推入工具块）。

        第二个参数由旧的渲染后端自增整型 id 改为 `tool_call_id`（字符串）：事件
        模型里它就是配对的键，旧式整型 id 的分配已收进 `RendererBridge`。"""
        result = truncate_output(result, config.MAX_TOOL_OUTPUT)
        self._emit(ToolEnd(tool_call_id=tool_call_id, result=result,
                           expand=name in DEFAULT_EXPAND_TOOLS))
        return result

    @staticmethod
    def _describe(name: str, args) -> str:
        """工具调用的一行短摘要；未注册 describe 的工具回退为 [Tool] 名字(参数) 格式。"""
        describe = DESCRIBERS.get(name)
        if describe is not None and isinstance(args, dict):
            return describe(args)
        return f"[Tool] {name}({json.dumps(args, ensure_ascii=False)[:80]})"

    def _preflight_path(self, raw: str, read_only: bool = False) -> Path | str | None:
        """检查单个路径是否落在授权目录之外；之外时先交互确认。

        返回 "deny"（用户拒绝本次访问）、Path（"仅本次"，执行时需临时放行该信任根）、
        None（路径在授权范围内，或用户已选"本会话总是"——信任根已入库）。
        只读工具（READ_ONLY_TOOLS）的目标落在技能目录只读白名单内时直接放行，
        避免读技能引用文件反复弹越界确认；写路径不受影响。
        与工具内部的越界检查互为备份：预检管交互体验，工具侧管强制执行。
        """
        target = (Path(config.WORKSPACE_ROOT) / str(raw)).resolve()
        if any(target.is_relative_to(r) for r in config.allowed_roots()):
            return None
        if read_only and any(target.is_relative_to(r) for r in config.skill_roots()):
            return None
        action, root = self.permission.ask_outside_access(str(raw), target)
        if action == "deny":
            return "deny"
        return root if action == "once" else None

    def _preflight_outside_path(self, args: dict, read_only: bool = False) -> Path | str | None:
        """单路径工具（path 参数）的越界预检入口。"""
        raw = args.get("path")
        return self._preflight_path(raw, read_only=read_only) if raw else None
