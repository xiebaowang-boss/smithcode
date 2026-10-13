"""无 UI 设置服务：读/写 `~/.smithcode/` 的纯逻辑（终端向导与桌面设置页共用）。

分层：本模块只做"读现状、写文件、调 `config.reload()`"，不做任何 `input()` /
`print()`——交互（终端提问 / Tauri 设置页）由调用方负责。所以 `wizard.run_setup()`
的采集部分留在 wizard，本模块只留可复用的写盘与快照逻辑。
"""

from __future__ import annotations

import json
from pathlib import Path

import tomlkit

from . import config

DEFAULT_URL = "https://api.deepseek.com/v1"


def _restrict(path: Path) -> None:
    """凭据文件仅属主可读写（Windows 上尽力而为）。"""
    try:
        path.chmod(0o600)
    except OSError:
        pass


def snapshot() -> dict:
    """当前生效配置的快照（设置页初值 / 诊断展示用，不含 key 明文）。

    key 只报"是否已配"，不回显——设置页按"空=保留、非空=覆盖"处理。
    """
    return {
        "url": config.URL or DEFAULT_URL,
        "model": config.MODEL,
        "effort": config.REASONING_EFFORT or config.DEFAULT_EFFORT,
        "key_configured": bool(config.KEY.strip()),
        "budget": config.CONTEXT_TOKEN_BUDGET,
    }


def _write_credentials(path: Path, key: str) -> None:
    """把 key 写入 credentials.json（空值不动）；已有其他字段原样保留。"""
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
    _restrict(path)


def _write_provider_context(path: Path, *, url: str, model: str, budget: int) -> None:
    """更新 config.toml 的 [provider] 与 [context] 段；tomlkit 保留注释与既有段落。"""
    if path.is_file():
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
        provider = doc.setdefault("provider", tomlkit.table())
        provider["url"] = url
        provider["model"] = model
        context = doc.setdefault("context", tomlkit.table())
        context["budget"] = budget
        path.write_text(tomlkit.dumps(doc), encoding="utf-8")
        return
    from .wizard import _CONFIG_TEMPLATE

    path.write_text(
        _CONFIG_TEMPLATE.format(url=url, model=model, budget=budget), encoding="utf-8"
    )


def apply_provider(*, url: str, model: str, key: str = "", budget: int = 0) -> dict:
    """应用 provider 设置：写盘 + `config.reload()`，返回变更集。

    空 key = 保留已有；`budget <= 0` = 不动 budgets 段。写盘失败抛 `OSError`，
    由调用方转成人话（终端打印 / HTTP 500）。
    """
    home = config.smithcode_home()
    home.mkdir(parents=True, exist_ok=True)
    current = snapshot()
    _write_credentials(home / "credentials.json", key.strip() if key else "")
    _write_provider_context(
        home / "config.toml",
        url=url.strip() or current["url"],
        model=model.strip() or current["model"],
        budget=int(budget) if budget and budget > 0 else current["budget"],
    )
    return config.reload()
