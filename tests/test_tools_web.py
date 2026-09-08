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


def test_webfetch_batch_fetches_all_urls(monkeypatch):
    def fake_urlopen(req, timeout):
        return FakeResp(f"<p>内容-{req.full_url}</p>")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = web.webfetch(["https://a.example.com/", "https://b.example.com/"])
    assert "内容-https://a.example.com/" in out
    assert "内容-https://b.example.com/" in out
    # 每个地址的结果有分隔头，可对应回原地址
    assert "===== [1] https://a.example.com/ =====" in out
    assert "===== [2] https://b.example.com/ =====" in out


def test_webfetch_batch_keeps_single_url_result_plain(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout: FakeResp("<p>单页</p>"),
    )
def test_webfetch_batch_rejects_too_many_urls():
    urls = [f"https://example.com/{i}" for i in range(web.MAX_URLS + 1)]
    out = web.webfetch(urls)
    assert "最多并行抓取" in out and "多次调用" in out


def test_webfetch_batch_empty_list():
    assert "url 列表为空" in web.webfetch([])


def test_webfetch_batch_partial_failure(monkeypatch):
    def fake_urlopen(req, timeout):
        if "bad" in req.full_url:
            raise urllib.error.URLError("getaddrinfo failed")
        return FakeResp("<p>正常</p>")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = web.webfetch(["https://bad.invalid/", "https://good.example.com/"])
    # 失败的地址不拖垮整批：正常结果与错误信息都在，且各归各的分隔段
    assert "正常" in out
    assert "抓取失败" in out
