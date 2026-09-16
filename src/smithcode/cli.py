import argparse
import sys
import threading
from pathlib import Path

from . import __version__, commands, config, renderer, sessions, title
from .agent import INTERRUPTED_NOTE, Agent
from .session import Session
from .utils.proxy import normalize_proxy_env
from .utils.terminal import (
    confirmations_available,
    read_user_input,
    setup_console_encoding,
)
from .wizard import run_setup


def build_parser():
    parser = argparse.ArgumentParser(
        prog="smith",
        description="终端 AI 编程助手：让大模型调用工具帮你读写文件、执行命令。\n"
        "首次使用先运行 `smith setup` 完成初始化。",
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
    session_group = parser.add_mutually_exclusive_group()
    session_group.add_argument(
        "-c", "--continue", dest="continue_session", action="store_true",
        help="恢复当前目录最近一次的交互会话",
    )
    session_group.add_argument(
        "-r", "--resume", dest="resume", nargs="?", const="", default=None,
        metavar="ID",
        help="恢复指定会话（id 或唯一前缀；不带值时恢复最近一次）",
    )
    parser.add_argument(
        "--name", metavar="名称",
        help="给新会话命名，便于之后按名称查找与恢复",
    )
    parser.add_argument(
        "--no-session-persistence", action="store_true",
        help="本次运行不保存会话记录（不可与 -c/--resume 同用）",
    )
    parser.add_argument(
        "-y", "--yes", action="store_true",
        help="自动批准所有工具调用（含工作区外路径访问），不再逐个询问；deny 规则依然生效",
    )
    parser.add_argument(
        "--max-iterations", type=int, default=None, metavar="N",
        help="单次任务最大迭代轮数；-1 为不限（默认），正整数达上限后会请求模型总结收尾",
    )
    parser.add_argument(
        "-V", "--version", action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def _run_agent_task(agent: Agent, text: str) -> None:
    """后台线程执行一次任务：结果/错误在流式过程中实时打印。"""
    try:
        result = agent.run_with_goal(text)  # 目标激活时自动续跑，无目标等价 run
    except Exception as e:  # noqa: BLE001
        print(f"\n[错误] {type(e).__name__}: {e}")
        return
    if result.status == "interrupted":
        print(INTERRUPTED_NOTE)


def _wait_for_task(agent: Agent, task: threading.Thread) -> None:
    """等待后台任务结束，主线程专职做取消通道。

    第一次 Ctrl+C：走与 TUI Esc 相同的协作式取消（流截停 + 会话修复），
    继续等待任务收尾；第二次 Ctrl+C：不再等待、直接退出进程——正在执行的
    工具最长可跑 300 秒，必须有明确的逃生通道（TUI 对应 Ctrl+Q）。

    短轮询 join：Windows 下长阻塞 join 不能及时响应 Ctrl+C。
    """
    halted = False
    while task.is_alive():
        try:
            task.join(0.2)
        except KeyboardInterrupt:
            if not halted:
                halted = True
                agent.interrupt()
                print("\n（已中断，等待当前步骤收尾… 再按一次 Ctrl+C 退出）")
            else:
                print("\n再见!")
                raise SystemExit(130)


def repl(agent: Agent):
    from .welcome import welcome_text

    print(welcome_text(compact=True))

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
            if outcome.select is not None:
                _print_select(outcome.select)
            if outcome.wizard is not None:
                _run_wizard(agent, outcome.wizard)
            if outcome.start_task is not None:
                # /goal 设定/恢复后立即开跑，走与普通任务相同的后台线程 + 取消通道
                task = threading.Thread(
                    target=_run_agent_task, args=(agent, outcome.start_task), daemon=True
                )
                task.start()
                _wait_for_task(agent, task)
            continue

        # 任务放后台线程跑，主线程留作取消通道：Ctrl+C 时经 agent.interrupt()
        # 走与 TUI Esc 相同的协作式取消（流截停 + 会话修复），而非整个进程退出
        task = threading.Thread(target=_run_agent_task, args=(agent, user_input), daemon=True)
        task.start()
        _wait_for_task(agent, task)


def _print_select(select):
    """非交互 REPL 无法弹选择器：列出候选并提示改用带参形式（fail-closed）。"""
    print(f"{select.title}:")
    for index, choice in enumerate(select.items, 1):
        mark = "（当前）" if choice.current else ""
        print(f"  {index}. {choice.label}{mark}")
    print(f"非交互模式无法弹出选择器，请用 /{select.command} <候选值> 指定。")


def _run_wizard(agent, wizard) -> None:
    """REPL 行式向导：完成计划后存密钥、写配置并触发后台连接。"""
    from .mcp.errors import McpConfigError
    from .mcp.wizard import apply_plan, run_line_mode

    if getattr(wizard, "name", "") != "mcp.add":
        print(f"不支持的向导: {getattr(wizard, 'name', '?')}")
        return
    plan = run_line_mode(wizard.payload)
    if plan is None:
        print("已取消。")
        return
    try:
        apply_plan(agent.mcp, plan)
    except McpConfigError as e:
        print(f"添加失败: {e}")
        return
    print(f"已保存 MCP 服务器 {plan.config.name}，正在后台连接…用 /mcp 查看状态。")


def run_once(agent: Agent, task: str):
    try:
        result = agent.run_with_goal(task)  # 回复已在流式过程中实时打印
    except Exception as e:  # noqa: BLE001
        print(f"\n[错误] {type(e).__name__}: {e}")
        sys.exit(1)
    if result.status == "interrupted":
        print(INTERRUPTED_NOTE)


def _cleanup_old_sessions() -> None:
    """启动时按 [sessions].cleanup_days 清理过期转录（best-effort，静默）。"""
    cfg = config.load_sessions_config()
    if not cfg.enabled or cfg.cleanup_days <= 0:
        return
    try:
        sessions.sweep(cfg.cleanup_days)
    except Exception:  # noqa: BLE001 清理失败不影响启动
        return


def _locate_session(last: bool, target: str):
    """定位 -c/--resume 的目标；无匹配返回 None，歧义/坏文件抛 StoreError。"""
    if last or target == "":
        return sessions.find_last()
    path = Path(target)
    if path.is_file():
        if path.suffix == ".json":
            return sessions.import_json(path)  # 旧格式：导入后继续
        return sessions.summary_from_path(path)
    summary = sessions.find(target)
    if summary is None:
        raise sessions.StoreError(f"未找到会话：{target}")
    return summary


def _resume_session(agent: Agent, last: bool, target: str) -> None:
    """启动时恢复会话：失败只提示并保持新会话，绝不阻断启动。"""
    try:
        summary = _locate_session(last, target)
    except sessions.StoreError as exc:
        print(f"[会话] {exc}；已开始新会话。")
        return
    if summary is None:
        print("[会话] 当前目录没有可恢复的会话，已开始新会话。")
        return
    try:
        report = agent.resume(summary)
    except sessions.StoreError as exc:
        print(f"[会话] 恢复失败：{exc}；已开始新会话。")
        return
    note = f"[会话] 已恢复 {report.session_id[:8]}（{report.message_count} 条消息）"
    if report.title:
        note += f"：{report.title}"
    if report.repair == "appended":
        note += "；已修复中断留下的未完成工具调用"
    elif report.repair == "truncated":
        note += "；检测到历史损坏，已截断修复"
    if report.bad_lines:
        note += f"；跳过 {report.bad_lines} 行损坏记录"
    print(note)


def main(argv=None):
    # 必须早于任何 OpenAI / httpx 客户端的构造：把系统代理写入的 socks://
    # 归一化为 httpx 认的 socks5://，否则 FlClash 等一设代理就启动即崩。
    normalize_proxy_env()
    setup_console_encoding()

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.task == ["setup"]:  # 仅当唯一位置参数恰好是 setup，避免 subparsers 破坏自由文本任务
        sys.exit(run_setup())
    restoring = args.continue_session or args.resume is not None
    if args.name and restoring:
        parser.error("--name 只能用于新会话，不能与 -c/--resume 同用")
    if args.no_session_persistence and restoring:
        parser.error("--no-session-persistence 与 -c/--resume 不能同用")
    if args.workspace:
        config.set_workspace(args.workspace)
    for extra in args.add or []:
        config.add_workspace(extra)
    if args.model:
        config.MODEL = args.model

    try:
        agent = Agent(
            Session(),
            max_iterations=args.max_iterations,
            persist=not args.no_session_persistence,
            oneshot=bool(args.task),
        )
    except config.ConfigError as e:
        print(e)  # 指引文案由 ConfigError 自带，打印后安静退出，不甩 traceback
        sys.exit(1)
    if args.yes:
        agent.permission.approved_all = True
    agent.start()  # 启动模型目录：外部未配置时后台拉取 /models（不阻塞启动）
    _cleanup_old_sessions()
    interactive = not args.task and confirmations_available()
    if interactive:
        # 尽早接管窗口标题：`--name` / 恢复会话的标题事件发生在宿主启动之前，
        # 由 title.Relay 转给呈现器暂存，宿主首屏时统一写出（非 tty 自动失效）
        renderer.set_renderer(title.attach(renderer.current()))
    if restoring:
        _resume_session(agent, args.continue_session, args.resume)
    if args.name:
        agent.rename_session(args.name)

    try:
        if args.task:
            run_once(agent, " ".join(args.task))
        elif interactive:  # 交互终端：全屏 TUI（Textual）
            from .tui import run_tui

            run_tui(agent)
        else:
            repl(agent)  # 非 tty（管道/CI）保持富行式 REPL
    finally:
        agent.close()  # flush + 关闭转录句柄