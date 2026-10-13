"""询问端口：`asked → replied` 的请求-应答（对齐 opencode 的 `permission.asked/replied`）。

**为什么要事件 + 应答**：提问是唯一"必须拿到答案才能继续"的交互。用事件表达
「在等什么」（`PromptStarted`），用应答表达「等到了什么」（`PromptFinished`），
消费者（终端标题、面板、将来的远程客户端）据此判定状态；而**答案**由当前会话的
前端给出。

**一个口、两种等法**：

- `await port.ask(request)`：循环线程上的调用方用（权限预检、`ask_user` 工具）；
- `port.ask_sync(request)`：**同步上下文**用（命令层没有 async 分发、启动期装载
  技能信任确认）——它借运行中的循环把协程投过去（`run_coroutine_threadsafe`），
  或在自己线程里新起一个循环。

两条路走的是**同一个** `ask()`、同一批事件、同一套 fail-closed 兜底，所以"谁在等、
等到了什么"在任何调用路径上都一致。

**两种作答路径**（进程内 vs 跨进程）：

- 进程内（REPL / TUI）：`ask()` 发布 `PromptStarted` 后直接 await 当前前端——
  答案出自同一个 await，不需要登记表；
- 跨进程（`smith serve` + 桌面端）：serve 给端口挂一个 `RemoteAsker`
 （`set_remote_asker`），`ask()` 改为登记 `PendingRegistry` 等 `reply()` 唤醒；
  `PromptStarted(id)` 经 SSE 下行，桌面端作答后调 `reply(prompt_id, answer)`。
  两种路径的事件（`PromptStarted/Finished` 配对）与 fail-closed 语义完全一致。

**前端只实现一个方法**：`async def ask(AskRequest) -> AskAnswer`。请求里的
`payload` 是各 kind 的专属数据（权限确认的 `valid`/`hint`/`descriptions`、提问表单
的 `questions`…），**怎么呈现由前端决定**——所以"编号选择还是方向键"这类差异不再
散落在调用点，也不会把终端细节泄漏到权限引擎里。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from .bus import publish
from .catalog import PromptFinished, PromptKind, PromptOutcome, PromptStarted
from .envelope import new_id
from .pending import PendingRegistry, make_prompt

#: 各 kind 的 payload 形状（前端按 kind 解释，不做跨 kind 解析）：
#:
#: - `permission` / `outside_access` / `skill_trust` / `confirm`
#:   `{"hint": str, "descriptions": {key: 说明}, "content": str | None}`
#:   —— 选项键在 `AskRequest.options`（如 ("y", "n", "a")）；
#: - `ask_user`：`{"questions": [{"question", "options", "descriptions", "multiple"}]}`
#:   —— 答案走 `AskAnswer.values`（与 questions 对齐）。
PAYLOAD_SHAPES = {
    "permission": "hint/descriptions/content",
    "outside_access": "hint/descriptions/content",
    "skill_trust": "hint/descriptions/content",
    "confirm": "hint/descriptions/content",
    "ask_user": "questions",
}


@dataclass(frozen=True)
class AskRequest:
    """一次提问（数据化：呈现所需的一切都在这里，不含"怎么问"）。"""

    kind: PromptKind
    title: str
    detail: tuple[str, ...] = ()
    options: tuple[str, ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AskAnswer:
    """一次作答。

    `outcome != "answered"` 时调用方一律按 fail-closed 收尾（拒绝 / 取消），
    与改造前的返回值语义逐字一致：权限确认 → 拒绝，提问 → 取消。
    """

    outcome: PromptOutcome = "answered"
    #: 单选结果（选项键或自定义文本）；表单类提问不看它。
    value: str = ""
    #: 多题答案（与 `AskRequest.payload["questions"]` 对齐）。
    values: tuple[str, ...] = ()

    @property
    def answered(self) -> bool:
        return self.outcome == "answered"


#: 用户没作答时的兜底（界面收尾 / 会话取消）：一律当作拒绝/取消。
FAIL_CLOSED = AskAnswer(outcome="cancelled")


class AskPort:
    """每会话一个的询问端口：发布 asked 事件 → 等前端作答。"""

    def __init__(self, session_id: str = "") -> None:
        self.session_id = session_id
        self._loop: asyncio.AbstractEventLoop | None = None
        self._in_flight: dict[str, asyncio.Task] = {}
        self._lock = threading.Lock()
        self._closing = False  # 见 cancel_in_flight：收尾期间的取消按 fail-closed 收口
        #: 跨进程作答路径（`smith serve` 挂 `RemoteAsker` 后生效）：
        #: `ask()` 不再直调前端，而是登记 `pending` 等 `reply()` 唤醒。
        self._remote = None
        self.pending = PendingRegistry(session_id)

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """记下本会话的循环：`ask_sync` 靠它把协程投回去。"""
        self._loop = loop

    def set_remote_asker(self, remote=None) -> None:
        """挂/摘跨进程作答路径：非 None 时 `ask()` 走登记表等 `reply()`。

        同一端口同时只能有一种作答路径——挂上 remote 即停用进程内直调，
        摘掉即恢复（serve 关闭会话时摘）。
        """
        self._remote = remote

    @property
    def remote(self):
        """当前是否走跨进程作答（serve 模式）。"""
        return self._remote

    def reply(self, prompt_id: str, answer: AskAnswer) -> bool:
        """远端作答（serve 的 `POST reply` 调）：唤醒等该 id 的 `ask()`。

        迟到/重复作答返回 False（调用方忽略）；`answer` 非 `AskAnswer`
        时按 fail-closed 收成取消——跨进程边界不信任输入形状。
        """
        if not isinstance(answer, AskAnswer):
            answer = FAIL_CLOSED
        return self.pending.reply(prompt_id, answer)

    def pending_snapshot(self):
        """挂起快照（`GET pending` / SSE 断线重连补发）。"""
        return self.pending.snapshot()

    # ---------- 询问（循环线程） ----------

    async def ask(self, request: AskRequest) -> AskAnswer:
        """发布 asked → 交给当前前端作答 → 发布 replied。

        进程内（默认）：直接 await 当前前端，答案出自同一个 await；
        跨进程（挂了 remote）：登记 `pending` 等 `reply()` 唤醒，`PromptStarted(id)`
        经 SSE 下行、桌面端作答后调 `reply(prompt_id, answer)` 上行。
        两种路径的事件配对与 fail-closed 语义一致。
        """
        self._closing = False  # 新提问开始：收尾标记针对"当前这批"
        prompt_id = new_id()
        publish(PromptStarted(
            id=prompt_id, kind=request.kind, title=request.title,
            detail=request.detail, options=request.options, payload=request.payload,
        ))
        if self._remote is None:
            return await self._ask_local(prompt_id, request)
        return await self._ask_remote(prompt_id, request)

    async def _ask_local(self, prompt_id: str, request: AskRequest) -> AskAnswer:
        """进程内作答：await 当前前端（原 `ask()` 本体，行为逐字不变）。"""
        from .. import frontend  # 局部导入：避免 frontend → event 的包初始化环

        task = asyncio.ensure_future(frontend.current().ask(request))
        with self._lock:
            self._in_flight[prompt_id] = task
        try:
            answer = await task
        except asyncio.CancelledError:
            if not self._closing:
                raise  # 外层取消（Esc / 进程退出）：照常传播，由调用方收尾
            # 会话/界面收尾主动取消：给一个 fail-closed 的答案放行等待方，
            # 让它把这一轮按"用户拒绝/取消"正常收口（历史保持合法），而不是炸给上层
            answer = FAIL_CLOSED
        except Exception as exc:  # 前端故障也要收口，否则消费者永远停在"在等"
            publish(PromptFinished(id=prompt_id, kind=request.kind, outcome="error",
                                   error=str(exc)))
            raise
        finally:
            with self._lock:
                self._in_flight.pop(prompt_id, None)
        return self._finish(prompt_id, request, answer)

    async def _ask_remote(self, prompt_id: str, request: AskRequest) -> AskAnswer:
        """跨进程作答：登记 `pending` 等远端 `reply()`（serve 模式）。

        等待的 future 与 `cancel_in_flight()` 的取消是同一套：收尾取消时
        future 被取消，走与本地路径相同的 fail-closed 收口。
        """
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending.register(make_prompt(self.session_id, prompt_id, request), future)
        with self._lock:
            self._in_flight[prompt_id] = future
        try:
            answer = await future
        except asyncio.CancelledError:
            if not self._closing:
                raise
            answer = FAIL_CLOSED
        except Exception as exc:
            publish(PromptFinished(id=prompt_id, kind=request.kind, outcome="error",
                                   error=str(exc)))
            raise
        finally:
            with self._lock:
                self._in_flight.pop(prompt_id, None)
            self.pending.settle(prompt_id)
        if not isinstance(answer, AskAnswer):
            answer = FAIL_CLOSED
        return self._finish(prompt_id, request, answer)

    @staticmethod
    def _finish(prompt_id: str, request: AskRequest, answer: AskAnswer) -> AskAnswer:
        """收口：发布 `PromptFinished`（与 `PromptStarted` 同 id 配对）并返回答案。"""
        publish(PromptFinished(
            id=prompt_id, kind=request.kind, outcome=answer.outcome,
            value=answer.value or None,
            values=tuple(answer.values) if answer.values else (),
        ))
        return answer

    async def ask_user_questions(self, questions: list[dict]) -> AskAnswer:
        """`ask_user` 的表单入口：把归一化后的问题列表交给前端一次问完。"""
        title = questions[0].get("question", "") if questions else ""
        return await self.ask(AskRequest(
            kind="ask_user", title=title, payload={"questions": tuple(questions)},
        ))

    # ---------- 询问（同步上下文） ----------

    def ask_sync(self, request: AskRequest) -> AskAnswer:
        """同步上下文里提问（命令层 / 启动期装载）。

        借运行中的循环把协程投回去：TUI 的面板必须等在**它自己的**循环上
        （`push_screen_wait`），所以在 worker 线程里跑命令时不能自己新起循环。
        没有运行中的循环（启动期、纯单测）时就在本线程跑完。
        跨进程（挂了 remote 且循环在跑）同样走投递——等待的是远端 reply，
        `future.result()` 在此阻塞直到作答或取消，语义与进程内一致。
        """
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                # 在本循环线程上同步等自己 = 自锁；正常路径不该走到这里
                raise RuntimeError("ask_sync 不能在事件循环线程上调用（会自锁）")
            future = asyncio.run_coroutine_threadsafe(self.ask(request), loop)
            return future.result()
        return asyncio.run(self.ask(request))

    # ---------- 收尾 ----------

    def cancel_in_flight(self) -> int:
        """取消本会话所有挂起询问（界面收尾 / `/new` / 进程退出），返回条数。

        取消会让 `ask()` 以 fail-closed 兜底收口：等待方被放行、事件成对收口，
        不会留下"永远在等"的提问——这正是退出卡死那个故障的根因。
        跨进程路径同样在此取消：远端收到 `session.prompt.cancelled`
       （serve 由调用方扇出，见 `serve.py`）后关闭弹窗；重复/迟到的 reply
        由 `PendingRegistry.reply` 的幂等忽略。
        """
        with self._lock:
            self._closing = True
            tasks = list(self._in_flight.values())
        for task in tasks:
            task.cancel()
        return len(tasks)


# --------------------------------------------------------------------------
# 当前上下文的询问端口（与事件总线同形：并发会话各拿各的）
# --------------------------------------------------------------------------

_current: ContextVar[AskPort | None] = ContextVar("smithcode_ask_port", default=None)


def current() -> AskPort | None:
    """当前会话的询问端口；不在任何会话上下文里时为 None。"""
    return _current.get()


def require() -> AskPort:
    """取当前端口；没有则报错。

    调用方（权限引擎 / ask_user）都在会话运行路径上，没有端口意味着接线出了
    问题——静默退化会让"没人可问"看起来像"用户拒绝"，排查方向就错了。
    """
    port = _current.get()
    if port is None:
        raise RuntimeError("没有活动的询问端口（AskPort）：调用点应在会话运行路径上")
    return port


def activate(port: AskPort | None) -> Token:
    """挂载询问端口，返回复位令牌（与 `event.activate` 配对使用）。"""
    return _current.set(port)


def reset(token: Token) -> None:
    """按令牌复位（与 `activate` 配对；测试隔离也用它）。"""
    _current.reset(token)


def has_port() -> bool:
    """当前上下文有没有询问端口（调用点据此决定走事件还是退化路径）。"""
    return _current.get() is not None


def has_remote() -> bool:
    """当前上下文是否挂着**跨进程作答者**（`smith serve` + 桌面端）。

    挂了 = `ask()` 走登记表等 `POST reply`，**不读本进程 stdin**——sidecar 的
    stdin 是 /dev/null，按 `isatty()` 判会把桌面端的权限确认全部误杀。调用点
    （权限引擎 / ask_user）用它与 `confirmations_available()`（stdin 判定）合并成
    "有没有人可以问"，两边都没有才 fail-closed。
    """
    port = _current.get()
    return port is not None and port.remote is not None
