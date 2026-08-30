import argparse
import sys
import time

from . import __version__, config
from .agent import Agent
from .session import Session
from .utils.terminal import setup_console_encoding, stdin_has_pending

HELP = """命令:
  /help   显示帮助
  /new    开启新会话
  /save   保存会话记录
  /usage  显示 token 用量统计
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


def _print_usage_hint(agent: Agent):
    """每轮任务结束后的用量速览：本轮一行、会话累计一行，缓存命中内联在输入后。"""
    run = agent.last_usage
    if not run.calls:
        return
    acc = agent.session.usage.current_session
    print(f"\n[tokens] 本轮 {run.humanize()}")
    print(f"         会话累计 {acc.humanize()}")


def read_user_input() -> str:
    """读一条用户输入；粘贴的后续行会合并进同一条多行消息。

    input() 是按行读的，多行粘贴会被逐行消费成多条独立消息（还会在工具
    确认时被误当成回答）。交互模式下首次回车后，只要控制台缓冲区仍有
    排队内容就继续读取，直到出现短暂静默——手动输入的下一句话必然晚于
    该静默窗口，不会被误并进来。非交互 stdin（管道）不做合并，行为不变。
    """
    first = input("\n你> ")
    if not sys.stdin.isatty():
        return first
    lines = [first]
    while True:
        if not stdin_has_pending():
            time.sleep(0.05)
            if not stdin_has_pending():
                return "\n".join(lines)
        lines.append(input())


def repl(agent: Agent):
    print("CodeAgent 已启动 (输入 /help 查看命令)")

    while True:
        try:
            user_input = read_user_input().strip()
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
        if user_input == "/usage":
            print(agent.session.usage.summary())
            continue
        if user_input == "/help":
            print(HELP)
            continue

        try:
            agent.run(user_input)  # 回复已在流式过程中实时打印
            _print_usage_hint(agent)
        except Exception as e:  # noqa: BLE001
            print(f"\n[错误] {type(e).__name__}: {e}")


def run_once(agent: Agent, task: str):
    try:
        agent.run(task)  # 回复已在流式过程中实时打印
        _print_usage_hint(agent)
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