"""协议生成器的幂等与覆盖测试：声明一改，生成产物必须跟着变（--check 会红）。

不测生成内容本身（那是实现的细节），只锁住三条契约：
1. 目录里 34 个事件全在产物里（一个不少）；
2. 同一输入两次生成逐字节相同（幂等）；
3. `--check` 在产物最新时返回 0。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parents[1]
PROTOCOL = CORE.parents[0] / "protocol"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "scripts/gen_protocol.py", *args],
        cwd=CORE, capture_output=True, text=True, check=False,
    )


def test_all_declared_events_are_in_the_output():
    import smithcode.event.catalog  # noqa: F401 触发声明
    from smithcode.event import registry

    schema = json.loads((PROTOCOL / "events.schema.json").read_text(encoding="utf-8"))
    ts = (PROTOCOL / "events.d.ts").read_text(encoding="utf-8")
    for cls in registry.declared_classes():
        if cls.__module__ != "smithcode.event.catalog":
            continue
        info = registry.meta(cls)
        assert f'"{info.type}"' in ts, f"{info.type} 缺失于 events.d.ts"
        assert cls.__name__ in schema["$defs"], f"{cls.__name__} 缺失于 schema"


def test_generation_is_idempotent_and_check_passes():
    before_ts = (PROTOCOL / "events.d.ts").read_text(encoding="utf-8")
    before_schema = (PROTOCOL / "events.schema.json").read_text(encoding="utf-8")
    result = _run()
    assert result.returncode == 0, result.stderr
    assert (PROTOCOL / "events.d.ts").read_text(encoding="utf-8") == before_ts
    assert (PROTOCOL / "events.schema.json").read_text(encoding="utf-8") == before_schema
    check = _run("--check")
    assert check.returncode == 0, check.stdout + check.stderr


def test_envelope_records_match_generated_schema_shapes():
    """真实信封的 to_record() 形状与生成产物一致（抽查一持久一易失）。"""
    from smithcode.event import catalog
    from smithcode.event.envelope import wrap

    schema = json.loads((PROTOCOL / "events.schema.json").read_text(encoding="utf-8"))
    ts = (PROTOCOL / "events.d.ts").read_text(encoding="utf-8")
    for payload in (
        catalog.PromptFinished(id="x", kind="permission", outcome="answered", value="y"),
        catalog.MessageUpdate(message={"role": "assistant", "content": "hi"}, delta="hi", kind="content"),
    ):
        record = wrap(payload, session_id="s").to_record()
        cls_name = type(payload).__name__
        assert cls_name in schema["$defs"], f"{cls_name} 缺失于 schema"
        data_schema = schema["$defs"][cls_name]["properties"]["data"]
        for key in record["data"]:
            assert key in data_schema["properties"], f"{cls_name}.{key} 缺失于 schema"
        assert record["type"] in ts
