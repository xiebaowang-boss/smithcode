from __future__ import annotations

import json
from pathlib import Path

from . import config
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
from .permission import Permission
from .session import Session
from .tools import DESCRIBERS, FUNCTIONS, PATHS_EXTRACTORS, SCHEMAS
from .usage import format_call

# ANSI 转义：思考内容以灰色展示（90m 比 dim/2m 在 Windows 终端上兼容性好得多）
DIM, RESET = "\033[90m", "\033[0m"

# 工具调用短摘要行（如 `read src/agent.py`）的最大显示宽度，超出截断
MAX_SUMMARY_LEN = 80


class Agent:
    def __init__(self, session: Session | None = None, max_iterations: int | None = None):
        self.llm = LLMClient()
        self.session = session or Session()
        self.permission = Permission()
        self.context = ContextMeter()  # 上下文快照计量：真实锚点 + 临近阈值提醒
        self.max_iterations = max_iterations or config.MAX_ITERATIONS
        self.display_mode = config.load_tool_display()  # 工具调用展示粒度：summary / detail

    def run(self, user_input: str) -> str:
        self.session.add("user", user_input)

        for _ in range(self.max_iterations):
            self._compact_if_needed()
            msg, usage = self._chat_with_recovery()
            self.session.usage.add(usage)
            self.context.record(usage)  # 记下真实 prompt_tokens 作估算锚点
            if isinstance(usage, dict):
                # 每次交互一行：输入即该次请求的上下文，随任务增长可见轨迹
                print(f"  [tokens] {format_call(usage)}")
            self.session.messages.append(msg)

            if not msg.get("tool_calls"):
                return msg.get("content", "")

            for tc in msg["tool_calls"]:
                result = self._execute(tc)
                self.session.messages.append(
                    {"role": "tool", "content": result, "tool_call_id": tc["id"]}
                )

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
        print("\n[context] 上下文溢出，压缩后重试…")
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
            print("[context] 摘要未按模板生成，放弃本次压缩，原样继续")
            return False

        self.session.messages = assemble(
            messages[0].get("content", ""), summary, messages[tail_start:]
        )
        self.context.compact_count += 1
        print(f"\n[context] 已压缩: {before:,} → {total_tokens(self.session.messages):,} tokens")
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
        不提供时为 None。思考内容（reasoning_content，仅部分模型返回）
        以灰色实时展示，但不写入会话——多数 OpenAI 兼容服务不接受它被回传。
        """
        msg = {}
        usage = None
        mode = None  # 当前流式内容类型："reasoning" / "content"
        for kind, payload in self.llm.chat_stream(self.session.messages, tools=SCHEMAS):
            if kind == "message":
                msg = payload
            elif kind == "usage":
                usage = payload
            else:
                if kind != mode:
                    if mode == "reasoning":  # 思考段结束，恢复正常样式
                        print(RESET, end="", flush=True)
                    print("\n助手> ", end="", flush=True)
                    if kind == "reasoning":
                        print(f"{DIM}[Thinking] ", end="", flush=True)
                    mode = kind
                print(payload, end="", flush=True)
        if mode == "reasoning":  # 流在思考段中结束（如模型直接发起工具调用）
            print(RESET, end="")
        if mode is not None:
            print()
        return msg, usage

    def _execute(self, tc: dict) -> str:
        name = tc["function"]["name"]
        args_json = tc["function"]["arguments"]

        try:
            args = json.loads(args_json or "{}")
        except json.JSONDecodeError as e:
            print(f"  [Tool] {name}({args_json[:80]})")
            return self._finish(f"错误: JSONDecodeError: {e}")

        line = self._describe(name, args)
        print(f"  {line[:MAX_SUMMARY_LEN]}{'...' if len(line) > MAX_SUMMARY_LEN else ''}")

        # 多路径工具（如 apply_patch）：从参数提取目标路径，逐路径预检 + 聚合权限检查
        extractor = PATHS_EXTRACTORS.get(name)
        if extractor is not None:
            try:
                paths = [str(p) for p in extractor(args)]
            except Exception as e:  # noqa: BLE001
                return self._finish(f"错误: 无法解析目标路径: {type(e).__name__}: {e}")
            if paths:
                return self._execute_with_paths(name, args, paths)
        return self._execute_single(name, args)

    def _execute_with_paths(self, name: str, args: dict, paths: list[str]) -> str:
        """多路径工具：任一路径越界被拒则整体拒绝；聚合权限检查；整体原子执行。

        路径预检（根信任门）→ 聚合操作权限门 → 执行，两道关卡有序，与单路径工具一致。
        """
        widened = []
        for raw in paths:
            pre = self._preflight_path(raw)
            if pre == "deny":
                return self._finish("用户拒绝了此操作")
            if isinstance(pre, Path):
                widened.append(pre)

        if not self.permission.check_paths(name, paths):
            return self._finish("用户拒绝了此操作")

        try:
            with config.widen_roots(widened):
                result = str(FUNCTIONS[name](**args))
        except Exception as e:  # noqa: BLE001
            # 工具执行的任何失败都只作为结果回传给模型，不中断循环
            result = f"错误: {type(e).__name__}: {e}"

        return self._finish(result)

    def _execute_single(self, name: str, args: dict) -> str:
        # 路径预检：目标在授权目录之外时先请用户确认（目录信任 → 操作权限，两道关卡有序）
        preflight = self._preflight_outside_path(args)
        if preflight == "deny":
            return self._finish("用户拒绝了此操作")

        try:
            if not self.permission.check(name, args):
                result = "用户拒绝了此操作"
            elif isinstance(preflight, Path):
                with config.widen_roots([preflight]):
                    result = str(FUNCTIONS[name](**args))
            else:
                result = str(FUNCTIONS[name](**args))
        except Exception as e:  # noqa: BLE001
            # 工具执行的任何失败都只作为结果回传给模型，不中断循环
            result = f"错误: {type(e).__name__}: {e}"

        return self._finish(result)

    def _finish(self, result: str) -> str:
        """回传前截断超长输出；终端展示按 display_mode 分支：summary 模式只有
        执行前那行短摘要，detail 模式追加结果内容。失败信息（错误/用户拒绝）
        无论何种模式都原样展示——失败的细节比格式化摘要更重要。"""
        result = truncate_output(result, config.MAX_TOOL_OUTPUT)
        if result.startswith("错误:") or result == "用户拒绝了此操作":
            print(f"  {result}\n")
            return result
        if self.display_mode == "detail":
            display = result[:500] + ("..." if len(result) > 500 else "")
            print(f"  [Result] {display}\n")
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
