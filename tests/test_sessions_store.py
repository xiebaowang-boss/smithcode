"""会话转录存储：懒物化、追加/加载、列举定位、标题、导入与失败降级。"""

import json
import os
import time

import pytest

from smithcode import config
from smithcode.sessions import (
    SessionStore,
    StoreError,
    delete,
    find,
    find_last,
    import_json,
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


def test_lazy_materialization_and_roundtrip(_isolated):
    store = SessionStore.create(model="m1")
    assert not store.path.exists()  # 懒物化：没有消息就没有文件
    store.append_message({"role": "system", "content": "system 不落盘"})
    assert not store.path.exists()
    store.append_message({"role": "user", "content": "你好"})
    store.append_message({"role": "assistant", "content": "在"})
    store.flush()

    assert store.path.is_file()
    lines = store.path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    assert first["t"] == "meta"
    assert first["id"] == store.id
    assert first["model"] == "m1"
    assert len(lines) == 3  # meta + user + assistant

    loaded = load(summary_from_path(store.path))
    assert [m["role"] for m in loaded.messages] == ["user", "assistant"]
    assert loaded.id == store.id


def test_compaction_checkpoint_resets_projection(_isolated):
    store = SessionStore.create()
    store.append_message({"role": "user", "content": "旧问题"})
    store.append_message({"role": "assistant", "content": "旧回答"})
    summary = {"role": "user", "content": "<context-summary>摘要</context-summary>"}
    tail = [{"role": "user", "content": "近期问题"}]
    store.append_compaction(summary, tail, before=100, after=10)
    store.append_message({"role": "assistant", "content": "新回答"})

    loaded = load(summary_from_path(store.path))
    assert loaded.messages[0]["content"].startswith("<context-summary>")
    assert loaded.messages[1]["content"] == "近期问题"
    assert loaded.messages[2]["content"] == "新回答"
    assert loaded.compact_count == 1


def test_list_find_rename_delete(_isolated):
    first = SessionStore("a" * 32)
    first.append_message({"role": "user", "content": "第一个会话"})
    first.close()
    second = SessionStore("ab" + "0" * 30)
    second.append_message({"role": "user", "content": "第二个会话"})
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
    normal.append_message({"role": "user", "content": "正常会话"})
    normal.close()
    oneshot = SessionStore.create(oneshot=True)
    oneshot.append_message({"role": "user", "content": "一次性任务"})
    oneshot.close()
    future = time.time() + 5
    os.utime(oneshot.path, (future, future))  # 一次性会话更新也不参与 -c

    last = find_last()
    assert last is not None and last.id == normal.id
    assert find(oneshot.id).id == oneshot.id  # 显式 id 仍可直达


def test_import_legacy_json(_isolated):
    legacy_dir = _isolated / "sessions"
    legacy_dir.mkdir()
    legacy = legacy_dir / "20260101_000000.json"
    legacy.write_text(
        json.dumps(
            [
                {"role": "user", "content": "旧消息"},
                {"role": "assistant", "content": "旧回复"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    summary = import_json(legacy)
    loaded = load(summary)
    assert [m["role"] for m in loaded.messages] == ["user", "assistant"]
    assert loaded.messages[0]["content"] == "旧消息"


def test_import_legacy_json_rejects_invalid(_isolated):
    bad = _isolated / "bad.json"
    bad.write_text("{不是数组}", encoding="utf-8")
    with pytest.raises(StoreError):
        import_json(bad)


def test_write_failure_disables_store(_isolated, monkeypatch, capsys):
    from smithcode.sessions import store as store_mod

    def boom(path):
        raise OSError("磁盘只读")

    monkeypatch.setattr(store_mod.paths, "ensure_private_dir", boom)
    store = SessionStore.create()
    store.append_message({"role": "user", "content": "x"})  # 不得抛异常
    assert store.disabled is True
    assert not store.path.exists()
    assert "不会被自动保存" in capsys.readouterr().out


def test_sweep_removes_expired(_isolated):
    old = SessionStore.create()
    old.append_message({"role": "user", "content": "旧"})
    old.close()
    fresh = SessionStore.create()
    fresh.append_message({"role": "user", "content": "新"})
    fresh.close()
    stale = time.time() - 40 * 86400
    os.utime(old.path, (stale, stale))

    assert sweep(30) == 1
    assert not old.path.exists()
    assert fresh.path.exists()
    assert sweep(0) == 0  # 0 = 不清理
