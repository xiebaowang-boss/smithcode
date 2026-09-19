"""端到端冒烟（headless）：本地 OpenAI 兼容 stub + **真实** 客户端/流解析/循环/落盘。

与其余测试的区别：这里**不 monkeypatch `LLMClient`**——走真实 `LLMClient.from_config()`
（含 openai SDK、SSE 解析、重试层）、真实 `Agent.run_with_goal`、真实转录落盘与
`cli._run_agent_task` 驱动。补的是单测覆盖不到的那一段：配置 → 网络 → 解析 → 历史。

真实交互（TUI 面板、Esc 语义、队列面板）由使用者在实际终端体验，不在自动化范围。
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from smithcode import config
from smithcode.agent import Agent
from smithcode.session import Session

MODEL = "stub-model"


def _chunk(delta: dict, finish: str | None = None) -> str:
    payload = {
        "id": "chatcmpl-stub",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": MODEL,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n"


class StubServer:
    """最小 OpenAI 兼容端点：/chat/completions 回 SSE，/models 回模型列表。"""

    def __init__(self, chunks: list[str]):
        self.chunks = chunks
        self.requests: list[dict] = []

    def start(self) -> None:
        chunks, requests = self.chunks, self.requests

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b"{}"
                requests.append({"path": self.path, "body": json.loads(body or b"{}")})
                data = "".join(chunks).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                requests.append({"path": self.path, "body": None})
                data = json.dumps(
                    {"object": "list", "data": [{"id": MODEL, "object": "model"}]}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"


@pytest.fixture
def stub():
    server = StubServer([
        _chunk({"role": "assistant", "content": "你好"}),
        _chunk({"content": "，世界"}),
        _chunk({}, finish="stop"),
        "data: [DONE]\n\n",
    ])
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def pointed_at_stub(monkeypatch, stub, tmp_path):
    """把配置指向 stub，并给一个干净的会话目录（不碰用户 ~/.smithcode）。"""
    monkeypatch.setattr(config, "URL", stub.url)
    monkeypatch.setattr(config, "KEY", "sk-stub")
    monkeypatch.setattr(config, "MODEL", MODEL)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    return stub


def test_full_turn_over_http(pointed_at_stub, capfd):
    """一次完整回合：真实 HTTP + SSE 解析 + 循环 + 渲染 + 历史。"""
    agent = Agent(session=Session())  # 不替换 LLMClient：走真实客户端

    result = asyncio.run(agent.session_owner.run_with_goal("打个招呼"))

    assert result.status == "ok"
    assert result.text == "你好，世界"
    assert [m["role"] for m in agent.session.messages] == ["system", "user", "assistant"]
    # 请求确实发出去了，且形状正确（模型名 / 消息 / 工具 schema）
    sent = [r for r in pointed_at_stub.requests if r["path"].endswith("/chat/completions")]
    assert len(sent) == 1
    body = sent[0]["body"]
    assert body["model"] == MODEL
    assert body["messages"][-1]["content"] == "打个招呼"
    assert body["tools"]  # 工具 schema 照常随请求发送
    # 正文实时上屏（ConsoleRenderer 走的是流式打印）
    assert "你好" in capfd.readouterr().out


def test_repl_driver_runs_the_same_path(pointed_at_stub, capfd):
    """`cli._run_agent_task`（REPL 后台任务入口）同样跑得通：事件循环 + 出错上报。"""
    from smithcode.cli import _run_agent_task

    agent = Agent(session=Session())

    _run_agent_task(agent, "打个招呼")

    assert "你好，世界" in capfd.readouterr().out


def test_transcript_is_persisted_and_resumable(pointed_at_stub, tmp_path, monkeypatch):
    """落盘：转录（消息）与状态投影都写进磁盘——fsync 屏障的真实路径。

    `SMITHCODE_HOME` 指到临时目录，避免碰用户真实的 ~/.smithcode。
    """
    home = tmp_path / "home"
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    agent = Agent(session=Session(), persist=True)

    asyncio.run(agent.session_owner.run_with_goal("第一句"))
    agent.close()

    files = sorted(home.rglob("*.jsonl"))
    assert files, "持久化会话应当留下转录文件"
    raw = files[-1].read_text(encoding="utf-8")
    assert "第一句" in raw and "你好，世界" in raw
    assert "state" in raw  # 状态投影（t=state）也落了盘


def test_http_failure_is_reported_not_swallowed(monkeypatch, tmp_path):
    """服务端不可用：错误要如实抛给宿主（而不是静默返回空回复）。"""
    monkeypatch.setattr(config, "URL", "http://127.0.0.1:1/v1")  # 必然连不上
    monkeypatch.setattr(config, "KEY", "sk-stub")
    monkeypatch.setattr(config, "MODEL", MODEL)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "MAX_RETRIES", 0)
    agent = Agent(session=Session())

    result = asyncio.run(agent.session_owner.run_with_goal("打个招呼"))

    # 契约：流层故障不向上抛，而是以 stream_error 状态 + 原因交宿主渲染
    assert result.status == "stream_error"
    assert result.reason
    assert result.text == ""
