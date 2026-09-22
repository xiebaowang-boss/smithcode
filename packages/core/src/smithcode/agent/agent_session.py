"""会话所有权：把「属于一次会话的入口与外观」收进一个对象（计划 §9）。

现状：会话口径的状态散落在 `Agent`（消息、权限、MCP、取消令牌）、`goal.py` /
`plan.py` / `skills/state.py`（模块单例）、`title.py`（进程单例）里。本模块先把
**入口**收进来——任务驱动（`run` / `run_with_goal` / `prompt`）、运行中排队、
交互端口、会话边界动作（`new_session` / `resume` / `rename`）、状态快照
（`snapshot` / `restore` / `reset` / `state_parts`）。

**尚未实例化的部分（明确记录，不是遗漏）**：`goal` / `plan` / `skills` 仍是模块
单例，把它们改成实例要动 15 个以上读取点（`session.py`、`commands/*`、`tui/app.py`、
`tui/sidebar`），且必须与宿主的持有方式一起切换——半途而废会得到「同一个状态两份
真相」。因此 `snapshot` / `restore` / `reset` 目前是**转调单例的适配层**，
`t=state` 的三个 key（`goal` / `plan` / `skills`）保持不变，由
`tests/agent/test_agent_session.py` 的断言锁住。

**目标续跑驱动器为什么在这里**：它裁决的是「目标状态机要不要继续」，而目标属于
会话（`/new` 会清、恢复会读），所以由会话层驱动；单轮的编排（一次模型调用 + 它
的工具批）留在 `Agent`。外层多轮共用一个事件流与一对回合事件，由 `Agent` 的
`outer_turn_begin` / `outer_turn_end` 提供——这样「谁拥有事件流」只有一处判断。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import goal, plan
from ..event.catalog import Notice
from ..skills import state as skills_state
from .queues import QueueItem
from .result import RunResult

if TYPE_CHECKING:  # Agent 只在注解里用；运行时导入会与 agent/agent.py 成环
    from .agent import Agent

# `t=state` 记录里的三个 key；改动即破坏旧转录的恢复（见模块 docstring）
STATE_KEYS = ("goal", "plan", "skills")


def create(agent: Agent | None = None) -> AgentSession:
    """建一个会话对象；不传 agent 时新建一个。

    延迟导入 `Agent`：`agent/agent.py` 需要引用本模块（`session_owner`），
    `agent_session.py` 需要引用 `Agent`——顶层互相导入会成环。这与
    `skills/registry.py` 里「延迟导入避免成环」的既有做法一致。
    """
    if agent is None:
        from .agent import Agent

        agent = Agent()
    return AgentSession(agent)


class AgentSession:
    """一次会话的入口与外观。持有 Agent，不复制它的状态。"""

    def __init__(self, agent: Agent) -> None:
        self.agent = agent
        # 会话口径对象的引用（同一个对象，不是副本）：宿主从这里取会话相关的东西
        self.session = agent.session
        self.permission = agent.permission
        self.mcp = agent.mcp
        # 事件总线：本会话唯一的发布/订阅通道（前端订阅它；会话标识由总线注入）
        self.events = agent.events
        # 三个会话状态**实例**（目标 / 步骤清单 / 技能激活集合）：本会话的唯一真相。
        # goal.py / plan.py / skills/state.py 原先各自是模块级单例，恢复或切换会话时
        # 会串味（旧会话的目标出现在新会话里）。现在状态随会话对象存续。
        self.goal_state = goal.GoalState()
        self.plan_state = plan.PlanState()
        self.skills_state = skills_state.SkillsState()
        # 先继承默认实例的当前值：会话建立**之前**设的目标 / 清单 / 激活技能
        # （命令层直接调 goal.set / plan.restore 的路径）属于随后的这个会话，
        # 否则用户刚设的目标会凭空消失。
        # 接管**当前活动实例**的状态：会话建立前设的目标 / 清单 / 激活技能属于随后
        # 这个会话（接管默认实例或上一个会话实例都一样，进程里同时只有一个活动会话）
        self.goal_state.inherit(goal.active_state())
        self.plan_state.inherit(plan.active_state())
        self.skills_state.inherit(skills_state.active_state())
        self.bind_state()
        # 属性名带 `_queue` 后缀：与 Agent 同名方法（steer / follow_up）区分开，
        # 否则实例属性会遮蔽同名方法（`TypeError: 'MessageQueue' object is not callable`）。
        self.steering_queue = agent.steering_queue
        self.follow_up_queue = agent.follow_up_queue

    def bind_state(self) -> None:
        """让三个状态模块的既有函数指向本会话的实例。

        为什么需要"绑定"而不是把 15+ 个读取点（`session.py`、`commands/*`、
        `tui/app.py`、`tui/sidebar`）逐个改成 `session.goal_state.xxx()`：那些读取点
        分散在命令层与 UI 层，逐个改的收益只是写法更显式，风险却是漏改一处就得到
        "同一个状态两份真相"。绑定让所有既有调用点自动落到本会话的实例上。

        起会话时、以及每一轮任务开始时各绑一次（后者覆盖"另一个会话后建"的情况）。
        **局限**：同进程真并发跑两个会话会互相覆盖——与改造前的单例行为一致，
        TUI/REPL 都是一个进程一个会话。
        """
        goal.bind(self.goal_state)
        plan.bind(self.plan_state)
        skills_state.bind(self.skills_state)

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """启动期装载（模型目录 / 技能 / 项目指令 / MCP 后台连接）。"""
        self.agent.start()

    def close(self) -> None:
        """进程退出前收尾。"""
        self.agent.close()

    # ---------- 任务入口 ----------

    async def run(self, user_input: str) -> RunResult:
        """一轮任务（一次模型调用 + 其工具批）。"""
        self.bind_state()  # 本轮所属会话的状态是本轮唯一真相
        return await self.agent.run(user_input)

    async def continue_run(self) -> RunResult:
        """接着当前上下文再跑一轮（不注入新 user 消息；见 `Agent.continue_run`）。"""
        return await self.agent.continue_run()

    async def run_with_goal(self, user_input: str) -> RunResult:
        """执行一次任务，并在持久目标激活时自动续跑直到目标结束或触发刹车。

        /goal 的唯一续跑驱动器（REPL / TUI / 一次性任务共用）：无目标时与
        `run()` 完全等价。目标 active 时，每轮结束后按下述规则决定是否注入
        续跑提示词开启下一回合：

        - 上一轮正常结束且执行过工具（推进动作）→ 继续；
        - 续跑轮没有任何工具调用 → 暂停目标，防止空转；
        - 被中断 → 保留 active，停止循环（用户主动叫停）；
        - 被拒 / 迭代上限 → 暂停目标（继续只会重复失败）；
        - 配置了回合预算且回合数达到上限 → 标记预算用尽并注入收尾提示词跑最后一轮
          （默认预算不限，此时不触发）。
        """
        agent = self.agent
        goal_turn = goal.is_active()
        if goal_turn:
            goal.begin_turn()
        # 外层再包一对回合事件：多回合续跑期间计数不归零（每轮的 run() 内层各包
        # 一对），标题不因回合切换的一瞬空闲而闪烁
        owner = agent.outer_turn_begin()
        result = None
        try:
            result = await agent.run(user_input)
            agent.note_goal_run(result)
            while goal.is_active():
                if result.status != "ok":
                    if result.status in ("denied", "max_iterations") and goal.pause(
                        "上一次任务未正常结束，已暂停自动推进"
                    ):
                        agent._emit(Notice(
                            "[目标] 已暂停自动推进（/goal resume 可继续）", level="warning"
                        ))
                    break
                if not result.tools_used:
                    if goal.pause("本轮没有产生工具调用，已暂停自动推进以防空转"):
                        agent._emit(Notice(
                            "[目标] 已暂停：本轮没有产生工具调用（/goal resume 可继续）",
                            level="warning",
                        ))
                    break
                current = goal.current()
                if current is None:  # 目标在本轮结束时被清除
                    break
                if not current.unlimited and current.turns >= current.max_turns:
                    goal.budget_limited()
                    agent._emit(Notice(
                        f"[目标] 回合预算用尽（{current.max_turns} 回合），正在收尾…",
                        level="warning",
                    ))
                    result = await agent.run(current.wrapup_prompt())
                    agent.note_goal_run(result)
                    break
                goal.begin_turn()
                marker = goal.current()
                if marker is None:  # 目标在本轮结束时被清除
                    break
                agent._emit(Notice(f"[目标] 继续推进 · 第 {marker.turn_label()} 回合"))
                result = await agent.run(marker.continuation_prompt())
                agent.note_goal_run(result)
        except BaseException as exc:
            agent.outer_turn_end(owner, None, exc)
            raise
        agent.outer_turn_end(owner, result)
        return result

    async def prompt(self, text: str, *, images=None,
                     delivery: str = "auto") -> RunResult | None:
        """统一的任务入口：空闲即开跑，运行中按投递方式入队。"""
        return await self.agent.prompt(text, images=images, delivery=delivery)

    @property
    def busy(self) -> bool:
        return self.agent.busy

    def interrupt(self) -> None:
        self.agent.interrupt()

    # ---------- 运行中排队 ----------

    def enqueue(self, text: str, images=None, delivery: str = "auto") -> QueueItem:
        return self.agent.enqueue(text, images, delivery)

    def steer(self, text: str, images=None) -> QueueItem:
        return self.agent.steer(text, images)

    def follow_up(self, text: str, images=None) -> QueueItem:
        return self.agent.follow_up(text, images)

    def cancel_queued(self, item_id: str) -> bool:
        return self.agent.cancel_queued(item_id)

    def take_queued(self, item_id: str) -> QueueItem | None:
        """摘出某条排队项（UI 取回编辑用；内容由调用方处置）。"""
        return self.agent.take_queued(item_id)

    def clear_queue(self) -> tuple[list[str], list[str]]:
        return self.agent.clear_queue()

    def clear_steering_queue(self) -> list[str]:
        return self.agent.clear_steering_queue()

    def clear_follow_up_queue(self) -> list[str]:
        return self.agent.clear_follow_up_queue()

    def get_steering_messages(self) -> tuple[QueueItem, ...]:
        """投递点：按抽水策略取走待插话项（循环内部用，宿主一般不需要）。"""
        return self.agent.get_steering_messages()

    def get_follow_up_messages(self) -> tuple[QueueItem, ...]:
        """投递点：按抽水策略取走待续跑项。"""
        return self.agent.get_follow_up_messages()

    @property
    def pending_message_count(self) -> int:
        return self.agent.pending_message_count

    # ---------- 会话边界与状态 ----------

    def new_session(self) -> None:
        self.agent.new_session()

    def resume(self, target):
        return self.agent.resume(target)

    def rename(self, title: str) -> bool:
        return self.agent.rename_session(title)

    def reset(self) -> None:
        """把会话状态复位到「新会话」的初始值（不换对象）。"""
        self.agent.new_session()

    def state_parts(self) -> tuple[str, ...]:
        """状态投影里注册的部分名（`t=state` 的 key）。"""
        return tuple(part.name for part in self.agent._state_registry())

    def snapshot(self) -> dict:
        """当前状态投影快照（与写盘用同一份口径）。"""
        return {part.name: part.snapshot() for part in self.agent._state_registry()}

    def restore(self, data: dict) -> None:
        """按 name 恢复状态投影（缺失的保持默认值）。"""
        parts = {part.name: part for part in self.agent._state_registry()}
        for name, payload in (data or {}).items():
            part = parts.get(name)
            if part is not None:
                part.restore(payload)
