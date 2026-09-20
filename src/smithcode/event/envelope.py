"""事件信封：身份与路由 + 领域载荷。

形态对齐 opencode 的 `{id, type, version, created, session_id, durable, data}`：

- 信封负责**身份与路由**（谁发的、属于哪个会话、第几版、是否持久、落盘后的 seq）；
- `data` 是**领域载荷**（冻结 dataclass，见 `catalog.py`），只描述发生了什么。

`id` / `created` / `session_id` 由**发布侧注入**（`bus.publish` 统一填），调用方
不手写——这是「会话标识只有一个来源」的落点，也让多客户端能按 `session_id` 路由。

反向（结构 → 载荷实例）只在**回放**时需要，随持久日志一起落地（阶段 E）；
届时由「重放视图 == 在线视图」的等价性测试覆盖。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from typing import Any

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
