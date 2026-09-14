"""MCP 密钥：`${VAR}` 引用展开、凭据库存储与输出脱敏。

配置只写引用、值单独存放（安全约定，与项目技能信任同级的边界）：

- 解析链：进程环境变量 > `~/.smithcode/credentials.json` 的
  `mcp.<服务器名>.<变量名>` > 引用自带的默认值（`${VAR:-default}`）；
- 三条都拿不到时记为缺失（`missing`），由调用方决定交互补录或 fail-closed；
- 展开出的值全部登记进全局 `Redactor`，工具结果 / stderr / 日志 / 预览
  展示前统一过一遍，避免密钥泄露到终端或会话转录。

凭据文件写入走原子替换（临时文件在 POSIX 下由 mkstemp 保证 0600）。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from .config import ServerConfig
from .errors import McpConfigError

# ${VAR} 或 ${VAR:-默认值}
_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

# 脱敏值的最短长度：太短的值（如 "1"）全局替换会误伤正常文本
_MIN_REDACT_LEN = 4


@dataclass
class ResolvedServer:
    """展开引用后的 spawn 参数；missing 为缺失且无默认值的变量名（去重保序）。"""

    command: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    cwd: str = ""
    missing: list = field(default_factory=list)


def lookup(server: str, var: str):
    """按解析链取一个变量值；找不到返回 None（空串视为未配置）。"""
    value = os.environ.get(var)
    if value:
        return value
    tree = config._read_credentials().get("mcp")
    if isinstance(tree, dict):
        entry = tree.get(server)
        if isinstance(entry, dict):
            value = entry.get(var)
            if isinstance(value, str) and value:
                return value
    return None


def expand_text(text: str, server: str, missing: list) -> str:
    """展开文本中的 `${VAR}` 引用；缺失且无默认值的引用原样保留并记入 missing。"""
    def replace(match: re.Match) -> str:
        var, default = match.group(1), match.group(2)
        value = lookup(server, var)
        if value is not None:
            redactor().add(value)
            return value
        if default is not None:
            return default
        if var not in missing:
            missing.append(var)
        return match.group(0)

    return _REF_PATTERN.sub(replace, text)


def resolve(cfg: ServerConfig) -> ResolvedServer:
    """展开一个服务器配置中的全部引用（command / env / cwd）。

    展开失败不抛异常：缺失的变量收集在 `ResolvedServer.missing` 里，
    调用方据此走交互补录或 fail-closed，而不是带着空值把 server 拉起来。
    """
    missing: list = []
    command = [expand_text(item, cfg.name, missing) for item in cfg.command]
    env = {key: expand_text(value, cfg.name, missing) for key, value in cfg.env.items()}
    cwd = expand_text(cfg.cwd, cfg.name, missing) if cfg.cwd else ""
    return ResolvedServer(command=command, env=env, cwd=cwd, missing=missing)


def missing_refs(cfg: ServerConfig) -> list:
    """只做缺失检查（不产生副作用），供启动预检与 /mcp 状态展示。"""
    return resolve(cfg).missing


def store_secret(server: str, var: str, value: str) -> Path:
    """把一个变量写入凭据库的 `mcp.<server>.<var>`；保留文件内其他内容。"""
    if not var:
        raise McpConfigError("变量名不能为空")
    path = config.credentials_path()
    data = config._read_credentials()
    tree = data.get("mcp")
    if not isinstance(tree, dict):
        tree = {}
        data["mcp"] = tree
    entry = tree.get(server)
    if not isinstance(entry, dict):
        entry = {}
        tree[server] = entry
    entry[var] = value

    _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    if value:
        redactor().add(value)
    return path


def clear_secret(server: str, var: str) -> bool:
    """删除一个凭据；不存在返回 False。"""
    path = config.credentials_path()
    data = config._read_credentials()
    tree = data.get("mcp")
    if not isinstance(tree, dict):
        return False
    entry = tree.get(server)
    if not isinstance(entry, dict) or var not in entry:
        return False
    del entry[var]
    _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return True


class Redactor:
    """已知敏感值的内存登记与文本脱敏（线程安全）。"""

    def __init__(self):
        self._values: set = set()
        self._lock = threading.Lock()

    def add(self, value: str) -> None:
        if isinstance(value, str) and len(value) >= _MIN_REDACT_LEN:
            with self._lock:
                self._values.add(value)

    def scrub(self, text: str) -> str:
        if not text:
            return text
        with self._lock:
            values = sorted(self._values, key=len, reverse=True)
        for value in values:
            if value in text:
                text = text.replace(value, "***")
        return text

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


_redactor = Redactor()


def redactor() -> Redactor:
    """全局脱敏器：所有展开出的敏感值都登记在这里。"""
    return _redactor


def _atomic_write(path: Path, text: str) -> None:
    """原子写凭据文件；临时文件由 mkstemp 创建，POSIX 下权限 0600。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
