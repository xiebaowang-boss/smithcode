"""ask_user 工具：让 Agent 在任务中途暂停并向用户提问。

用户的回答通过工具结果回传给模型，供其继续决策。与 REPL 共用
read_user_input（多行粘贴自动合并），非交互 stdin 下 fail-closed 取消，
避免在管道/CI 场景里阻塞等待输入。
"""
from ..utils.terminal import (
    confirmations_available,
    flush_pending_input,
    read_user_input,
)
from .base import register

_CANCELLED = "（非交互模式，无法向用户提问，已取消。请基于已有信息自行决策或继续。）"


@register(
    {
        "name": "ask_user",
        "description": "向用户提问并等待回答（最后手段：仅当已做过实际工作、"
        "且确实需要用户决策才能继续时使用；任务刚开始还没动手时尽量先自己动手）。"
        "用户的回答会作为工具结果返回。非交互模式下无法提问，会提示已取消。",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "要问用户的问题，尽量具体，一句话说完"},
            },
            "required": ["question"],
        },
    }
)
def ask_user(question: str) -> str:
    if not confirmations_available():
        return _CANCELLED
    flush_pending_input()  # 丢弃缓冲区内提前键入/粘贴的内容，防止被误当成回答
    print(f"\n[提问] {question}")
    answer = read_user_input(prompt="回答> ").strip()
    return answer or "（用户未输入内容）"