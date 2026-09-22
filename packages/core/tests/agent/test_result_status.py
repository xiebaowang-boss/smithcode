"""`RunResult.status` 取值集合的守卫（方案 §12 阶段 3 的额外断言）。

宿主按 status 分支渲染，取值漂移会让前端走进"未知状态"的兜底分支且不易察觉：
这里静态扫描**源码**里所有 `RunResult("<字面量>"` 构造，断言都在冻结集合内。
不用运行时断言的原因：漏掉的那条路径可能只在特定分支才被执行到。
"""

from __future__ import annotations

import re
from pathlib import Path

from smithcode.agent.result import RESULT_STATUSES

SRC = Path(__file__).resolve().parents[2] / "src" / "smithcode"
PATTERN = re.compile(r'RunResult\(\s*"([a-z_]+)"')


def _literal_statuses() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in SRC.rglob("*.py"):
        for match in PATTERN.finditer(path.read_text(encoding="utf-8")):
            found.setdefault(match.group(1), []).append(path.name)
    return found


def test_every_literal_status_is_declared():
    found = _literal_statuses()

    assert found, "源码里应当能扫到 RunResult 的字面量构造"
    unknown = {status: where for status, where in found.items() if status not in RESULT_STATUSES}
    assert not unknown, f"未声明的 RunResult.status：{unknown}（新增状态须同步 result.py 与各宿主）"


def test_declared_statuses_are_all_used():
    """反过来也查一次：声明了却没人用的状态说明文档/实现已经脱节。"""
    used = set(_literal_statuses())

    assert used <= RESULT_STATUSES
    assert RESULT_STATUSES - used == set() or RESULT_STATUSES - used == set(), (
        RESULT_STATUSES - used
    )


def test_status_set_matches_the_documented_five():
    """取值集合本身是接口：变了必须是有意的。"""
    assert RESULT_STATUSES == {
        "ok", "interrupted", "denied", "max_iterations", "stream_error",
    }
