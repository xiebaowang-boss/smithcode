"""事件信封：身份与路由 + 领域载荷。

形态对齐 opencode 的 `{id, type, version, created, session_id, durable, data}`：

- 信封负责**身份与路由**（谁发的、属于哪个会话、第几版、是否持久、落盘后的 seq）；
- `data` 是**领域载荷**（冻结 dataclass，见 `catalog.py`），只描述发生了什么。

`id` / `created` / `session_id` 由**发布侧注入**（`bus.publish` 统一填），调用方
不手写——这是「会话标识只有一个来源」的落点，也让多客户端能按 `session_id` 路由。

反向（结构 → 载荷实例）是**回放**需要的：日志里存的是 JSON，读回来要还原成
逐个类型化的载荷，才能与在线跑的事件走同一个折叠函数（见 `sessions/project.py`）。
`payload_from_dict` 按类型注解还原，所以 `tuple[X, ...]` 会回到 tuple 而不是
list——「重放得到的视图」与「在线视图」才能逐字段相等。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from typing import Any, get_type_hints

from . import registry


@dataclass(frozen=True)
class Envelope:
    """一条已发布事件的完整形态。"""

    id: str
    type: str
    version: int
    created: float
    session_id: str | None
    durable: bool
    data: object
    #: 持久事件落盘后的序号（每会话单调递增）；易失事件恒为 None。
    seq: int | None = None

    @property
    def payload(self) -> object:
        """`data` 的别名（读起来更像「载荷」时用）。"""
        return self.data

    def to_record(self) -> dict:
        """落盘 / 跨进程传输的形态（可 JSON 化）。"""
        return {
            "id": self.id,
            "type": self.type,
            "version": self.version,
            "created": self.created,
            "session_id": self.session_id,
            "durable": self.durable,
            "seq": self.seq,
            "data": payload_to_dict(self.data),
        }


def new_id() -> str:
    """事件 id（与前端无关，由发布侧生成）。"""
    return uuid.uuid4().hex


def wrap(data: object, *, session_id: str | None = None,
         created: float | None = None, seq: int | None = None) -> Envelope:
    """把领域载荷装进信封：类型名 / 版本 / 持久性全部取自声明。"""
    info = registry.meta(type(data))
    return Envelope(
        id=new_id(),
        type=info.type,
        version=info.version,
        created=time.time() if created is None else created,
        session_id=session_id,
        durable=info.durable,
        data=data,
        seq=seq,
    )


def payload_to_dict(value: Any) -> Any:
    """载荷 → 可 JSON 化的结构（dataclass 展开、tuple → list）。

    只做形状转换，不认识的对象原样保留——**durable 事件不得携带这类对象**，
    该约束由声明侧的完备性测试强制。
    """
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: payload_to_dict(getattr(value, field.name))
                for field in fields(value)}
    if isinstance(value, tuple):
        return [payload_to_dict(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): payload_to_dict(item) for key, item in value.items()}
    return value


# --------------------------------------------------------------------------
# 反向：结构 → 载荷（回放用）
# --------------------------------------------------------------------------


def payload_from_dict(cls: type, payload: Any) -> Any:
    """可 JSON 化结构 → 载荷实例（形状不认识时原样返回）。

    只处理用得到的几种注解：嵌套 dataclass、`tuple[X, ...]`、`X | None`、
    `Mapping`（原样留着，它本来就是 JSON 友好的）。其余一律原样返回——回放的
    容忍度优先于严格性：一条读不懂的载荷不该让整个会话打不开。
    """
    if not (is_dataclass(cls) and isinstance(payload, Mapping)):
        return payload
    try:
        hints = get_type_hints(cls)
    except Exception:  # noqa: BLE001 注解解析失败时退回松散还原
        hints = {}
    kwargs = {
        field.name: _convert(hints.get(field.name), payload[field.name])
        for field in fields(cls)
        if field.name in payload
    }
    return cls(**kwargs)


def _convert(hint: Any, raw: Any) -> Any:
    """按注解还原一个值（见 `payload_from_dict` 的说明）。"""
    if hint is None or raw is None:
        return raw
    origin = getattr(hint, "__origin__", None)
    args = getattr(hint, "__args__", ())
    if origin is not None and type(None) in args:  # X | None
        inner = next((arg for arg in args if arg is not type(None)), None)
        return _convert(inner, raw)
    if origin is tuple and isinstance(raw, list):
        item_hint = args[0] if args else None
        return tuple(_convert(item_hint, item) for item in raw)
    if origin is list and isinstance(raw, list):
        item_hint = args[0] if args else None
        return [_convert(item_hint, item) for item in raw]
    if isinstance(hint, type):
        if is_dataclass(hint):
            return payload_from_dict(hint, raw)
        if hint is tuple and isinstance(raw, list):
            return tuple(raw)
    return raw


def from_record(record: Mapping) -> Envelope | None:
    """日志记录 → 信封（类型不认识时返回 None，由调用方计入坏行）。

    类型名带版本（`session.step.ended.1`）：写库时用 `versioned_type`，读回时按
    「不超过该版本的最新一个」取类，因此旧日志仍能被读（见 `registry.class_for`）。
    """
    from . import registry  # 局部导入：registry 与本模块互相引用

    type_name = record.get("type")
    if not isinstance(type_name, str):
        return None
    base, _, version_text = type_name.rpartition(".")
    version: int | None = None
    if base and version_text.isdigit():
        version = int(version_text)
    else:
        base = type_name
    cls = registry.class_for(base, version)
    if cls is None:
        return None
    data = payload_from_dict(cls, record.get("data"))
    if not is_dataclass(data):
        return None
    return Envelope(
        id=str(record.get("id") or new_id()),
        type=base,
        version=registry.meta(cls).version,
        created=float(record.get("created") or time.time()),
        session_id=record.get("session_id"),
        durable=bool(record.get("durable", registry.meta(cls).durable)),
        data=data,
        seq=record.get("seq") if isinstance(record.get("seq"), int) else None,
    )
