"""事件声明注册表：类型名、持久性、版本、聚合根。

对齐 opencode 的 `Event.define({type, durable: {version, aggregate}, schema})`：
**声明**决定「是否落盘」「按哪个字段做聚合根」「当前是哪个版本」，编解码、分发、
落盘与回放都据此工作——所以事件类型只能在这里登记，其他模块不得自行定义。

规则（守卫测试强制）：
- 事件类只在 `catalog.py` 定义并用本模块的 `declare` 登记；
- 类型名是稳定契约（写进日志），改名等于破坏兼容，需要升 `version`；
- 会话类事件必须声明 `aggregate="session_id"`，否则无法按会话路由 / 回放。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EventMeta:
    """一个事件类型的声明。"""

    type: str
    durable: bool = False
    version: int = 1
    #: `data` 中作为聚合根 ID 的字段名（会话事件恒为 "session_id"）。
    aggregate: str | None = None


_BY_CLASS: dict[type, EventMeta] = {}
_BY_TYPE: dict[str, dict[int, type]] = {}


def declare(
    type: str,
    *,
    durable: bool = False,
    version: int = 1,
    aggregate: str | None = None,
):
    """声明一个事件类型（装饰器）。

    同名同版本重复声明直接报错——那意味着两处定义争同一份日志契约，
    静默覆盖会让回放读到错的类。
    """

    def decorator(cls: type) -> type:
        meta = EventMeta(type=type, durable=durable, version=version, aggregate=aggregate)
        existing = _BY_CLASS.get(cls)
        if existing is not None and existing != meta:
            raise ValueError(f"事件类型重复声明且不一致：{cls.__name__} {existing} != {meta}")
        versions = _BY_TYPE.setdefault(type, {})
        clash = versions.get(version)
        if clash is not None and clash is not cls:
            raise ValueError(
                f"类型名 {type!r} 的版本 {version} 已被 {clash.__name__} 占用"
            )
        _BY_CLASS[cls] = meta
        versions[version] = cls
        return cls

    return decorator


def meta(cls: type) -> EventMeta:
    """取事件类的声明；未声明的类说明漏了 `declare`。"""
    found = _BY_CLASS.get(cls)
    if found is None:
        raise KeyError(f"{cls.__name__} 没有事件声明（应经 catalog.declare 登记）")
    return found


def type_name(cls: type) -> str:
    return meta(cls).type


def is_durable(cls: type) -> bool:
    return meta(cls).durable


def aggregate_of(cls: type) -> str | None:
    return meta(cls).aggregate


def declared_classes() -> tuple[type, ...]:
    """全部已声明的事件类（供完备性断言遍历）。"""
    return tuple(_BY_CLASS)


def declared_types() -> tuple[str, ...]:
    """全部已声明的类型名（稳定契约清单）。"""
    return tuple(_BY_TYPE)


def latest(type: str) -> type | None:
    """某类型的最新版本类；未登记返回 None。"""
    versions = _BY_TYPE.get(type)
    if not versions:
        return None
    return versions[max(versions)]


def class_for(type: str, version: int | None = None) -> type | None:
    """按类型名（+ 可选版本）取类。

    不给版本时取**最新**（对齐 opencode `Event.latest()`）；给了版本时取
    「不超过该版本的最新一个」，使旧日志仍能被读回。
    """
    versions = _BY_TYPE.get(type)
    if not versions:
        return None
    if version is None:
        return versions[max(versions)]
    candidates = [v for v in versions if v <= version]
    if not candidates:
        return None
    return versions[max(candidates)]


def versioned_type(type: str, version: int) -> str:
    """写库用的版本化类型名：`session.step.started.1`。"""
    return f"{type}.{version}"


def reset() -> None:
    """清空注册表（仅供测试隔离，不要在生产路径调用）。"""
    _BY_CLASS.clear()
    _BY_TYPE.clear()
