"""工具批次的流式调度：一次 assistant 消息内多个工具调用的顺序与并发编排。

自 `agent/agent.py` 拆出：「工具怎么跑」与「循环怎么转」是两件事，混在一个
一千三百行的文件里改动容易互相牵连。依赖是**单向**的——本模块只调用 Agent
实例上的 `_preflight_safe` / `_collect` / `_placeholder` / `_interrupt_batch`，
不反过来 import 循环实现，因此没有导入环（`Agent` 类型注解走 `TYPE_CHECKING`）。
"""

from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING

from .events import Notice
from .signal import AbortSignal

if TYPE_CHECKING:
    from .agent import Agent

# 权限被拒时的统一工具结果文本（回传模型 + 终端展示共用）
DENIED_RESULT = "用户拒绝了此操作"
# 权限被拒后为同条 assistant 消息中剩余 tool_calls 补的占位结果（防悬空 tool_call_id）
SKIPPED_RESULT = "（未执行：权限请求被拒绝，任务已中止）"
# 用户中断后为未执行项补的占位结果（同上，防悬空 tool_call_id）
INTERRUPTED_RESULT = "（未执行：用户中断了任务）"
# 被 before_tool_call 钩子拦下、且钩子没给理由时的占位结果
HOOK_BLOCKED_RESULT = "（未执行：被 before_tool_call 钩子阻止）"


class ToolPlan:
    """单个工具调用的执行计划：预检阶段的产物。

    run 闭包封装全部执行细节（临时放行 widen、异常转结果文本），预检时
    权限与路径检查已完成，执行阶段可直接调用。
    被拒的计划 run() 返回 DENIED_RESULT；display_result=False 的计划
    （todo_write）由 run() 自行渲染，收集时跳过 tool_result 展示与截断。
    """

    __slots__ = ("display_result", "name", "rendered", "run", "serial", "tc")

    def __init__(self, tc: dict, name: str, run, serial: bool = False,
                 display_result: bool = True, rendered: bool = True):
        self.tc = tc
        self.name = name
        self.run = run
        self.serial = serial
        self.display_result = display_result
        # 该调用是否已经在界面上开出工具块（预检时发了 ToolStart）。收尾补占位
        # 结果时用它决定要不要发 ToolEnd：没开过块的（既有计划的静默更新、预检
        # 兜底）不该凭空多出一个结果块。它替代了旧的 `tool_id is not None`。
        self.rendered = rendered


class BatchScheduler:
    """一次 assistant 消息内工具批次的流式调度器（只负责「顺序 + 并发」编排）。

    与旧「两阶段（预检完全部再执行）」的区别：**边预检边调度**——预检一个就
    决定其去向：可并行计划进入波次缓冲，串行计划作为顺序屏障先冲刷波次、再
    就地执行。效果：串行工具在「后续工具的权限确认」之前就已执行完，确认框
    与执行一一对应。

    不变量（改动此处务必对照 tests/test_agent_parallel.py）：
    - I1 结果消息 / tool_result 事件严格按 tool_call 提交顺序；
    - I2 每个 tool_call_id 恰有一条结果（执行 / SKIPPED / INTERRUPTED 三路完备）；
    - I3 串行工具是顺序屏障：执行前先冲刷前面的并行波次，保证副作用可见性；
    - I4 波次缓冲期零副作用——并行计划只在屏障/结束时才提交执行，因此
      「首个串行工具执行前」的整段仍可原子取消；并行工具仅只读/网络类；
    - I5 渲染只在本协程所在线程发生（`to_thread` 里的 worker 只算结果字符串，
      经 `_collect` 收集）；
    - I7 limit==1 或波次仅 1 个计划时不并发（退化为纯串行）。

    **并发实现**：Python 3.10 没有 `asyncio.TaskGroup`（3.11+，项目红线是
    3.10），并发波次用 `asyncio.gather(..., return_exceptions=True)`——它按
    入参顺序返回结果，天然满足 I1；`return_exceptions` 把异常也变成结果，
    维持 I2（一个计划炸了不该让整批失去结果消息）。阻塞型工作（工具执行、
    权限确认）统一经 `asyncio.to_thread` 下放，事件循环不被冻住。
    """

    def __init__(self, agent: Agent, token: AbortSignal | None, limit: int,
                 assistant_message: dict | None = None):
        self._agent = agent
        self._token = token
        self._limit = limit
        self._assistant_message = assistant_message or {}
        self._wave: list[ToolPlan] = []  # 已预检、待并行执行的计划（保持提交顺序）
        self.collected: list[str] = []  # 已完成计划的结果文本（提交序），供 TurnContext
        self._terminate_votes: list[bool] = []  # 钩子的提前结束投票（见 hooks.terminate）

    @property
    def terminate_all(self) -> bool:
        """整批结果都投票提前结束（pi 的 `shouldTerminateToolBatch` 语义）。"""
        return bool(self._terminate_votes) and all(self._terminate_votes)

    async def run(self, tool_calls: list[dict]) -> str | None:
        """按接收顺序「预检一个 → 调度一个」，返回 None / "denied" / "interrupted"。"""
        i, n = 0, len(tool_calls)
        while i < n:
            if self._cancelled():  # 循环顶部中断：当前项尚未预检
                return self._abort_interrupted(self._wave, tool_calls[i:])
            # 钩子决策点（循环线程，可 await）：在预检与权限之前
            blocked = await self._agent.before_tool_call(self._assistant_message, tool_calls[i])
            if blocked is not None:
                self._terminate_votes.append(bool(blocked.terminate))
                if blocked.block:
                    # 被钩子拦下：只影响本次调用，其余照常（对齐 pi 的 block 语义）
                    self._collect_blocked(tool_calls[i], blocked.reason)
                    i += 1
                    continue
            # 预检含权限确认（读 stdin / 等弹窗，纯阻塞）→ 下放线程
            tool_plan, denied = await asyncio.to_thread(
                self._agent._preflight_safe, tool_calls[i]
            )
            if self._cancelled():  # 预检（含权限确认）期间中断：当前项也不执行
                return self._abort_interrupted(self._wave + [tool_plan], tool_calls[i + 1:])
            if denied:
                return await self._abort_denied(tool_plan, tool_calls[i + 1:])
            if tool_plan.serial or self._limit == 1:  # 顺序屏障 / 退化纯串行
                await self._flush()  # 先收前面的并行波次，再执行串行项
                await self._finish_plan(tool_plan)
            else:
                self._wave.append(tool_plan)  # 缓冲，屏障 / 结束时才跑
            i += 1
        await self._flush()
        return None

    def _collect_blocked(self, tool_call: dict, reason: str) -> None:
        """钩子拦下的调用：不预检、不执行，直接补一条结果（会话仍需成对）。"""
        plan = ToolPlan(tool_call, tool_call.get("function", {}).get("name", ""),
                        lambda: reason or HOOK_BLOCKED_RESULT, rendered=False)
        result = plan.run()
        self._agent.session.messages.append(
            {"role": "tool", "content": result, "tool_call_id": tool_call.get("id")}
        )
        self.collected.append(result)

    async def _finish_plan(self, plan: ToolPlan) -> None:
        """执行一个计划 → after 钩子（可替换文本 / 投票结束）→ 收集。"""
        result = await self._run_plan(plan)
        outcome = await self._agent.after_tool_call(self._assistant_message, plan, result)
        if outcome is not None:
            if outcome.content is not None:
                result = str(outcome.content)
            self._terminate_votes.append(bool(outcome.terminate))
        self.collected.append(self._agent._collect(plan, result))

    async def _flush(self) -> None:
        """冲刷波次：按提交顺序执行并收集（I1）；长度 1 或 limit==1 走单条路径（I7）。

        并发波次的 after 钩子与结果收集同处一步（收齐后按提交序逐个调用）——
        钩子只做文本替换与投票，与「谁先跑完」无关，放在这里能保住 I1 的确定性。
        """
        wave, self._wave = self._wave, []
        if not wave:
            return
        if len(wave) == 1 or self._limit == 1:
            for plan in wave:
                await self._finish_plan(plan)
            return
        # I4：到这一步才真正提交并发；此前（预检阶段）零副作用
        results = await asyncio.gather(
            *(self._run_plan(plan) for plan in wave), return_exceptions=True
        )
        for plan, outcome in zip(wave, results):  # I1：按提交序收集，不按完成序
            result = as_result_text(outcome)
            decided = await self._agent.after_tool_call(self._assistant_message, plan, result)
            if decided is not None:
                if decided.content is not None:
                    result = str(decided.content)
                self._terminate_votes.append(bool(decided.terminate))
            self.collected.append(self._agent._collect(plan, result))

    async def _run_plan(self, plan: ToolPlan) -> str:
        """执行一个计划：同步工具下放线程，**异步工具直接在循环上 await**。

        两种签名都收（对齐方案 §7(2)）：同步工具是阻塞 IO（子进程 / 文件 / 网络）
        经 `to_thread` 执行，不冻住事件循环；工具函数若返回 awaitable（`async def`
        工具，如将来异步化的 MCP 工具），则在这里 await 它——不必为了一个新工具
        去改全部内置工具。

        **异步工具的约束**：协程里不得做同步阻塞调用（那会冻住整个循环，而同步
        工具没有这个问题，因为它被下放到了线程）。这条要写进工具的注册处。
        """
        try:
            value = await asyncio.to_thread(plan.run)
            if inspect.isawaitable(value):  # 异步工具：协程在循环上跑
                value = await value
            return str(value)
        except Exception as e:  # noqa: BLE001 闭包之外的意外也要变成结果（I2）
            return f"错误: {type(e).__name__}: {e}"

    async def _abort_denied(self, denied_plan: ToolPlan, remaining_tcs: list[dict]) -> str:
        """权限被拒收尾：未执行的波次与被拒项之后的剩余项补 SKIPPED，
        被拒项收尾为 DENIED（其 run() 返回 DENIED_RESULT），返回 "denied"。"""
        for plan in self._wave:
            self._agent._placeholder(SKIPPED_RESULT, plan.tc.get("id"), rendered=plan.rendered)
        self._agent._collect(denied_plan, await self._run_plan(denied_plan))
        for tc in remaining_tcs:
            self._agent._placeholder(SKIPPED_RESULT, tc["id"])
        self._agent._emit(Notice("权限请求被拒绝，任务已停止", level="error"))
        return "denied"

    def _abort_interrupted(self, pending_plans: list[ToolPlan],
                           remaining_tcs: list[dict]) -> str:
        """中断收尾：未执行的计划与剩余 tool_calls 一律补占位（顺序天然对齐）。

        无 await：补占位只写会话与渲染，不需要让出事件循环。
        """
        return self._agent._interrupt_batch(pending_plans, remaining_tcs)

    def _cancelled(self) -> bool:
        return self._token is not None and self._token.cancelled


def as_result_text(outcome: object) -> str:
    """把 `gather` 的单个结果转成工具结果文本（正常为 str，异常转错误文本）。"""
    if isinstance(outcome, BaseException):
        return f"错误: {type(outcome).__name__}: {outcome}"
    return str(outcome)
