import subprocess

from .. import config
from .base import register


@register(
    {
        "name": "run_command",
        "pattern_arg": "command",
        "description": "在工作区根目录执行一条 shell 命令并返回输出",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令"},
            },
            "required": ["command"],
        },
    }
)
def run_command(command: str) -> str:
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=config.COMMAND_TIMEOUT,
            cwd=config.WORKSPACE_ROOT,
            errors="replace",
        )
        output = result.stdout or ""
        if result.stderr:
            output += "\n[stderr]\n" + result.stderr
        output += f"\n[exit code: {result.returncode}]"
        return output.strip() or "(无输出)"
    except subprocess.TimeoutExpired:
        return f"错误: 命令超时 ({config.COMMAND_TIMEOUT}s)"
