import argparse
import sys

from . import __version__, commands, config
from .agent import Agent
from .session import Session
from .utils.terminal import (
    confirmations_available,
    read_user_input,
    setup_console_encoding,
)
from .wizard import run_setup


def build_parser():
    parser = argparse.ArgumentParser(
        prog="smithcode",
        description="终端 AI 编程助手：让大模型调用工具帮你读写文件、执行命令。\n"
        "首次使用先运行 `smithcode setup` 完成初始化。",
    )
    parser.add_argument(
        "task", nargs="*",
        help="一次性任务描述；留空则进入交互模式",
    )
    parser.add_argument(
        "-w", "--workspace",
        help="工作区目录（默认当前目录）",
    )
    parser.add_argument(
        "--add", action="append", default=None, metavar="DIR",
        help="追加授权目录（可重复传入），供一个会话内跨项目访问",
    )
    parser.add_argument(
        "-m", "--model",
        help=f"模型名（默认 {config.MODEL}）",
    )
    parser.add_argument(
        "-y", "--yes", action="store_true",
        help="自动批准所有工具调用（含工作区外路径访问），不再逐个询问；deny 规则依然生效",
    )
    parser.add_argument(
        "--max-iterations", type=int, default=None, metavar="N",
        help=f"单次任务最大迭代轮数（默认 {config.MAX_ITERATIONS}）",
    )
    parser.add_argument(
        "-V", "--version", action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def repl(agent: Agent):
    print("SmithCode 已启动 (输入 /help 查看命令)")

    while True:
        try:
            user_input = read_user_input().strip()
        except (KeyboardInterrupt, EOFError):
            print("\n再见!")
            break

        if not user_input:
            continue
        if user_input.startswith("/"):
            outcome = commands.dispatch(agent, user_input)
            if outcome.exit:
                print("再见!")
                break
            if outcome.text is not None:
                print(outcome.text)
            continue

        try:
            agent.run(user_input)  # 回复已在流式过程中实时打印
        except Exception as e:  # noqa: BLE001
            print(f"\n[错误] {type(e).__name__}: {e}")


def run_once(agent: Agent, task: str):
    try:
        agent.run(task)  # 回复已在流式过程中实时打印
    except Exception as e:  # noqa: BLE001
        print(f"\n[错误] {type(e).__name__}: {e}")
        sys.exit(1)


def main(argv=None):
    setup_console_encoding()

    args = build_parser().parse_args(argv)
    if args.task == ["setup"]:  # 仅当唯一位置参数恰好是 setup，避免 subparsers 破坏自由文本任务
        sys.exit(run_setup())
    if args.workspace:
        config.set_workspace(args.workspace)
    for extra in args.add or []:
        config.add_workspace(extra)
    if args.model:
        config.MODEL = args.model

    try:
        agent = Agent(Session(), max_iterations=args.max_iterations)
    except config.ConfigError as e:
        print(e)  # 指引文案由 ConfigError 自带，打印后安静退出，不甩 traceback
        sys.exit(1)
    if args.yes:
        agent.permission.approved_all = True

    if args.task:
        run_once(agent, " ".join(args.task))
    elif confirmations_available():  # 交互终端：全屏 TUI（Textual）
        from .tui import run_tui

        run_tui(agent)
    else:
        repl(agent)  # 非 tty（管道/CI）保持富行式 REPL