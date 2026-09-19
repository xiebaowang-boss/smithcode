"""pytest 全局隔离与共享测试设施。

隔离：终端标题是进程级单例，且把 tty 判定换成 False。理由：TUI 挂载即调用
`title.attach()`，若此时 stdout 恰好是终端（例如 pytest 带 `-s` 运行），测试
就会真的改写用户终端的标题、并向进程注册退出钩子。这里统一挡掉；需要观察
写入的用例（tests/test_title.py）自行覆盖这两个打桩。

设施：`fake_http_proxy` 提供本地假 HTTP 代理，用来验证网络工具真的读取并
使用环境代理（见该 fixture 的 docstring）。
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _isolate_terminal_title(monkeypatch):
    from smithcode import title

    title.reset()
    monkeypatch.setattr(title, "stdout_is_tty", lambda: False)
    monkeypatch.setattr(title, "write_terminal_control", lambda seq: None)
    yield
    title.reset()


@pytest.fixture(autouse=True)
def _isolate_interaction_bridge():
    """交互桥挂在 ContextVar 上（`Agent.start` 在主线程挂载）：用完复位。

    不复位会把上一个用例的 Agent 泄漏给同线程的下一个用例——那时权限确认
    会朝一个已经结束的 Agent 发事件。
    """
    from smithcode.agent import interactions

    token = interactions.activate(None)
    yield
    interactions.reset(token)


# ---------- 本地假 HTTP 代理（网络工具的环境代理回归用例） ----------

_PROXY_PAGE_BODY = "<html><body><p>来自代理 {path}</p></body></html>"
# 检索用例把后端端点指到 .../search，据此回一份 Brave 结构的结果页
_PROXY_SEARCH_BODY = (
    '<html><body><div data-type="web"><a href="https://proxy.example.org/r">'
    '<div class="title search-snippet-title">代理结果</div></a>'
    '<div class="content line-clamp-dynamic">代理摘要</div>'
    "</div></body></html>"
)


@pytest.fixture
def fake_http_proxy():
    """本地假 HTTP 代理：证明工具真的走了环境代理，且不依赖外网。

    按路径分流：含 `search` 的回一份 Brave 结构的结果页（websearch），其余回
    `来自代理 <绝对 URL>`（webfetch）；`.requests` 记录每个被转发的请求
    （方法 / 绝对 URI / 表单体 / UA）。目标域名请用 `.invalid` 保留域——不走
    代理就必然 DNS 失败，所以用例通过即等价于"代理生效"。
    """
    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _relay(self):
            length = int(self.headers.get("Content-Length") or 0)
            payload = self.rfile.read(length) if length else b""
            requests.append({
                "method": self.command,
                "path": self.path,  # 经代理的明文 HTTP 请求行是绝对 URL
                "body": payload.decode("utf-8", "replace"),
                "user_agent": self.headers.get("User-Agent", ""),
            })
            template = _PROXY_SEARCH_BODY if "search" in self.path else _PROXY_PAGE_BODY
            body = template.format(path=self.path).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _relay
        do_POST = _relay

        def log_message(self, *args):  # 静音，别把代理访问日志打进测试输出
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield SimpleNamespace(url=f"http://{host}:{port}", requests=requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
