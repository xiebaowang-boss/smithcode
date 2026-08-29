import json

from . import config
from .llm import LLMClient, message_to_dict
from .permission import Permission
from .session import Session
from .tools import FUNCTIONS, SCHEMAS


class Agent:
    def __init__(self, session: Session = None, max_iterations: int = None):
        self.llm = LLMClient()
        self.session = session or Session()
        self.permission = Permission()
        self.max_iterations = max_iterations or config.MAX_ITERATIONS

    def run(self, user_input: str) -> str:
        self.session.add("user", user_input)

        for _ in range(self.max_iterations):
            response = self.llm.chat(self.session.messages, tools=SCHEMAS)
            msg_dict = message_to_dict(response)
            self.session.messages.append(msg_dict)

            if not msg_dict.get("tool_calls"):
                return msg_dict.get("content", "")

            for tc in msg_dict["tool_calls"]:
                result = self._execute(tc)
                self.session.messages.append(
                    {"role": "tool", "content": result, "tool_call_id": tc["id"]}
                )

        return "达到最大迭代次数，任务中止。"

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
        except Exception as e:
            result = f"错误: {type(e).__name__}: {e}"

        display = result[:500] + ("..." if len(result) > 500 else "")
        print(f"  [Result] {display}\n")
        return result

