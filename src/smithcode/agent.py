from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config, plan, renderer
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
from .plan import render_current, summary
from .session import Session
from .tools import (
    DESCRIBERS,
    DISPLAY,
    FUNCTIONS,
    PATHS_EXTRACTORS,
    PREVIEWS,
    SCHEMAS,
    SERIAL,
    reset_read_tracking,
)

# 工具调用短摘要行（如 `read src/agent.py`）的最大显示宽度，超出截断
MAX_SUMMARY_LEN = 80

# 变更预览（diff）最多展示的行数，超出截断
MAX_PREVIEW_LINES = 40

# 写/编辑类工具：调用详情默认展开（diff 是本次改动的关键信息，直接可见可收起）
FILE_EXPAND_TOOLS = frozenset({"write_file", "edit_file"})


# 权限被拒时的统一工具结果文本（回传模型 + 终端展示共用）
DENIED_RESULT = "用户拒绝了此操作"
# 权限被拒后为同条 assistant 消息中剩余 tool_calls 补的占位结果（防悬空 tool_call_id）
SKIPPED_RESULT = "（未执行：权限请求被拒绝，任务已中止）"

# Esc 中断：终端提示行与占位结果文本（语义同上两条）
INTERRUPTED_NOTE = "\n⏹ 已中断"
INTERRUPTED_RESULT = "（未执行：用户中断了任务）"


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


class Agent:
    def __init__(self, session: Session | None = None, max_iterations: int | None = None):
        reset_read_tracking()  # 新会话开始，「已读文件」记录从零开始
        self.llm = LLMClient()
        self.session = session or Session()
        self.permission = Permission()
        self.context = ContextMeter()  # 上下文快照计量：真实锚点 + 临近阈值提醒
        self._token: CancellationToken | None = None  # 当前轮次的取消令牌（run 期间非空）
        self.max_iterations = max_iterations or config.MAX_ITERATIONS
        # 候选模型目录：命令层只读 `agent.models.list()`，不关心来源与装载时机
        cache = ModelCache()
        self.models = ModelCatalog(
            configured=ConfiguredModelSource(),
            cached=CachedModelSource(cache),
            remote=RemoteModelSource(self.llm, cache),
            current_model=lambda: config.MODEL,
        )

    def start(self) -> None:
        """启动期装载模型目录：外部配置优先；未配置则后台拉取远端 `/models`。"""
        self.models.bootstrap()

    def new_session(self) -> None:
        """开启新会话（/new 的实际动作）：集中重置全部会话口径状态。

        覆盖：消息历史与会话 id、会话用量、权限会话规则、越界信任目录、
        上下文快照（压缩计数与真实 token 锚点）、工具侧「已读文件」记录、
        步骤清单。新增会话级状态时在对应模块加 reset 后在此补一行，
        命令层（commands）不感知重置细节。
        """
        self.session.reset()
        self.permission.new_session()
        config.SESSION_EXTRA_ROOTS.clear()
        self.context.new_session()
        reset_read_tracking()
        plan.reset()

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
        self.session.ensure_system()  # 首次发请求前才把系统提示词放入历史（懒加载）
        self.session.add("user", user_input)
        token = CancellationToken()
        self._token = token
        reset_token = activate_token(token)
        try:
            return self._run_loop(token)
        finally:
            self._token = None
            reset_token()

    def _run_loop(self, token: CancellationToken) -> RunResult:
        for _ in range(self.max_iterations):
            if token.cancelled:
                renderer.current().info(INTERRUPTED_NOTE)
                return RunResult("interrupted")
            self._compact_if_needed()
            msg, usage, interrupted = self._chat_with_recovery()
            self.session.usage.add(usage)
            self.context.record(usage)  # 记下真实 prompt_tokens 作估算锚点
            self.session.messages.append(msg)

            if interrupted:
                renderer.current().info(INTERRUPTED_NOTE)
                return RunResult("interrupted", partial=True)

            if not msg.get("tool_calls"):
                return RunResult("ok", msg.get("content", ""))

            if token.cancelled:
                # 流刚好走完时才取消：这批 tool_calls 一个都未执行，补占位后停止
                self._interrupt_batch([], msg.get("tool_calls", []))
                renderer.current().info(INTERRUPTED_NOTE)
                return RunResult("interrupted")

            stopped = self._execute_batch(msg["tool_calls"])
            if stopped == "denied":
                return RunResult("denied", "任务已停止：权限请求被用户拒绝。")
            if stopped == "interrupted":
                renderer.current().info(INTERRUPTED_NOTE)
                return RunResult("interrupted")

        return RunResult("max_iterations", "达到最大迭代次数，任务中止。")

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
        renderer.current().info("\n[context] 上下文溢出，压缩后重试…")
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

        self.session.messages = assemble(
            messages[0].get("content", ""), summary, messages[tail_start:]
        )
        self.context.compact_count += 1
        renderer.current().info(
            f"\n[context] 已压缩: {before:,} → {total_tokens(self.session.messages):,} tokens"
        )
        return True

    def _complete(self, request: list[dict]) -> str:
        """一次不带工具的补全，收集完整文本（摘要生成专用）。"""
        parts = []
        for kind, payload in self.llm.chat_stream(request, tools=None):
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
        for kind, payload in self.llm.chat_stream(self.session.messages, tools=SCHEMAS):
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
            tool_id = renderer.current().tool_call(f"[Tool] {name}({args_json[:80]})")
            return _ToolPlan(tc, name, tool_id, lambda: text), False

        line = self._describe(name, args)
        display = DISPLAY.get(name, "inline")
        tool_id = renderer.current().tool_call(
            line[:MAX_SUMMARY_LEN] + ("..." if len(line) > MAX_SUMMARY_LEN else ""), display
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
            if not self.permission.check_paths(name, paths):
                return denied_plan, True

            snapshot = _diff_preview(name, args)  # 执行前快照（apply_patch 暂无 preview，为空串）
            if snapshot:
                renderer.current().tool_preview(tool_id, snapshot)

            # "仅本次"越界放行经 widen_roots 全局生效，并行窗口内其他线程会
            # 意外获得该目录的访问权——带临时放行目录的计划强制串行
            return _ToolPlan(tc, name, tool_id,
                             self._make_runner(name, args, widened), serial=bool(widened)), False

        if name == "todo_write":
            if not self.permission.check(name, args):
                return denied_plan, True

            def run_todo() -> str:
                try:
                    result = str(FUNCTIONS[name](**args))
                except Exception as e:  # noqa: BLE001
                    result = f"错误: {type(e).__name__}: {e}"
                    renderer.current().tool_result(result, tool_id)
                    return result
                renderer.current().plan(summary(), render_current(color=True))
                return result

            # todo_write 改写会话级状态机并渲染计划，必须独占主线程
            return _ToolPlan(tc, name, tool_id, run_todo, serial=True, display_result=False), False

        # 单路径/无路径工具：变更预览（diff）在路径预检 / 权限确认 / 执行之前
        # 推送到工具调用块：审核时改动内容已经可见，权限框保持纯净
        snapshot = _diff_preview(name, args)
        if snapshot:
            renderer.current().tool_preview(tool_id, snapshot)

        # 路径预检：目标在授权目录之外时先请用户确认（目录信任 → 操作权限，两道关卡有序）
        preflight = self._preflight_outside_path(args)
        if preflight == "deny":
            return denied_plan, True
        if not self.permission.check(name, args):
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
        """执行同一条 assistant 消息里的全部工具调用（两阶段：预检串行、执行并发）。

        第一阶段（主线程，串行，按接收顺序）：逐个预检——解析参数、渲染摘要、
        路径预检与权限确认；任一被拒即终止任务，为剩余 tool_calls 补占位结果
        （防悬空 tool_call_id 破坏下一轮请求），此时还没有任何工具被执行。
        每个预检项之前检查取消令牌：任务被中断时已过预检的计划与剩余
        tool_calls 一律补占位、不再继续弹确认框（中断 = 不再发起任何新工作）。
        第二阶段（并发）：按序分段——连续的可并行计划合并为一个波次扔进线程池，
        serial 计划在主线程单独执行、作为顺序屏障（保证串行工具看见之前所有
        副作用、后续工具又看见串行工具的改动）；结果一律按提交顺序收集，
        模型看到的 tool 结果顺序与它请求的顺序严格一致。每个波次/串行项
        之前检查取消令牌：任务被中断时未执行的计划补占位后停止，已提交
        的波次让其自然跑完（线程不可强杀）并照常收集结果。

        返回 None 表示正常完成；"denied" 表示权限被拒；"interrupted" 表示用户中断。
        """
        token = current_token()
        plans: list[_ToolPlan] = []
        for i, tc in enumerate(tool_calls):
            if token is not None and token.cancelled:
                return self._interrupt_batch(plans, tool_calls[i:])
            plan, denied = self._preflight_safe(tc)
            if token is not None and token.cancelled:
                # 预检（含权限确认）期间用户中断：优先按中断处理——当前项即使
                # 刚答了 y/n 也不执行、不按 denied 收尾，整个批次补占位。
                return self._interrupt_batch(plans, tool_calls[i:])
            if denied:
                # 此前已过预检但尚未执行的计划：任务中止，一并补占位结果
                # （防悬空 tool_call_id 破坏下一轮请求）
                for done in plans:
                    self._placeholder(SKIPPED_RESULT, done.tc.get("id"))
                self._collect(plan, plan.run())
                for pending in tool_calls[i + 1:]:
                    self._placeholder(SKIPPED_RESULT, pending["id"])
                renderer.current().info("\n⛔ 权限请求被拒绝，任务已停止")
                return "denied"
            plans.append(plan)

        limit = max(1, int(config.MAX_TOOL_CONCURRENCY))
        if limit == 1 or len(plans) < 2 or all(p.serial for p in plans):
            for idx, p in enumerate(plans):  # 纯串行路径：不启用线程，行为与逐个执行完全一致
                if token is not None and token.cancelled:
                    return self._interrupt_batch(plans[idx:], [])
                self._collect(p, p.run())
            return None

        with ThreadPoolExecutor(max_workers=limit) as pool:
            i = 0
            while i < len(plans):
                if token is not None and token.cancelled:
                    return self._interrupt_batch(plans[i:], [])
                if plans[i].serial:
                    self._collect(plans[i], plans[i].run())
                    i += 1
                    continue
                j = i
                while j < len(plans) and not plans[j].serial:
                    j += 1
                wave = plans[i:j]
                futures = [pool.submit(p.run) for p in wave]  # 按序提交
                for p, f in zip(wave, futures):
                    self._collect(p, f.result())  # 按提交序收集，不按完成序
                i = j
        return None

    def _interrupt_batch(self, pending_plans: list[_ToolPlan],
                         remaining_tcs: list[dict]) -> str:
        """中断收尾：已预检未执行 / 尚未预检的 tool_calls 一律补占位结果。

        会话不变量：每条 assistant 消息的每个 tool_call_id 都必须有配对
        的 tool 结果，否则下一次请求会被服务商拒绝。返回 "interrupted"。
        """
        for p in pending_plans:
            self._placeholder(INTERRUPTED_RESULT, p.tc.get("id"))
        for tc in remaining_tcs:
            self._placeholder(INTERRUPTED_RESULT, tc.get("id"))
        return "interrupted"

    def _placeholder(self, content: str, tool_call_id) -> None:
        """为未执行的 tool_call 补占位结果（拒绝 / 中断的会话修复共用）。"""
        self.session.messages.append(
            {"role": "tool", "content": content, "tool_call_id": tool_call_id}
        )

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
                                       expand=name in FILE_EXPAND_TOOLS)
        return result

    @staticmethod
    def _describe(name: str, args) -> str:
        """工具调用的一行短摘要；未注册 describe 的工具回退为 [Tool] 名字(参数) 格式。"""
        describe = DESCRIBERS.get(name)
        if describe is not None and isinstance(args, dict):
            return describe(args)
        return f"[Tool] {name}({json.dumps(args, ensure_ascii=False)[:80]})"

    def _preflight_path(self, raw: str) -> Path | str | None:
        """检查单个路径是否落在授权目录之外；之外时先交互确认。

        返回 "deny"（用户拒绝本次访问）、Path（"仅本次"，执行时需临时放行该信任根）、
        None（路径在授权范围内，或用户已选"本会话总是"——信任根已入库）。
        与工具内部的越界检查互为备份：预检管交互体验，工具侧管强制执行。
        """
        target = (Path(config.WORKSPACE_ROOT) / str(raw)).resolve()
        if any(target.is_relative_to(r) for r in config.allowed_roots()):
            return None
        action, root = self.permission.ask_outside_access(str(raw), target)
        if action == "deny":
            return "deny"
        return root if action == "once" else None

    def _preflight_outside_path(self, args: dict) -> Path | str | None:
        """单路径工具（path 参数）的越界预检入口。"""
        raw = args.get("path")
        return self._preflight_path(raw) if raw else None
