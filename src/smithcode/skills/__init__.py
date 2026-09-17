"""Agent Skills（技能）子系统：发现 / 披露 / 激活 / 会话状态的公共入口。

设计文档见 docs/architecture.md 的「技能（Skills）」节。当前扫描范围（P1）：项目级
`<工作区>/.agents/skills/` 与用户级 `~/.smithcode/skills/`（外加 [skills].paths
配置目录），暂不兼容其他客户端的技能目录。

用法：
- 启动：Agent.start() -> refresh() 发现技能并同步 use_skill 工具；
- 每轮：session.sync_system() -> render_section() 把**目录段**拼进 messages[0]
  （未装载过时返回空串，不做磁盘 IO）；正文不在这里，它随加载进对话历史；
- 加载：use_skill 工具 / 技能名命令（`/技能名`）-> activate() 返回第 2 层载荷，
  由调用方投递（`use_skill` 直接作为工具结果，技能名命令作为一条 user 消息注入）；
- 压缩：Agent.compact() -> prune_active() 剔除正文已被摘要掉的技能；
- /new：reset() 清空加载集合。
"""
from __future__ import annotations

from . import render
from .registry import Skill  # noqa: F401 公共类型
from .state import (  # noqa: F401 公共 API
    activate,
    active_names,
    all_skills,
    clear,
    current_settings,
    diagnostics,
    ensure,
    get,
    is_active,
    is_loaded,
    model_skills,
    prune_active,
    refresh,
    reset,
    restore,
    snapshot,
)


def render_section() -> str:
    """系统提示词的技能动态段：仅「可用技能」目录（渐进式披露第 1 层）。

    技能正文不进系统提示词——它随 use_skill 的工具结果或技能名命令注入的 user
    消息进对话历史，系统提示词因此不随加载变化（提示前缀缓存全程稳定）。
    未装载（Agent.start 之前）或 [skills].enabled=false 时返回空串。
    """
    cfg = current_settings()
    if cfg is None or not cfg.enabled:
        return ""
    return render.catalog_section(model_skills(), cfg.max_catalog_chars)


def status_text() -> str:
    """/skills 命令文案；显式入口，允许触发按需发现。"""
    ensure()
    cfg = current_settings()
    enabled = bool(cfg.enabled) if cfg is not None else True
    return render.status_text(all_skills(), active_names(), diagnostics(), enabled)
