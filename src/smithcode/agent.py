from __future__ import annotations

import json
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from . import config, goal, instructions, plan, renderer, sessions, skills
from .cancel import CancellationToken, RunResult, activate_token, current_token
from .context import (
    ContextMeter,
    assemble,
    build_summary_request,
    is_context_overflow,
    pick_tail,
    total_tokens,
    truncate_output,
    validate_summary,
)
from .llm import LLMClient
from .llm.models import (
    CachedModelSource,
    ConfiguredModelSource,
    ModelCache,
    ModelCatalog,
    RemoteModelSource,
)
from .permission import Permission
from .plan import has_active, render_current, summary
from .session import Session
from .tools import (
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
from .tools.skills import sync_schema

# 工具调用短摘要行（如 `read src/agent.py`）的最大显示宽度，超出截断
MAX_SUMMARY_LEN = 80

# 变更预览（diff）最多展示的行数，超出截断
MAX_PREVIEW_LINES = 40

# 结果详情默认展开的工具：写/编辑类的 diff 是本次改动的关键信息（apply_patch
# 与 edit_file 同族），ask_user 的结果就是用户回答（页面主体）；都直接可见、可收起。
DEFAULT_EXPAND_TOOLS = frozenset({"write_file", "edit_file", "apply_patch", "ask_user"})


# 权限被拒时的统一工具结果文本（回传模型 + 终端展示共用）
DENIED_RESULT = "用户拒绝了此操作"
# 权限被拒后为同条 assistant 消息中剩余 tool_calls 补的占位结果（防悬空 tool_call_id）
SKIPPED_RESULT = "（未执行：权限请求被拒绝，任务已中止）"

# Esc 中断：控制台（REPL / 一次性任务）收尾提示行与占位结果文本。
# TUI 不再经 renderer 打印此提示，改由宿主机按 RunResult.status 渲染到
# 运行动画行（正在停止）/ 轮次页脚（已停止）。
INTERRUPTED_NOTE = "\n⏹ 已中断"
INTERRUPTED_RESULT = "（未执行：用户中断了任务）"
# 中断回写上下文：任务被手动中止时，作为一条 user 消息追加进会话历史
# （不触发任何新请求），下一轮用户提问时模型即可看到上轮是被主动叫停的、
# 任务未完成，避免把部分输出当成完整结果。
INTERRUPTED_CONTEXT = (
    "（用户手动中断了上一个任务，任务未完成。此前部分输出可能不完整，"
    "未执行的工具已标记为「未执行：用户中断了任务」。请以用户的最新输入为准。）"
)


@dataclass
class ResumeReport:
    """恢复会话的结果摘要：宿主据此渲染（标题 / 条数 / 崩溃修复情况）。"""

    path: Path
    session_id: str
    title: str
    message_count: int
    repair: str  # none / appended / truncated
    bad_lines: int


class _StatePart(NamedTuple):
    """会话级状态的统一协议：快照 / 恢复 / 重置（注册表遍历执行）。"""

    name: str
    snapshot: Callable[[], object]
    restore: Callable[[object], None]
    reset: Callable[[], None]


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


class _ToolPlan:
    """单个工具调用的执行计划：预检阶段（主线程）的产物。

    run 闭包封装全部执行细节（临时放行 widen、异常转结果文本），预检时
    权限与路径检查已完成，执行阶段可直接在主线程或线程池 worker 中调用。
    被拒的计划 run() 返回 DENIED_RESULT；display_result=False 的计划
    （todo_write）由 run() 自行渲染，收集时跳过 tool_result 展示与截断。
    """

    __slots__ = ("display_result", "name", "run", "serial", "tc", "tool_id")

    def __init__(self, tc: dict, name: str, tool_id: int | None, run, serial: bool = False,
                 display_result: bool = True):
        self.tc = tc
        self.name = name
        self.tool_id = tool_id
        self.run = run
        self.serial = serial
        self.display_result = display_result


class _BatchScheduler:
    """一次 assistant 消息内工具批次的流式调度器（只负责「顺序 + 并发」编排）。

    与旧「两阶段（预检完全部再执行）」的区别：**边预检边调度**——预检一个就
    决定其去向：可并行计划进入波次缓冲，串行计划作为顺序屏障先冲刷波次、再
    就地执行。效果：串行工具在「后续工具的权限确认」之前就已执行完，确认框
    与执行一一对应。

    不变量（改动此处务必对照 tests/test_agent_parallel.py）：
    - I1 结果消息 / tool_result 事件严格按 tool_call 提交顺序；
    - I2 每个 tool_call_id 恰有一条结果（执行 / SKIPPED / INTERRUPTED 三路完备）；
    - I3 串行工具是顺序屏障：执行前先冲刷前面的并行波次，保证副作用可见性；
    - I4 波次缓冲期零副作用——并行计划只在屏障/结束时才提交线程池，因此
      「首个串行工具执行前」的整段仍可原子取消；并行工具仅只读/网络类；
    - I5 渲染只在 run 线程发生（worker 只算结果字符串，经 _collect 收集）；
    - I7 limit==1 或波次仅 1 个计划时不启用线程池（退化为纯串行）。
    """

    def __init__(self, agent: Agent, token: CancellationToken | None, limit: int):
        self._agent = agent
        self._token = token
        self._limit = limit
        self._wave: list[_ToolPlan] = []  # 已预检、待并行执行的计划（保持提交顺序）

    def run(self, tool_calls: list[dict]) -> str | None:
        """按接收顺序「预检一个 → 调度一个」，返回 None / "denied" / "interrupted"。"""
        i, n = 0, len(tool_calls)
        while i < n:
            if self._cancelled():  # 循环顶部中断：当前项尚未预检
                return self._abort_interrupted(self._wave, tool_calls[i:])
            tool_plan, denied = self._agent._preflight_safe(tool_calls[i])
            if self._cancelled():  # 预检（含权限确认）期间中断：当前项也不执行
                return self._abort_interrupted(self._wave + [tool_plan], tool_calls[i + 1:])
            if denied:
                return self._abort_denied(tool_plan, tool_calls[i + 1:])
            if tool_plan.serial or self._limit == 1:  # 顺序屏障 / 退化纯串行
                self._flush()  # 先收前面的并行波次，再执行串行项
                self._agent._collect(tool_plan, tool_plan.run())
            else:
                self._wave.append(tool_plan)  # 缓冲，屏障 / 结束时才跑
            i += 1
        self._flush()
        return None

    def _flush(self) -> None:
        """冲刷波次：按提交顺序执行并收集（I1）；长度 1 或 limit==1 走主线程（I7）。"""
        wave, self._wave = self._wave, []
        if not wave:
            return
        if len(wave) == 1 or self._limit == 1:
            for p in wave:
                self._agent._collect(p, p.run())
            return
        with ThreadPoolExecutor(max_workers=self._limit) as pool:
            futures = [pool.submit(p.run) for p in wave]  # 按序提交
            for p, future in zip(wave, futures):
                self._agent._collect(p, future.result())  # 按提交序收集，不按完成序

    def _abort_denied(self, denied_plan: _ToolPlan, remaining_tcs: list[dict]) -> str:
        """权限被拒收尾：未执行的波次与被拒项之后的剩余项补 SKIPPED，
        被拒项收尾为 DENIED（其 run() 返回 DENIED_RESULT），返回 "denied"。"""
        for p in self._wave:
            self._agent._placeholder(SKIPPED_RESULT, p.tc.get("id"), p.tool_id)
        self._agent._collect(denied_plan, denied_plan.run())
        for tc in remaining_tcs:
            self._agent._placeholder(SKIPPED_RESULT, tc["id"])
        renderer.current().error("权限请求被拒绝，任务已停止")
        return "denied"

    def _abort_interrupted(self, pending_plans: list[_ToolPlan],
                           remaining_tcs: list[dict]) -> str:
        """中断收尾：未执行的计划与剩余 tool_calls 一律补占位（顺序天然对齐）。"""
        return self._agent._interrupt_batch(pending_plans, remaining_tcs)

    def _cancelled(self) -> bool:
        return self._token is not None and self._token.cancelled


class Agent:
    def __init__(self, session: Session | None = None, max_iterations: int | None = None,
                 store=None, persist: bool = False, oneshot: bool = False):
        reset_read_tracking()  # 新会话开始，「已读文件」记录从零开始
        self.llm = LLMClient()
        self.session = session or Session()
        self.permission = Permission()
        self.context = ContextMeter()  # 上下文快照计量：真实锚点 + 临近阈值提醒
        self._token: CancellationToken | None = None  # 当前轮次的取消令牌（run 期间非空）
        self.max_iterations = max_iterations or config.MAX_ITERATIONS
        # 进程级服务留在 Agent（不随会话存取）：llm / permission / models / 令牌
        self.sessions_config = config.load_sessions_config()
        self._persist = bool(persist) and self.sessions_config.enabled
        self._last_state = None  # 最近一次写盘的 state 快照（去重）
        self._title_attempted = False  # 自动标题只触发一次
        if store is not None:
            self.session.bind_store(store)
        elif self._persist:
            self.session.bind_store(self._new_store(oneshot=oneshot))
        # 候选模型目录：命令层只读 `agent.models.list()`，不关心来源与装载时机
        cache = ModelCache()
        self.models = ModelCatalog(
            configured=ConfiguredModelSource(),
            cached=CachedModelSource(cache),
            remote=RemoteModelSource(self.llm, cache),
            current_model=lambda: config.MODEL,
        )

    def _new_store(self, oneshot: bool = False):
        """新建一个会话转录（轮换 id，文件懒物化）。"""
        return sessions.SessionStore.create(
            model=config.MODEL, effort=config.REASONING_EFFORT, oneshot=oneshot,
        )

    def start(self) -> None:
        """启动期装载模型目录、技能目录与项目指令：模型未配置时后台拉取 `/models`。

        技能发现可能弹出项目级信任确认（渲染后端此时为 ConsoleRenderer，
        TUI 尚未接管，交互行为一致）。项目指令在首次 `sync_system()` 前装载，
        使首个请求即带上 AGENTS.md；读取失败只警告、不阻断启动。
        """
        self.models.bootstrap()
        self.refresh_skills()
        instructions.refresh()

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
        命令层（commands）不感知重置细节。
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
        self._title_attempted = False
        if self._persist:
            self.session.bind_store(self._new_store())
        else:
            self.session.bind_store(None)

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
        self._title_attempted = False
        self.session.sync_system()  # system 段按当前提示词立即重建

        return ResumeReport(
            path=loaded.path,
            session_id=loaded.id,
            title=loaded.title,
            message_count=len(loaded.messages),
            repair=loaded.repair,
            bad_lines=loaded.bad_lines,
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
        return (
            _StatePart("goal", goal.snapshot, goal.restore, goal.reset),
            _StatePart("plan", plan.snapshot, plan.restore, plan.reset),
            _StatePart("skills", skills.snapshot, skills.restore, skills.reset),
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

    # ---------- 自动标题（后台，失败静默） ----------

    def _maybe_generate_title(self) -> None:
        """首轮正常结束后自动生成标题：仅一次，用户标题优先，失败保留 fallback。"""
        if self._title_attempted or self.session.store is None:
            return
        if not self.sessions_config.auto_title:
            return
        self._title_attempted = True
        if not sessions.should_generate(self.session.title, self.session.title_source):
            return
        # 在主线程取快照（后台线程不再读会话消息），再交给 daemon 线程
        request = sessions.build_title_request(self.session.messages)
        model = self.sessions_config.title_model or None
        threading.Thread(
            target=self._title_worker, args=(request, model),
            name="smithcode-title", daemon=True,
        ).start()

    def _title_worker(self, request, model) -> None:
        try:
            text = self._complete(request, model=model)
            title = sessions.clean_title(text, self.sessions_config.title_max_chars)
            if not title:
                return
            self.session.set_title(title, source="auto")
            renderer.current().title_changed(title)
        except Exception:  # noqa: BLE001 标题失败静默，不影响主流程
            return

    def rename_session(self, title: str) -> bool:
        """用户命名当前会话（/rename / --name）：刷新标题记录，自动标题不再覆盖。"""
        text = str(title or "").strip()
        if not text:
            return False
        self.session.set_title(text, source="user")
        renderer.current().title_changed(text)
        return True

    def close(self) -> None:
        """进程退出前收尾：flush + 关闭转录句柄。"""
        if self.session.store is not None:
            self.session.store.close()

    def interrupt(self) -> None:
        """请求中断当前任务（线程安全：TUI / REPL 主线程调用，Agent 在后台线程运行）。

        取消是协作式的：LLM 流在下一块数据到达前截停，正在执行的工具让
        其跑完，未执行的工具调用补占位结果——会话历史始终保持合法。
        """
        if self._token is not None:
            self._token.cancel()

    def run(self, user_input: str) -> RunResult:
        """执行一次任务直至模型给出最终回复（或中断 / 拒绝 / 迭代上限）。

        每次调用激活一个新令牌并经 ContextVar 沿调用链传播（llm 流层、
        工具调度层按需读取）；结束后复位，保证下一次任务不受残留取消
        状态影响。
        """
        self.session.sync_system()  # 发请求前同步系统提示词（含当前持久目标段）
        self.session.add("user", user_input)
        token = CancellationToken()
        self._token = token
        reset_token = activate_token(token)
        try:
            result = self._run_loop(token)
        finally:
            self._token = None
            reset_token()
        self._persist_turn()  # 状态投影缓存落盘（无变化不写）
        if result.status == "ok":
            self._maybe_generate_title()  # 首轮结束后自动标题（后台、仅一次）
        if result.status == "interrupted":
            self._note_interrupted()  # 回写上下文但不发请求，供下一轮模型看到
        return result

    def _note_interrupted(self) -> None:
        """把「用户中断」事件作为 user 消息写进会话历史（不触发新请求）。

        追加在所有占位结果之后，是本次轮次的最后一条消息；下一轮用户提问时
        模型即可看到上一轮被主动中止、任务未完成，不会把部分输出当作结果。"""
        self.session.add("user", INTERRUPTED_CONTEXT)

    def run_with_goal(self, user_input: str) -> RunResult:
        """执行一次任务，并在持久目标激活时自动续跑直到目标结束或触发刹车。

        /goal 的唯一续跑驱动器（REPL / TUI / 一次性任务共用）：无目标时与
        run() 完全等价。目标 active 时，每轮结束后按下述规则决定是否注入
        续跑提示词开启下一回合：

        - 上一轮正常结束且执行过工具（推进动作）→ 继续；
        - 续跑轮没有任何工具调用 → 暂停目标，防止空转；
        - 被中断 → 保留 active，停止循环（用户主动叫停）；
        - 被拒 / 迭代上限 → 暂停目标（继续只会重复失败）；
        - 回合数达到预算 → 标记预算用尽并注入收尾提示词跑最后一轮。
        """
        goal_turn = goal.is_active()
        if goal_turn:
            goal.begin_turn()
        result = self.run(user_input)
        self._note_goal_run(result)
        while goal.is_active():
            if result.status != "ok":
                if result.status in ("denied", "max_iterations") and goal.pause(
                    "上一次任务未正常结束，已暂停自动推进"
                ):
                    renderer.current().warn("[目标] 已暂停自动推进（/goal resume 可继续）")
                break
            if not result.tools_used:
                if goal.pause("本轮没有产生工具调用，已暂停自动推进以防空转"):
                    renderer.current().warn(
                        "[目标] 已暂停：本轮没有产生工具调用（/goal resume 可继续）"
                    )
                break
            current = goal.current()
            if current is None:  # 目标在本轮结束时被清除
                break
            if current.turns >= current.max_turns:
                goal.budget_limited()
                renderer.current().warn(
                    f"[目标] 回合预算用尽（{current.max_turns} 回合），正在收尾…"
                )
                result = self.run(current.wrapup_prompt())
                self._note_goal_run(result)
                break
            goal.begin_turn()
            marker = goal.current()
            if marker is None:  # 目标在本轮结束时被清除
                break
            renderer.current().info(
                f"[目标] 继续推进 · 第 {marker.turns}/{marker.max_turns} 回合"
            )
            result = self.run(marker.continuation_prompt())
            self._note_goal_run(result)
        return result

    def _note_goal_run(self, result: RunResult) -> None:
        """把一轮结果同步给目标状态机：推进动作重置阻碍连击、累计 token 用量。"""
        goal.note_run(
            result.tools_used,
            self.session.usage.current_session.get("total_tokens"),
        )

    def _run_loop(self, token: CancellationToken) -> RunResult:
        tools_used: list = []  # 本任务执行过的工具名（去重保序），供 /goal 续跑裁决
        for _ in range(self.max_iterations):
            if token.cancelled:
                return RunResult("interrupted")
            # 每轮同步系统提示词：本轮加载的技能正文下一轮生效（内容不变时不重建）
            self.session.sync_system()
            self._compact_if_needed()
            msg, usage, interrupted = self._chat_with_recovery()
            self.session.usage.add(usage)
            self.context.record(usage)  # 记下真实 prompt_tokens 作估算锚点
            if usage and self.session.store is not None:
                self.session.store.append_usage(usage)
            self.session.messages.append(msg)

            if interrupted:
                return RunResult("interrupted", partial=True, tools_used=tuple(tools_used))

            if not msg.get("tool_calls"):
                return RunResult("ok", msg.get("content", ""), tools_used=tuple(tools_used))

            if token.cancelled:
                # 流刚好走完时才取消：这批 tool_calls 一个都未执行，补占位后停止
                self._interrupt_batch([], msg.get("tool_calls", []))
                return RunResult("interrupted", tools_used=tuple(tools_used))

            for tc in msg["tool_calls"]:
                name = tc.get("function", {}).get("name", "")
                if name and name not in tools_used:
                    tools_used.append(name)
            stopped = self._execute_batch(msg["tool_calls"])
            if stopped == "denied":
                return RunResult(
                    "denied", "任务已停止：权限请求被用户拒绝。", tools_used=tuple(tools_used)
                )
            if stopped == "interrupted":
                return RunResult("interrupted", tools_used=tuple(tools_used))

        return RunResult(
            "max_iterations", "达到最大迭代次数，任务中止。", tools_used=tuple(tools_used)
        )

    def _chat_with_recovery(self) -> tuple[dict, dict | None, bool]:
        """一次模型调用；上下文溢出时压缩后重试一次（opencode 的溢出恢复）。

        仅当错误文本命中溢出特征才走这条路，其他异常原样上抛。恢复后的
        调用再溢出就直接抛给 REPL——每步只重试一次，不反复烧钱。
        返回 (消息, 用量, 是否被中断)。
        """
        try:
            return self._chat()
        except Exception as e:
            if not is_context_overflow(e):
                raise
        renderer.current().info("[context] 上下文溢出，压缩后重试…")
        self.compact()
        return self._chat()

    def _compact_if_needed(self) -> None:
        """每轮调用前的预检：估算越过阈值（预算 × COMPACT_TRIGGER）就先压缩。"""
        budget = config.CONTEXT_TOKEN_BUDGET
        if total_tokens(self.session.messages) > budget * config.COMPACT_TRIGGER:
            self.compact()

    def compact(self) -> bool:
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
        for _ in range(2):  # 摘要缺必需标题时重试一次
            token = current_token()
            if token is not None and token.cancelled:
                return False  # 已中断：不再发起摘要请求，静默放弃（中断提示由收尾路径给出）
            text = self._complete(build_summary_request(old))
            if validate_summary(text):
                summary = text
                break
        if summary is None:
            renderer.current().info("[context] 摘要未按模板生成，放弃本次压缩，原样继续")
            return False

        assembled = assemble(
            messages[0].get("content", ""), summary, messages[tail_start:]
        )
        self.session.set_compacted(
            assembled[1], assembled[2:], before=before,
            after=total_tokens(assembled),
        )
        self.context.compact_count += 1
        renderer.current().info(
            f"[context] 已压缩: {before:,} → {total_tokens(self.session.messages):,} tokens"
        )
        return True

    def _complete(self, request: list[dict], model: str | None = None) -> str:
        """一次不带工具的补全，收集完整文本（摘要 / 标题生成专用）。"""
        parts = []
        kwargs = {"model": model} if model else {}
        for kind, payload in self.llm.chat_stream(request, tools=None, **kwargs):
            if kind == "content":
                parts.append(payload)
            elif kind == "message" and not parts:
                parts.append(payload.get("content") or "")
        return "".join(parts)

    def _chat(self) -> tuple[dict, dict | None, bool]:
        """一次流式模型调用：思考与正文各占一行（均带 助手> 前缀）。

        返回 (消息, 用量, 是否被中断)。用量由 llm 层从流中提取，服务商
        不提供时为 None。渲染交给 renderer（CLI 逐字打印 / TUI 进组件）。
        任务被取消时流在下一块数据前截停（llm 层负责），已收到的正文拼
        成部分 assistant 消息返回并标记 interrupted——残缺的工具调用不
        回传（无法解析），完整正文得以保留。
        思考内容（reasoning_content，仅部分模型返回）以灰色实时展示，
        但不写入会话——多数 OpenAI 兼容服务不接受它被回传。
        """
        msg: dict = {}
        usage = None
        parts: list[str] = []
        r = renderer.current()
        for kind, payload in self.llm.chat_stream(self.session.messages, tools=visible_schemas()):
            if kind == "message":
                msg = payload
            elif kind == "usage":
                usage = payload
            else:
                if kind == "content":
                    parts.append(payload)
                r.stream(kind, payload)
        r.stream_done()
        token = current_token()
        if token is not None and token.cancelled and not msg:
            msg = {"role": "assistant", "content": "".join(parts)}
            return msg, usage, True
        return msg, usage, False

    def _preflight_safe(self, tc: dict) -> tuple[_ToolPlan, bool]:
        """预检的兜底包装：预检自身的意外异常转为该工具的错误结果，不外抛。

        旧实现里权限确认与执行同处一个 try 块，交互层异常（如终端不可用）
        只体现为该工具的错误结果、循环继续；这里保持同样的容错边界。
        """
        try:
            return self._preflight(tc)
        except Exception as e:  # noqa: BLE001
            name = tc["function"]["name"]
            text = f"错误: {type(e).__name__}: {e}"
            return _ToolPlan(tc, name, None, lambda: text), False

    def _preflight(self, tc: dict) -> tuple[_ToolPlan, bool]:
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
            tool_id = renderer.current().tool_call(f"[Tool] {name}({args_json[:80]})", name=name)
            return _ToolPlan(tc, name, tool_id, lambda: text), False

        line = self._describe(name, args)
        display = DISPLAY.get(name, "inline")
        # 既有计划的更新不上屏工具行：todo_write 每完成一步就更新一次，若每次都
        # 生成工具块会往对话区反复打印进度；更新只静默刷新侧边栏，新建清单才展示
        todo_update = name == "todo_write" and has_active()
        if todo_update:
            tool_id = None
        else:
            tool_id = renderer.current().tool_call(
                line[:MAX_SUMMARY_LEN] + ("..." if len(line) > MAX_SUMMARY_LEN else ""), display, name
            )
        denied_plan = _ToolPlan(tc, name, tool_id, lambda: DENIED_RESULT)

        # 多路径工具（如 apply_patch）：从参数提取目标路径，逐路径预检 + 聚合权限检查
        extractor = PATHS_EXTRACTORS.get(name)
        if extractor is not None:
            try:
                paths = [str(p) for p in extractor(args)]
            except Exception as e:  # noqa: BLE001
                text = f"错误: 无法解析目标路径: {type(e).__name__}: {e}"
                return _ToolPlan(tc, name, tool_id, lambda: text), False

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
                renderer.current().tool_preview(tool_id, snapshot)

            # "仅本次"越界放行经 widen_roots 全局生效，并行窗口内其他线程会
            # 意外获得该目录的访问权——带临时放行目录的计划强制串行
            return _ToolPlan(tc, name, tool_id,
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
                    renderer.current().tool_result(result, tool_id)
                    return result
                renderer.current().plan(
                    summary(), render_current(color=True), created=created, tool_id=tool_id
                )
                return result

            # todo_write 改写会话级状态机并渲染计划，必须独占主线程
            return _ToolPlan(tc, name, tool_id, run_todo, serial=True, display_result=False), False

        # 单路径/无路径工具：变更预览（diff）在路径预检 / 权限确认 / 执行之前
        # 推送到工具调用块：审核时改动内容已经可见，权限框保持纯净
        snapshot = _diff_preview(name, args)
        if snapshot:
            renderer.current().tool_preview(tool_id, snapshot)

        # 路径预检：目标在授权目录之外时先请用户确认（目录信任 → 操作权限，两道关卡有序）
        preflight = self._preflight_outside_path(args, read_only=name in READ_ONLY_TOOLS)
        if preflight == "deny":
            return denied_plan, True
        if not self.permission.check(name, args, content=line):
            return denied_plan, True

        # 注册为 serial 的工具（shell / 写文件 / 交互确认等）与需要临时放行的
        # 调用在主线程串行执行，其余进并发波次
        widen = [preflight] if isinstance(preflight, Path) else []
        return _ToolPlan(tc, name, tool_id,
                         self._make_runner(name, args, widen),
                         serial=bool(SERIAL.get(name)) or bool(widen)), False

    @staticmethod
    def _make_runner(name: str, args: dict, widen: list[Path]) -> Callable[[], str]:
        """生成工具执行闭包：临时放行（widen）+ 调用 + 异常转结果文本。

        闭包在预检完成后于主线程或线程池 worker 中调用；工具执行的任何
        失败都只作为结果回传给模型，不中断循环。widen 为空时 widen_roots
        直接放行，等价无放行调用。
        """
        def run() -> str:
            try:
                with config.widen_roots(widen):
                    return str(FUNCTIONS[name](**args))
            except Exception as e:  # noqa: BLE001
                return f"错误: {type(e).__name__}: {e}"

        return run

    def _execute_batch(self, tool_calls: list[dict]) -> str | None:
        """执行同一条 assistant 消息里的全部工具调用（流式调度，见 _BatchScheduler）。

        逐项预检、边预检边执行：可并行计划进波次缓冲，串行计划作为顺序屏障
        （先冲刷前面的并行波次再执行），因此串行工具在后续工具的权限确认之前
        就已执行完。结果严格按提交顺序回传；被拒 / 中断时未执行的项补占位结果，
        保证每个 tool_call_id 成对。返回 None / "denied" / "interrupted"。
        """
        limit = max(1, int(config.MAX_TOOL_CONCURRENCY))
        return _BatchScheduler(self, current_token(), limit).run(tool_calls)

    def _interrupt_batch(self, pending_plans: list[_ToolPlan],
                         remaining_tcs: list[dict]) -> str:
        """中断收尾：已预检未执行 / 尚未预检的 tool_calls 一律补占位结果。

        会话不变量：每条 assistant 消息的每个 tool_call_id 都必须有配对
        的 tool 结果，否则下一次请求会被服务商拒绝。返回 "interrupted"。
        """
        for p in pending_plans:
            self._placeholder(INTERRUPTED_RESULT, p.tc.get("id"), p.tool_id)
        for tc in remaining_tcs:
            self._placeholder(INTERRUPTED_RESULT, tc.get("id"))
        return "interrupted"

    def _placeholder(self, content: str, tool_call_id, tool_id: int | None = None) -> None:
        """为未执行的 tool_call 补占位结果（拒绝 / 中断的会话修复共用）。

        tool_id 非空时同步更新渲染后端对应的 pending 工具块（已预检的计划
        已上屏转轮，跳过执行后必须收尾，否则 TUI 停在 pending 态）。"""
        self.session.messages.append(
            {"role": "tool", "content": content, "tool_call_id": tool_call_id}
        )
        if tool_id is not None:
            renderer.current().tool_result(content, tool_id)

    def _collect(self, plan: _ToolPlan, result: str) -> str:
        """收集一个执行完的计划：截断、终端展示、按序追加进会话。

        只在主线程调用（渲染不进 worker）；todo_write 的计划展示已由 run()
        自行渲染，display_result=False 时跳过 tool_result 展示与截断。
        """
        if plan.display_result:
            result = self._finish(result, plan.tool_id, plan.name)
        self.session.messages.append(
            {"role": "tool", "content": result, "tool_call_id": plan.tc.get("id")}
        )
        return result

    def _finish(self, result: str, tool_id: int | None = None,
                name: str | None = None) -> str:
        """回传前截断超长输出；终端展示交给 renderer（summary 模式只有
        执行前那行短摘要，detail 模式追加结果内容）。失败信息无论何种模式
        都原样展示——失败的细节比格式化摘要更重要。

        name 非空且属写/编辑类时，TUI 中调用详情默认展开（diff 已在
        tool_preview 阶段推入工具块）。"""
        result = truncate_output(result, config.MAX_TOOL_OUTPUT)
        renderer.current().tool_result(result, tool_id,
                                       expand=name in DEFAULT_EXPAND_TOOLS)
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
