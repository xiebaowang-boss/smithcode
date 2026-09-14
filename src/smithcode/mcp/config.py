"""MCP 服务器配置：双作用域加载、合并与原子写入。

作用域与文件：

- 用户级：`~/.smithcode/config.toml` 的 `[mcp.servers.<名称>]`（TOML，
  写入用 tomlkit，保留用户注释与其他配置）；
- 项目级：`<工作区>/.smithcode/mcp.json`（JSON，`mcpServers` 结构，兼容
  Claude/Cursor 生态的片段写法，随仓库分发）。

启停状态：`enabled` 是服务器条目的普通字段，写在定义它的文件里（用户 TOML 或
项目 JSON），默认启用时省略该键——不做跨文件的覆盖表。

合并规则：同名服务器**项目条目整体覆盖用户条目**（字段不做深合并），
与 Claude Code / opencode v2 的语义一致，避免合并出半个配置。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit

from .. import config
from .errors import McpConfigError

# 单次工具调用的默认超时（秒）；条目可覆盖
DEFAULT_TIMEOUT = 60.0

# 允许的传输类型；MVP 仅 stdio（别名 local 兼容部分生态写法）
_SUPPORTED_TYPES = ("stdio", "local")


@dataclass
class ServerConfig:
    """一个 MCP 服务器的归一化配置（双作用域解析后的统一形态）。"""

    name: str
    command: list = field(default_factory=list)  # argv（首项为可执行文件）
    env: dict = field(default_factory=dict)      # 值可含 ${VAR} 引用，spawn 前展开
    cwd: str = ""
    timeout: float = DEFAULT_TIMEOUT
    enabled: bool = True
    scope: str = "user"     # "user" | "project"（来源作用域）
    source: str = ""        # 来源文件路径（展示/诊断用）

    @property
    def fingerprint(self) -> str:
        """配置指纹：命令/参数/工作目录/环境键集变化的稳定摘要。"""
        payload = json.dumps(
            {"command": self.command, "cwd": self.cwd, "env": sorted(self.env)},
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class LoadResult:
    """一次配置装载的结果：合并后的服务器 + 诊断信息 + 项目文件路径。"""

    servers: list = field(default_factory=list)
    diagnostics: list = field(default_factory=list)
    project_path: Path | None = None


# ---------- 路径 ----------

def project_config_path() -> Path:
    """项目级 MCP 配置文件：<工作区>/.smithcode/mcp.json。"""
    return Path(config.WORKSPACE_ROOT) / ".smithcode" / "mcp.json"


# ---------- 加载 ----------

def load_servers() -> LoadResult:
    """装载并合并用户级与项目级配置；坏条目只诊断、不阻断。

    解析全程容错：类型错误、缺 command、暂不支持的 transport 都记入
    diagnostics 并跳过该条，保证一个坏 server 不影响其余条目与启动。
    """
    diagnostics: list = []
    merged: dict = {}

    section = config._read_config_file().get("mcp")
    if section is None:
        section = {}
    if not isinstance(section, dict):
        diagnostics.append("config.toml 的 [mcp] 段不是配置表，已忽略")
        section = {}

    servers_table = section.get("servers")
    if servers_table is None:
        servers_table = {}
    if not isinstance(servers_table, dict):
        diagnostics.append("config.toml 的 mcp.servers 不是配置表，已忽略")
        servers_table = {}
    for name, entry in servers_table.items():
        parsed = _parse_entry(
            str(name), entry, scope="user",
            source=str(config.config_path()), diagnostics=diagnostics,
        )
        if parsed is not None:
            merged[parsed.name] = parsed

    project = project_config_path()
    if project.is_file():
        data, error = _read_project_data(project)
        if error:
            diagnostics.append(error)
        else:
            entries = data.get("mcpServers")
            if entries is None:
                entries = {}
            if not isinstance(entries, dict):
                diagnostics.append(f"{project} 的 mcpServers 不是对象，已忽略")
            else:
                for name, entry in entries.items():
                    parsed = _parse_entry(
                        str(name), entry, scope="project",
                        source=str(project), diagnostics=diagnostics,
                    )
                    if parsed is not None:
                        merged[parsed.name] = parsed  # 项目条目整体覆盖同名用户条目

    return LoadResult(
        servers=list(merged.values()),
        diagnostics=diagnostics,
        project_path=project if project.is_file() else None,
    )


def _parse_entry(name: str, entry, scope: str, source: str, diagnostics: list):
    """归一化一条服务器配置；不可用时记录诊断并返回 None。"""
    if not name.strip():
        diagnostics.append(f"{source}: 存在空名称的 mcp 服务器条目，已忽略")
        return None
    if not isinstance(entry, dict):
        diagnostics.append(f"{source}: mcp 服务器 {name!r} 的配置不是表/对象，已忽略")
        return None

    transport = entry.get("type", "stdio")
    if not isinstance(transport, str) or transport.lower() not in _SUPPORTED_TYPES:
        diagnostics.append(
            f"{source}: mcp 服务器 {name!r} 的 type={transport!r} 暂不支持"
            f"（MVP 仅支持 stdio），已忽略"
        )
        return None

    command = _parse_command(name, entry, source, diagnostics)
    if not command:
        return None

    env: dict = {}
    raw_env = entry.get("env")
    if raw_env is not None:
        if not isinstance(raw_env, dict):
            diagnostics.append(f"{source}: mcp 服务器 {name!r} 的 env 不是表/对象，已忽略")
        else:
            for key, value in raw_env.items():
                if isinstance(value, str):
                    env[str(key)] = value
                else:
                    diagnostics.append(
                        f"{source}: mcp 服务器 {name!r} 的 env.{key} 不是字符串，已忽略"
                    )

    raw_cwd = entry.get("cwd")
    cwd = raw_cwd if isinstance(raw_cwd, str) else ""
    if raw_cwd is not None and not isinstance(raw_cwd, str):
        diagnostics.append(f"{source}: mcp 服务器 {name!r} 的 cwd 不是字符串，已忽略")

    timeout = DEFAULT_TIMEOUT
    raw_timeout = entry.get("timeout")
    if raw_timeout is not None:
        if (isinstance(raw_timeout, (int, float)) and not isinstance(raw_timeout, bool)
                and raw_timeout > 0):
            timeout = float(raw_timeout)
        else:
            diagnostics.append(
                f"{source}: mcp 服务器 {name!r} 的 timeout={raw_timeout!r} 不是正数，"
                f"已用默认值 {DEFAULT_TIMEOUT:g}"
            )

    enabled = entry.get("enabled", True)
    if not isinstance(enabled, bool):
        diagnostics.append(f"{source}: mcp 服务器 {name!r} 的 enabled 不是布尔值，按启用处理")
        enabled = True

    return ServerConfig(
        name=name, command=command, env=env, cwd=cwd,
        timeout=timeout, enabled=enabled, scope=scope, source=source,
    )


def _parse_command(name: str, entry: dict, source: str, diagnostics: list) -> list:
    """解析命令：支持 command 字符串 + args 数组（生态写法）或 command 数组。"""
    raw_command = entry.get("command")
    if isinstance(raw_command, str):
        argv = [raw_command]
    elif isinstance(raw_command, list):
        argv = [item for item in raw_command if isinstance(item, str)]
        if len(argv) != len(raw_command):
            diagnostics.append(f"{source}: mcp 服务器 {name!r} 的 command 含非字符串项，已忽略")
    else:
        diagnostics.append(f"{source}: mcp 服务器 {name!r} 缺少 command，已忽略")
        return []

    raw_args = entry.get("args")
    if raw_args is not None:
        if not isinstance(raw_args, list):
            diagnostics.append(f"{source}: mcp 服务器 {name!r} 的 args 不是数组，已忽略")
        else:
            for item in raw_args:
                if isinstance(item, str):
                    argv.append(item)
                elif isinstance(item, (int, float)) and not isinstance(item, bool):
                    argv.append(str(item))
                else:
                    diagnostics.append(
                        f"{source}: mcp 服务器 {name!r} 的 args 含非字符串项，已忽略"
                    )

    if not argv:
        diagnostics.append(f"{source}: mcp 服务器 {name!r} 的命令为空，已忽略")
    return argv


# ---------- 写入（用户级） ----------

def write_user_server(cfg: ServerConfig) -> Path:
    """写入/覆盖用户级服务器配置（tomlkit 保留注释）。"""
    if not cfg.command:
        raise McpConfigError(f"服务器 {cfg.name!r} 的命令为空，无法写入")
    path = config.config_path()
    doc = _load_toml_document(path)
    mcp = doc.get("mcp")
    if mcp is None:
        mcp = tomlkit.table()
        doc["mcp"] = mcp
    if not isinstance(mcp, dict):
        raise McpConfigError(f"{path} 的 [mcp] 段不是配置表，无法写入")
    servers = mcp.get("servers")
    if servers is None:
        servers = tomlkit.table()
        mcp["servers"] = servers

    entry = tomlkit.table()
    if not cfg.enabled:
        entry["enabled"] = False  # 启停字段放最前
    entry["command"] = list(cfg.command)
    if cfg.env:
        # 内联表：一个服务器的属性（enabled / command / env / cwd / timeout）聚合在同一段
        inline = tomlkit.inline_table()
        for key, value in cfg.env.items():
            inline[key] = value
        entry["env"] = inline
    if cfg.cwd:
        entry["cwd"] = cfg.cwd
    if cfg.timeout != DEFAULT_TIMEOUT:
        entry["timeout"] = cfg.timeout
    servers[cfg.name] = entry
    mcp["servers"] = servers

    _atomic_write(path, tomlkit.dumps(doc))
    return path


def remove_user_server(name: str) -> bool:
    """从用户配置删除一个服务器；不存在返回 False。"""
    path = config.config_path()
    if not path.is_file():
        return False
    doc = _load_toml_document(path)
    mcp = doc.get("mcp")
    servers = mcp.get("servers") if isinstance(mcp, dict) else None
    if not isinstance(servers, dict) or name not in servers:
        return False
    del servers[name]
    _atomic_write(path, tomlkit.dumps(doc))
    return True


def set_enabled(name: str, enabled: bool) -> Path:
    """记录启停状态：`enabled` 写在定义该服务器的文件里，属性聚合同一段。

    用户级服务器写 `[mcp.servers.<名称>]` 条目；项目级服务器写
    `.smithcode/mcp.json` 的对应条目（个人启停会改到共享文件，这是不加
    覆盖表的取舍）。启用是默认值：删除 `enabled` 键而不是写 `true`。
    """
    scope = _effective_scope(name)
    if scope is None:
        raise McpConfigError(f"未找到 MCP 服务器 {name!r}，无法设置启停")
    target = _set_enabled_project if scope == "project" else _set_enabled_user
    return target(name, bool(enabled))


def _effective_scope(name: str):
    """合并后该服务器的来源作用域；未找到返回 None。"""
    for cfg in load_servers().servers:
        if cfg.name == name:
            return cfg.scope
    return None


def _set_enabled_user(name: str, enabled: bool) -> Path:
    path = config.config_path()
    doc = _load_toml_document(path)
    mcp = doc.get("mcp")
    servers = mcp.get("servers") if isinstance(mcp, dict) else None
    if not isinstance(servers, dict) or name not in servers:
        raise McpConfigError(f"{path} 中未找到服务器 {name!r}")
    entry = servers[name]
    rebuilt = tomlkit.table()
    if not enabled:
        rebuilt["enabled"] = False  # 启停字段始终放最前
    for key, value in entry.items():
        if key == "enabled":
            continue
        rebuilt[key] = value
    servers[name] = rebuilt
    _atomic_write(path, tomlkit.dumps(doc))
    return path


def _set_enabled_project(name: str, enabled: bool) -> Path:
    path = project_config_path()
    data, error = _read_project_data(path)
    if error:
        raise McpConfigError(error)
    servers = data.get("mcpServers")
    entry = servers.get(name) if isinstance(servers, dict) else None
    if not isinstance(entry, dict):
        raise McpConfigError(f"{path} 中未找到服务器 {name!r}")
    rebuilt: dict = {}
    if not enabled:
        rebuilt["enabled"] = False  # 启停字段始终放最前
    for key, value in entry.items():
        if key == "enabled":
            continue
        rebuilt[key] = value
    servers[name] = rebuilt
    _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return path


# ---------- 写入（项目级） ----------

def write_project_server(cfg: ServerConfig) -> Path:
    """写入/覆盖项目级 `.smithcode/mcp.json`，保留其他 keys 与未知字段。"""
    if not cfg.command:
        raise McpConfigError(f"服务器 {cfg.name!r} 的命令为空，无法写入")
    path = project_config_path()
    data, error = _read_project_data(path)
    if error:
        raise McpConfigError(error)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers

    entry: dict = {}
    if not cfg.enabled:
        entry["enabled"] = False  # 启停字段放最前
    entry["type"] = "stdio"
    entry["command"] = cfg.command[0]
    if len(cfg.command) > 1:
        entry["args"] = list(cfg.command[1:])
    if cfg.env:
        entry["env"] = dict(cfg.env)
    if cfg.cwd:
        entry["cwd"] = cfg.cwd
    if cfg.timeout != DEFAULT_TIMEOUT:
        entry["timeout"] = cfg.timeout
    servers[cfg.name] = entry

    _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return path


def remove_project_server(name: str) -> bool:
    """从项目配置删除一个服务器；文件缺失或条目不存在返回 False。"""
    path = project_config_path()
    if not path.is_file():
        return False
    data, error = _read_project_data(path)
    if error:
        raise McpConfigError(error)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or name not in servers:
        return False
    del servers[name]
    _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return True


# ---------- 内部工具 ----------

def _load_toml_document(path: Path):
    """读取 config.toml 为可编辑的 tomlkit 文档；缺失/损坏给出明确报错。"""
    if not path.is_file():
        return tomlkit.document()
    try:
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    except (OSError, tomlkit.exceptions.ParseError) as e:
        raise McpConfigError(f"无法解析 {path}: {e}") from e


def _read_project_data(path: Path):
    """读取项目 JSON；返回 (data, error)。缺失视为空文档。"""
    if not path.is_file():
        return {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {}, f"无法解析 {path}: {e}"
    if not isinstance(data, dict):
        return {}, f"{path} 的顶层不是对象，已忽略"
    return data, None


def _atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    """临时文件 + os.replace 原子落盘；写入失败不破坏原文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        if mode is not None and os.name != "nt":
            os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
