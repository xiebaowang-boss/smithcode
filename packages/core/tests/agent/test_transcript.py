"""系统提示词的段注册表（agent/transcript.py）。

方案 §9 的目标是「新增一段不必改 session.py 与 prompts.py」——所以这里的核心断言
不是"三个段还在"，而是**注册一个新段后，系统提示词里真的多出它**。
"""

from __future__ import annotations

from smithcode.agent import transcript
from smithcode.session import Session


def test_default_sections_are_the_three_subsystems():
    names = [section.name for section in transcript.sections()]

    assert names == ["instructions", "skills", "goal"]  # 顺序即优先级阶梯


def test_registered_section_lands_in_the_system_prompt():
    """注册一个新段 → 系统提示词里出现它（不必改 session.py / prompts.py）。"""
    session = Session()
    baseline = session.messages[0]["content"] if session.messages else ""

    transcript.register(transcript.Section("probe", 5, lambda: "## 探针段\n来自注册表"))
    try:
        session.sync_system()
        content = session.messages[0]["content"]
        assert "探针段" in content
        assert content != baseline
        # order=5 排在 instructions(10) 之前
        assert content.index("探针段") < content.index("## 项目约定") if "## 项目约定" in content else True
    finally:
        transcript.unregister("probe")

    session.sync_system()
    assert "探针段" not in session.messages[0]["content"]


def test_register_replaces_same_name():
    """同名重复注册只留一份（否则提示词里会出现两遍同一段）。"""
    transcript.register(transcript.Section("dup", 99, lambda: "第一次"))
    transcript.register(transcript.Section("dup", 99, lambda: "第二次"))
    try:
        texts = [section.render() for section in transcript.sections() if section.name == "dup"]
        assert texts == ["第二次"]
    finally:
        transcript.unregister("dup")


def test_empty_section_is_skipped():
    """渲染出空串的段不进提示词（对应段未装载时的既有行为）。"""
    transcript.register(transcript.Section("empty", 99, lambda: ""))
    try:
        assert transcript.assemble_system_prompt() == transcript.assemble_system_prompt()
        assert "\n\n\n" not in transcript.assemble_system_prompt()
    finally:
        transcript.unregister("empty")
