import json
import math
import os
import platform
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("OPENAI_API_KEY", "")
API_BASE = os.getenv("OPENAI_API_BASE")
MODEL = os.getenv("OPENAI_MODEL", "deepseek-v4-flash")

WORKSPACE_ROOT = os.getenv("ROOT", os.getcwd())
MAX_ITERATIONS = 30
COMMAND_TIMEOUT = 60
MAX_TOOL_OUTPUT = 20_000  # 单次工具输出进入上下文的最大字符数，超出则头尾截断


def _env_number(name: str, default):
    """读一个数值型环境变量；缺失用默认值，无效值打印警告并降级，不影响启动。

    先按 float 统一解析（兼容 "1e5"、".85" 等写法），再转回默认值的类型——
    配置要 int 还是 float 是配置项自己的语义，由默认值决定，与输入写法无关。
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
        if not math.isfinite(value):  # nan/inf 是合法 float 但不是合法配置
            raise ValueError(raw)
        return type(default)(value)
    except ValueError:
        print(f"[警告] 环境变量 {name}={raw!r} 不是有效数字，已用默认值 {default}")
        return default


# 上下文窗口预算与压缩阈值（token 估算基准），/context 展示与阈值提醒的依据
CONTEXT_TOKEN_BUDGET = _env_number("CONTEXT_BUDGET", 65536)
COMPACT_TRIGGER = _env_number("COMPACT_TRIGGER", 0.8)  # 占预算的比例
COMPACT_KEEP_TOKENS = _env_number(
    "COMPACT_KEEP_TOKENS", 15000
)  # 压缩时尾部原样保留的 token 数
MAX_RETRIES = 3  # LLM 瞬时错误（限流/断网/5xx）自动重试次数
LLM_TIMEOUT = 120  # 单次 LLM 请求超时（秒）

# 操作系统信息
OS_INFO = platform.platform()
PYTHON_VERSION = platform.python_version()
OS_TYPE = platform.system().lower()  # 'windows', 'linux', 'darwin'


def set_workspace(path):
    global WORKSPACE_ROOT
    WORKSPACE_ROOT = str(Path(path).resolve())


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


def _read_workspace_config(path=None) -> dict:
    """读取工作区 smithcode.json；文件缺失返回 {}，损坏时打印警告并降级为 {}。"""
    path = Path(path) if path else Path(WORKSPACE_ROOT) / "smithcode.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[警告] 无法读取 {path}，已忽略该文件中的自定义配置: {e}")
        return {}


def load_permissions(path=None):
    """读取工作区 smithcode.json 的 permissions 字段，返回 [(工具, 模式, 动作)]。

    支持两种写法：字符串简写对该工具全部模式生效；对象写法按模式细分。
    """
    permissions = _read_workspace_config(path).get("permissions") or {}

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


def load_tool_display(path=None):
    """读取工作区 smithcode.json 的 tool_display 字段，决定工具调用的展示粒度。

    缺失用 summary；非法值（含显式 null）打印警告并降级，不中断程序。
    """
    value = _read_workspace_config(path).get("tool_display", DEFAULT_TOOL_DISPLAY)
    if value in TOOL_DISPLAYS:
        return value
    print(
        f"[警告] tool_display 的值 {value!r} 无效"
        f"（可选 {' / '.join(TOOL_DISPLAYS)}），已用默认值 {DEFAULT_TOOL_DISPLAY}"
    )
    return DEFAULT_TOOL_DISPLAY
