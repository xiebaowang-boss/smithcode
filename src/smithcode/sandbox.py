"""沙箱授权目录：**一个会话一份**，按上下文解析当前会话的那一份。

改造前这些目录散在 `config` 里当进程级可变状态：

- `SESSION_EXTRA_ROOTS`：越界确认积累的信任目录（`/new` 清空）
- `SKILL_READ_ROOTS`：技能目录只读白名单（读放行、写不认）
- `_WIDENED_ROOTS`：单次工具调用临时放行（"仅本次"语义）

进程级的问题不是"难看"，是**并发下会算错**：`widen_roots` 用
`del _WIDENED_ROOTS[-len(added):]` 退出，两个会话/两批工具交错时会把别人放行的
条目删掉（放行范围凭空消失）。这里三项各自归位：

- 会话内的两组目录是 `Roots` 的**字段**；
- 临时放行是 `Roots` 上的 **ContextVar**（每个会话一份，进出用 `set/reset`，
  交错也各算各的）。

`config` 只留**只读默认值**（`WORKSPACE_ROOT` / `EXTRA_ROOTS`，启动时确定）；
本会话的工作区在会话建立时快照进 `Roots.workspace`——`/new`、恢复会话、将来的
多客户端都从这里取，而不是读进程全局。
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path

from . import config


def _resolve(path) -> Path:
    """归一化目录：展开 ~ 并解析为绝对路径（与 `config.allowed_roots` 同款）。"""
    return Path(os.path.expanduser(str(path))).resolve()


class Roots:
    """一个会话的授权目录集合。

    `workspace` / `extra` 传 `None` 时**跟随 `config`**（读时解析）：这是"进程默认
    沙箱"的形态——启动期与测试直接改 `config.WORKSPACE_ROOT` 就能生效。会话建立时
    传入具体值即**快照**下来，此后本会话不再看进程全局（`/new`、恢复、多客户端
    都取自己的那一份）。
    """

    def __init__(self, workspace=None, extra=None):
        self._workspace = None if workspace is None else _resolve(workspace)
        self._extra = None if extra is None else [_resolve(p) for p in extra]
        # 越界确认积累的信任目录（`/new` 清空）
        self.session_extra: list[Path] = []
        # 技能目录只读白名单（`skills.refresh` 全量重建）
        self.skill: list[Path] = []
        # 单次调用临时放行：ContextVar，进出成对（并发交错也不会互相删条目）
        self._widened: ContextVar[tuple[Path, ...]] = ContextVar(
            f"smithcode_widened_roots_{id(self)}", default=()
        )

    # ---------- 查询 ----------

    @property
    def workspace(self) -> Path:
        """本会话的工作区（未快照时跟随 `config.WORKSPACE_ROOT`）。"""
        if self._workspace is not None:
            return self._workspace
        return _resolve(config.WORKSPACE_ROOT)

    @property
    def extra(self) -> list[Path]:
        """启动参数 `--add` 传进来的附加目录（未快照时跟随 `config.EXTRA_ROOTS`）。"""
        if self._extra is not None:
            return self._extra
        return [_resolve(p) for p in config.EXTRA_ROOTS]

    def allowed(self) -> list[Path]:
        """全部授权目录（主工作区在前）：沙箱判定与权限模式归一化的共同依据。"""
        return [self.workspace, *self.extra, *self.session_extra, *self._widened.get()]

    def read_roots(self) -> list[Path]:
        """读工具的可用根：授权目录 + 技能只读白名单。"""
        return self.allowed() + list(self.skill)

    # ---------- 变更 ----------

    @contextmanager
    def widen(self, roots):
        """把目录临时加入授权列表，仅覆盖 with 块内的那次工具调用。"""
        added = tuple(_resolve(r) for r in roots)
        if not added:
            yield
            return
        token = self._widened.set(self._widened.get() + added)
        try:
            yield
        finally:
            self._widened.reset(token)  # 成对复位：不碰别人的条目

    def set_skill_roots(self, paths) -> None:
        self.skill[:] = [_resolve(p) for p in paths]

    def new_session(self) -> None:
        """`/new`：清空会话内积累的信任目录（工作区与启动参数不变）。"""
        self.session_extra.clear()


# --------------------------------------------------------------------------
# 当前上下文的沙箱（无会话时用进程默认，保证启动期与单测可用）
# --------------------------------------------------------------------------

_default = Roots()
_current: ContextVar[Roots | None] = ContextVar("smithcode_roots", default=None)
_init_lock = threading.Lock()


def current() -> Roots:
    """当前会话的沙箱目录；无会话时用进程默认那一份。"""
    found = _current.get()
    return found if found is not None else _default


def activate(roots: Roots | None) -> Token:
    """挂载会话沙箱，返回复位令牌（与 `event.activate` 配对使用）。"""
    return _current.set(roots)


def reset(token: Token) -> None:
    """按令牌复位（与 `activate` 配对；测试隔离也用它）。"""
    _current.reset(token)


def fresh(workspace=None, extra=()) -> Roots:
    """建一份新的会话沙箱（Agent 在会话边界的唯一构造入口）。"""
    with _init_lock:
        return Roots(workspace=workspace, extra=extra)
