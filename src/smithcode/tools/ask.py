"""ask_user 工具：让 Agent 在任务中途暂停并向用户提问。

支持 opencode 式选项提问：模型可给出 2-4 个候选项（options），用户按键
即答，也可选"自定义回答"自由输入；不传 options 时退化为纯文本提问（老
行为不变）。multiple=True 允许多选。用户的回答通过工具结果回传给模型，
供其继续决策。渲染交给 renderer（CLI 编号选择，TUI 方向键/数字键选择），
非交互 stdin 下 fail-closed 取消，避免在管道/CI 场景里阻塞等待输入。
"""
from __future__ import annotations

from .. import renderer
from ..utils.terminal import confirmations_available
from .base import register

_CANCELLED = "（非交互模式，无法向用户提问，已取消。请基于已有信息自行决策或继续。）"
_EMPTY = "（用户未回答）"


@register(
    {
        "name": "ask_user",
        "description": "向用户提问并等待回答（最后手段：仅当已做过实际工作、"
        "且确实需要用户决策才能继续时使用；任务刚开始还没动手时尽量先自己动手）。"
        "有明确候选方案时提供 options（2-4 项，每项一句话），用户可直接按编号选择，"
        "回答更快；没有合适候选时省略 options 让用户自由输入。"
        "multiple=True 表示可多选。用户的回答（所选项或自定义文本）会作为工具结果返回。"
        "非交互模式下无法提问，会提示已取消。",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "要问用户的问题，尽量具体，一句话说完"},
                "options": {
                    "type": "array",
                    "description": "候选选项（可选，2-4 项效果最好）；用户也可跳过选项自由输入",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "description": "选项短文本（回传给模型的就是它）"},
                            "description": {"type": "string", "description": "选项的补充说明（可选，展示用）"},
                        },
                        "required": ["label"],
                    },
                },
                "multiple": {"type": "boolean", "description": "是否允许多选（默认单选）"},
            },
        },
    }
)
def ask_user(question: str, options: list[dict] | None = None, multiple: bool = False) -> str:
    if not confirmations_available():
        return _CANCELLED
    choices = []
    for opt in options or []:
        if isinstance(opt, dict) and opt.get("label"):
            choices.append(str(opt["label"]))
    if not choices:
        answer = renderer.current().ask_text(question)
        return answer or _EMPTY
    answer = renderer.current().ask_choice(question, choices, multiple)
    return answer or _EMPTY
