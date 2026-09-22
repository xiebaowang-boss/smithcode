"""会话日志的写路径与加载：**一个写入方法**（`append_event`）+ 重放加载。

写路径：懒物化（首条事件才建文件）、每条事件一次 `write` + `flush`、任何
`OSError` 降级为禁用持久化（绝不阻断 Agent）。`sync()` 是持久化屏障
（flush + fsync），只在语义点调用：工具执行前与每轮结束——单条 `flush` 只把数据
交给内核页缓存，扛不住断电。

读路径：列举只读文件头/尾（列表页不解析整个日志），加载重放全部事件并折叠出
视图；崩溃收尾把占位结果**作为事件**写回日志（见 `format.repair_dangling_tool_calls`）。

历史沿革：这里原来有 7 个 `append_*`（msg/compact/state/title/model/usage）和一
套"记录种类"协议（`t=msg/…`）；现在只有事件——会话状态由折叠得出
（见 `sessions/project.py`），所以写入路径只有一个入口。
"""
from __future__ import annotations

import os
import threading
from dataclasses import replace
from pathlib import Path

from .. import __version__, config
from ..event.catalog import MessageEnd
from ..event.envelope import Envelope, wrap
from . import format, paths
from .model import LoadedSession, SessionSummary

# 列举时的头/尾读取窗口：created 与首轮 prompt 在头部，标题事件在尾部
HEAD_BYTES = 8192
TAIL_BYTES = 16384

# 首轮 prompt 截断为展示名时的长度
PROMPT_CLIP = 40


class StoreError(Exception):
    """会话存储错误：异常消息即面向用户的修复提示。"""


class SessionStore:
    """单个会话的写路径（一个会话一个文件）。"""

    def __init__(self, session_id: str, cwd=None, model: str = "",
                 effort: str = "", oneshot: bool = False):
        self.id = str(session_id)
        self._path = paths.session_path(self.id, cwd=cwd)
        # 供 `session.created` 事件取用的元数据（原先写在文件头 meta 里）
        self.cwd = str(Path(cwd or config.WORKSPACE_ROOT).resolve())
        self.model = model or config.MODEL
        self.effort = effort or config.REASONING_EFFORT
        self.oneshot = bool(oneshot)
        self.app = __version__
        self._fh = None
        self._lock = threading.Lock()  # 标题线程与主线程并发追加时串行化写
        self._disabled = False
        self._reported = False
        self._materialized = False
        self._next_seq = 1  # 每会话单调递增；恢复时由 load 续上（见 adopt_seq）

    # ---------- 构造 ----------

    @classmethod
    def create(cls, cwd=None, model: str = "", effort: str = "",
               oneshot: bool = False) -> SessionStore:
        """新建会话：轮换会话 id，文件懒物化（首条事件时才落盘）。"""
        return cls(
            config.new_session_id(), cwd=cwd, model=model, effort=effort,
            oneshot=oneshot,
        )

    @classmethod
    def open(cls, path, session_id: str, cwd=None) -> SessionStore:
        """恢复既有会话：沿用原文件（后续事件追加到同一日志）。"""
        store = cls(session_id, cwd=cwd)
        store._path = Path(path)
        store._materialized = store._path.exists()
        return store

    # ---------- 属性 ----------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def disabled(self) -> bool:
        return self._disabled

    @property
    def materialized(self) -> bool:
        return self._materialized

    @property
    def next_seq(self) -> int:
        return self._next_seq

    def adopt_seq(self, next_seq: int) -> None:
        """恢复会话后续上 seq（日志里已有 1..N，下一条从 N+1 开始）。"""
        self._next_seq = max(1, int(next_seq))

    # ---------- 追加（唯一写入口） ----------

    def append_event(self, env: Envelope) -> Envelope:
        """把一条事件写进日志：分配 seq 后追加，返回带 seq 的信封。

        「只写 durable」的判断在调用方（`sessions/journal.py`）：这里只负责写，
        于是"写日志"只有一条路径，新增事件不会漏记（是否持久在声明里，见
        `event/registry.py`）。
        """
        numbered = replace(env, seq=self._next_seq)
        self._next_seq += 1
        self._append(format.to_record(numbered))
        return numbered

    def _append(self, record: dict) -> None:
        if self._disabled:
            return
        line = format.dump_record(record)
        error = None
        with self._lock:
            try:
                if self._fh is None:
                    paths.ensure_private_dir(self._path.parent)
                    # 会话期持有句柄（每条 flush），close() 统一释放；故不用 with
                    self._fh = open(self._path, "a", encoding="utf-8")  # noqa: SIM115
                    paths.restrict_file(self._path)
                    self._materialized = True
                self._fh.write(line + "\n")
                self._fh.flush()
            except OSError as exc:
                error = exc
                self._disabled = True
                if self._fh is not None:
                    try:
                        self._fh.close()
                    except OSError:
                        pass
                    self._fh = None
        if error is not None:
            self._report(error)

    def _report(self, exc: OSError) -> None:
        """写失败只警告一次并降级为纯内存会话（与权限的 fail-closed 相反）。"""
        if self._reported:
            return
        self._reported = True
        try:
            from ..event import publish
            from ..event.catalog import Notice

            publish(Notice(
                f"[会话] 无法写入会话记录（{type(exc).__name__}: {exc}），"
                "本次会话不会被自动保存。",
                level="warning",
            ))
        except Exception:  # noqa: BLE001 事件通道故障不影响主流程
            return

    # ---------- 收尾 ----------

    def flush(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                except OSError:
                    pass

    def sync(self) -> None:
        """持久化屏障：flush + fsync（手动保存与每轮结束的语义点用）。"""
        with self._lock:
            if self._fh is None:
                return
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None


# --------------------------------------------------------------------------
# 列举与查找
# --------------------------------------------------------------------------


def list_sessions(cwd=None, limit: int = 20,
                  include_oneshot: bool = False) -> list:
    """列出当前项目的会话摘要，按最近更新倒序。

    只读每个文件的头部（created + 首轮 prompt）与尾部（最近的标题事件）；
    尾部未命中标题时回退全文扫描（标题可能被长会话推到文件深处）。
    """
    root = paths.sessions_dir(cwd)
    if not root.is_dir():
        return []
    summaries = []
    for path in root.glob("*.jsonl"):
        summary = _read_summary(path)
        if summary is None:
            continue
        if summary.oneshot and not include_oneshot:
            continue
        summaries.append(summary)
    summaries.sort(key=lambda item: item.updated, reverse=True)
    if limit and limit > 0:
        return summaries[:limit]
    return summaries


def find_last(cwd=None) -> SessionSummary | None:
    """`-c/--continue`：当前项目最近一次交互会话（一次性任务默认排除）。"""
    sessions = list_sessions(cwd=cwd, limit=1)
    return sessions[0] if sessions else None


def find(session_id_or_prefix, cwd=None) -> SessionSummary | None:
    """按 id 或唯一前缀查找（含一次性任务）；前缀不唯一时抛 StoreError。"""
    needle = str(session_id_or_prefix or "").strip()
    if not needle:
        return None
    candidates = list_sessions(cwd=cwd, limit=0, include_oneshot=True)
    for summary in candidates:
        if summary.id == needle:
            return summary
    matches = [item for item in candidates if item.id.startswith(needle)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = "、".join(f"{item.short_id}（{item.display_name}）" for item in matches[:5])
        raise StoreError(
            f"会话 id 前缀「{needle}」不唯一，候选：{names}。请补全更多字符。"
        )
    return None


def summary_from_path(path) -> SessionSummary:
    """从任意路径的日志构造摘要（`--resume <路径>` 用）。"""
    target = Path(path)
    if not target.is_file():
        raise StoreError(f"会话文件不存在: {target}")
    summary = _read_summary(target)
    if summary is None:
        raise StoreError(f"会话文件为空或无法解析: {target}")
    return summary


def delete(session_id, cwd=None) -> bool:
    """删除一个会话文件（目标会话不应处于活动状态，Windows 下句柄需已关闭）。"""
    summary = find(session_id, cwd=cwd)
    if summary is None:
        return False
    try:
        summary.path.unlink()
    except OSError as exc:
        raise StoreError(f"无法删除会话 {summary.short_id}: {exc}") from exc
    return True


def rename(session_id, title: str, cwd=None) -> bool:
    """给指定会话追加一条标题事件（最后一条生效，不重写文件）。"""
    summary = find(session_id, cwd=cwd)
    if summary is None:
        return False
    from ..event.catalog import TitleChanged

    store = SessionStore.open(summary.path, session_id=summary.id)
    try:
        store.append_event(wrap(TitleChanged(title, source="user"), session_id=summary.id))
    finally:
        store.close()
    return True


def sweep(cleanup_days) -> int:
    """保留期清理：删除全部项目中 mtime 早于截止时间的日志。返回删除数。"""
    import time

    try:
        days = float(cleanup_days)
    except (TypeError, ValueError):
        return 0
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    root = paths.projects_dir()
    if not root.is_dir():
        return 0
    removed = 0
    for path in root.glob("*/sessions/*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# --------------------------------------------------------------------------
# 加载（重放折叠）
# --------------------------------------------------------------------------


def load(summary) -> LoadedSession:
    """加载日志：重放全部事件并折叠出视图；崩溃收尾补占位结果。

    与 `format.read_log` 的分工：这里负责"折叠 + 收尾 + 装配 LoadedSession"，
    读文件与坏行容忍在 format 层。
    """
    from . import project

    path = Path(summary.path if isinstance(summary, SessionSummary) else summary)
    try:
        events, bad_lines = format.read_log(path)
    except OSError as exc:
        raise StoreError(f"无法读取会话记录 {path}: {exc}") from exc

    view = project.fold(events)
    meta = dict(view.meta) or _synthesize_meta(path)
    session_id = str(meta.get("session_id") or path.stem)
    repair, appended = format.repair_dangling_tool_calls(view.messages)
    if appended:
        # 崩溃收尾的产物也是事件：写回日志，下次恢复就是合法历史（不再读时打补丁）
        store = SessionStore.open(path, session_id=session_id)
        try:
            store.adopt_seq(max([env.seq or 0 for env in events] or [0]) + 1)
            for message in appended:
                events.append(store.append_event(
                    wrap(MessageEnd(message=message), session_id=session_id)
                ))
        finally:
            store.close()
    meta.setdefault("id", session_id)
    return LoadedSession(
        path=path, meta=meta, messages=view.messages, state=view.state or None,
        title=view.title, title_source=view.title_source,
        compact_count=view.compactions,
        model=view.model or str(meta.get("model") or ""),
        effort=view.effort or str(meta.get("effort") or ""),
        bad_lines=bad_lines, repair=repair, repaired=bool(appended),
        events=events, usage=dict(view.usage), context_tokens=view.last_input_tokens,
    )


def _synthesize_meta(path: Path) -> dict:
    """没有 `session.created` 事件时，从文件名与 mtime 合成最小元数据。"""
    import time

    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = time.time()
    return {
        "id": path.stem,
        "session_id": path.stem,
        "cwd": "",
        "created": mtime,
        "model": "",
        "effort": "",
        "app": "",
        "oneshot": False,
        "synthesized": True,
    }


def _read_summary(path: Path) -> SessionSummary | None:
    """读会话摘要：只看头尾两段。"""
    try:
        stat = path.stat()
    except OSError:
        return None
    size = stat.st_size
    if size <= 0:
        return None
    try:
        with open(path, "rb") as fh:
            head = fh.read(HEAD_BYTES)
            tail = b""
            if size > HEAD_BYTES:
                fh.seek(max(0, size - TAIL_BYTES))
                tail = fh.read()
    except OSError:
        return None

    meta: dict = {}
    first_prompt = ""
    parsed = 0
    for record in _parse_chunk(head):
        base, data = _event_parts(record)
        if base == "session.created" and not meta:
            meta = dict(data)
            meta.setdefault("session_id", record.get("session_id"))
        elif base == "session.message.ended" and not first_prompt:
            message = data.get("message") or {}
            if message.get("role") == "user":
                first_prompt = _clip(_text_of(message.get("content")), PROMPT_CLIP)
    parsed += bool(meta or first_prompt)
    title, source, model = _tail_facts(_parse_chunk(tail or head))
    if not title and size > len(head) + len(tail):
        # 标题可能被长日志推到深处：回退全文扫描（仅此一种情况读全文）
        title, source, scanned_model = _tail_facts(_iter_records(path))
        model = model or scanned_model
    if not meta and not first_prompt and not title and parsed == 0:
        # 整段都读不出事件（空文件 / 全坏行）才算"无法解析"
        return None
    return SessionSummary(
        id=str(meta.get("session_id") or meta.get("id") or path.stem),
        path=path,
        cwd=str(meta.get("cwd") or ""),
        created=float(meta.get("created") or stat.st_mtime),
        updated=stat.st_mtime,
        # 首轮 prompt 与标题来自头尾窗口；模型取最后一条 model 事件（回退创建时模型）
        model=model or str(meta.get("model") or ""),
        title=title,
        title_source=source,
        first_prompt=first_prompt,
        oneshot=bool(meta.get("oneshot")),
        size=size,
    )


def _event_parts(record: dict) -> tuple[str, dict]:
    """记录 → (去掉版本号的类型名, data)。"""
    type_name = str(record.get("type") or "")
    base, _, version_text = type_name.rpartition(".")
    if base and version_text.isdigit():
        type_name = base
    data = record.get("data")
    return type_name, data if isinstance(data, dict) else {}


def _parse_chunk(data: bytes) -> list:
    records = []
    for line in data.decode("utf-8", "ignore").splitlines():
        record = format.parse_line(line)
        if record is not None:
            records.append(record)
    return records


def _iter_records(path: Path):
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            record = format.parse_line(line)
            if record is not None:
                yield record


def _tail_facts(records) -> tuple[str, str, str]:
    """一段记录里最后生效的 (标题, 标题来源, 模型)。"""
    title, source, model = "", "", ""
    for record in records:
        base, data = _event_parts(record)
        if base == "session.title.changed":
            title = str(data.get("title") or "")
            source = str(data.get("source") or "")
        elif base == "session.model.selected":
            model = str(data.get("model") or "") or model
    return title, source, model


def _text_of(content) -> str:
    """消息 content 的纯文本：字符串原样；多模态列表拼接 text 部分。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return " ".join(part for part in parts if part)
    return ""


def _clip(text: str, limit: int) -> str:
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"
