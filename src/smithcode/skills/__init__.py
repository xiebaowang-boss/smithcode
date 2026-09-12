"""Agent Skills（技能）子系统：发现 / 披露 / 激活 / 会话状态的公共入口。

设计文档见 docs/skills-architecture.md。当前扫描范围（P1）：项目级
`<工作区>/.agents/skills/` 与用户级 `~/.smithcode/skills/`（外加 [skills].paths
配置目录），暂不兼容其他客户端的技能目录。

用法：
- 启动：Agent.start() -> refresh() 发现技能并同步 use_skill 工具；
- 每轮：session.sync_system() -> render_section() 把目录与已激活正文拼进
  messages[0]（未装载过时返回空串，不做磁盘 IO）；
- 激活：use_skill 工具 / /skill 命令 -> activate()；
- /new：reset() 清空激活集合。
"""
from __future__ import annotations

from . import render
from .registry import Skill  # noqa: F401 公共类型
from .state import (  # noqa: F401 公共 API
    activate,
    active_names,
    active_skills,
    all_skills,
    clear,
    current_settings,
    diagnostics,
    ensure,
    get,
    is_active,
    is_loaded,
    model_skills,
    refresh,
    reset,
    restore,
    snapshot,
)


def render_section() -> str:
    """系统提示词的技能动态段：可用技能目录 + 已激活技能正文。

    未装载（Agent.start 之前）或 [skills].enabled=false 时返回空串。
    """
    cfg = current_settings()
    if cfg is None or not cfg.enabled:
        return ""
    parts = [
        render.catalog_section(model_skills(), cfg.max_catalog_chars),
        render.active_section(active_skills()),
    ]
    return "\n\n".join(part for part in parts if part)


def status_text() -> str:
    """/skills 命令文案；显式入口，允许触发按需发现。"""
    ensure()
    cfg = current_settings()
    enabled = bool(cfg.enabled) if cfg is not None else True
    return render.status_text(all_skills(), active_names(), diagnostics(), enabled)
