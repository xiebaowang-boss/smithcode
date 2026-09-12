"""CLI 会话参数与启动恢复：-c/--resume 互斥、定位与失败降级。"""

import pytest

from smithcode import config
from smithcode.cli import _locate_session, _resume_session, build_parser
from smithcode.sessions import SessionStore, StoreError


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(workspace))
    yield workspace


def test_parser_session_flags():
    parser = build_parser()
    args = parser.parse_args(["-c"])
    assert args.continue_session is True
    assert args.resume is None
    args = parser.parse_args(["--resume"])
    assert args.resume == ""
    args = parser.parse_args(["--resume", "abc123"])
    assert args.resume == "abc123"
    args = parser.parse_args(["--name", "迁移会话"])
    assert args.name == "迁移会话"
    args = parser.parse_args(["--no-session-persistence"])
    assert args.no_session_persistence is True
    with pytest.raises(SystemExit):
        parser.parse_args(["-c", "--resume"])  # 互斥


class _FakeAgent:
    def __init__(self):
        self.resumed = None

    def resume(self, summary):
        from smithcode.agent import ResumeReport

        self.resumed = summary
        return ResumeReport(
            path=summary.path,
            session_id=summary.id,
            title=summary.title,
            message_count=3,
            repair="none",
            bad_lines=0,
        )


def test_locate_returns_latest_and_explicit():
    store = SessionStore.create()
    store.append_message({"role": "user", "content": "hi"})
    store.close()

    assert _locate_session(True, "").id == store.id
    assert _locate_session(False, store.id[:8]).id == store.id
    with pytest.raises(StoreError):
        _locate_session(False, "不存在的会话")


def test_resume_session_uses_latest(capsys):
    store = SessionStore.create()
    store.append_message({"role": "user", "content": "hi"})
    store.close()

    agent = _FakeAgent()
    _resume_session(agent, True, "")
    assert agent.resumed is not None and agent.resumed.id == store.id
    assert "已恢复" in capsys.readouterr().out


def test_resume_session_missing_notice(capsys):
    agent = _FakeAgent()
    _resume_session(agent, True, "")
    assert agent.resumed is None
    assert "没有可恢复的会话" in capsys.readouterr().out


def test_resume_session_bad_target_notice(capsys):
    agent = _FakeAgent()
    _resume_session(agent, False, "nope")
    assert agent.resumed is None
    assert "未找到会话" in capsys.readouterr().out
