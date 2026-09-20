"""会话转录的读写：append-only JSONL + 项目级列举 / 查找 / 加载。

写路径（`SessionStore`）：懒物化（首条非 system 消息才建文件）、每条记录
一次 `write` + `flush`、任何 `OSError` 降级为禁用持久化（绝不阻断 Agent）。
`sync()` 是持久化屏障（flush + fsync），只在语义点调用：工具执行前与每轮结束
——单条 `flush` 只把数据交给内核页缓存，扛不住断电。
读路径（模块函数）：列举只读文件头/尾，加载单遍流式并做崩溃修复。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .. import __version__, config
from ..event import publish
from ..event.catalog import Notice
from . import format, paths
from .model import LoadedSession, SessionSummary

# 列举时的头/尾读取窗口：meta 与首轮 prompt 在头部，标题记录在尾部
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
        self._header = format.meta_record(
            self.id,
            cwd=str(Path(cwd or config.WORKSPACE_ROOT).resolve()),
            model=model or config.MODEL,
            effort=effort or config.REASONING_EFFORT,
            app=__version__,
            oneshot=oneshot,
        )
        self._fh = None
        self._lock = threading.Lock()  # 标题线程与主线程并发追加时串行化写
        self._disabled = False
        self._reported = False
        self._materialized = False
        self._last_model: tuple[str, str] | None = None  # 已写入的 (模型, 强度)：去重

    # ---------- 构造 ----------

    @classmethod
    def create(cls, cwd=None, model: str = "", effort: str = "",
               oneshot: bool = False) -> SessionStore:
        """新建会话：轮换会话 id，文件懒物化（首条消息时才落盘）。"""
        return cls(
            config.new_session_id(), cwd=cwd, model=model, effort=effort,
            oneshot=oneshot,
        )

    @classmethod
    def open(cls, path, session_id: str, cwd=None) -> SessionStore:
        """恢复既有会话：沿用原文件（后续消息追加到同一转录）。"""
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

    # ---------- 追加 ----------

    def append_message(self, message: dict) -> None:
        if not isinstance(message, dict) or message.get("role") == "system":
            return  # system 每次由 sync_system 重建，不入转录
        self._append(format.msg_record(message))

    def append_compaction(self, summary: dict, tail: list,
                          before: int = 0, after: int = 0) -> None:
        self._append(format.compact_record(summary, tail, before, after))

    def append_state(self, payload: dict) -> None:
        if isinstance(payload, dict):
            self._append(format.state_record(payload))

    def append_title(self, title: str, source: str = format.TITLE_SOURCE_USER) -> None:
        self._append(format.title_record(title, source))

    def append_usage(self, usage) -> None:
        if isinstance(usage, dict) and usage:
            self._append(format.usage_record(usage))

    def append_model(self, model: str, effort: str = "") -> None:
        """记录本次调用实际使用的模型 / 思考强度（与上一条相同则不写）。

        首次调用必写一条（`_last_model` 为空），此后仅在变化时追加——`meta`
        只记创建时的模型，`/model` 中途切换后需要靠这条记录回答「谁生成的」。
        """
        key = (str(model or ""), str(effort or ""))
        if not key[0] or key == self._last_model:
            return
        self._last_model = key
        self._append(format.model_record(*key))

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
                if not self._materialized:  # 懒物化：meta 首行与首条消息同批写入
                    self._fh.write(format.dump_record(self._header))
                    self._materialized = True
                self._fh.write(line)
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

    def flush(self) -> None:
        with self._lock:
            if self._fh is None:
                return
            try:
                self._fh.flush()
            except OSError as exc:
                self._disabled = True
                error = exc
            else:
                return
        self._report(error)

    def sync(self) -> None:
        """持久化屏障：flush + `os.fsync`，确保已追加记录挺过断电 / 内核崩溃。

        与 `flush()` 的区别只在落盘层级：`flush()` 把数据交给内核页缓存（进程被
        kill 通常能保住），`fsync()` 才真正写进设备。成本是每次一次系统调用，
        所以只在语义点调用（工具执行前 / 每轮结束），不追求每条记录都 fsync。
        写失败仍按既有策略降级为纯内存会话（fail-open）。
        """
        if self._disabled:
            return
        error = None
        with self._lock:
            fh = self._fh
            if fh is None:
                return
            try:
                fh.flush()
                os.fsync(fh.fileno())
            except ValueError:
                return  # 句柄已被 close() 关闭（并发竞争），不是写失败，不降级
            except OSError as exc:
                self._disabled = True
                error = exc
        if error is not None:
            self._report(error)

    def close(self) -> None:
        with self._lock:
            fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            fh.flush()
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass

    def _report(self, exc: OSError) -> None:
        """写失败只警告一次并降级为纯内存会话（与权限的 fail-closed 相反）。"""
        if self._reported:
            return
        self._reported = True
        try:
            publish(Notice(
                f"[会话] 无法写入会话记录（{type(exc).__name__}: {exc}），"
                "本次会话不会被自动保存。"
            , level="warning"))
        except Exception:  # noqa: BLE001 渲染失败不影响主流程
            return


# ---------- 项目级查询 ----------

def list_sessions(cwd=None, limit: int = 20,
                  include_oneshot: bool = False) -> list:
    """列出当前项目的会话摘要，按最近更新倒序。

    只读每个文件的头部（meta + 首轮 prompt）与尾部（最近的 title 记录）；
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
    """从任意路径的转录文件构造摘要（`--resume <路径>` 用）。"""
    target = Path(path)
    if not target.is_file():
        raise StoreError(f"会话文件不存在: {target}")
    summary = _read_summary(target)
    if summary is None:
        raise StoreError(f"会话文件为空或无法解析: {target}")
    return summary


def load(summary) -> LoadedSession:
    """加载转录：单遍解析 + 投影重置（compact）+ 崩溃修复，返回 LoadedSession。"""
    path = Path(summary.path if isinstance(summary, SessionSummary) else summary)
    try:
        records, bad_lines = format.read_transcript(path)
    except OSError as exc:
        raise StoreError(f"无法读取会话记录 {path}: {exc}") from exc
    return _assemble(path, records, bad_lines)


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
    """给指定会话追加用户标题记录（最后一条生效，不重写文件）。"""
    summary = find(session_id, cwd=cwd)
    if summary is None:
        return False
    _append_raw(summary.path, format.title_record(title, format.TITLE_SOURCE_USER))
    return True


def import_json(path, cwd=None) -> SessionSummary:
    """把旧 `<workspace>/sessions/*.json`（纯 messages 数组）导入为 v1 转录。"""
    source = Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise StoreError(f"无法读取旧会话文件 {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise StoreError(f"旧会话文件不是合法 JSON：{source}（{exc}）") from exc
    if not isinstance(data, list) or not data:
        raise StoreError(f"旧会话文件应是非空的消息数组：{source}")
    store = SessionStore.create(cwd=cwd)
    for message in data:
        if isinstance(message, dict):
            store.append_message(message)
    store.close()
    return summary_from_path(store.path)


def sweep(cleanup_days) -> int:
    """保留期清理：删除全部项目中 mtime 早于截止时间的转录。返回删除数。"""
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


# ---------- 内部 ----------

def _assemble(path: Path, records: list, bad_lines: int = 0) -> LoadedSession:
    """把记录流装配为加载结果：compact 重置投影，最后一条 state/title/model 生效。"""
    meta = {}
    messages: list = []
    state = None
    title = ""
    title_source = ""
    model = ""
    effort = ""
    compact_count = 0
    for record in records:
        kind = record.get("t")
        if kind == format.T_META:
            meta = record
        elif kind == format.T_MSG:
            message = record.get("m")
            if isinstance(message, dict) and message.get("role") != "system":
                messages.append(message)
        elif kind == format.T_COMPACT:
            summary = record.get("summary")
            tail = record.get("tail") or []
            messages = ([summary] if isinstance(summary, dict) else []) + [
                item for item in tail if isinstance(item, dict)
            ]
            compact_count += 1
        elif kind == format.T_STATE:
            state = record.get("state") if isinstance(record.get("state"), dict) else None
        elif kind == format.T_TITLE:
            title = str(record.get("title") or "")
            title_source = str(record.get("source") or "")
        elif kind == format.T_MODEL:
            # 只采纳带模型的记录：残缺记录退回上一条，而不是清空已还原的值
            candidate = str(record.get("model") or "")
            if candidate:
                model, effort = candidate, str(record.get("effort") or "")
    if not meta:
        meta = _synthesize_meta(path)
    if not model:  # 旧转录（或首次调用尚未登记）回退 meta 的创建时模型
        model = str(meta.get("model") or "")
        effort = effort or str(meta.get("effort") or "")
    repair, repaired = format.repair_dangling_tool_calls(messages)
    return LoadedSession(
        path=path, meta=meta, messages=messages, state=state,
        title=title, title_source=title_source, compact_count=compact_count,
        model=model, effort=effort,
        bad_lines=bad_lines, repair=repair, repaired=repaired,
    )


def _read_summary(path: Path) -> SessionSummary | None:
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
    for record in _parse_chunk(head):
        kind = record.get("t")
        if kind == format.T_META and not meta:
            meta = record
        elif kind == format.T_MSG and not first_prompt:
            message = record.get("m") or {}
            if message.get("role") == "user":
                first_prompt = _clip(_text_of(message.get("content")), PROMPT_CLIP)
        if meta and first_prompt:
            break

    title, title_source, model = _last_tail_facts(_parse_chunk(tail or head))
    if not title and size > len(head) + len(tail):
        # 标题可能被长会话推到文件深处：回退全文扫描（仅此一种情况读全文）
        title, title_source, scanned_model = _last_tail_facts(_iter_records(path))
        model = model or scanned_model
    if not meta and not first_prompt and not title:
        return None
    return SessionSummary(
        id=str(meta.get("id") or path.stem),
        path=path,
        cwd=str(meta.get("cwd") or ""),
        created=float(meta.get("created") or stat.st_mtime),
        updated=stat.st_mtime,
        # 首轮 prompt 与标题来自头尾窗口；模型取最后一条 model 记录（回退创建时模型）
        model=model or str(meta.get("model") or ""),
        title=title,
        title_source=title_source,
        first_prompt=first_prompt,
        oneshot=bool(meta.get("oneshot")),
        size=size,
    )


def _synthesize_meta(path: Path) -> dict:
    """首行 meta 缺失/损坏时，从文件名与 mtime 合成最小元数据。"""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = time.time()
    return {
        "v": format.FORMAT_VERSION,
        "t": format.T_META,
        "id": path.stem,
        "cwd": "",
        "created": mtime,
        "model": "",
        "effort": "",
        "app": "",
        "oneshot": False,
        "synthesized": True,
    }


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


def _last_tail_facts(records) -> tuple[str, str, str]:
    """一段记录里最后生效的 (标题, 标题来源, 模型)：列举只读头尾窗口时用。"""
    title, source, model = "", "", ""
    for record in records:
        kind = record.get("t")
        if kind == format.T_TITLE:
            title = str(record.get("title") or "")
            source = str(record.get("source") or "")
        elif kind == format.T_MODEL:
            model = str(record.get("model") or "") or model
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


def _append_raw(path: Path, record: dict) -> None:
    paths.ensure_private_dir(path.parent)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(format.dump_record(record))
        fh.flush()
    paths.restrict_file(path)
