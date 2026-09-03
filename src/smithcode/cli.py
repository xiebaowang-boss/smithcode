import argparse
import sys

from . import __version__, config, context
from .agent import Agent
from .session import Session
from .utils.terminal import read_user_input, setup_console_encoding
from .wizard import run_setup

HELP = """命令:
  /help   显示帮助
  /new    开启新会话
  /save   保存会话记录
  /usage  显示 token 用量统计
  /context 显示上下文占用分布
  /compact 手动压缩上下文
  /exit   退出"""


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
        if user_input == "/exit":
            print("再见!")
            break
        if user_input == "/new":
            agent.session.reset()
            agent.permission.session_rules.clear()
            config.SESSION_EXTRA_ROOTS.clear()
            agent.context.compact_count = 0  # 压缩计数是会话口径，随 /new 清零
            print("已开启新会话。")
            continue
        if user_input == "/save":
            path = agent.session.save()
            print(f"会话已保存到 {path}")
            continue
        if user_input == "/usage":
            print(agent.session.usage.summary())
            continue
        if user_input == "/context":
            print(
                context.report(
                    agent.session.messages,
                    config.CONTEXT_TOKEN_BUDGET,
                    config.COMPACT_TRIGGER,
                    agent.context.last_actual,
                    agent.context.compact_count,
                )
            )
            continue
        if user_input == "/compact":
            if agent.compact():
                print("已压缩上下文。")
            else:
                print("没有可压缩的上下文（历史太短或摘要未生成）。")
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
    else:
        repl(agent)