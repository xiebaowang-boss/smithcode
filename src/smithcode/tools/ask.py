"""ask_user 工具：让 Agent 在任务中途暂停并向用户提一个或多个问题。

入参为复数 `questions`（1-4 项，每项含 `question` 与 `options`），一次调用即可把相关
的多个决策一起问完，避免来回打断。**每题必须给 1-5 个候选项 options**（强制遵守）：
用户按键即选，另有一行「输入自定义回答」可自由输入，因此无需再留"无选项的纯文本题"。
multiple=True 表示该题可多选（可同时勾选多个选项，并与自定义回答合并计入答案）。
工具把入参归一化后交给 renderer 的 `ask_form`：CLI 逐题串行提问，TUI 用一个面板承载
全部问题并支持手动切题（←/→ 或 Tab 切换）。用户的回答通过工具结果回传给模型：单题
直接返回答案，多题返回编号列表。渲染交给 renderer（CLI 编号选择，TUI 方向键/数字键
选择），非交互 stdin 下 fail-closed 取消，避免在管道/CI 场景里阻塞等待输入。
"""
from __future__ import annotations

from .. import renderer
from ..utils.terminal import confirmations_available
from .base import register

_CANCELLED = "（非交互模式，无法向用户提问，已取消。请基于已有信息自行决策或继续。）"
_EMPTY = "（用户未回答）"
_CANCELLED_MARK = "（已取消）"
_BAD_ARGS = "错误: questions 不能为空，请至少给出一个 {question, options, multiple?} 对象。"
_MAX_OPTIONS = 5  # 每题候选项上限（强制遵守）


def _describe(args: dict) -> str:
    """终端摘要：单题显示题干，多题显示「首题 等 N 项」。"""
    questions = args.get("questions") or []
    first = ""
    if questions and isinstance(questions[0], dict):
        first = str(questions[0].get("question") or "").strip()
    if len(questions) > 1:
        return f"提问：{first or '?'} 等 {len(questions)} 项"
    return f"提问：{first or '?'}"


def _normalize(questions: list[dict] | None) -> list[dict]:
    """把模型入参拍平成 renderer.ask_form 认识的统一结构。

    每项输出 {question, options: [label...], descriptions: [说明...], multiple}；
    缺题干/非 dict 的项被跳过（模型偶发脏数据时不抛异常，交由调用方判空兜底）。
    候选项按上限 `_MAX_OPTIONS` 截断（强制遵守 1-5 项，模型多给时丢弃多余）。
    """
    normalized = []
    for item in questions or []:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        if not question:
            continue
        choices, descriptions = [], []
        for opt in item.get("options") or []:
            if isinstance(opt, dict) and opt.get("label"):
                choices.append(str(opt["label"]))
                descriptions.append(str(opt.get("description") or ""))
        normalized.append({
            "question": question,
            "options": choices[:_MAX_OPTIONS],
            "descriptions": descriptions[:_MAX_OPTIONS],
            "multiple": bool(item.get("multiple", False)),
        })
    return normalized


def _format(questions: list[dict], answers: list[str]) -> str:
    """把逐题答案拼成回传给模型的文本：单题原样返回，多题编号列出。"""
    if len(questions) == 1:
        return answers[0] or _EMPTY
    lines = []
    for index, (item, answer) in enumerate(zip(questions, answers), 1):
        lines.append(f"{index}. {item['question']} → {answer or _CANCELLED_MARK}")
    return "\n".join(lines)


@register(
    {
        "name": "ask_user",
        "serial": True,  # 终端交互抢 stdin，必须独占主线程
        "display": "block",  # 结果（用户回答）单独成块展示，不塞进摘要行
        "describe": _describe,
        "description": "向用户提问并等待回答（最后手段：仅当已做过实际工作、"
        "且确实需要用户决策才能继续时使用；任务刚开始还没动手时尽量先自己动手）。"
        "一次可提 1-4 个问题：把相关的多个决策放进 questions 一起问完，避免来回打断。"
        "【强制】每题必须提供 1-5 个候选项 options（每项一句话），少于 1 个或多于 5 个"
        "都视为违规；用户按键即选，界面上另有一行「输入自定义回答」可自由输入，"
        "所以不需要留无选项的纯文本题。"
        "multiple=True 表示该题可多选；"
        "用户的回答会作为工具结果返回（单题直接返回答案，多题按编号列出）。"
        "非交互模式下无法提问，会提示已取消。",
        "parameters": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "minItems": 1,
                    "description": "要问用户的 1-4 个问题；把相关的多个决策一次问完，避免来回打断",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string", "description": "要问用户的问题，尽量具体，一句话说完"},
                            "options": {
                                "type": "array",
                                "description": "该题的候选项，必填且必须为 1-5 项（强制遵守），每项一句话",
                                "minItems": 1,
                                "maxItems": 5,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string", "description": "选项短文本（回传给模型的就是它）"},
                                        "description": {"type": "string", "description": "选项的补充说明（可选，展示用）"},
                                    },
                                    "required": ["label"],
                                },
                            },
                            "multiple": {"type": "boolean", "description": "该题是否允许多选（默认单选）"},
                        },
                        "required": ["question", "options"],
                    },
                },
            },
            "required": ["questions"],
        },
    }
)
def ask_user(questions: list[dict]) -> str:
    if not confirmations_available():
        return _CANCELLED
    normalized = _normalize(questions)
    if not normalized:
        return _BAD_ARGS
    answers = renderer.current().ask_form(normalized)
    return _format(normalized, answers)
