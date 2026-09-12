"""配置中心：唯一来源是 ~/.smithcode/（config.toml + credentials.json）。

优先级：内置默认 < 配置文件 < 环境变量（仅 SMITHCODE_KEY / SMITHCODE_MODEL /
SMITHCODE_URL 三个）< CLI 参数。凭据与行为配置分文件存放：credentials.json
只装 key（永不入库），config.toml 不含秘密、可安全分享。
"""
from __future__ import annotations

import json
import math
import os
import platform
import sys
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


class ConfigError(Exception):
    """启动所需配置缺失；异常消息即面向用户的修复指引。"""


# ---------- 路径 ----------

def smithcode_home() -> Path:
    """用户级配置根：SMITHCODE_HOME 可覆盖（测试隔离/便携），否则 ~/.smithcode。

    Path.home() 在 Windows 与 Unix 各自返回正确的家目录，无需平台分支。
    """
    override = os.getenv("SMITHCODE_HOME")
    return Path(override) if override else Path.home() / ".smithcode"


def config_path() -> Path:
    return smithcode_home() / "config.toml"


def credentials_path() -> Path:
    return smithcode_home() / "credentials.json"


def models_cache_path() -> Path:
    """远端 `/models` 拉取结果的磁盘缓存路径（按接口地址校验，见 models.ModelCache）。"""
    return smithcode_home() / "models.json"


def skills_trust_path() -> Path:
    """项目级技能信任库：记录用户"始终信任"的项目（技能子系统专用，非 config.toml）。"""
    return smithcode_home() / "skills_trust.json"


WORKSPACE_ROOT = os.getcwd()


def set_workspace(path):
    global WORKSPACE_ROOT
    WORKSPACE_ROOT = str(Path(path).resolve())


# ---------- 文件读取 ----------

def _read_config_file() -> dict:
    """读取 config.toml；缺失返回 {}，损坏时打印警告并降级为 {}。"""
    path = config_path()
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"[警告] 无法读取 {path}，已忽略该文件中的自定义配置: {e}")
        return {}


def _read_credentials() -> dict:
    """读取 credentials.json；缺失/损坏/结构不对一律返回 {}，不影响启动。"""
    path = credentials_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[警告] 无法读取 {path}，已忽略凭据文件: {e}")
        return {}
    return data if isinstance(data, dict) else {}


def _credentials_key() -> str:
    value = _read_credentials().get("key")
    return value if isinstance(value, str) else ""


# ---------- 取值辅助 ----------

def _file_str(section: str, key: str):
    """取一个字符串配置项；缺失或类型不对返回 None（类型不对时警告降级）。"""
    value = _read_config_file().get(section, {}).get(key)
    if value is None or isinstance(value, str):
        return value
    print(f"[警告] config.toml 的 {section}.{key} = {value!r} 不是有效字符串，已忽略")
    return None


def _resolve_number(section: str, key: str, default):
    """解析数值配置：内置默认 < config.toml；非法值（含 nan/inf/布尔）警告降级。

    配置要 int 还是 float 是配置项自己的语义，由默认值决定，与输入写法无关。
    """
    value = default
    file_value = _read_config_file().get(section, {}).get(key)
    if file_value is not None:
        if isinstance(file_value, (int, float)) and not isinstance(file_value, bool) and math.isfinite(file_value):
            value = type(default)(file_value)
        else:
            print(f"[警告] config.toml 的 {section}.{key} = {file_value!r} 不是有效数字，已用默认值 {default}")
    return value


# ---------- 三项核心配置：key / model / url ----------

# or 链从高到低即优先级：环境变量（临时覆盖）> 文件（长期归宿）> 内置默认；
# 空串视为"没配"，自动落到下一级
KEY = os.getenv("SMITHCODE_KEY") or _credentials_key()
MODEL = os.getenv("SMITHCODE_MODEL") or _file_str("provider", "model") or "deepseek-v4-flash"
URL = os.getenv("SMITHCODE_URL") or _file_str("provider", "url")
# 模型思考强度（reasoning_effort）：内置默认 high，config.toml 的
# [provider].reasoning_effort 可覆盖；候选档位见 models.DEFAULT_EFFORTS
DEFAULT_EFFORT = "high"
REASONING_EFFORT = _file_str("provider", "reasoning_effort") or DEFAULT_EFFORT

# 当前对话会话 id：一次对话（单次任务或 /new 之后）内保持稳定，新会话轮换。
# 供自定义请求头 [provider.headers] 中的 {$session} 占位符使用（如 OpenCode Go
# 要求每会话稳定的 x-opencode-session，用于路由与提示缓存）。
SESSION_ID = uuid.uuid4().hex


def new_session_id() -> str:
    """开新会话时轮换会话 id：保证每个对话的 {$session} 值唯一且会话内稳定。"""
    global SESSION_ID
    SESSION_ID = uuid.uuid4().hex
    return SESSION_ID


def use_session_id(session_id: str) -> str:
    """恢复既有会话时采用持久化 id：同一对话跨进程的 {$session} 请求头保持稳定。"""
    global SESSION_ID
    SESSION_ID = str(session_id)
    return SESSION_ID


def load_provider_headers() -> dict:
    """读取 config.toml 的 [provider.headers] 段：随每个 LLM 请求发送的自定义请求头。

    值须为字符串，原样发送；含 {$session} 占位符的值在每次请求时替换为当前
    会话 id。整段非表、单个值非字符串时打印警告并忽略该项，不中断启动。
    """
    section = _read_config_file().get("provider") or {}
    headers = section.get("headers")
    if headers is None:
        return {}
    if not isinstance(headers, dict):
        print(f"[警告] config.toml 的 provider.headers = {headers!r} 不是配置表，已忽略")
        return {}
    result = {}
    for name, value in headers.items():
        if isinstance(value, str):
            result[name] = value
        else:
            print(
                f"[警告] config.toml 的 provider.headers.{name} = {value!r}"
                f" 不是有效字符串，已忽略"
            )
    return result


def read_configured_models():
    """读取 config.toml 的 [provider].models 列表；未配置或非法返回 None。

    返回 None 表示"外部没有配置"（区别于空列表），由 ModelCatalog 决定后续
    来源（磁盘缓存 / 远端 `/models`）。非列表或含非字符串项时打印警告并忽略。
    """
    section = _read_config_file().get("provider") or {}
    raw = section.get("models")
    if raw is None:
        return None
    if not isinstance(raw, list):
        print(f"[警告] config.toml 的 provider.models = {raw!r} 不是字符串列表，已忽略")
        return None
    models = []
    for item in raw:
        if not isinstance(item, str):
            print(f"[警告] config.toml 的 provider.models 含非字符串项 {item!r}，已忽略")
        elif item.strip() and item not in models:
            models.append(item)
    return models or None


def ensure_api_key():
    """启动前校验 API 凭证，缺失时抛带修复指引的 ConfigError，而不是放任 SDK 抛裸 traceback。"""
    if KEY.strip():
        return
    raise ConfigError(
        "[启动失败] 缺少 API Key。\n"
        "\n"
        "两种解决方式，任选其一：\n"
        "\n"
        "    1. 运行初始化向导（推荐，长期生效）：\n"
        "           smithcode setup\n"
        "       key 将写入 ~/.smithcode/credentials.json\n"
        "\n"
        "    2. 只在当前终端临时设置环境变量，然后重新运行：\n"
        '           PowerShell:  $env:SMITHCODE_KEY = "sk-你的密钥"\n'
        "           CMD:         set SMITHCODE_KEY=sk-你的密钥"
    )


# ---------- 行为配置（只读 config.toml） ----------

MAX_ITERATIONS = _resolve_number("limits", "max_iterations", 30)
COMMAND_TIMEOUT = _resolve_number("limits", "command_timeout", 60)
COMMAND_TIMEOUT_MAX = _resolve_number("limits", "command_timeout_max", 300)  # run_command timeout 参数的上限
MAX_TOOL_OUTPUT = _resolve_number("limits", "max_tool_output", 20_000)  # 单次工具输出进入上下文的最大字符数，超出则头尾截断

# 上下文窗口预算与压缩阈值（token 估算基准），/context 展示与阈值提醒的依据
CONTEXT_TOKEN_BUDGET = _resolve_number("context", "budget", 65536)
COMPACT_TRIGGER = _resolve_number("context", "compact_trigger", 0.8)  # 占预算的比例
COMPACT_KEEP_TOKENS = _resolve_number("context", "compact_keep_tokens", 15000)  # 压缩时尾部原样保留的 token 数
MAX_RETRIES = _resolve_number("limits", "max_retries", 3)  # LLM 瞬时错误（限流/断网/5xx）自动重试次数
LLM_TIMEOUT = _resolve_number("limits", "llm_timeout", 120)  # 单次 LLM 请求超时（秒）

# /goal 持久目标的默认回合预算：目标存续期间最多自动推进的回合数，
# 用尽后系统注入收尾提示词并停止（/goal budget N 可改当前目标）
GOAL_MAX_TURNS = _resolve_number("limits", "goal_max_turns", 50)

# 一轮内多个工具调用的并发执行上限（线程池 max_workers）。
# 模型一次返回的多个调用中，可并行的部分最多同时跑这么多，其余排队；
# 权限需确认/被拒的调用不走并发池，决策与展示均在主线程完成。
MAX_TOOL_CONCURRENCY = max(1, _resolve_number("limits", "max_tool_concurrency", 5))

# 操作系统信息
OS_INFO = platform.platform()
PYTHON_VERSION = platform.python_version()
OS_TYPE = platform.system().lower()  # 'windows', 'linux', 'darwin'


# 附加授权目录：cli 的 --add 可重复传入，供一个会话内跨项目访问
EXTRA_ROOTS: list[str] = []
# 会话内通过"越界确认"积累的信任目录（/new 时清空）
SESSION_EXTRA_ROOTS: list[str] = []
# 仅单次工具调用期间临时放行的目录（由 widen_roots 维护，正常情况下为空）
_WIDENED_ROOTS: list[str] = []
# _WIDENED_ROOTS 的增删锁：工具并发执行时多个线程同时进出 widen_roots，
# 列表 extend/del 不是原子操作，无锁会竞态导致放行目录被误删
_WIDENED_ROOTS_LOCK = threading.Lock()


def add_workspace(path):
    """把一个目录加入附加授权列表，与主工作区享有同等的工具访问权。"""
    EXTRA_ROOTS.append(str(Path(path).resolve()))


def allowed_roots() -> list[Path]:
    """全部授权目录（主工作区在前）：工具沙箱与权限模式归一化的共同依据。"""
    return (
        [Path(WORKSPACE_ROOT).resolve()]
        + [Path(p) for p in EXTRA_ROOTS]
        + [Path(p) for p in SESSION_EXTRA_ROOTS]
        + [Path(p) for p in _WIDENED_ROOTS]
    )


@contextmanager
def widen_roots(roots):
    """把目录临时加入授权列表，仅覆盖 with 块内的那次工具调用（"仅本次"语义）。"""
    added = [str(Path(r).resolve()) for r in roots]
    if not added:
        yield
        return
    with _WIDENED_ROOTS_LOCK:
        _WIDENED_ROOTS.extend(added)
    try:
        yield
    finally:
        with _WIDENED_ROOTS_LOCK:
            del _WIDENED_ROOTS[-len(added):]


# 技能目录只读白名单：由 skills 子系统发现后写入。读工具放行（免越界确认），
# 写工具（files._resolve(write=True)）不认——技能文件不可被静默改写。
SKILL_READ_ROOTS: list = []


def set_skill_roots(paths) -> None:
    """替换技能只读白名单（skills.refresh 时全量重建）。"""
    SKILL_READ_ROOTS[:] = [str(Path(p).resolve()) for p in paths]


def skill_roots() -> list[Path]:
    return [Path(p) for p in SKILL_READ_ROOTS]


def read_roots() -> list[Path]:
    """读工具的可用根：授权目录 + 技能只读白名单。"""
    return allowed_roots() + skill_roots()


def load_permissions():
    """读取 config.toml 的 [permissions] 段，返回 [(工具, 模式, 动作)]。

    支持两种写法：字符串简写对该工具全部模式生效；表写法按模式细分（glob 通配符）。
    """
    permissions = _read_config_file().get("permissions") or {}

    rules = []
    for tool, value in permissions.items():
        if isinstance(value, str):
            rules.append((tool, "*", value))
        elif isinstance(value, dict):
            for pattern, action in value.items():
                rules.append((tool, str(pattern), action))
        else:
            print(f"[警告] permissions.{tool} 的值类型无效，已忽略")
    return rules


# 工具调用的终端展示粒度：summary 只打印一行短摘要，detail 追加结果内容
TOOL_DISPLAYS = ("summary", "detail")
DEFAULT_TOOL_DISPLAY = "summary"


def load_tool_display():
    """读取 config.toml 顶层 tool_display 键，决定工具调用的展示粒度。

    缺失用 summary；非法值（含显式 null）打印警告并降级，不中断程序。
    """
    value = _read_config_file().get("tool_display", DEFAULT_TOOL_DISPLAY)
    if value in TOOL_DISPLAYS:
        return value
    print(
        f"[警告] tool_display 的值 {value!r} 无效"
        f"（可选 {' / '.join(TOOL_DISPLAYS)}），已用默认值 {DEFAULT_TOOL_DISPLAY}"
    )
    return DEFAULT_TOOL_DISPLAY


# ---------- 技能（Skills） ----------

SKILLS_PROJECT_MODES = ("ask", "on", "off")


@dataclass(frozen=True)
class SkillsConfig:
    """[skills] 段的解析结果；非法项警告后回退默认值。"""

    enabled: bool = True
    paths: tuple = ()
    project: str = "ask"
    max_catalog_chars: int = 8000
    disabled: tuple = ()


def _str_list(value, label: str) -> tuple:
    """字符串列表配置项：缺失返回 ()；类型不对警告后忽略。"""
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        print(f"[警告] config.toml 的 {label} 应为字符串列表，已忽略")
        return ()
    return tuple(value)


def load_skills_config() -> SkillsConfig:
    """读取 [skills] 段：enabled / paths / project / max_catalog_chars / disabled。"""
    data = _read_config_file().get("skills") or {}
    if not isinstance(data, dict):
        print("[警告] config.toml 的 [skills] 段不是表，已忽略")
        return SkillsConfig()

    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        print(f"[警告] config.toml 的 skills.enabled = {enabled!r} 不是布尔值，已用默认值 True")
        enabled = True

    project = data.get("project", "ask")
    if project not in SKILLS_PROJECT_MODES:
        print(
            f"[警告] config.toml 的 skills.project = {project!r} 无效"
            f"（可选 {' / '.join(SKILLS_PROJECT_MODES)}），已用默认值 ask"
        )
        project = "ask"

    budget = data.get("max_catalog_chars", 8000)
    if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
        print(
            f"[警告] config.toml 的 skills.max_catalog_chars = {budget!r} 无效"
            "（应为正整数），已用默认值 8000"
        )
        budget = 8000

    return SkillsConfig(
        enabled=enabled,
        paths=_str_list(data.get("paths"), "skills.paths"),
        project=project,
        max_catalog_chars=budget,
        disabled=_str_list(data.get("disabled"), "skills.disabled"),
    )


# ---------- 会话（Sessions） ----------

@dataclass(frozen=True)
class SessionsConfig:
    """[sessions] 段的解析结果；非法项警告后回退默认值。"""

    enabled: bool = True
    cleanup_days: float = 30  # 保留天数；0 = 不自动清理
    persist_state: bool = True  # 是否持久化 goal / plan / skills 激活集
    list_limit: int = 20  # /sessions 与 picker 默认展示条数
    auto_title: bool = True  # 首轮结束后自动生成标题（后台、失败静默）
    title_model: str = ""  # 标题专用模型；空 = 当前模型
    title_max_chars: int = 60  # 标题长度上限


def load_sessions_config() -> SessionsConfig:
    """读取 [sessions] 段：enabled / cleanup_days / persist_state / list_limit /
    auto_title / title_model / title_max_chars。"""
    data = _read_config_file().get("sessions") or {}
    if not isinstance(data, dict):
        print("[警告] config.toml 的 [sessions] 段不是表，已忽略")
        return SessionsConfig()

    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        print(
            f"[警告] config.toml 的 sessions.enabled = {enabled!r}"
            " 不是布尔值，已用默认值 True"
        )
        enabled = True

    cleanup_days = _resolve_number("sessions", "cleanup_days", 30)
    if cleanup_days < 0:
        print(
            f"[警告] config.toml 的 sessions.cleanup_days = {cleanup_days!r} 不能为负，"
            "已用默认值 30"
        )
        cleanup_days = 30

    persist_state = data.get("persist_state", True)
    if not isinstance(persist_state, bool):
        print(
            f"[警告] config.toml 的 sessions.persist_state = {persist_state!r}"
            " 不是布尔值，已用默认值 True"
        )
        persist_state = True

    auto_title = data.get("auto_title", True)
    if not isinstance(auto_title, bool):
        print(
            f"[警告] config.toml 的 sessions.auto_title = {auto_title!r}"
            " 不是布尔值，已用默认值 True"
        )
        auto_title = True

    title_max_chars = data.get("title_max_chars", 60)
    if not isinstance(title_max_chars, int) or isinstance(title_max_chars, bool) \
            or title_max_chars <= 0:
        print(
            f"[警告] config.toml 的 sessions.title_max_chars = {title_max_chars!r} 无效"
            "（应为正整数），已用默认值 60"
        )
        title_max_chars = 60

    list_limit = _resolve_number("sessions", "list_limit", 20)
    if list_limit <= 0:
        list_limit = 20

    return SessionsConfig(
        enabled=enabled,
        cleanup_days=float(cleanup_days),
        persist_state=persist_state,
        list_limit=int(list_limit),
        auto_title=auto_title,
        title_model=_file_str("sessions", "title_model") or "",
        title_max_chars=title_max_chars,
    )
