"""从 core 事件声明生成桌面端协议文件（TS 类型 + JSON Schema）。

唯一输入是 `smithcode.event.catalog` 的 `@declare` 声明 + `envelope` 的信封形态：
类型名 / 版本 / 持久性 / 聚合根全部取自注册表，不手写第二份清单——契约只有一个来源。

用法（packages/core 下）：
    uv run python scripts/gen_protocol.py              # 写 packages/protocol/
    uv run python scripts/gen_protocol.py --check      # CI 校验：生成结果与已提交一致
    uv run python scripts/gen_protocol.py --out DIR    # 写到指定目录
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CORE_SRC = REPO_ROOT / "packages" / "core" / "src"
DEFAULT_OUT = REPO_ROOT / "packages" / "protocol"

sys.path.insert(0, str(CORE_SRC))

from smithcode.event import (
    catalog,
    registry,
)

# Python 字段类型 → TS / JSON Schema 的映射（未知形状一律 unknown，避免谎报类型）
_TS_SCALARS = {"str": "string", "int": "number", "float": "number", "bool": "boolean"}
_JSON_SCALARS = {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}


_LITERAL_NAMES = {
    "PromptKind", "PromptOutcome", "StatusKind", "NoticeLevel",
    "StreamKind", "QueueItemKind", "InterruptReason", "StepFinish",
}


def _literals(hint: str) -> list[str] | None:
    """Literal['a', 'b'] 或词汇别名（InterruptReason 等）→ 可选值列表。"""
    text = hint.strip()
    if text in _LITERAL_NAMES:
        return list(getattr(catalog, text).__args__)
    if not text.startswith("Literal["):
        return None
    inner = text[len("Literal["):]
    depth = 1
    for i, ch in enumerate(inner):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                inner = inner[:i]
                break
    return [part.strip().strip("'\"") for part in inner.split(",") if part.strip()]


def _strip_optional(hint: str) -> tuple[str, bool]:
    """'X | None' → ('X', True)；其余原样。"""
    text = hint.strip()
    if text.endswith("| None"):
        return text[: -len("| None")].strip(), True
    if text.startswith("Optional[") and text.endswith("]"):
        return text[len("Optional[") : -1].strip(), True
    return text, False


def _ts_type(hint: str) -> str:
    lits = _literals(hint)
    if lits is not None:
        return " | ".join(f'"{v}"' for v in lits)
    base, optional = _strip_optional(hint)
    if base in _TS_SCALARS:
        ts = _TS_SCALARS[base]
    elif base.startswith("tuple["):
        inner = base[len("tuple[") : -1].strip()
        item, _, _ = inner.partition(",")
        ts = f"{_ts_type(item.strip())}[]"
    elif base.startswith("list["):
        ts = f"{_ts_type(base[len('list[') : -1].strip())}[]"
    elif base.startswith("Mapping["):
        ts = "Record<string, unknown>"
    elif base in ("AgentMessage", "QueueItem", "StepUsage"):
        ts = base
    else:
        ts = "unknown"
    return f"{ts} | null" if optional and ts != "unknown" else ts


def _json_schema(hint: str) -> dict:
    lits = _literals(hint)
    if lits is not None:
        return {"enum": lits}
    base, _ = _strip_optional(hint)
    if base in _JSON_SCALARS:
        return {"type": _JSON_SCALARS[base]}
    if base.startswith(("tuple[", "list[")):
        inner = base[base.index("[") + 1 : -1].strip()
        item, _, _ = inner.partition(",")
        item = item.strip()
        if item in ("Any", ""):
            return {"type": "array"}
        return {"type": "array", "items": _json_schema(item)}
    if base.startswith("Mapping["):
        return {"type": "object"}
    if base == "AgentMessage":
        return {"type": "object"}
    if base == "QueueItem":
        return {"$ref": "#/$defs/QueueItem"}
    if base == "StepUsage":
        return {"$ref": "#/$defs/StepUsage"}
    return {}


def _vocabularies() -> dict[str, list[str]]:
    """catalog 里的 Literal 词汇表（PromptKind 等跨事件复用的枚举）。"""
    vocabs = {}
    for name in (
        "PromptKind", "PromptOutcome", "StatusKind", "NoticeLevel",
        "StreamKind", "QueueItemKind", "InterruptReason", "StepFinish",
    ):
        vocabs[name] = list(getattr(catalog, name).__args__)
    return vocabs


def _nested_defs() -> dict[str, dict]:
    """嵌套 dataclass（StepUsage / QueueItem）的字段表。"""
    defs = {}
    for name in ("StepUsage", "QueueItem"):
        cls = getattr(catalog, name)
        props, required = {}, []
        for f in dataclasses.fields(cls):
            props[f.name] = _json_schema(str(f.type))
            required.append(f.name)
        defs[name] = {"type": "object", "properties": props, "required": required}
    return defs


def _events() -> list[dict]:
    """全部已声明事件：类型名 / 版本 / 持久性 / 聚合根 / 字段。"""
    out = []
    for cls in registry.declared_classes():
        if cls.__module__ != "smithcode.event.catalog":
            continue
        info = registry.meta(cls)
        fields = []
        if dataclasses.is_dataclass(cls):
            for f in dataclasses.fields(cls):
                hint = str(f.type)
                fields.append({"name": f.name, "py": hint, "ts": _ts_type(hint)})
        out.append({
            "type": info.type,
            "class": cls.__name__,
            "durable": info.durable,
            "version": info.version,
            "aggregate": info.aggregate,
            "fields": fields,
        })
    out.sort(key=lambda e: e["type"])
    return out


def _render_ts(events: list[dict], vocabs: dict[str, list[str]]) -> str:
    lines = [
        "/** 自动生成（packages/core/scripts/gen_protocol.py），不要手改。来源：event/catalog.py 的 @declare 声明。 */",
        "",
        "export interface SmithEnvelope<T = unknown> {",
        "  id: string;",
        "  type: string;",
        "  version: number;",
        "  created: number;",
        "  session_id: string | null;",
        "  durable: boolean;",
        "  seq: number | null;",
        "  data: T;",
        "}",
        "",
        "export type AgentMessage = Record<string, unknown>;",
        "",
        "export interface StepUsage {",
        "  input_tokens: number;",
        "  output_tokens: number;",
        "  total_tokens: number;",
        "}",
        "",
        "export interface QueueItem {",
        "  id: string;",
        "  text: string;",
        "  kind: QueueItemKind;",
        "  images: unknown[] | null;",
        "  created_at: number;",
        "}",
        "",
    ]
    for name, values in vocabs.items():
        lines.append(f"export type {name} = {' | '.join(chr(34) + v + chr(34) for v in values)};")
    lines += ["", "export interface SmithEventMap {"]
    for ev in events:
        if ev["fields"]:
            lines.append(f'  "{ev["type"]}": {{')
            for f in ev["fields"]:
                lines.append(f'    {f["name"]}: {f["ts"]};')
            lines.append("  };")
        else:
            lines.append(f'  "{ev["type"]}": Record<string, never>;')
    lines += [
        "}",
        "",
        "export type SmithEventType = keyof SmithEventMap;",
        "",
        "/** durable=true：可回放骨架（重连 after_seq 重放）；volatile：重连可丢。 */",
        "export const DURABLE_EVENTS: ReadonlySet<string> = new Set([",
    ]
    for ev in events:
        if ev["durable"]:
            lines.append(f'  "{ev["type"]}",')
    lines += ["]);", ""]
    return "\n".join(lines)


def _render_schema(events: list[dict], vocabs: dict[str, list[str]]) -> dict:
    defs: dict[str, dict] = dict(_nested_defs())
    for name, values in vocabs.items():
        defs[name] = {"enum": values}
    for ev in events:
        props, required = {}, []
        for f in dataclasses.fields(getattr(catalog, ev["class"])):
            props[f.name] = _json_schema(str(f.type))
            required.append(f.name)
        defs[ev["class"]] = {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "type": {"const": ev["type"]},
                "version": {"const": ev["version"]},
                "created": {"type": "number"},
                "session_id": {"type": ["string", "null"]},
                "durable": {"const": ev["durable"]},
                "seq": {"type": ["integer", "null"]},
                "data": {"type": "object", "properties": props, "required": required},
            },
            "required": ["id", "type", "version", "created", "session_id", "durable", "data"],
        }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "SmithCode session events",
        "$defs": defs,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 core 事件声明生成桌面端协议文件")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录（默认 packages/protocol）")
    parser.add_argument("--check", action="store_true", help="只校验：生成结果与已提交文件一致时返回 0")
    args = parser.parse_args(argv)

    events = _events()
    vocabs = _vocabularies()
    assert len(events) == len(registry.declared_classes()), "有声明不在 catalog.py，生成器只认目录"
    ts = _render_ts(events, vocabs)
    schema = _render_schema(events, vocabs)

    out = Path(args.out)
    new_files = {
        out / "events.d.ts": ts + "\n" if not ts.endswith("\n") else ts,
        out / "events.schema.json": json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
    }
    if args.check:
        mismatched = [
            str(path) for path, content in new_files.items()
            if not path.is_file() or path.read_text(encoding="utf-8") != content
        ]
        if mismatched:
            print(f"协议文件过期，请跑 scripts/gen_protocol.py：{mismatched}")
            return 1
        print(f"协议一致：{len(events)} 个事件")
        return 0
    out.mkdir(parents=True, exist_ok=True)
    for path, content in new_files.items():
        path.write_text(content, encoding="utf-8")
    print(f"已生成：{len(events)} 个事件 → {out}/events.d.ts + events.schema.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
