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
from contextlib import contextmanager
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
MAX_TOOL_OUTPUT = _resolve_number("limits", "max_tool_output", 20_000)  # 单次工具输出进入上下文的最大字符数，超出则头尾截断

# 上下文窗口预算与压缩阈值（token 估算基准），/context 展示与阈值提醒的依据
CONTEXT_TOKEN_BUDGET = _resolve_number("context", "budget", 65536)
COMPACT_TRIGGER = _resolve_number("context", "compact_trigger", 0.8)  # 占预算的比例
COMPACT_KEEP_TOKENS = _resolve_number("context", "compact_keep_tokens", 15000)  # 压缩时尾部原样保留的 token 数
MAX_RETRIES = _resolve_number("limits", "max_retries", 3)  # LLM 瞬时错误（限流/断网/5xx）自动重试次数
LLM_TIMEOUT = _resolve_number("limits", "llm_timeout", 120)  # 单次 LLM 请求超时（秒）

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
    _WIDENED_ROOTS.extend(added)
    try:
        yield
    finally:
        del _WIDENED_ROOTS[-len(added):]


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
