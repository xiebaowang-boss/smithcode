from __future__ import annotations

import json

from . import config
from .llm import LLMClient
from .permission import Permission
from .session import Session
from .tools import FUNCTIONS, SCHEMAS

# ANSI 转义：思考内容以暗色展示（terminal.py 已在 Windows 上启用解析）
DIM, RESET = "\033[2m", "\033[0m"


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
        """一次流式模型调用：正文逐字打印，返回组装好的完整消息。

        思考内容（reasoning_content，仅部分模型返回）以暗色实时展示，
        但不写入会话——多数 OpenAI 兼容服务不接受它被回传。
        """
        msg = {}
        mode = None  # 当前流式内容类型："reasoning" / "content"
        shown = False  # 本轮是否输出过可见内容（决定收尾换行）
        for kind, payload in self.llm.chat_stream(self.session.messages, tools=SCHEMAS):
            if kind == "message":
                msg = payload
                continue
            if not shown:
                print("\n助手> ", end="", flush=True)
                shown = True
            if kind != mode:
                if mode == "reasoning":  # 思考结束，恢复常规样式
                    print(RESET, end="", flush=True)
                if kind == "reasoning":
                    print(f"{DIM}[思考] ", end="", flush=True)
                mode = kind
            print(payload, end="", flush=True)
        if shown:
            print()
        return msg

    def _execute(self, tc: dict) -> str:
        name = tc["function"]["name"]
        args_json = tc["function"]["arguments"]
        print(f"  [Tool] {name}({args_json[:80]})")

        try:
            # 先解析参数，权限引擎需要用参数（路径/命令）做模式匹配
            args = json.loads(args_json or "{}")
            if not self.permission.check(name, args):
                result = "用户拒绝了此操作"
            else:
                result = str(FUNCTIONS[name](**args))
        except Exception as e:  # noqa: BLE001
            # 工具执行的任何失败都只作为结果回传给模型，不中断循环
            result = f"错误: {type(e).__name__}: {e}"

        result = truncate_output(result, config.MAX_TOOL_OUTPUT)
        display = result[:500] + ("..." if len(result) > 500 else "")
        print(f"  [Result] {display}\n")
        return result
