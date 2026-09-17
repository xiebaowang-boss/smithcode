"""转录格式 v1：JSONL 记录的编解码与容错（纯逻辑，零 IO 依赖可单测）。

每行一个 JSON 对象，恒有 `v`（格式版本）与 `t`（记录类型）：

- `meta`    首行；id / cwd / created / model / effort / app / oneshot
- `msg`     非 system 消息（OpenAI 消息格式原样保存）
- `compact` 压缩检查点：从本行起活动上下文 = summary + tail（自包含）
- `state`   会话级投影缓存（goal / plan / skills），取最后一条
- `title`   标题记录（source=user/auto），取最后一条
- `model`   生效模型 / 思考强度（与上一条不同时才追加，取最后一条）
- `branch`  分支血缘（fork 时写入新文件）
- `usage`   单次调用的 token 用量（列表/统计用，不参与上下文装配）
"""
from __future__ import annotations

import json
import time
from pathlib import Path

FORMAT_VERSION = 1

T_META = "meta"
T_MSG = "msg"
T_COMPACT = "compact"
T_STATE = "state"
T_TITLE = "title"
T_MODEL = "model"
T_BRANCH = "branch"
T_USAGE = "usage"

TITLE_SOURCE_USER = "user"
TITLE_SOURCE_AUTO = "auto"

# 崩溃恢复时补的占位工具结果（悬空 tool_calls 会让服务商拒绝下一次请求）。
# 措辞必须承认「结果未知」，不能断言「未执行」：崩溃点只能证明结果没落盘，
# 工具可能早已执行并产生了副作用；断言未执行会诱使模型直接重试写操作。
CRASH_PLACEHOLDER = (
    "（上次会话中断，此工具调用的执行结果未知：它可能已经执行并产生了副作用。"
    "只读或幂等操作可以直接重试；写文件、运行命令等可能改动外部状态的操作，"
    "请先核实当前状态（重新读取文件、检查命令输出等）再决定，必要时询问用户。）"
)

# 标题长度上限的兜底（config [sessions].title_max_chars 可配）
DEFAULT_TITLE_CHARS = 60


def meta_record(
    session_id: str,
    cwd: str,
    model: str = "",
    effort: str = "",
    app: str = "",
    oneshot: bool = False,
) -> dict:
    return {
        "v": FORMAT_VERSION,
        "t": T_META,
        "id": str(session_id),
        "cwd": str(cwd),
        "created": time.time(),
        "model": str(model),
        "effort": str(effort),
        "app": str(app),
        "oneshot": bool(oneshot),
    }


def msg_record(message: dict) -> dict:
    return {"v": FORMAT_VERSION, "t": T_MSG, "m": message}


def compact_record(summary: dict, tail: list, before: int = 0, after: int = 0) -> dict:
    return {
        "v": FORMAT_VERSION,
        "t": T_COMPACT,
        "summary": summary,
        "tail": list(tail),
        "before": int(before or 0),
        "after": int(after or 0),
        "ts": time.time(),
    }


def state_record(payload: dict) -> dict:
    return {"v": FORMAT_VERSION, "t": T_STATE, "state": payload, "ts": time.time()}


def title_record(title: str, source: str = TITLE_SOURCE_USER) -> dict:
    return {
        "v": FORMAT_VERSION,
        "t": T_TITLE,
        "title": str(title),
        "source": str(source),
        "ts": time.time(),
    }


def model_record(model: str, effort: str = "") -> dict:
    """生效模型记录（与上一条不同时才写，取最后一条）。

    `meta` 只记创建会话时的模型，`/model` 中途切换后转录无法回答「这条消息是
    谁生成的」；本记录补上这个事实，恢复时据此还原上一轮的模型与思考强度。
    """
    return {
        "v": FORMAT_VERSION,
        "t": T_MODEL,
        "model": str(model),
        "effort": str(effort),
        "ts": time.time(),
    }


def branch_record(parent_id: str) -> dict:
    return {"v": FORMAT_VERSION, "t": T_BRANCH, "from": str(parent_id), "ts": time.time()}


def usage_record(usage: dict) -> dict:
    return {"v": FORMAT_VERSION, "t": T_USAGE, "usage": usage, "ts": time.time()}


def dump_record(record: dict) -> str:
    """一条记录 → 一行 JSON（ensure_ascii=False 保留中文可读性）。"""
    return json.dumps(record, ensure_ascii=False) + "\n"


def parse_line(line: str) -> dict | None:
    """解析一行：空行 / 非对象 / 非法 JSON 返回 None（调用方决定容错语义）。"""
    text = line.strip()
    if not text:
        return None
    try:
        record = json.loads(text)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def read_transcript(path: Path) -> tuple[list[dict], int]:
    """读取全部记录：返回 (记录列表, 中间坏行数)。

    - 空行跳过；
    - 文件末尾没有换行的残行视为崩溃残留，静默忽略；
    - 其余解析失败的行计数返回，由调用方决定是否提示。
    """
    text = Path(path).read_text(encoding="utf-8")
    partial = bool(text) and not text.endswith("\n")
    lines = text.splitlines()
    records: list[dict] = []
    bad = 0
    for index, line in enumerate(lines):
        record = parse_line(line)
        if record is not None:
            records.append(record)
        elif line.strip():
            if partial and index == len(lines) - 1:
                continue  # 崩溃残行：预期内，不算坏行
            bad += 1
    return records, bad


def repair_dangling_tool_calls(messages: list) -> tuple[str, list]:
    """修复悬空 `tool_calls`（崩溃恢复的关键，就地修改 messages）。

    返回 (状态, 追加的占位消息列表)：

    - `none`      ：历史合法，无改动；
    - `appended`  ：悬空在尾部（崩溃点，assistant 已落盘、结果未落盘），
                    按 tool_calls 顺序补占位结果（内容为 `CRASH_PLACEHOLDER`，
                    声明「结果未知」并给出核实建议，不断言工具未执行）——
                    调用方应把追加的消息持久化，避免下次恢复被误判为中段损坏；
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

    indices = {index for index, _ in dangling}
    last = max(indices)
    tail_only = all(
        isinstance(m, dict) and m.get("role") == "tool" for m in messages[last + 1:]
    )
    if len(indices) == 1 and tail_only:
        appended = [
            {"role": "tool", "tool_call_id": call_id, "content": CRASH_PLACEHOLDER}
            for _, call_id in dangling
        ]
        messages.extend(appended)
        return "appended", appended

    del messages[min(indices):]
    return "truncated", []
