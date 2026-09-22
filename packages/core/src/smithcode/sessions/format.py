"""会话日志格式：**只有事件**（JSON Lines，一行一个信封）。

```
{"seq": 3, "id": "…", "type": "session.message.ended.1", "version": 1,
 "created": 1730000000.0, "session_id": "…", "durable": true, "data": {...}}
```

三条规则：

1. **版本化类型名**：写库时用 `类型.版本`（如 `session.step.ended.1`），读回时按
   「不超过该版本的最新一个」取类——所以将来改载荷可以并存，旧日志仍能打开
   （见 `event/registry.py` 的 `class_for`）。
2. **坏行容忍**：空行跳过；文件末尾没有换行的残行是崩溃残留，静默忽略；其余
   解析失败的行计数上报（既有语义逐字保留）。
3. **读不懂的类型计入坏行**：不在注册表里的类型（降级安装、手工编辑）不炸，
   由调用方提示——回放少一条事件比打不开会话可接受。

这里不再有 `t=msg/compact/state/title/model` 这类"记录种类"：会话的一切都是事件，
折叠规则集中在 `sessions/project.py`。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from ..event import registry
from ..event.envelope import Envelope, from_record

#: 日志格式版本（记录级）。载荷级版本在事件声明里（`@declare(version=…)`）。
LOG_VERSION = 1

#: 崩溃恢复的占位结果：结果未知，不断言工具未执行（既有文案，逐字保留）
CRASH_PLACEHOLDER = (
    "（结果未知：进程在工具执行期间中断，未记录到结果。"
    "如需确认，请重新执行或人工核实。）"
)


def versioned_type(env: Envelope) -> str:
    """写库用的类型名：`类型.版本`（读回时按版本取类）。"""
    return registry.versioned_type(env.type, env.version)


def to_record(env: Envelope) -> dict:
    """信封 → 日志记录。

    与 `Envelope.to_record` 的唯一差别：`type` 写成版本化名字（`类型.版本`），
    供回放按版本解析；并记下记录级格式版本。
    """
    record = env.to_record()
    record["type"] = versioned_type(env)
    record["log"] = LOG_VERSION
    return record


def dump_record(record: Mapping) -> str:
    """记录 → 一行文本（紧凑、UTF-8 原样）。"""
    return json.dumps(record, ensure_ascii=False)


def parse_line(line: str) -> dict | None:
    """一行文本 → 记录（空行与坏行返回 None）。"""
    text = line.strip()
    if not text:
        return None
    try:
        record = json.loads(text)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def read_log(path: Path) -> tuple[list[Envelope], int]:
    """读取全部事件：返回 (事件列表, 坏行数)。

    坏行 = 解析失败的行 + 类型不认识的行（见模块说明的第 2、3 条）。
    """
    text = Path(path).read_text(encoding="utf-8")
    partial = bool(text) and not text.endswith("\n")
    lines = text.splitlines()
    events: list[Envelope] = []
    bad = 0
    for index, line in enumerate(lines):
        record = parse_line(line)
        if record is None:
            if line.strip() and not (partial and index == len(lines) - 1):
                bad += 1  # 崩溃残行：预期内，不算坏行
            continue
        env = from_record(record)
        if env is None:
            bad += 1  # 类型不认识（降级安装 / 手工编辑）
            continue
        events.append(env)
    return events, bad


def repair_dangling_tool_calls(messages: list) -> tuple[str, list]:
    """修复悬空 `tool_calls`（崩溃恢复的关键，就地修改 messages）。

    返回 (状态, 追加的占位消息列表)：

    - `none`      ：历史合法，无改动；
    - `appended`  ：悬空在尾部（崩溃点，assistant 已落盘、结果未落盘），
                    按 tool_calls 顺序补占位结果（内容为 `CRASH_PLACEHOLDER`）——
                    调用方把追加的消息**作为事件写进日志**，于是这次恢复本身也
                    成了可回放的事实（而不是只在内存里打个补丁）；
    - `truncated` ：悬空出现在历史中段（手改/截断等异常），从最早的悬空
                    assistant 消息起截断尾部，保证「每个 tool_call_id 恰有
                    一条结果」的不变量。
    """
    satisfied = {
        m.get("tool_call_id")
        for m in messages
        if isinstance(m, dict) and m.get("role") == "tool" and m.get("tool_call_id")
    }
    dangling: list[tuple[int, str]] = []
    for index, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for call in msg.get("tool_calls") or []:
            call_id = call.get("id")
            if call_id and call_id not in satisfied:
                dangling.append((index, call_id))
    if not dangling:
        return "none", []
    assistant_indexes = [
        index for index, msg in enumerate(messages)
        if isinstance(msg, dict) and msg.get("role") == "assistant"
    ]
    last_assistant = max(assistant_indexes) if assistant_indexes else -1
    if all(index == last_assistant for index, _ in dangling):
        appended = [
            {"role": "tool", "content": CRASH_PLACEHOLDER, "tool_call_id": call_id}
            for _, call_id in dangling
        ]
        messages.extend(appended)
        return "appended", appended
    cut = min(index for index, _ in dangling)
    del messages[cut:]
    return "truncated", []
