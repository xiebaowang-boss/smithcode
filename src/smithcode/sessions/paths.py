"""会话存储路径：按项目分区放在用户目录下，绝不写入工作区。

布局：`<smithcode_home>/projects/<slug>/sessions/<session-id>.jsonl`。
slug 由 cwd 生成：可读前缀（非法字符替换为 `-`，截断 48 字符）+ 8 位路径
hash 消歧——长路径、中文路径、Windows 盘符都安全且确定性。
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
from pathlib import Path

from .. import config

_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9-]+")
MAX_SLUG_PREFIX = 48


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
