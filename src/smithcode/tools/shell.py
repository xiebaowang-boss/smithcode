from __future__ import annotations

from .. import config
from ..process import run as run_process
from .base import register


def _describe(args: dict) -> str:
    timeout = args.get("timeout")
    base = f"command {args.get('command', '?')}"
    return f"{base} (timeout={timeout}s)" if timeout else base


@register(
    {
        "name": "run_command",
        "pattern_arg": "command",
        "display": "block",
        "serial": True,
        "describe": _describe,
        "description": "在工作区根目录执行一条 shell 命令并返回输出。"
        "默认 60 秒超时，跑测试、构建等耗时命令前用 timeout 参数延长（上限 300 秒）。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令"},
                "timeout": {
                    "type": "integer",
                    "description": "超时秒数，默认 60，上限 300",
                },
            },
            "required": ["command"],
        },
    }
)
def run_command(command: str, timeout: int | None = None) -> str:
    seconds = min(max(1, int(timeout) if timeout else config.COMMAND_TIMEOUT),
                  config.COMMAND_TIMEOUT_MAX)
    result = run_process(command, timeout=seconds, cwd=config.WORKSPACE_ROOT)
    if result.status == "interrupted":
        return "错误: 命令被用户中断，已终止进程"
    if result.status == "timeout":
        return (f"错误: 命令超时 ({seconds}s)。"
                f"耗时命令先用 timeout 参数延长（上限 {config.COMMAND_TIMEOUT_MAX}s），"
                "或拆成更小的步骤")
    output = result.stdout or ""
    if result.stderr:
        output += "\n[stderr]\n" + result.stderr
    output += f"\n[exit code: {result.returncode}]"
    return output.strip() or "(无输出)"
