"""会话存储路径：按项目分区放在用户目录下，绝不写入工作区。

布局：`<smithcode_home>/projects/<slug>/sessions/<session-id>.jsonl`。
slug 由 cwd 生成：可读前缀（非法字符替换为 `-`，截断 48 字符）+ 8 位路径
hash 消歧——长路径、中文路径、Windows 盘符都安全且确定性。

slug 是**不可逆**的（sha1 摘要），而多项目要按 slug 反查目录（跨项目历史、
文件面板），所以另有项目注册表 `projects/projects.json`：`slug → {cwd, name}`。
注册表只是加速表——丢了照样能从会话日志的 `session.created.cwd` 回填
（见 `sessions/store.py` 的 `resolve_project`），写失败一律静默降级。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

from .. import config

_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9-]+")
MAX_SLUG_PREFIX = 48

#: 注册表文件名（位于 `projects_dir()` 下；遍历项目目录时它是文件，天然被跳过）
REGISTRY_NAME = "projects.json"

#: 注册表读写锁：serve 的 HTTP 线程并发登记/查询
_registry_lock = threading.Lock()


def project_slug(cwd=None) -> str:
    """工作目录 → 项目目录名（可读前缀 + 路径 hash 后缀）。"""
    raw = str(Path(cwd or config.WORKSPACE_ROOT).resolve())
    prefix = _SLUG_UNSAFE.sub("-", raw).strip("-") or "root"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"{prefix[:MAX_SLUG_PREFIX]}-{digest}"


def sessions_dir(cwd=None) -> Path:
    """当前项目的会话目录（可能尚不存在）。"""
    return config.smithcode_home() / "projects" / project_slug(cwd) / "sessions"


def session_path(session_id: str, cwd=None) -> Path:
    return sessions_dir(cwd) / f"{session_id}.jsonl"


def projects_dir() -> Path:
    """全部项目的会话根目录（保留期清理遍历用）。"""
    return config.smithcode_home() / "projects"


def ensure_private_dir(path: Path) -> None:
    """创建目录并收紧权限（POSIX 0700；Windows 下 chmod 语义有限，失败忽略）。"""
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)


def restrict_file(path: Path) -> None:
    """会话转录含工作区内容与对话，尽量收紧为仅属主可读写（0600）。"""
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


# --------------------------------------------------------------------------
# 项目注册表（slug → cwd）
# --------------------------------------------------------------------------


def registry_path() -> Path:
    return projects_dir() / REGISTRY_NAME


def read_registry() -> dict:
    """读注册表 `{slug: {cwd, name, created}}`；缺失/损坏一律当空表。"""
    try:
        data = json.loads(registry_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_registry(data: dict) -> None:
    """原子落盘（临时文件 + `os.replace`）+ 0600。写失败抛 OSError，由调用方降级。"""
    path = registry_path()
    ensure_private_dir(path.parent)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    restrict_file(tmp)
    os.replace(tmp, path)


def touch_project(cwd) -> str:
    """登记一个项目目录，返回它的 slug。

    值没变就不写盘（恢复会话也会走这里，别让它变成热路径上的写放大）；
    写失败静默返回 slug——注册表丢了还能从会话日志回填，不阻断会话创建。
    """
    resolved = str(Path(cwd or config.WORKSPACE_ROOT).resolve())
    slug = project_slug(resolved)
    with _registry_lock:
        try:
            data = read_registry()
            entry = data.get(slug)
            if isinstance(entry, dict) and entry.get("cwd") == resolved:
                return slug
            now = time.time()
            created = float(entry.get("created") or now) if isinstance(entry, dict) else now
            data[slug] = {
                "cwd": resolved,
                "name": Path(resolved).name or resolved,
                "created": created,
            }
            _write_registry(data)
        except OSError:
            return slug
    return slug


def read_project(slug: str) -> str:
    """注册表里查 slug 对应的项目目录；未登记返回空串（不回填，回填见 store）。"""
    slug = str(slug or "").strip()
    if not slug:
        return ""
    with _registry_lock:
        entry = read_registry().get(slug)
    if isinstance(entry, dict):
        return str(entry.get("cwd") or "")
    return ""
