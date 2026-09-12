"""websearch 工具测试：结果解析、跳转链接还原、条数限制与错误处理（不联网）。"""
import urllib.error

from smithcode.tools import FUNCTIONS, SCHEMAS
from smithcode.tools import websearch as ws


def test_websearch_registered():
    assert "websearch" in FUNCTIONS
    assert any(s["name"] == "websearch" for s in SCHEMAS)


class FakeHeaders:
    """模拟 email.message.Message 的 get_content_charset。"""

    def __init__(self, charset: str = "utf-8"):
        self._charset = charset

    def get_content_charset(self):
        return self._charset


class FakeResp:
    """模拟 urlopen 的返回：上下文管理器 + headers + read。"""

    def __init__(self, body: str, charset: str = "utf-8"):
        self._body = body.encode(charset)
        self.headers = FakeHeaders(charset)

    def read(self, n=-1):
        return self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


PAGE = """
<html><body>
<div class="result results_links results_links_deep web-result">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=x">Example <b>A</b></a>
  </h2>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">First <b>snippet</b> text.</a>
  <a class="result__url" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">example.com</a>
</div>
<div class="result results_links results_links_deep web-result">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a" href="https://direct.example.org/page">Direct Title</a>
  </h2>
  <a class="result__snippet" href="https://direct.example.org/page">Second snippet.</a>
</div>
</body></html>
"""


def _patch_page(monkeypatch, body: str):
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: FakeResp(body))


def test_parse_results_extracts_title_url_snippet():
    results = ws._parse_results(PAGE, ws.MAX_RESULTS)
    assert len(results) == 2
    assert results[0]["title"] == "Example A"
    assert results[0]["url"] == "https://example.com/a"
    assert results[0]["snippet"] == "First snippet text."
    assert results[1]["title"] == "Direct Title"
    assert results[1]["url"] == "https://direct.example.org/page"


def test_parse_results_dedupes_same_url():
    page = PAGE + PAGE  # 同一结果重复出现两次
    assert len(ws._parse_results(page, ws.MAX_RESULTS)) == 2


def test_real_url_decodes_relative_redirect():
    href = "/l/?uddg=https%3A%2F%2Fexample.com%2Fb%3Fx%3D1"
    assert ws._real_url(href) == "https://example.com/b?x=1"
    assert ws._real_url("https://plain.example.com/") == "https://plain.example.com/"


def test_websearch_formats_numbered_results(monkeypatch):
    _patch_page(monkeypatch, PAGE)
    out = ws.websearch("python")
    assert "1. Example A" in out
    assert "https://example.com/a" in out
    assert "First snippet text." in out
    assert "2. Direct Title" in out


def test_websearch_respects_max_results(monkeypatch):
    _patch_page(monkeypatch, PAGE)
    out = ws.websearch("python", max_results=1)
    assert "Example A" in out
    assert "Direct Title" not in out


def test_websearch_no_results(monkeypatch):
    _patch_page(monkeypatch, "<html><body>nothing here</body></html>")
    assert "无搜索结果" in ws.websearch("zzz")


def test_websearch_empty_query():
    assert "错误" in ws.websearch("   ")


def test_websearch_network_error(monkeypatch):
    def raise_urlerror(req, timeout):
        raise urllib.error.URLError("getaddrinfo failed")

    monkeypatch.setattr("urllib.request.urlopen", raise_urlerror)
    assert "错误" in ws.websearch("python")


def test_websearch_http_error(monkeypatch):
    def raise_http(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", None, None)

    monkeypatch.setattr("urllib.request.urlopen", raise_http)
    out = ws.websearch("python")
    assert "HTTP 429" in out
