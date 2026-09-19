"""系统提示词的动态段装配（"transcript 拥有 prompt 分段"，方案 §9）。

每个会话作用域的子系统自己提供 `render_section()`（项目约定 / 技能目录 / 持久
目标），本模块把它们按 `order` 排好交给 `llm.prompts.build_system_prompt`。

**为什么要有这一层**：改动之前，段是 `session.sync_system()` 里的三个具名实参、
一路传到 `build_system_prompt(instructions_section=…, skills_section=…, goal_section=…)`
——新增一段要同时改两个函数。现在只需在这里多注册一行。

注册放在本模块（而不是各子系统内部）：子系统在别处被 import 时也会注册，注册
时机随 import 顺序漂移；集中一处让"提示词里有哪些段、顺序如何"一眼可见。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .. import goal, instructions, skills
from ..llm.prompts import build_system_prompt


@dataclass(frozen=True)
class Section:
    """一个动态段：`order` 决定位置（越小越靠前），`render` 产出该段文本。"""

    name: str
    order: int
    render: Callable[[], str]


_sections: list[Section] = []


def register(section: Section) -> None:
    """注册一个段（同名替换，保证重复注册不产生两份）。"""
    _sections[:] = [existing for existing in _sections if existing.name != section.name]
    _sections.append(section)


def unregister(name: str) -> None:
    _sections[:] = [existing for existing in _sections if existing.name != name]


def sections() -> list[Section]:
    """按 order 排序的段列表（快照）。"""
    return sorted(_sections, key=lambda section: section.order)


def render_sections() -> list[str]:
    """各段文本（空串保留——由 `build_system_prompt` 决定跳过）。"""
    return [section.render() for section in sections()]


def assemble_system_prompt() -> str:
    """按注册表拼装系统提示词（`Session.sync_system()` 的唯一入口）。"""
    return build_system_prompt(render_sections())


# 三个默认段，顺序即优先级阶梯：项目约定 → 技能目录 → 持久目标
register(Section("instructions", 10, instructions.render_section))
register(Section("skills", 20, skills.render_section))
register(Section("goal", 30, goal.render_section))
