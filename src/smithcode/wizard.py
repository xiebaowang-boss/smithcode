"""`smithcode setup` 初始化向导：交互式采集 接口地址 / 模型 / Key / 上下文预算。

写入 ~/.smithcode/config.toml（[provider] 与 [context] 段）和 credentials.json
（仅 key）。重跑幂等：提示符默认值取当前生效配置，直接回车即保留；文件中
其余段落（permissions、limits 等用户手写内容）永不触碰。
"""
from __future__ import annotations

import getpass
import json
import math
import os

import tomlkit

from . import config

DEFAULT_URL = "https://api.deepseek.com/v1"
DEFAULT_BUDGET = 65536

_CONFIG_TEMPLATE = """\
# SmithCode 配置（本文件不含秘密、可分享；API Key 在同级 credentials.json）
# 优先级：内置默认 < 本文件 < 环境变量（SMITHCODE_KEY / SMITHCODE_MODEL / SMITHCODE_URL）< CLI 参数

[provider]
url = "{url}"
model = "{model}"

[context]
budget = {budget}  # 上下文预算（token），建议不超过模型窗口大小
# compact_trigger = 0.8        # 占预算的比例，越过即触发自动压缩
# compact_keep_tokens = 15000  # 压缩时尾部原样保留的 token 数

[limits]
# max_iterations = 30       # 单次任务最大迭代轮数
# command_timeout = 60      # run_command 超时（秒）
# max_tool_output = 20000   # 工具输出进入上下文的最大字符数
# max_retries = 3           # LLM 瞬时错误（限流/断网/5xx）自动重试次数
# llm_timeout = 120         # 单次 LLM 请求超时（秒）

[permissions]
# 动作：allow / ask / deny；最后一条匹配的规则生效（宽泛在前、精确在后）
# read_file = "allow"
# write_file = {{ "*" = "ask" }}
# run_command = {{ "*" = "ask", "git status" = "allow", "rm -rf*" = "deny" }}

# 工具调用的终端展示粒度：summary（默认）/ detail
# tool_display = "summary"
"""


def _ask(prompt: str, current: str) -> str:
    raw = input(f"{prompt} [{current}]: ").strip()
    return raw or current


def _ask_secret(current: str) -> str:
    """读 API Key：不回显；已有配置时回车即保留。无终端环境 getpass 会抛，退化为普通输入。"""
    hint = "API Key (已配置，回车保留；输入新值则覆盖): " if current else "API Key (输入不回显): "
    try:
        raw = getpass.getpass(hint)
    except Exception:  # noqa: BLE001
        raw = input(hint)
    return raw.strip() or current


def _ask_int(prompt: str, current: int) -> int:
    """读数值：支持 128k / 64K 这类后缀写法；非法输入警告后保留当前值。"""
    raw = input(f"{prompt} [{current}]: ").strip()
    if not raw:
        return current
    try:
        text, scale = (raw[:-1], 1000) if raw[-1] in "kK" else (raw, 1)
        value = float(text) * scale
        if not math.isfinite(value):
            raise ValueError(raw)
        return int(value)
    except ValueError:
        print(f"[警告] {raw!r} 不是有效数字，已保留 {current}")
        return current


def _write_credentials(path, key: str):
    """把 key 写入 credentials.json（空 key 不动文件）；已有其他字段原样保留。"""
    if not key:
        return
    data = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                data = existing
        except (OSError, json.JSONDecodeError):
            data = {}
    data["key"] = key
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)  # 仅本机可读；Windows 上尽力而为
    except OSError:
        pass


def _write_config(path, *, url: str, model: str, budget: int):
    """更新 config.toml 的 [provider] 与 [context] 段；tomlkit 保留注释与既有其他段落。"""
    if path.is_file():
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
        provider = doc.setdefault("provider", tomlkit.table())
        provider["url"] = url
        provider["model"] = model
        context = doc.setdefault("context", tomlkit.table())
        context["budget"] = budget
        path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    else:
        fresh = _CONFIG_TEMPLATE.format(url=url, model=model, budget=budget)
        path.write_text(fresh, encoding="utf-8")


def run_setup() -> int:
    """执行向导，返回进程退出码。"""
    home = config.smithcode_home()
    print(f"SmithCode 初始化（配置将写入 {home}）\n")

    if os.getenv("SMITHCODE_KEY"):
        print("提示：已检测到环境变量 SMITHCODE_KEY，它的优先级高于凭据文件。\n")

    try:
        url = _ask("接口地址", config.URL or DEFAULT_URL)
        model = _ask("模型名", config.MODEL)
        key = _ask_secret(config.KEY)
        budget = _ask_int("上下文预算 token", config.CONTEXT_TOKEN_BUDGET)
    except (EOFError, KeyboardInterrupt):
        print("\n已取消，未做任何修改。")
        return 1

    try:
        home.mkdir(parents=True, exist_ok=True)
        credentials = home / "credentials.json"
        config_file = home / "config.toml"
        _write_credentials(credentials, key)
        _write_config(config_file, url=url, model=model, budget=budget)
    except OSError as e:
        print(f"\n[错误] 写入失败：{e}")
        print("可改用环境变量 SMITHCODE_KEY / SMITHCODE_MODEL / SMITHCODE_URL（无需写盘）。")
        return 1

    print(f"\n✓ {credentials}")
    print(f"✓ {config_file}")
    if not key:
        print("提示：尚未设置 API Key，可重新运行 smithcode setup 或设置环境变量 SMITHCODE_KEY。")
    print("完成。运行 smithcode 开始使用。")
    return 0
