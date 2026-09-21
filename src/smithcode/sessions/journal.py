"""会话日志：**订阅事件总线**，把持久事件写进转录并折叠进会话视图。

这是"事件溯源"的落点：会话状态不是被"改"出来的，而是**折叠事件**折出来的。
`Journal` 是唯一同时做这两件事的地方（顺序固定：先落盘、再折叠——落盘失败
也要让内存视图继续前进，否则一次写失败会让会话卡死在半路）。

为什么放在订阅者位置：核心只 `publish`，日志与前端都是它的消费者——所以
"持久化"不需要在业务代码里到处埋 `store.append_*`，新增事件也不会漏记
（`durable` 声明决定要不要落盘，见 `event/registry.py`）。
"""

from __future__ import annotations

from ..event.catalog import SessionCreated
from ..event.envelope import Envelope, wrap
from . import project


class Journal:
    """一个会话的日志：落盘 durable 事件 + 维护会话视图。"""

    def __init__(self, session) -> None:
        self.session = session
        self.view = project.SessionView()
        self.session_id = getattr(session, "id", "")
        self._created = False  # 首条事件必须是 session.created（日志的出生证明）

    # ---------- 订阅者接口 ----------

    def __call__(self, env: Envelope) -> None:
        """总线订阅者入口：落盘 + 折叠。"""
        self.record(env)

    def record(self, env: Envelope, *, source_override: str | None = None) -> None:
        """记一条事件：durable 的先落盘（带 seq），随后折叠进视图。"""
        if env.durable:
            store = self.session.store
            if store is not None:
                store.append_event(env)  # 失败由 store 内部降级为"纯内存会话"
        # 视图的消息列表与会话**共享同一个对象**（`reset()` / 恢复会换掉它，
        # 所以每次折叠前重新指一次）
        self.view.messages = self.session.messages
        project.apply(self.view, env)
        self._sync_session(source_override)

    # ---------- 会话视图 → Session 的公开字段 ----------

    def _sync_session(self, source_override: str | None = None) -> None:
        """把折叠结果同步到 `Session` 的公开字段（`messages` / `title` / `usage`…）。

        `Session` 就是宿主与工具看到的视图，所以折叠完随手同步，读取点（98 处
        `.messages`）保持原样——事件溯源的代价因此落在写入路径，而不是遍地改读取点。
        """
        session = self.session
        session._apply_view(self.view, source_override=source_override)

    # ---------- 生命周期 ----------

    def created(self) -> None:
        """写首条事件：会话建立（转录的元数据就在这条事件里）。"""
        if self._created:
            return
        self._created = True
        session = self.session
        store = session.store
        self.record(wrap(SessionCreated(
            cwd=str(getattr(store, "cwd", "") or ""),
            model=str(getattr(store, "model", "") or ""),
            effort=str(getattr(store, "effort", "") or ""),
            app=str(getattr(store, "app", "") or ""),
            oneshot=bool(getattr(store, "oneshot", False)),
        ), session_id=self.session_id or session.id))

    def mark_created(self) -> None:
        """标记"日志已有出生事件"（重放既有日志后调用，避免再写一条）。"""
        self._created = True
