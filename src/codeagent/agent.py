from __future__ import annotations

import json
from pathlib import Path

from . import config
from .llm import LLMClient
from .permission import Permission
from .session import Session
from .tools import FUNCTIONS, SCHEMAS

# ANSI 转义：思考内容以灰色展示（90m 比 dim/2m 在 Windows 终端上兼容性好得多）
DIM, RESET = "\033[90m", "\033[0m"


def truncate_output(text: str, limit: int) -> str:
    """把超长的工具输出截断为头尾各半，中间用省略标记代替。

    报错信息常出现在输出末尾，保留尾部比只留头部更不容易丢关键信息。
    """
    if limit <= 0 or len(text) <= limit:
        return text
    half = limit // 2
    omitted = len(text) - limit
    return (
        f"{text[:half]}\n\n"
        f"[... 输出过长，已省略中间 {omitted} 字符 ...]\n\n"
        f"{text[-half:]}"
    )


class Agent:
    def __init__(self, session: Session | None = None, max_iterations: int | None = None):
        self.llm = LLMClient()
        self.session = session or Session()
        self.permission = Permission()
        self.max_iterations = max_iterations or config.MAX_ITERATIONS

    def run(self, user_input: str) -> str:
        self.session.add("user", user_input)

        for _ in range(self.max_iterations):
            msg = self._chat()
            self.session.messages.append(msg)

            if not msg.get("tool_calls"):
                return msg.get("content", "")

            for tc in msg["tool_calls"]:
                result = self._execute(tc)
                self.session.messages.append(
                    {"role": "tool", "content": result, "tool_call_id": tc["id"]}
                )

        return "达到最大迭代次数，任务中止。"

    def _chat(self) -> dict:
        """一次流式模型调用：思考与正文各占一行（均带 助手> 前缀），返回完整消息。

        思考内容（reasoning_content，仅部分模型返回）以灰色实时展示，
        但不写入会话——多数 OpenAI 兼容服务不接受它被回传。
        """
        msg = {}
        mode = None  # 当前流式内容类型："reasoning" / "content"
        for kind, payload in self.llm.chat_stream(self.session.messages, tools=SCHEMAS):
            if kind == "message":
                msg = payload
                continue
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
        return msg

    def _execute(self, tc: dict) -> str:
        name = tc["function"]["name"]
        args_json = tc["function"]["arguments"]
        print(f"  [Tool] {name}({args_json[:80]})")

        try:
            args = json.loads(args_json or "{}")
        except json.JSONDecodeError as e:
            return self._finish(f"错误: JSONDecodeError: {e}")

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
        result = truncate_output(result, config.MAX_TOOL_OUTPUT)
        display = result[:500] + ("..." if len(result) > 500 else "")
        print(f"  [Result] {display}\n")
        return result

    def _preflight_outside_path(self, args: dict) -> Path | str | None:
        """检查 path 参数是否落在授权目录之外；之外时先交互确认。

        返回 "deny"（用户拒绝本次访问）、Path（"仅本次"，执行时需临时放行该信任根）、
        None（路径在授权范围内，或用户已选"本会话总是"——信任根已入库）。
        与工具内部的越界检查互为备份：预检管交互体验，工具侧管强制执行。
        """
        raw = args.get("path")
        if raw is None:
            return None
        target = (Path(config.WORKSPACE_ROOT) / str(raw)).resolve()
        if any(target.is_relative_to(r) for r in config.allowed_roots()):
            return None
        action, root = self.permission.ask_outside_access(str(raw), target)
        if action == "deny":
            return "deny"
        return root if action == "once" else None
