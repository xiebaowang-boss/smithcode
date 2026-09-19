"""技能的会话级状态：发现结果缓存、加载集合与生命周期。

与 plan.py / goal.py 同款"进程内单例（会话口径）"：
- refresh() 在 Agent.start() 与 /skills refresh 时重扫磁盘；
- reset() 在 /new 时清空加载集合（发现结果与信任决定保留，省 IO）；
- activate() 返回第 2 层载荷，由调用方投递进对话历史（工具结果或 user 消息）；
- prune_active() 在压缩后剔除正文已被摘要掉的技能（agent.compact 调用）；
- render_section() 被 Session.sync_system() 每轮调用，只渲染目录段，未发现过时
  返回空串而不是触发磁盘扫描——系统提示词装配是纯读路径，不做 IO（技能由
  Agent.start 装载）。
"""
from __future__ import annotations

from .. import config
from . import registry, render


class SkillsState:
    """一个会话的技能状态（原模块级单例 `_skills` / `_index` / `_active` …）。

    发现结果（`skills` / `index` / `diagnostics` / `settings`）事实上是进程级的
    （同一份技能目录），但**激活集合 `active` 属于会话**（`/new` 清空、恢复时按
    `t=state` 读回）。整体放进一个实例，换来的是"会话持有的状态只有一处"。
    """

    def __init__(self) -> None:
        self.skills: list = []
        self.index: dict = {}
        self.active: list = []
        self.diagnostics: list = []
        self.settings = None
        self.loaded = False

    def inherit(self, other: SkillsState) -> None:
        """接管另一个实例的状态（会话建立时从默认实例接过来）。

        发现结果（`skills` / `index` / `diagnostics` / `settings` / `loaded`）事实上
        是进程级的，直接共享引用（`refresh()` 会整体替换）；**激活集合**是会话级的，
        复制一份，避免两个会话共享同一个列表。
        """
        self.skills = other.skills
        self.index = other.index
        self.diagnostics = other.diagnostics
        self.settings = other.settings
        self.loaded = other.loaded
        self.active = list(other.active)


    def reset(self) -> None:
        """`/new`：清空激活集合；发现结果与信任决定保留。"""
        self.active.clear()

    def snapshot(self) -> list:
        """会话级激活集合快照（持久化投影缓存用）。"""
        return list(self.active)

    def restore(self, names) -> None:
        """从快照恢复激活集合：仅保留当前发现结果中可用且未禁用的技能。"""
        self.active[:] = [
            name
            for name in (names or [])
            if isinstance(name, str) and name in self.index and not self.index[name].disabled
        ]

# 进程级默认实例：没有会话绑定时的落点（见 `bind` 的说明）
_default_state = SkillsState()
_active_state = _default_state


def bind(state: SkillsState | None) -> None:
    """切换本模块函数作用的状态实例（`None` = 回到默认实例）。"""
    global _active_state
    _active_state = state if state is not None else _default_state


def refresh() -> list:
    """重新发现全部技能（含信任门控），同步只读白名单，返回诊断列表。"""
    _active_state.settings = config.load_skills_config()
    result = registry.discover(_active_state.settings)
    if result.enabled:
        preview = [s for s in result.skills if s.scope == "project"]
        if preview and not registry.resolve_project_trust(
            _active_state.settings, preview, result.diagnostics
        ):
            result.skills = [s for s in result.skills if s.scope != "project"]
    _active_state.skills = result.skills
    _active_state.index = {s.name: s for s in _active_state.skills}
    _active_state.diagnostics = result.diagnostics
    _active_state.loaded = True
    _active_state.active[:] = [n for n in _active_state.active if n in _active_state.index and not _active_state.index[n].disabled]
    _sync_read_roots()
    return list(_active_state.diagnostics)





def clear() -> None:
    """彻底清空全部状态与只读白名单（测试、进程内完全重载用）。"""
    _active_state.skills = []
    _active_state.index = {}
    _active_state.active.clear()
    _active_state.diagnostics = []
    _active_state.settings = None
    _active_state.loaded = False
    config.set_skill_roots([])
    registry.reset_session_trust()


def ensure() -> None:
    """按需发现（命令、激活等显式入口用；系统提示词装配不走这里）。"""
    if not _active_state.loaded:
        refresh()


def is_loaded() -> bool:
    return _active_state.loaded


def current_settings():
    """已装载的 [skills] 配置；从未刷新过时为 None。"""
    return _active_state.settings


def all_skills() -> list:
    ensure()
    return list(_active_state.skills)


def model_skills() -> list:
    """可进入系统提示词目录与 use_skill 枚举的技能。"""
    ensure()
    return [s for s in _active_state.skills if s.model_invocable and not s.disabled]


def get(name: str):
    ensure()
    return _active_state.index.get(name)


def is_active(name: str) -> bool:
    return name in _active_state.active


def active_names() -> list:
    return list(_active_state.active)


def diagnostics() -> list:
    return list(_active_state.diagnostics)


def activate(name: str, by: str = "model") -> str:
    """加载技能：登记进加载集合，首次加载返回第 2 层载荷（正文块）。

    载荷由调用方投递：`use_skill` 作为工具结果返回，技能名命令（`/技能名`）作为
    一条 user 消息注入会话历史——系统提示词只保留「可用技能」目录，不随加载
    变化，所以提示前缀缓存在会话内全程稳定。重复加载不重复注入正文（幂等）：
    模型通道（`use_skill` 工具结果）回一句已加载提示，模型照着去历史里找正文；
    用户通道（技能名命令）的重复语义由命令层决定（静默开跑 + 历史回找引导，
    不向用户打印）；失败返回 `错误: ...`。
    """
    ensure()
    skill = _active_state.index.get(name)
    if skill is None:
        available = "、".join(s.name for s in model_skills()) or "（无）"
        return f"错误: 未找到技能 {name}。可用技能: {available}"
    if skill.disabled:
        return f"错误: 技能 {name} 已被配置禁用（规则 {skill.disabled_by}）"
    if not skill.model_invocable and by == "model":
        return f"错误: 技能 {name} 仅允许用户手动加载（输入 /{name} 加载）"
    if name in _active_state.active:
        if by == "user":
            # 命令层专用：调用方据 `is_payload` 为假判定重复；正recall_notice文见 render.recall_notice。
            return f"__already_loaded__:{name}"
        return (
            f"技能 {name} 已加载，完整指令已在本会话的对话历史中（本次调用之前"
            f"返回的载荷消息，以“以下为技能「{name}」的完整指令”开头）。"
            f"请先在历史中找到它并按其中步骤执行；历史中找不到完整载荷时，"
            f"用 read_file 读取 {skill.location}（技能目录: {skill.base}）。"
        )
    _active_state.active.append(name)
    return _fit_payload(render.payload(skill), skill)


def _fit_payload(text: str, skill) -> str:
    """载荷长度护栏：超上限时保留头部并给出读取指引。

    只留头部而不是头尾各半（`truncate_output` 的做法）：技能指令被从中间切碎
    比缺尾部更糟，头部已含前言、`<skill>` 包装与资源清单。尾部正文可用
    read_file 从技能文件取回（技能根目录在只读白名单内）；末尾补回 `</skill>`
    保持包装闭合，长度精确落在上限内，工具结果的通用截断随即成为 no-op。
    """
    limit = config.MAX_TOOL_OUTPUT
    if limit <= 0 or len(text) <= limit:
        return text
    note = (
        f"\n\n（正文过长已截断；完整指令见 {skill.location}，可用 read_file 读取。）\n</skill>"
    )
    budget = limit - len(note)
    if budget <= 0:
        return note.strip()
    return text[:budget] + note


def prune_active(messages) -> list:
    """压缩后剔除正文已不在上下文中的技能，返回被剔除的名字（保序）。

    判定从严：只有载荷完整出现在某条消息里才算仍然可用——中段会被摘要替换、
    尾部超长 tool 消息会被截断，两者都意味着模型已经看不到完整指令。宁可多
    提示一次重载（幂等、只多花 token），也不让模型以为手上还有看不见的指令。
    """
    text = "\n".join(
        message.get("content")
        for message in messages
        if isinstance(message.get("content"), str)
    )
    dropped: list = []
    for name in list(_active_state.active):
        skill = _active_state.index.get(name)
        if skill is not None and render.payload(skill) in text:
            continue
        _active_state.active.remove(name)
        dropped.append(name)
    return dropped


def _sync_read_roots() -> None:
    """把技能根目录登记为只读白名单（读工具放行、写工具不认）。"""
    roots: list = []
    for skill in _active_state.skills:
        if skill.root not in roots:
            roots.append(skill.root)
    config.set_skill_roots(roots)


def reset(*args, **kwargs):
    """对**当前绑定的实例**做 reset（见 `bind`）；会话内的等价调用用
    `AgentSession` 持有的实例，避免依赖绑定状态。"""
    return _active_state.reset(*args, **kwargs)


def snapshot(*args, **kwargs):
    """对**当前绑定的实例**做 snapshot（见 `bind`）；会话内的等价调用用
    `AgentSession` 持有的实例，避免依赖绑定状态。"""
    return _active_state.snapshot(*args, **kwargs)


def restore(*args, **kwargs):
    """对**当前绑定的实例**做 restore（见 `bind`）；会话内的等价调用用
    `AgentSession` 持有的实例，避免依赖绑定状态。"""
    return _active_state.restore(*args, **kwargs)


def default_state() -> SkillsState:
    """进程级默认实例（无会话绑定时的作用对象，会话建立时从其继承）。"""
    return _default_state


def active_state() -> SkillsState:
    """当前绑定生效的实例（会话建立时从它接管状态，见 AgentSession）。"""
    return _active_state
