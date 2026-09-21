"""会话转录存储：懒物化、追加/加载、列举定位、标题、导入与失败降级。"""

import json
import os
import time

import pytest

from smithcode import config
from smithcode.event.catalog import (
    HistoryCompacted,
    MessageEnd,
    ModelSelected,
    SessionCreated,
)
from smithcode.event.envelope import wrap
from smithcode.sessions import (
    SessionStore,
    StoreError,
    delete,
    find,
    find_last,
    list_sessions,
    load,
    rename,
    summary_from_path,
    sweep,
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """SMITHCODE_HOME + 工作区都指向临时目录，绝不碰真实用户目录。"""
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(workspace))
    return workspace


def birth(store, **overrides):
    """写一条 `session.created`（生产由 `Journal.created()` 做）。"""

    fields = {
        "cwd": store.cwd, "model": store.model, "effort": store.effort,
        "app": store.app, "oneshot": store.oneshot,
    }
    fields.update(overrides)
    return store.append_event(wrap(SessionCreated(**fields), session_id=store.id))


def test_lazy_materialization_and_roundtrip(_isolated):
    """懒物化：没有事件就没有文件；首条事件（`session.created`）即物化。

    "system 不落盘"现在是**调用方**的职责（`sync_system` 用 `insert`，不发事件），
    所以这里不再由 store 过滤——见 `session.py` 的说明。
    """
    store = SessionStore.create(model="m1")
    assert not store.path.exists()  # 懒物化：没有事件就没有文件
    birth(store)  # 会话出生事件（生产的 `Journal.created()` 负责写它）
    body = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "在"}]
    for message in body:
        store.append_event(wrap(MessageEnd(message=message)))
    store.flush()

    assert store.path.is_file()
    lines = store.path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    assert first["type"].startswith("session.created")  # 类型名带版本
    assert first["session_id"] == store.id
    assert first["data"]["model"] == "m1"
    assert first["seq"] == 1  # 单调序号从 1 开始
    assert len(lines) == 3  # created + user + assistant

    loaded = load(summary_from_path(store.path))
    assert [m["role"] for m in loaded.messages] == ["user", "assistant"]
    assert loaded.id == store.id
    assert loaded.model == "m1"


def test_compaction_checkpoint_resets_projection(_isolated):
    store = SessionStore.create()
    store.append_event(wrap(MessageEnd(message={"role": "user", "content": "旧问题"})))
    store.append_event(wrap(MessageEnd(message={"role": "assistant", "content": "旧回答"})))
    summary = {"role": "user", "content": "<context-summary>摘要</context-summary>"}
    tail = [{"role": "user", "content": "近期问题"}]
    store.append_event(wrap(HistoryCompacted(
        summary=summary, tail=tuple(tail), before_tokens=100, after_tokens=10,
    )))
    store.append_event(wrap(MessageEnd(message={"role": "assistant", "content": "新回答"})))

    loaded = load(summary_from_path(store.path))
    assert loaded.messages[0]["content"].startswith("<context-summary>")
    assert loaded.messages[1]["content"] == "近期问题"
    assert loaded.messages[2]["content"] == "新回答"
    assert loaded.compact_count == 1


def test_list_find_rename_delete(_isolated):
    first = SessionStore("a" * 32)
    first.append_event(wrap(MessageEnd(message={"role": "user", "content": "第一个会话"})))
    first.close()
    second = SessionStore("ab" + "0" * 30)
    second.append_event(wrap(MessageEnd(message={"role": "user", "content": "第二个会话"})))
    second.close()

    summaries = list_sessions(limit=0)
    assert {item.id for item in summaries} == {first.id, second.id}

    assert find("a" * 32).id == first.id
    assert find("ab").id == second.id  # 唯一前缀
    with pytest.raises(StoreError):
        find("a")  # 前缀同时命中两个

    assert rename(first.id[:8], "会话甲")
    by_id = {item.id: item for item in list_sessions(limit=0)}
    assert by_id[first.id].title == "会话甲"
    assert by_id[first.id].display_name == "会话甲"

    assert delete(second.id)
    assert not second.path.exists()
    assert delete("不存在") is False


def test_find_last_excludes_oneshot(_isolated):
    normal = SessionStore.create()
    birth(normal)
    normal.append_event(wrap(MessageEnd(message={"role": "user", "content": "正常会话"})))
    normal.close()
    oneshot = SessionStore.create(oneshot=True)
    birth(oneshot)  # oneshot 标记记在出生事件里
    oneshot.append_event(wrap(MessageEnd(message={"role": "user", "content": "一次性任务"})))
    oneshot.close()
    future = time.time() + 5
    os.utime(oneshot.path, (future, future))  # 一次性会话更新也不参与 -c

    last = find_last()
    assert last is not None and last.id == normal.id
    assert find(oneshot.id).id == oneshot.id  # 显式 id 仍可直达


def test_model_events_last_wins(_isolated):
    """模型事件回答「谁生成的」：加载取**最后一条**（去重归调用方，见 Agent）。

    去重为什么归调用方：日志只记事实，"这个模型连着用了三次"与"换过一次模型"
    都是事实；而"要不要为同值再写一条"是实现策略，不该埋进写路径。
    """
    store = SessionStore.create(model="m1")
    birth(store)
    store.append_event(wrap(MessageEnd(message={"role": "user", "content": "第一轮"})))
    store.append_event(wrap(ModelSelected("m1", "high")))
    store.append_event(wrap(ModelSelected("m2", "low")))  # /model 切换
    store.close()

    lines = [json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines()]
    written = [(r["data"]["model"], r["data"]["effort"]) for r in lines
               if r["type"].startswith("session.model.selected")]
    assert written == [("m1", "high"), ("m2", "low")]

    loaded = load(summary_from_path(store.path))
    assert (loaded.model, loaded.effort) == ("m2", "low")  # 最后一条生效
    # 列举摘要也取最后一条
    assert summary_from_path(store.path).model == "m2"


def test_model_falls_back_to_created_event(_isolated):
    """没有模型事件时回退 `session.created` 的创建时模型，而不是留空。"""
    legacy = SessionStore.create(model="old-model")
    birth(legacy)
    legacy.append_event(wrap(MessageEnd(message={"role": "user", "content": "旧会话"})))
    legacy.close()
    loaded = load(summary_from_path(legacy.path))
    assert loaded.model == "old-model"

    # 列举同样取最后一条模型事件（长日志里标题/模型事件都靠尾窗读取）
    fresh = SessionStore.create(model="m1")
    birth(fresh)
    fresh.append_event(wrap(MessageEnd(message={"role": "user", "content": "新会话"})))
    fresh.append_event(wrap(ModelSelected("m2", "low")))
    fresh.close()
    assert summary_from_path(fresh.path).model == "m2"


def test_sync_fsyncs_only_with_a_live_handle(_isolated, monkeypatch):
    """sync 是 fsync 屏障：没有句柄（未物化 / 已 close）时是纯无操作。"""
    calls = []
    monkeypatch.setattr(os, "fsync", lambda fd: calls.append(fd))

    store = SessionStore.create()
    store.sync()
    assert calls == []
    assert not store.path.exists()  # 不因 sync 建出空转录

    store.append_event(wrap(MessageEnd(message={"role": "user", "content": "你好"})))
    store.sync()
    assert len(calls) == 1

    store.close()
    store.sync()
    assert len(calls) == 1  # close 之后不再触碰已释放的句柄


def test_write_failure_disables_store(_isolated, monkeypatch, capsys):
    from smithcode.sessions import store as store_mod

    def boom(path):
        raise OSError("磁盘只读")

    from smithcode.event import Bus, activate, reset
    from smithcode.frontend.console import ConsoleFrontend

    bus = Bus(session_id="t")
    bus.subscribe(ConsoleFrontend().on_event)  # 写失败警告走事件，装配终端前端读 stdout
    token = activate(bus)
    try:
        _write_failure_case(store_mod, monkeypatch, boom, capsys)
    finally:
        reset(token)


def _write_failure_case(store_mod, monkeypatch, boom, capsys):
    monkeypatch.setattr(store_mod.paths, "ensure_private_dir", boom)
    store = SessionStore.create()
    store.append_event(wrap(MessageEnd(message={"role": "user", "content": "x"})))  # 不得抛异常
    assert store.disabled is True
    assert not store.path.exists()
    assert "不会被自动保存" in capsys.readouterr().out


def test_sweep_removes_expired(_isolated):
    old = SessionStore.create()
    old.append_event(wrap(MessageEnd(message={"role": "user", "content": "旧"})))
    old.close()
    fresh = SessionStore.create()
    fresh.append_event(wrap(MessageEnd(message={"role": "user", "content": "新"})))
    fresh.close()
    stale = time.time() - 40 * 86400
    os.utime(old.path, (stale, stale))

    assert sweep(30) == 1
    assert not old.path.exists()
    assert fresh.path.exists()
    assert sweep(0) == 0  # 0 = 不清理
