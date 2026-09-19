"""技能发现：扫描技能根目录、解析 SKILL.md、处理优先级与信任门控。

扫描范围（P1，有意收窄）：
- 项目级：`<工作区>/.agents/skills/`（跨客户端互通标准位置）；
- 用户级：`~/.smithcode/skills/`；
- 附加：`[skills].paths` 配置的目录（最高优先级）。

不兼容其他客户端的技能目录（`.claude/skills`、`~/.agents/skills` 等），后续按需扩展。
同名技能遵守"先命中者生效"：附加路径 > 项目级 > 用户级；被遮蔽者记入诊断。
项目级技能来自可能不可信的仓库，加载前经 resolve_project_trust() 信任门控。
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import config, renderer
from . import frontmatter

SKILL_FILENAME = "SKILL.md"
PROJECT_SKILLS_SUBDIR = Path(".agents") / "skills"
MAX_SCAN_DEPTH = 4
MAX_SCAN_DIRS = 2000
MAX_SKILL_FILE_BYTES = 1_000_000
MAX_RESOURCES = 50
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
_NAME_ALLOWED = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

_SCOPE_LABELS = {"config": "附加目录", "project": "项目", "user": "用户"}


@dataclass
class Skill:
    """一个已解析的技能；body 在发现时读入，激活零 IO。"""

    name: str
    description: str
    location: Path  # SKILL.md 绝对路径
    base: Path  # 技能根目录（解析相对路径、列资源用）
    root: Path  # 技能所在的技能根目录（只读白名单依据）
    scope: str  # config / project / user
    body: str = ""
    license: str = ""
    compatibility: str = ""
    model_invocable: bool = True  # disable-model-invocation 取反
    disabled: bool = False  # 被 [skills].disabled 命中
    disabled_by: str = ""
    warnings: list = field(default_factory=list)


@dataclass
class Discovery:
    skills: list = field(default_factory=list)
    diagnostics: list = field(default_factory=list)
    enabled: bool = True


def discover(cfg=None) -> Discovery:
    """按配置扫描全部技能根目录，返回去重后的技能与诊断。

    调用方（skills.state）负责信任门控；这里产出全部候选（含被禁用的，
    供 /skills 诊断展示，模型侧由 model_skills() 过滤）。
    """
    cfg = cfg or config.load_skills_config()
    result = Discovery(enabled=cfg.enabled)
    if not cfg.enabled:
        return result

    seen: dict = {}
    for scope, root in _scan_plan(cfg):
        if not root.is_dir():
            continue
        for skill in _scan_root(root, scope, result.diagnostics, cfg.disabled):
            shadowed_by = seen.get(skill.name)
            if shadowed_by is not None:
                result.diagnostics.append(
                    f"技能 {skill.name}（{_source_label(skill)}）被同名技能遮蔽（{shadowed_by}）"
                )
                continue
            seen[skill.name] = _source_label(skill)
            result.skills.append(skill)
    return result


def _scan_plan(cfg) -> list:
    """返回按优先级排列的 (scope, 技能根目录) 列表。"""
    scans = []
    workspace = Path(config.WORKSPACE_ROOT).resolve()
    for raw in cfg.paths:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = workspace / path
        scans.append(("config", path.resolve()))
    scans.append(("project", workspace / PROJECT_SKILLS_SUBDIR))
    scans.append(("user", config.smithcode_home() / "skills"))
    return scans


def _scan_root(root: Path, scope: str, diagnostics: list, disabled_patterns: tuple) -> list:
    """扫描一个技能根目录：递归找"含 SKILL.md 的目录"，找到即不再下探。"""
    skills: list = []
    visited: set = set()
    budget = [MAX_SCAN_DIRS]
    _walk(root, 0, root, scope, disabled_patterns, skills, diagnostics, visited, budget)
    return skills


def _walk(directory: Path, depth: int, root: Path, scope: str, disabled_patterns: tuple,
          skills: list, diagnostics: list, visited: set, budget: list) -> None:
    if depth > MAX_SCAN_DEPTH or budget[0] <= 0:
        return
    try:
        real = directory.resolve()
    except OSError:
        real = directory
    if real in visited:  # 软链接成环保护
        return
    visited.add(real)
    budget[0] -= 1

    try:
        entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
    except OSError as e:
        diagnostics.append(f"无法读取目录 {directory}: {e}")
        return

    for entry in entries:
        if budget[0] <= 0:
            diagnostics.append(f"目录数超过扫描上限（{MAX_SCAN_DIRS}），已停止扫描 {root}")
            return
        if not entry.is_dir() or entry.name in SKIP_DIRS:
            continue
        skill_md = entry / SKILL_FILENAME
        if skill_md.is_file():
            skill = _load_skill(entry, skill_md, root, scope, disabled_patterns, diagnostics)
            if skill is not None:
                skills.append(skill)
            continue  # 技能目录内不再下探
        _walk(entry, depth + 1, root, scope, disabled_patterns, skills,
              diagnostics, visited, budget)


def _load_skill(base: Path, skill_md: Path, root: Path, scope: str,
                disabled_patterns: tuple, diagnostics: list):
    try:
        if skill_md.stat().st_size > MAX_SKILL_FILE_BYTES:
            diagnostics.append(f"跳过 {skill_md}: 文件超过 {MAX_SKILL_FILE_BYTES} 字节")
            return None
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        diagnostics.append(f"跳过 {skill_md}: 读取失败（{e}）")
        return None

    fm = frontmatter.parse(text)
    name = (fm.meta.get("name") or "").strip() or base.name
    description = " ".join((fm.meta.get("description") or "").split())
    if not description:
        reason = "frontmatter 解析失败" if fm.warnings else "缺少 description"
        diagnostics.append(f"跳过技能 {base}：{reason}")
        return None

    warnings = list(fm.warnings)
    raw_name = (fm.meta.get("name") or "").strip()
    if raw_name and raw_name != base.name:
        warnings.append(f"name 与目录名不一致（目录为 {base.name}）")
    if not _NAME_ALLOWED.match(name) or len(name) > 64:
        warnings.append("name 不符合规范（仅小写字母/数字/连字符，≤64 字符）")
    if len(description) > 1024:
        warnings.append("description 超过 1024 字符")

    raw_flag = str(fm.meta.get("disable-model-invocation", "")).strip().lower()
    disabled_by = _disabled_match(name, disabled_patterns)
    skill = Skill(
        name=name,
        description=description,
        location=skill_md,
        base=base,
        root=root,
        scope=scope,
        body=fm.body,
        license=str(fm.meta.get("license") or ""),
        compatibility=str(fm.meta.get("compatibility") or ""),
        model_invocable=raw_flag not in ("true", "yes", "1", "on"),
        disabled=bool(disabled_by),
        disabled_by=disabled_by or "",
        warnings=warnings,
    )
    for warning in warnings:
        diagnostics.append(f"技能 {name}: {warning}")
    return skill


def _disabled_match(name: str, patterns: tuple) -> str:
    for pattern in patterns:
        if fnmatch.fnmatch(name, pattern):
            return pattern
    return ""


def list_resources(base: Path) -> list:
    """列出技能目录内的资源文件（相对路径、`/` 分隔）；只列不读，上限 MAX_RESOURCES。"""
    resources: list = []
    for directory, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        current = Path(directory)
        for filename in sorted(files):
            if current == base and filename == SKILL_FILENAME:
                continue
            resources.append(current.joinpath(filename).relative_to(base).as_posix())
            if len(resources) >= MAX_RESOURCES:
                return resources
    return resources


def _source_label(skill: Skill) -> str:
    return f"{_SCOPE_LABELS.get(skill.scope, skill.scope)} {skill.root}"


# ---------- 项目级信任门控 ----------

_session_trusted: set = set()


def reset_session_trust() -> None:
    """清空本进程内的会话级信任（`[y] 仅本次` 的积累；测试与彻底重载用）。"""
    _session_trusted.clear()


def project_key(workspace: Path) -> str:
    """信任库键：向上找 git 根（无 .git 则用工作区本身），大小写归一便于跨写比较。"""
    resolved = workspace.resolve()
    current = resolved
    while True:
        if (current / ".git").exists():
            break
        if current.parent == current:
            current = resolved
            break
        current = current.parent
    return os.path.normcase(str(current))


def resolve_project_trust(cfg, preview: list, diagnostics: list) -> bool:
    """决定是否加载项目级技能（可能来自不可信仓库）。

    cfg.project：on 直接加载；off 永久跳过；ask（默认）先查信任库，未记录则交互确认
    ——`[a] 始终信任` 落盘，`[y]` 仅本进程，`[n]` 跳过；非交互模式 fail-closed 跳过。
    """
    if not preview:
        return True
    if cfg.project == "off":
        diagnostics.append("项目技能已被 [skills].project=off 跳过")
        return False
    if cfg.project == "on":
        return True

    key = project_key(Path(config.WORKSPACE_ROOT))
    if key in _session_trusted or bool(load_trust().get(key)):
        return True
    # 延迟导入：utils.terminal 导入 commands，而 commands 导入 skills，顶层导入会成环
    from ..agent.interactions import ask as ask_prompt
    from ..utils.terminal import confirmations_available

    if not confirmations_available():
        diagnostics.append("非交互模式，已跳过项目技能（[skills].project=ask）")
        renderer.current().info(
            f"  [技能] 发现 {len(preview)} 个项目技能，非交互模式已跳过"
            "（[skills].project=ask）"
        )
        return False

    r = renderer.current()
    detail = ["发现项目技能（随仓库分发，可能不可信）:"] + [
        f"- {skill.name}: {skill.description[:60]}" for skill in preview
    ]
    descriptions = {"y": "仅本次会话加载", "a": "始终信任此项目（落盘记录）", "n": "跳过本项目的技能"}
    answer = ask_prompt(
        "skill_trust",
        title="加载项目技能?",
        detail=tuple(detail),
        options=("once", "always", "skip"),
        payload={"project": key, "skills": tuple(skill.name for skill in preview)},
        run=lambda: r.confirm_choice(
            "加载项目技能? [y]仅本次 / [a]始终信任此项目 / [n]跳过: ", "yan", "y / a / n",
            detail=detail, descriptions=descriptions,
        ),
    )
    if answer in ("y", "a"):
        if answer == "a":
            remember_project(key)
            renderer.current().info(f"  已记住信任项目: {key}")
        _session_trusted.add(key)
        return True
    diagnostics.append("用户跳过了项目技能")
    return False


def load_trust() -> dict:
    """读取 ~/.smithcode/skills_trust.json 的 projects 映射；损坏时告警并降级为空。"""
    path = config.skills_trust_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[警告] 无法读取技能信任库 {path}，已忽略: {e}")
        return {}
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        return {}
    return {str(k): bool(v) for k, v in projects.items()}


def remember_project(key: str) -> None:
    """把一个项目写入"始终信任"库；写失败只告警，不影响本次会话。"""
    projects = load_trust()
    projects[key] = True
    path = config.skills_trust_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": 1, "projects": projects}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        print(f"[警告] 技能信任库写入失败 {path}: {e}")
