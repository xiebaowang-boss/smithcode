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
    try:
        from .llm.prompts import clear_git_repo_cache
    except ImportError:  # 极早期的导入阶段 prompts 尚未就绪，下次调用时自然生效
        pass
    else:
        clear_git_repo_cache()


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
        "           smith setup\n"
        "       key 将写入 ~/.smithcode/credentials.json\n"
        "\n"
        "    2. 只在当前终端临时设置环境变量，然后重新运行：\n"
        '           PowerShell:  $env:SMITHCODE_KEY = "sk-你的密钥"\n'
        "           CMD:         set SMITHCODE_KEY=sk-你的密钥"
    )


# ---------- 行为配置（只读 config.toml） ----------

# 单次任务最大迭代轮数（一轮 = 一次模型调用 + 执行其返回的工具调用）。
# -1 表示不限制（默认，对齐 opencode 的 steps 缺省「无限迭代」语义）；配置为
# 正整数时到达上限，达到后不再执行工具，改为注入收尾提示、强制模型用纯文本
# 总结已完成工作与剩余任务（对齐 opencode 的 max-steps 收尾行为）。
MAX_ITERATIONS = _resolve_number("limits", "max_iterations", -1)
COMMAND_TIMEOUT = _resolve_number("limits", "command_timeout", 60)
COMMAND_TIMEOUT_MAX = _resolve_number("limits", "command_timeout_max", 300)  # run_command timeout 参数的上限
MAX_TOOL_OUTPUT = _resolve_number("limits", "max_tool_output", 20_000)  # 单次工具输出进入上下文的最大字符数，超出则头尾截断

# 上下文窗口预算与压缩阈值（token 估算基准），/context 展示与阈值提醒的依据
CONTEXT_TOKEN_BUDGET = _resolve_number("context", "budget", 65536)
COMPACT_TRIGGER = _resolve_number("context", "compact_trigger", 0.8)  # 占预算的比例
COMPACT_KEEP_TOKENS = _resolve_number("context", "compact_keep_tokens", 15000)  # 压缩时尾部原样保留的 token 数
MAX_RETRIES = _resolve_number("limits", "max_retries", 3)  # LLM 瞬时错误（限流/断网/5xx）自动重试次数
LLM_TIMEOUT = _resolve_number("limits", "llm_timeout", 120)  # LLM 空闲超时（秒）：静默超过它就断开，不是整个请求的总时长上限

# /goal 持久目标的默认回合预算：正整数表示目标存续期间最多自动推进的回合数，
# 用尽后系统注入收尾提示词并停止（/goal budget N 可改当前目标）。默认 -1 表示
# 不限制——目标持续自动推进，直到模型核验证据后声明完成/受阻、或用户暂停/中断
# （对齐 Claude Code /goal 无原生成轮上限、Codex 由用户显式配置预算的做法）。
GOAL_MAX_TURNS = _resolve_number("limits", "goal_max_turns", -1)

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


# ---------- 终端窗口标题 ----------

_TERMINAL_TITLE_TRUTHY = ("1", "true", "yes", "on")
_TERMINAL_TITLE_FALSY = ("0", "false", "no", "off")


def load_terminal_title() -> bool:
    """是否把会话标题写进终端窗口标题（TUI / REPL 交互模式）。

    优先级：SMITHCODE_TERMINAL_TITLE > config.toml 顶层 terminal_title > 默认
    True（对齐 KEY / MODEL / URL 的 env > 文件 > 默认，见「三项核心配置」）。空串
    视为"没配"；非法值打印警告并降级为默认，不中断程序。非 tty 时由调用方
    （title.TerminalTitlePresenter）整体关闭，无需用户额外配置。
    """
    env = os.getenv("SMITHCODE_TERMINAL_TITLE", "").strip().lower()
    if env:
        if env in _TERMINAL_TITLE_TRUTHY:
            return True
        if env in _TERMINAL_TITLE_FALSY:
            return False
        print(
            f"[警告] SMITHCODE_TERMINAL_TITLE 的值 {env!r} 无效"
            "（可选 1/0），已用默认值 True"
        )
        return True
    value = _read_config_file().get("terminal_title", True)
    if isinstance(value, bool):
        return value
    print(
        f"[警告] config.toml 的 terminal_title = {value!r} 不是布尔值，已用默认值 True"
    )
    return True


# ---------- 网络工具（webfetch / websearch） ----------

_BOOL_TRUTHY = ("1", "true", "yes", "on")
_BOOL_FALSY = ("0", "false", "no", "off")


def load_allow_private_urls() -> bool:
    """webfetch 是否允许访问内网 / 本机地址（默认 False，即拦截）。

    优先级：SMITHCODE_ALLOW_PRIVATE_URLS > config.toml 顶层 allow_private_urls >
    默认 False。默认拦截私网、回环、链路本地（含云元数据 169.254.169.254）、CGNAT
    等非全局地址——webfetch 默认免确认放行，模型又可能被网页内容诱导，故按
    「默认安全」处理；本地开发要抓 localhost 文档时可显式打开。空串视为"没配"；
    非法值打印警告并降级为默认，不中断程序。
    """
    env = os.getenv("SMITHCODE_ALLOW_PRIVATE_URLS", "").strip().lower()
    if env:
        if env in _BOOL_TRUTHY:
            return True
        if env in _BOOL_FALSY:
            return False
        print(
            f"[警告] SMITHCODE_ALLOW_PRIVATE_URLS 的值 {env!r} 无效"
            "（可选 1/0），已用默认值 False"
        )
        return False
    value = _read_config_file().get("allow_private_urls", False)
    if isinstance(value, bool):
        return value
    print(
        f"[警告] config.toml 的 allow_private_urls = {value!r} 不是布尔值，已用默认值 False"
    )
    return False


# websearch 的检索后端：auto 按内置顺序逐个尝试（Tavily → Brave → Bing →
# DuckDuckGo），命中即用；也可固定其一。不同网络下可达性与结果质量差异极大（如
# 无代理时 Brave 不可达、走代理时 Bing 会返回无关结果），故开放配置而不写死。
# tavily 需要 key（见 load_tavily_key），无 key 时在 auto 里直接跳过。
SEARCH_BACKENDS = ("auto", "tavily", "brave", "bing", "ddg")
DEFAULT_SEARCH_BACKEND = "auto"


def load_search_backend() -> str:
    """websearch 使用哪个检索后端。

    优先级：SMITHCODE_SEARCH_BACKEND > config.toml 的 [search].backend > 默认 auto。
    可选 auto / tavily / brave / bing / ddg；空串视为"没配"，非法值打印警告并降级为 auto。
    """
    env = os.getenv("SMITHCODE_SEARCH_BACKEND", "").strip().lower()
    if env:
        if env in SEARCH_BACKENDS:
            return env
        print(
            f"[警告] SMITHCODE_SEARCH_BACKEND 的值 {env!r} 无效"
            f"（可选 {'/'.join(SEARCH_BACKENDS)}），已用默认值 {DEFAULT_SEARCH_BACKEND}"
        )
        return DEFAULT_SEARCH_BACKEND
    data = _read_config_file().get("search") or {}
    if not isinstance(data, dict):
        print("[警告] config.toml 的 [search] 段不是表，已忽略")
        return DEFAULT_SEARCH_BACKEND
    value = data.get("backend")
    if value is None:
        return DEFAULT_SEARCH_BACKEND
    if isinstance(value, str) and value.strip().lower() in SEARCH_BACKENDS:
        return value.strip().lower()
    print(
        f"[警告] config.toml 的 [search].backend = {value!r} 无效"
        f"（可选 {'/'.join(SEARCH_BACKENDS)}），已用默认值 {DEFAULT_SEARCH_BACKEND}"
    )
    return DEFAULT_SEARCH_BACKEND


def load_tavily_key() -> str:
    """Tavily 搜索 API key（websearch 的 tavily 后端用）。

    优先级：SMITHCODE_TAVILY_KEY > config.toml 的 [search].tavily_key >
    credentials.json 的 search.tavily_key > 空。空串视为"没配"，未配时 tavily
    后端不可用（auto 模式直接跳过它）。key 属秘密，正式存放位置是
    credentials.json（与 LLM key 同文件，写入走 0600）。
    """
    env = os.getenv("SMITHCODE_TAVILY_KEY", "").strip()
    if env:
        return env
    data = _read_config_file().get("search") or {}
    if isinstance(data, dict):
        value = data.get("tavily_key")
        if isinstance(value, str) and value.strip():
            return value.strip()
    section = _read_credentials().get("search", {})
    if isinstance(section, dict):
        key = section.get("tavily_key")
        if isinstance(key, str):
            return key.strip()
    return ""


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


# ---------- 项目指令（Instructions） ----------

INSTRUCTIONS_DEFAULT_FILES = ("AGENTS.md",)


@dataclass(frozen=True)
class InstructionsConfig:
    """[instructions] 段的解析结果；非法项警告后回退默认值。"""

    enabled: bool = True
    # 在各根目录（用户级 ~/.smithcode/ 与项目工作区）下探测的文件名；
    # 显式空列表表示不探测默认名（仅加载 paths 追加文件）
    files: tuple = INSTRUCTIONS_DEFAULT_FILES
    paths: tuple = ()  # 追加指令文件（相对工作区或绝对路径），优先级最高
    max_chars: int = 8000  # 注入段总字符预算，超出时从低优先级文件截断


def load_instructions_config() -> InstructionsConfig:
    """读取 [instructions] 段：enabled / files / paths / max_chars。

    与 skills 配置同款风格：类型不对警告后回退默认值，不中断启动。
    """
    data = _read_config_file().get("instructions") or {}
    if not isinstance(data, dict):
        print("[警告] config.toml 的 [instructions] 段不是表，已忽略")
        return InstructionsConfig()

    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        print(
            f"[警告] config.toml 的 instructions.enabled = {enabled!r}"
            " 不是布尔值，已用默认值 True"
        )
        enabled = True

    files = data.get("files")
    if files is None:
        files = INSTRUCTIONS_DEFAULT_FILES
    elif isinstance(files, list) and all(isinstance(v, str) for v in files):
        files = tuple(files)  # 显式空列表：不探测默认名，仅加载 paths
    else:
        print("[警告] config.toml 的 instructions.files 应为字符串列表，已用默认值")
        files = INSTRUCTIONS_DEFAULT_FILES

    max_chars = data.get("max_chars", 8000)
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
        print(
            f"[警告] config.toml 的 instructions.max_chars = {max_chars!r} 无效"
            "（应为正整数），已用默认值 8000"
        )
        max_chars = 8000

    return InstructionsConfig(
        enabled=enabled,
        files=tuple(files),
        paths=_str_list(data.get("paths"), "instructions.paths"),
        max_chars=max_chars,
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
