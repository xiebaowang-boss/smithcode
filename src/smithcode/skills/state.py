"""技能的会话级状态：发现结果缓存、激活集合与生命周期。

与 plan.py / goal.py 同款"进程内单例（会话口径）"：
- refresh() 在 Agent.start() 与 /skills refresh 时重扫磁盘；
- reset() 在 /new 时清空激活集合（发现结果与信任决定保留，省 IO）；
- render_section() 被 Session.sync_system() 每轮调用，未发现过时返回空串而不是
  触发磁盘扫描——系统提示词装配是纯读路径，不做 IO（技能由 Agent.start 装载）。
"""
from __future__ import annotations

from .. import config
from . import registry

_skills: list = []
_index: dict = {}
_active: list = []
_diagnostics: list = []
_settings = None
_loaded = False


def refresh() -> list:
    """重新发现全部技能（含信任门控），同步只读白名单，返回诊断列表。"""
    global _skills, _index, _diagnostics, _settings, _loaded
    _settings = config.load_skills_config()
    result = registry.discover(_settings)
    if result.enabled:
        preview = [s for s in result.skills if s.scope == "project"]
        if preview and not registry.resolve_project_trust(
            _settings, preview, result.diagnostics
        ):
            result.skills = [s for s in result.skills if s.scope != "project"]
    _skills = result.skills
    _index = {s.name: s for s in _skills}
    _diagnostics = result.diagnostics
    _loaded = True
    _active[:] = [n for n in _active if n in _index and not _index[n].disabled]
    _sync_read_roots()
    return list(_diagnostics)


def reset() -> None:
    """`/new`：清空激活集合；发现结果与信任决定保留。"""
    _active.clear()


def snapshot() -> list:
    """会话级激活集合快照（持久化投影缓存用）。"""
    return list(_active)


def restore(names) -> None:
    """从快照恢复激活集合：仅保留当前发现结果中可用且未禁用的技能。"""
    _active[:] = [
        name
        for name in (names or [])
        if isinstance(name, str) and name in _index and not _index[name].disabled
    ]


def clear() -> None:
    """彻底清空全部状态与只读白名单（测试、进程内完全重载用）。"""
    global _skills, _index, _diagnostics, _settings, _loaded
    _skills = []
    _index = {}
    _active.clear()
    _diagnostics = []
    _settings = None
    _loaded = False
    config.set_skill_roots([])
    registry.reset_session_trust()


def ensure() -> None:
    """按需发现（命令、激活等显式入口用；系统提示词装配不走这里）。"""
    if not _loaded:
        refresh()


def is_loaded() -> bool:
    return _loaded


def current_settings():
    """已装载的 [skills] 配置；从未刷新过时为 None。"""
    return _settings


def all_skills() -> list:
    ensure()
    return list(_skills)


def model_skills() -> list:
    """可进入系统提示词目录与 use_skill 枚举的技能。"""
    ensure()
    return [s for s in _skills if s.model_invocable and not s.disabled]


def get(name: str):
    ensure()
    return _index.get(name)


def is_active(name: str) -> bool:
    return name in _active


def active_names() -> list:
    return list(_active)


def active_skills() -> list:
    ensure()
    return [_index[n] for n in _active if n in _index]


def diagnostics() -> list:
    return list(_diagnostics)


def activate(name: str, by: str = "model") -> str:
    """激活技能：标记进激活集合，正文由下一轮 sync_system 注入。

    by="model"（use_skill 工具）与 by="user"（/skill 命令）返回不同话术；
    幂等——重复激活不重复注入。
    """
    ensure()
    skill = _index.get(name)
    if skill is None:
        available = "、".join(s.name for s in model_skills()) or "（无）"
        return f"错误: 未找到技能 {name}。可用技能: {available}"
    if skill.disabled:
        return f"错误: 技能 {name} 已被配置禁用（规则 {skill.disabled_by}）"
    if not skill.model_invocable and by == "model":
        return f"错误: 技能 {name} 仅允许用户用 /skill {name} 手动加载"
    if name in _active:
        return f"技能 {name} 已激活，无需重复加载。"
    _active.append(name)
    if by == "user":
        return f"已加载技能 {name}，完整指令已注入系统提示词（技能目录: {skill.base}）。"
    return (
        f"技能 {name} 已激活，完整指令已写入系统提示词「已激活技能」段"
        f"（技能目录: {skill.base}）。请按其中步骤继续执行。"
    )


def _sync_read_roots() -> None:
    """把技能根目录登记为只读白名单（读工具放行、写工具不认）。"""
    roots: list = []
    for skill in _skills:
        if skill.root not in roots:
            roots.append(skill.root)
    config.set_skill_roots(roots)
