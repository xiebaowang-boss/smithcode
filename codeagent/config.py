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
