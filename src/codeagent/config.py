import json
import os
import platform
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("OPENAI_API_KEY", "")
API_BASE = os.getenv("OPENAI_API_BASE")
MODEL = os.getenv("OPENAI_MODEL", "deepseek-v4-flash")

WORKSPACE_ROOT = os.getenv("CODEAGENT_ROOT", os.getcwd())
MAX_ITERATIONS = 30
COMMAND_TIMEOUT = 60

# 操作系统信息
OS_INFO = platform.platform()
PYTHON_VERSION = platform.python_version()
OS_TYPE = platform.system().lower()  # 'windows', 'linux', 'darwin'


def set_workspace(path):
    global WORKSPACE_ROOT
    WORKSPACE_ROOT = str(Path(path).resolve())


def load_permissions(path=None):
    """读取工作区 codeagent.json 的 permissions 字段，返回 [(工具, 模式, 动作)]。

    支持两种写法：字符串简写对该工具全部模式生效；对象写法按模式细分。
    文件缺失返回空列表；解析失败打印警告并降级，不中断程序。
    """
    path = Path(path) if path else Path(WORKSPACE_ROOT) / "codeagent.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        permissions = data.get("permissions") or {}
    except (OSError, json.JSONDecodeError) as e:
        print(f"[警告] 无法读取 {path}，已忽略自定义权限规则: {e}")
        return []

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
