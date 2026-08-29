import argparse
import sys

from . import __version__, config
from .agent import Agent
from .session import Session

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
            reply = agent.run(user_input)
            print(f"\n助手> {reply}\n")
        except Exception as e:
            print(f"\n[错误] {type(e).__name__}: {e}")


def run_once(agent: Agent, task: str):
    try:
        reply = agent.run(task)
        print(f"\n助手> {reply}\n")
    except Exception as e:
        print(f"\n[错误] {type(e).__name__}: {e}")
        sys.exit(1)


def main(argv=None):
    import io
    import os
    
    # 强制设置 UTF-8 编码
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    
    # 重新配置 stdout 为 UTF-8
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
        except Exception:
            pass
    
    if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
        try:
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
        except Exception:
            pass

    args = build_parser().parse_args(argv)
    if args.workspace:
        config.set_workspace(args.workspace)
    if args.model:
        config.MODEL = args.model

    agent = Agent(Session(), max_iterations=args.max_iterations)
    if args.yes:
        agent.permission.approved_all = True

    if args.task:
        run_once(agent, " ".join(args.task))
    else:
        repl(agent)