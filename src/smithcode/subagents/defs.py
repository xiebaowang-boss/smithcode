"""子代理类型目录：内置类型 + 用户/项目文件发现（宽容 frontmatter，无第三方 YAML）。

定义来源与优先级（同名后者覆盖）：
- 内置：`explore`（只读侦察）与 `general`（通用执行），代码内定义；
- 用户级：`~/.smithcode/agents/*.md`；
- 项目级：`<工作区>/.smithcode/agents/*.md`，随仓库分发，加载前过项目信任门控
  （复用技能子系统的项目信任库 `skills_trust.json`）；
- 附加：`[subagents].paths` 配置目录（最高优先级）。

`[subagents].disabled` 可按名（通配）禁用任意类型；`enabled=false` 时目录为空，
task 工具随之隐藏。文件格式：frontmatter 声明 name / description / tools /
model / max_turns，正文为角色系统提示词。
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

from .. import config

AGENTS_SUBDIR = Path(".smithcode") / "agents"
MAX_SPEC_FILE_BYTES = 256_000

# 子代理一律不可用的工具：递归 / 用户交互 / 会话级单例状态（plan、goal、skills）
FORBIDDEN_TOOLS = frozenset({
    "task",
    "ask_user",
    "todo_write",
    "todo_read",
    "goal_update",
    "goal_read",
    "use_skill",
})

# 白名单里算作只读的工具：这些工具为主时允许进入并行波次
READONLY_EXTRA = frozenset({"webfetch", "websearch"})

_EXPLORE_PROMPT = """\
你是代码库侦察子代理：用搜索、阅读与网络查证定位信息，只产出结论。

工作方式：
1. 先广后窄：用 glob / grep 按文件名与内容检索，不要逐层翻目录；一次搜索命中过多时
   收紧关键词或限定路径。
2. 只读不写：不修改任何文件、不执行命令（工具集本身也不提供）；需要运行代码才能
   确认的结论标注为"未验证"。
3. 追根问底到能下结论为止：关键路径要读到真实代码（给出 `file_path:line_number`），
   不要停在文件名层面。
4. 查不到时如实说明查过哪些位置、为什么不确定，不要编造。"""

_GENERAL_PROMPT = """\
你是通用执行子代理：独立完成主代理委派的一项子任务。

工作方式：
1. 先定位再动手：按文件名找用 glob，按内容找用 grep；修改前先 read_file 看现有内容。
2. 做最小改动：只完成委派的任务范围，不顺手重构、不添加未被要求的内容；保持项目
   已有的代码风格。
3. 改完要验证：任务允许时运行测试或命令确认结果；无法验证的部分如实说明。
4. 遇到权限拒绝、无法完成的阻碍时停止并如实报告，不要绕路或用其他工具变相绕过。"""


@dataclass(frozen=True)
class SubAgentSpec:
    """一个子代理类型的完整定义；`tools` 为 None 表示继承全部可用工具。"""

    name: str
    description: str
    system_prompt: str
    tools: tuple[str, ...] | None = None
    model: str | None = None
    max_turns: int = 0  # 0 = 用 [subagents].max_turns
    source: str = "builtin"  # builtin / user / project / path

    def allowed_tool(self, name: str, allow_mcp: bool = False) -> bool:
        """该工具是否在子代理的可调用范围内（执行期与 schema 期共用）。"""
        if name in FORBIDDEN_TOOLS:
            return False
        if name.startswith("mcp__") and not allow_mcp:
            return False
        if self.tools is None:
            return True
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in self.tools)

    def tool_filter(self, allow_mcp: bool = False):
        return lambda name: self.allowed_tool(name, allow_mcp=allow_mcp)

    def read_only(self) -> bool:
        """白名单全部落在只读工具内时为 True（决定能否进并行波次）。

        通配符匹配不到任何已注册工具、或匹配到只读集合之外的任何工具 → False
        （保守判定：宁可串行，不可并发副作用）。
        """
        if self.tools is None:
            return False
        from ..tools import READ_ONLY_TOOLS, all_schemas  # 延迟导入防环

        names = {str(s.get("name")) for s in all_schemas()}
        readonly = set(READ_ONLY_TOOLS) | READONLY_EXTRA
        matched: set = set()
        for pattern in self.tools:
            hit = {name for name in names if fnmatch.fnmatchcase(name, pattern)}
            if not hit:
                return False
            matched |= hit
        return bool(matched) and matched <= readonly


_BUILTIN_TOOLS = ("read_file", "list_dir", "glob", "grep", "webfetch", "websearch")

BUILTIN_SPECS: dict[str, SubAgentSpec] = {
    "explore": SubAgentSpec(
        name="explore",
        description="只读侦察：在代码库与网络中搜索、阅读与查证，只返回结论与关键位置，不修改任何文件",
        system_prompt=_EXPLORE_PROMPT,
        tools=_BUILTIN_TOOLS,
    ),
    "general": SubAgentSpec(
        name="general",
        description="通用执行：可读写文件、运行命令，用于上下文较重或可独立完成的子任务",
        system_prompt=_GENERAL_PROMPT,
        tools=None,
    ),
}

_specs: dict[str, SubAgentSpec] = dict(BUILTIN_SPECS)
_diagnostics: list[str] = []
_loaded = False
_session_trusted: set = set()


def reset_session_trust() -> None:
    """清空本进程内的会话级项目信任（测试与彻底重载用）。"""
    _session_trusted.clear()


def all_specs() -> list:
    """当前全部可用类型（按名排序）；[subagents].enabled=false 时为空。"""
    if not config.SUBAGENTS.enabled:
        return []
    return [_specs[name] for name in sorted(_specs)]


def get_spec(name: str) -> SubAgentSpec | None:
    if not config.SUBAGENTS.enabled:
        return None
    return _specs.get(str(name or "").strip())


def diagnostics() -> list:
    return list(_diagnostics)


def is_loaded() -> bool:
    return _loaded


def refresh() -> list:
    """重新发现全部类型（Agent 启动 / `/agents refresh` 调用），返回诊断。

    同时把最新的 [subagents] 配置写回 `config.SUBAGENTS`，保证 runner /
    渲染 / schema 同步读到同一份配置。
    """
    global _specs, _loaded
    cfg = config.load_subagents_config()
    config.SUBAGENTS = cfg
    _diagnostics.clear()
    specs: dict[str, SubAgentSpec] = dict(BUILTIN_SPECS) if cfg.enabled else {}
    if cfg.enabled:
        for scope, root in _scan_plan(cfg):  # 用户 < 项目 < 附加：后者覆盖同名
            if scope == "project":
                if not _project_trusted(root, _diagnostics):
                    continue
            elif not root.is_dir():
                continue
            for path in sorted(root.glob("*.md")):
                spec = _load_file(path, scope, _diagnostics)
                if spec is not None:
                    specs[spec.name] = spec
    for pattern in cfg.disabled:
        for name in [n for n in specs if fnmatch.fnmatch(n, pattern)]:
            _diagnostics.append(f"子代理类型 {name} 已被 [subagents].disabled 禁用")
            specs.pop(name)
    _specs = specs
    _loaded = True
    return list(_diagnostics)


def reset() -> None:
    """恢复内置默认状态（测试用；不触发磁盘扫描）。"""
    global _specs, _loaded
    _specs = dict(BUILTIN_SPECS)
    _diagnostics.clear()
    _loaded = False


def _scan_plan(cfg) -> list:
    """按优先级从低到高返回 (scope, 目录)：用户 < 项目 < 附加（后者覆盖同名）。"""
    workspace = Path(config.WORKSPACE_ROOT).resolve()
    scans = [
        ("user", config.smithcode_home() / "agents"),
        ("project", workspace / AGENTS_SUBDIR),
    ]
    for raw in cfg.paths:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = workspace / path
        scans.append(("path", path.resolve()))
    return scans


def _project_trusted(root: Path, diagnostics: list) -> bool:
    """项目级定义的信任门控：显式信任过 / 用户确认后才加载（非交互 fail-closed）。"""
    if not root.is_dir():
        return False
    candidates = sorted(root.glob("*.md"))
    if not candidates:
        return False
    from .. import renderer
    from ..skills.registry import load_trust, project_key, remember_project
    from ..utils.terminal import confirmations_available

    key = project_key(Path(config.WORKSPACE_ROOT))
    if key in _session_trusted or bool(load_trust().get(key)):
        return True
    if not confirmations_available():
        diagnostics.append("非交互模式，已跳过项目子代理定义（未信任，[subagents] 未提供 project 开关）")
        return False
    detail = ["发现项目子代理定义（随仓库分发，可能不可信）:"] + [
        f"- {path.name}" for path in candidates
    ]
    answer = renderer.current().confirm_choice(
        "加载项目子代理定义? [y]仅本次 / [a]始终信任此项目 / [n]跳过: ",
        "yan",
        "y / a / n",
        detail=detail,
        descriptions={
            "y": "仅本次会话加载",
            "a": "始终信任此项目（落盘记录）",
            "n": "跳过本项目的子代理定义",
        },
        scope=None,
    )
    if answer in ("y", "a"):
        if answer == "a":
            remember_project(key)
            renderer.current().info(f"  已记住信任项目: {key}")
        _session_trusted.add(key)
        return True
    diagnostics.append("用户跳过了项目子代理定义")
    return False


def _load_file(path: Path, scope: str, diagnostics: list) -> SubAgentSpec | None:
    try:
        if path.stat().st_size > MAX_SPEC_FILE_BYTES:
            diagnostics.append(f"跳过子代理定义 {path}: 文件超过 {MAX_SPEC_FILE_BYTES} 字节")
            return None
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        diagnostics.append(f"跳过子代理定义 {path}: 读取失败（{e}）")
        return None

    from ..skills.frontmatter import parse

    fm = parse(text)
    body = fm.body.strip()
    name = str(fm.meta.get("name") or path.stem).strip()
    description = " ".join(str(fm.meta.get("description") or "").split())
    if not name or any(c.isspace() for c in name):
        diagnostics.append(f"跳过子代理定义 {path}: name 无效")
        return None
    if not description:
        diagnostics.append(f"跳过子代理定义 {path}: 缺少 description")
        return None
    if not body:
        diagnostics.append(f"跳过子代理定义 {path}: 缺少系统提示词正文")
        return None
    for warning in fm.warnings:
        diagnostics.append(f"子代理 {name}: {warning}")
    return SubAgentSpec(
        name=name,
        description=description,
        system_prompt=body,
        tools=_parse_tools(fm.meta.get("tools"), name, diagnostics),
        model=str(fm.meta.get("model") or "").strip() or None,
        max_turns=_parse_int(fm.meta.get("max_turns")),
        source=scope,
    )


def _parse_tools(raw, name: str, diagnostics: list) -> tuple[str, ...] | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        items = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        items = [str(part).strip() for part in raw]
    else:
        diagnostics.append(f"子代理 {name}: tools 应为字符串或列表，已忽略")
        return None
    tools = tuple(item for item in items if item)
    return tools or None


def _parse_int(raw) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    return max(0, value)
