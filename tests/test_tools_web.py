"""webfetch 工具测试：协议白名单、HTML 转文本、截断与错误处理。"""
import urllib.error

from smithcode.tools import web


class FakeHeaders:
    """模拟 email.message.Message 的 get_content_charset。"""

    def __init__(self, charset: str):
        self._charset = charset

    def get_content_charset(self):
        return self._charset


class FakeResp:
    """模拟 urlopen 的返回：上下文管理器 + headers + read。"""

    def __init__(self, body: str, charset: str = "utf-8", final_url: str = "https://example.com/x"):
        self._body = body.encode(charset)
        self.headers = FakeHeaders(charset)
        self._final = final_url

    def geturl(self):
        return self._final

    def read(self, n=-1):
        return self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_webfetch_rejects_non_http_scheme():
    assert "仅支持" in web.webfetch("file:///C:/Windows/win.ini")
    assert "仅支持" in web.webfetch("ftp://example.com/a")


def test_webfetch_strips_html(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout: FakeResp(
            "<html><head><script>var x=1;</script></head>"
            "<body><h1>标题</h1><p>Hello <b>world</b></p></body></html>"
        ),
    )
    out = web.webfetch("https://example.com/x")
    assert "Hello" in out and "world" in out and "标题" in out
    assert "<" not in out
    assert "var x=1" not in out


def test_webfetch_truncates(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout: FakeResp("<p>" + "字" * 5000 + "</p>"),
    )
    out = web.webfetch("https://example.com/x", max_chars=700)
    assert len(out) == 700
    # 过小的值被抬到下限 500
    out2 = web.webfetch("https://example.com/x", max_chars=10)
    assert len(out2) == 500


def test_webfetch_http_error(monkeypatch):
    def raise_404(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", None, None)

    monkeypatch.setattr("urllib.request.urlopen", raise_404)
    out = web.webfetch("https://example.com/missing")
    assert "404" in out


def test_webfetch_network_error(monkeypatch):
    def raise_urlerror(req, timeout):
        raise urllib.error.URLError("getaddrinfo failed")

    monkeypatch.setattr("urllib.request.urlopen", raise_urlerror)
    out = web.webfetch("https://nonexistent.invalid/x")
    assert "错误" in out


def test_webfetch_rejects_non_http_redirect(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout: FakeResp("<p>x</p>", final_url="ftp://evil/x"),
    )
    assert "重定向" in web.webfetch("https://example.com/x")
