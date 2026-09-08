from __future__ import annotations

import json
from pathlib import Path

from . import config, renderer
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
from .models import (
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
    def __init__(self, session: Session | None = None, max_iterations: int | None = None):
        reset_read_tracking()  # 新会话开始，「已读文件」记录从零开始
        self.llm = LLMClient()
        self.session = session or Session()
        self.permission = Permission()
        self.context = ContextMeter()  # 上下文快照计量：真实锚点 + 临近阈值提醒
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

    def run(self, user_input: str) -> str:
        self.session.ensure_system()  # 首次发请求前才把系统提示词放入历史（懒加载）
        self.session.add("user", user_input)

        for _ in range(self.max_iterations):
            self._compact_if_needed()
            msg, usage = self._chat_with_recovery()
            self.session.usage.add(usage)
            self.context.record(usage)  # 记下真实 prompt_tokens 作估算锚点
            self.session.messages.append(msg)

            if not msg.get("tool_calls"):
                return msg.get("content", "")

            for i, tc in enumerate(msg["tool_calls"]):
                result, denied = self._execute(tc)
                self.session.messages.append(
                    {"role": "tool", "content": result, "tool_call_id": tc["id"]}
                )
                if denied:
                    # 权限被拒：为同条消息中剩余 tool_calls 补占位结果（防悬空
                    # tool_call_id 破坏下一轮请求），然后直接终止本轮任务。
                    for pending in msg["tool_calls"][i + 1:]:
                        self.session.messages.append(
                            {"role": "tool", "content": SKIPPED_RESULT, "tool_call_id": pending["id"]}
                        )
                    renderer.current().info("\n⛔ 权限请求被拒绝，任务已停止")
                    return "任务已停止：权限请求被用户拒绝。"

        return "达到最大迭代次数，任务中止。"

    def _chat_with_recovery(self) -> tuple[dict, dict | None]:
        """一次模型调用；上下文溢出时压缩后重试一次（opencode 的溢出恢复）。

        仅当错误文本命中溢出特征才走这条路，其他异常原样上抛。恢复后的
        调用再溢出就直接抛给 REPL——每步只重试一次，不反复烧钱。
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

    def _chat(self) -> tuple[dict, dict | None]:
        """一次流式模型调用：思考与正文各占一行（均带 助手> 前缀）。

        返回 (完整消息, 本次用量)；用量由 llm 层从流中提取，服务商
        不提供时为 None。渲染交给 renderer（CLI 逐字打印 / TUI 进组件）。
        思考内容（reasoning_content，仅部分模型返回）以灰色实时展示，
        但不写入会话——多数 OpenAI 兼容服务不接受它被回传。
        """
        msg = {}
        usage = None
        r = renderer.current()
        for kind, payload in self.llm.chat_stream(self.session.messages, tools=SCHEMAS):
            if kind == "message":
                msg = payload
            elif kind == "usage":
                usage = payload
            else:
                r.stream(kind, payload)
        r.stream_done()
        return msg, usage

    def _execute(self, tc: dict) -> tuple[str, bool]:
        """执行一个工具调用，返回 (结果文本, 是否权限被拒)。

        权限被拒时由 run() 终止整个任务循环（拒绝即停，不与模型继续拉扯）。
        """
        name = tc["function"]["name"]
        args_json = tc["function"]["arguments"]

        try:
            args = json.loads(args_json or "{}")
        except json.JSONDecodeError as e:
            tool_id = renderer.current().tool_call(f"[Tool] {name}({args_json[:80]})")
            return self._finish(f"错误: JSONDecodeError: {e}", tool_id), False

        line = self._describe(name, args)
        display = DISPLAY.get(name, "inline")
        tool_id = renderer.current().tool_call(
            line[:MAX_SUMMARY_LEN] + ("..." if len(line) > MAX_SUMMARY_LEN else ""), display
        )

        # 多路径工具（如 apply_patch）：从参数提取目标路径，逐路径预检 + 聚合权限检查
        extractor = PATHS_EXTRACTORS.get(name)
        if extractor is not None:
            try:
                paths = [str(p) for p in extractor(args)]
            except Exception as e:  # noqa: BLE001
                return self._finish(f"错误: 无法解析目标路径: {type(e).__name__}: {e}", tool_id), False
            if paths:
                return self._execute_with_paths(name, args, paths, tool_id)
        if name == "todo_write":
            return self._execute_todo(args, tool_id)
        return self._execute_single(name, args, tool_id)

    def _execute_with_paths(self, name: str, args: dict, paths: list[str],
                            tool_id: int | None = None) -> tuple[str, bool]:
        """多路径工具：任一路径越界被拒则整体拒绝；聚合权限检查；整体原子执行。

        路径预检（根信任门）→ 聚合操作权限门 → 执行，两道关卡有序，与单路径工具一致。
        """
        widened = []
        for raw in paths:
            pre = self._preflight_path(raw)
            if pre == "deny":
                return self._finish(DENIED_RESULT, tool_id), True
            if isinstance(pre, Path):
                widened.append(pre)

        if not self.permission.check_paths(name, paths):
            return self._finish(DENIED_RESULT, tool_id), True

        snapshot = _diff_preview(name, args)  # 执行前快照（apply_patch 暂无 preview，为空串）
        if snapshot:
            renderer.current().tool_preview(tool_id, snapshot)
        try:
            with config.widen_roots(widened):
                result = str(FUNCTIONS[name](**args))
        except Exception as e:  # noqa: BLE001
            # 工具执行的任何失败都只作为结果回传给模型，不中断循环
            result = f"错误: {type(e).__name__}: {e}"

        return self._finish(result, tool_id, name), False

    def _execute_single(self, name: str, args: dict, tool_id: int | None = None) -> tuple[str, bool]:
        # 变更预览（diff）在路径预检 / 权限确认 / 执行之前推送到工具调用块：
        # 审核时改动内容已经可见，权限框保持纯净
        snapshot = _diff_preview(name, args)
        if snapshot:
            renderer.current().tool_preview(tool_id, snapshot)

        # 路径预检：目标在授权目录之外时先请用户确认（目录信任 → 操作权限，两道关卡有序）
        preflight = self._preflight_outside_path(args)
        if preflight == "deny":
            return self._finish(DENIED_RESULT, tool_id), True

        denied = False
        try:
            if not self.permission.check(name, args):
                result = DENIED_RESULT
                denied = True
            elif isinstance(preflight, Path):
                with config.widen_roots([preflight]):
                    result = str(FUNCTIONS[name](**args))
            else:
                result = str(FUNCTIONS[name](**args))
        except Exception as e:  # noqa: BLE001
            # 工具执行的任何失败都只作为结果回传给模型，不中断循环
            result = f"错误: {type(e).__name__}: {e}"

        return self._finish(result, tool_id, name), denied

    def _execute_todo(self, args: dict, tool_id: int | None = None) -> tuple[str, bool]:
        """todo_write 专用执行路径：计划无论 display_mode 都必须完整展示，
        不走 tool_result 的粒度分支（summary 模式也不能只留一行摘要）。"""
        if not self.permission.check("todo_write", args):
            return self._finish(DENIED_RESULT, tool_id), True
        try:
            result = str(FUNCTIONS["todo_write"](**args))
        except Exception as e:  # noqa: BLE001
            result = f"错误: {type(e).__name__}: {e}"
            renderer.current().tool_result(result, tool_id)
            return result, False
        renderer.current().plan(summary(), render_current(color=True))
        return result, False

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
