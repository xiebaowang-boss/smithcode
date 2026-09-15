"""websearch 工具测试：结果解析、跳转链接还原、条数限制、错误处理与代理（不联网）。

HTTP 层用 httpx2 的 MockTransport 打桩（替换工具的客户端工厂）；代理用例用
`fake_http_proxy` 做真实回环验证（POST 表单经代理转发）。
"""
import httpx2

from smithcode.tools import FUNCTIONS, SCHEMAS
from smithcode.tools import websearch as ws


def _patch_client(monkeypatch, handler):
    """把工具的客户端工厂换成 MockTransport 版：签名与真实工厂一致。"""

    def factory(*, timeout, headers=None, follow_redirects=True):
        return httpx2.Client(
            transport=httpx2.MockTransport(handler),
            timeout=timeout,
            headers=headers,
            follow_redirects=follow_redirects,
        )

    monkeypatch.setattr(ws, "http_client", factory)


def _page(body: str, charset: str = "utf-8") -> httpx2.Response:
    return httpx2.Response(
        200,
        headers={"Content-Type": f"text/html; charset={charset}"},
        content=body.encode(charset),
    )


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


def test_websearch_registered():
    assert "websearch" in FUNCTIONS
    assert any(s["name"] == "websearch" for s in SCHEMAS)


# ---------- 纯逻辑：解析与格式化 ----------


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
    _patch_client(monkeypatch, lambda request: _page(PAGE))
    out = ws.websearch("python")
    assert "1. Example A" in out
    assert "https://example.com/a" in out
    assert "First snippet text." in out
    assert "2. Direct Title" in out


def test_websearch_respects_max_results(monkeypatch):
    _patch_client(monkeypatch, lambda request: _page(PAGE))
    out = ws.websearch("python", max_results=1)
    assert "Example A" in out
    assert "Direct Title" not in out


def test_websearch_no_results(monkeypatch):
    _patch_client(monkeypatch, lambda request: _page("<html><body>nothing</body></html>"))
    assert "无搜索结果" in ws.websearch("zzz")


def test_websearch_detects_challenge_page(monkeypatch):
    """被反爬拦截要如实报错，不能伪装成「无搜索结果」（后者会误导关键词排查）。"""
    challenge = (
        "<html><body><h1>Unfortunately, bots use DuckDuckGo too.</h1>"
        '<div class="anomaly-modal">Select all squares containing a duck</div>'
        "</body></html>"
    )
    _patch_client(monkeypatch, lambda request: _page(challenge))
    out = ws.websearch("python")
    assert out.startswith("错误:") and "反爬" in out


def test_websearch_challenge_words_in_results_are_not_blocked(monkeypatch):
    """结果页里出现这些词（正好在搜它们）不算被拦——判定要求「无结果 + 命中标记」。"""
    _patch_client(monkeypatch, lambda request: _page(
        PAGE.replace("Example A", "bots use duckduckgo too")
    ))
    out = ws.websearch("bots use duckduckgo too")
    assert not out.startswith("错误:")
    assert "https://example.com/a" in out


def test_websearch_empty_query():
    assert "错误" in ws.websearch("   ")


# ---------- 请求细节与错误处理 ----------


def test_websearch_posts_form_with_user_agent(monkeypatch):
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode("utf-8")
        seen["ua"] = request.headers.get("User-Agent", "")
        return _page(PAGE)

    _patch_client(monkeypatch, handler)
    ws.websearch("中文 查询")
    assert seen["method"] == "POST"
    assert seen["url"] == ws._ENDPOINT
    assert "q=%E4%B8%AD%E6%96%87" in seen["body"]  # URL 编码的表单体
    assert seen["ua"] == ws._USER_AGENT


def test_websearch_network_error(monkeypatch):
    def handler(request):
        raise httpx2.ConnectError("getaddrinfo failed")

    _patch_client(monkeypatch, handler)
    assert "错误" in ws.websearch("python")


def test_websearch_http_error(monkeypatch):
    _patch_client(monkeypatch, lambda request: httpx2.Response(429, content=b"slow down"))
    out = ws.websearch("python")
    assert "HTTP 429" in out


def test_websearch_uses_environment_proxy(monkeypatch, fake_http_proxy):
    """回归：检索必须走环境代理（.invalid 域名不走代理必然 DNS 失败）。

    端点换成 http：https 目标经 HTTP 代理要先发 CONNECT 建隧道，本地假代理只
    能中继明文请求，故用 http 才能验证"POST 表单确实经代理转发"。
    """
    for name in ("all_proxy", "ALL_PROXY", "https_proxy", "HTTPS_PROXY",
                 "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("http_proxy", fake_http_proxy.url)
    monkeypatch.setenv("HTTP_PROXY", fake_http_proxy.url)
    monkeypatch.setattr(ws, "_ENDPOINT", "http://search-proxy.invalid/html/")

    out = ws.websearch("python")
    assert "代理结果" in out  # 解析到了代理返回的结果页
    forwarded = fake_http_proxy.requests[0]
    assert forwarded["method"] == "POST"
    assert forwarded["path"] == "http://search-proxy.invalid/html/"
    assert "q=python" in forwarded["body"]  # 表单体确实经代理转发
