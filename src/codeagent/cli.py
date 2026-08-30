import argparse
import sys

from . import __version__, config
from .agent import Agent
from .session import Session
from .utils.terminal import setup_console_encoding

HELP = """命令:
  /help   显示帮助
  /new    开启新会话
  /save   保存会话记录
  /exit   退出"""


def build_parser():
    parser = argparse.ArgumentParser(
        prog="codeagent",
        description="终端 AI 编程助手：让大模型调用工具帮你读写文件、执行命令。",
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
        help="自动批准所有工具调用，不再逐个询问",
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
    print("CodeAgent 已启动 (输入 /help 查看命令)")

    while True:
        try:
            user_input = input("\n你> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n再见!")
            break

        if not user_input:
            continue
        if user_input == "/exit":
            print("再见!")
            break
        if user_input == "/new":
            agent.session.reset()
            agent.permission.session_rules.clear()
            config.SESSION_EXTRA_ROOTS.clear()
            print("已开启新会话。")
            continue
        if user_input == "/save":
            path = agent.session.save()
            print(f"会话已保存到 {path}")
            continue
        if user_input == "/help":
            print(HELP)
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
    if args.workspace:
        config.set_workspace(args.workspace)
    for extra in args.add or []:
        config.add_workspace(extra)
    if args.model:
        config.MODEL = args.model

    agent = Agent(Session(), max_iterations=args.max_iterations)
    if args.yes:
        agent.permission.approved_all = True

    if args.task:
        run_once(agent, " ".join(args.task))
    else:
        repl(agent)