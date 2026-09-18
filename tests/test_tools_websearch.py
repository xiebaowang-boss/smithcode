"""websearch 工具测试：多后端解析、auto 回退、条数限制、错误处理与代理（不联网）。

HTTP 层用 httpx2 的 MockTransport 打桩（替换工具的客户端工厂）；代理用例用
`fake_http_proxy` 做真实回环验证（GET 查询经代理转发）。

后端选择通过环境变量 SMITHCODE_SEARCH_BACKEND 控制（config 的 env 优先级最高），
测试里 monkeypatch 设置即可，无需碰用户 config.toml。
"""
import base64
import json
import urllib.parse

import httpx2
import pytest

from smithcode.tools import FUNCTIONS, SCHEMAS
from smithcode.tools import websearch as ws


@pytest.fixture(autouse=True)
def _fixed_backend(monkeypatch):
    """默认固定 Brave 且清空 Tavily key，避免用例隐式依赖用户配置。"""
    monkeypatch.setenv("SMITHCODE_SEARCH_BACKEND", "brave")
    monkeypatch.delenv("SMITHCODE_TAVILY_KEY", raising=False)


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


# ---------- 各后端的样例页面 ----------

BRAVE_PAGE = (
    "<html><body>"
    '<div data-type="web"><div class="result-body">'
    '<a href="https://www.python-httpx.org/" class="l1">'
    '<div class="title search-snippet-title line-clamp-1">HTTPX</div></a>'
    '<div class="generic-snippet"><div class="content desktop-default-regular '
    'line-clamp-dynamic">A next-generation HTTP client.</div></div>'
    "</div></div>"
    '<div data-type="web"><div class="result-body">'
    '<a href="https://example.org/direct">'
    '<div class="title search-snippet-title">Direct <b>Title</b></div></a>'
    '<div class="generic-snippet"><div class="content line-clamp-dynamic">Second snippet.</div></div>'
    "</div></div>"
    "</body></html>"
)

BING_PAGE = (
    '<html><body><ol id="b_results">'
    '<li class="b_algo" data-id iid=SERP.1>'
    '<h2><a href="https://www.python-httpx.org/">HTTPX</a></h2>'
    '<div class="b_caption"><p class="b_lineclamp2">A next-generation HTTP client.</p></div>'
    "</li>"
    '<li class="b_algo" data-id iid=SERP.2>'
    '<h2><a href="{ck}">Direct <b>Title</b></a></h2>'
    '<div class="b_caption"><p class="b_lineclamp4">Second snippet.</p></div>'
    "</li>"
    "</ol></body></html>"
)

def _ck_a(url: str) -> str:
    """构造 Bing 的 /ck/a 跳转链接（u 参数为 a1 + url-safe base64）。"""
    token = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"https://www.bing.com/ck/a?!&&p=deadbeef&u=a1{token}&ntb=1"


def test_websearch_registered():
    assert "websearch" in FUNCTIONS
    assert any(s["name"] == "websearch" for s in SCHEMAS)


# ---------- Brave 解析 ----------


def test_parse_brave_extracts_title_url_snippet():
    results = ws._parse_brave(BRAVE_PAGE, ws.MAX_RESULTS)
    assert len(results) == 2
    assert results[0]["title"] == "HTTPX"
    assert results[0]["url"] == "https://www.python-httpx.org/"
    assert results[0]["snippet"] == "A next-generation HTTP client."
    assert results[1]["title"] == "Direct Title"
    assert results[1]["url"] == "https://example.org/direct"


def test_parse_brave_dedupes_same_url():
    assert len(ws._parse_brave(BRAVE_PAGE + BRAVE_PAGE, ws.MAX_RESULTS)) == 2


# ---------- Bing 解析 ----------


def test_parse_bing_extracts_and_restores_ck_a():
    page = BING_PAGE.format(ck=_ck_a("https://example.org/direct"))
    results = ws._parse_bing(page, ws.MAX_RESULTS)
    assert results[0]["title"] == "HTTPX"
    assert results[0]["snippet"] == "A next-generation HTTP client."
    assert results[1]["url"] == "https://example.org/direct"  # /ck/a 已还原


def test_bing_real_url_passes_through_plain_url():
    assert ws._bing_real_url("https://plain.example.com/") == "https://plain.example.com/"


def test_bing_real_url_bad_ck_a_payload_falls_back():
    broken = "https://www.bing.com/ck/a?u=a1!!!not-base64&ntb=1"
    assert ws._bing_real_url(broken) == broken


def test_parse_bing_prefers_lineclamp_snippet():
    page = (
        '<ol id="b_results"><li class="b_algo">'
        '<p class="b_attribution">会被跳过的前置段落</p>'
        '<h2><a href="https://example.com/a">T</a></h2>'
        '<p class="b_lineclamp2">真正的摘要</p>'
        "</li></ol>"
    )
    assert ws._parse_bing(page, ws.MAX_RESULTS)[0]["snippet"] == "真正的摘要"


# ---------- Tavily（JSON API，需 key）----------


def _tavily_response(items):
    return httpx2.Response(200, json={"results": items})


def test_tavily_sends_bearer_and_parses_json(monkeypatch):
    monkeypatch.setenv("SMITHCODE_SEARCH_BACKEND", "tavily")
    monkeypatch.setenv("SMITHCODE_TAVILY_KEY", "tvly-test-key")
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["auth"] = request.headers.get("Authorization", "")
        seen["body"] = json.loads(request.content)
        return _tavily_response([
            {"title": "HTTPX", "url": "https://www.python-httpx.org/",
             "content": "A next-generation HTTP client."},
        ])

    _patch_client(monkeypatch, handler)
    out = ws.websearch("httpx", max_results=1)
    assert "1. HTTPX" in out and "https://www.python-httpx.org/" in out
    assert seen["method"] == "POST"
    assert seen["url"] == ws._TAVILY
    assert seen["auth"] == "Bearer tvly-test-key"
    assert seen["body"]["query"] == "httpx" and seen["body"]["max_results"] == 1


def test_tavily_key_never_leaks_into_output(monkeypatch):
    """key 绝不能出现在工具返回里（会写进会话转录）。"""
    monkeypatch.setenv("SMITHCODE_SEARCH_BACKEND", "tavily")
    monkeypatch.setenv("SMITHCODE_TAVILY_KEY", "tvly-super-secret")
    _patch_client(monkeypatch, lambda request: _tavily_response([
        {"title": "T", "url": "https://example.com/", "content": "c"},
    ]))
    assert "tvly-super-secret" not in ws.websearch("x")


def test_tavily_missing_key_reports_actionable_error(monkeypatch):
    monkeypatch.setenv("SMITHCODE_SEARCH_BACKEND", "tavily")
    _patch_client(monkeypatch, lambda request: _tavily_response([]))
    out = ws.websearch("x")
    assert out.startswith("错误") and "Tavily" in out and "SMITHCODE_TAVILY_KEY" in out


def test_tavily_bad_key_reports_error(monkeypatch):
    monkeypatch.setenv("SMITHCODE_SEARCH_BACKEND", "tavily")
    monkeypatch.setenv("SMITHCODE_TAVILY_KEY", "bad")
    _patch_client(monkeypatch, lambda request: httpx2.Response(401, json={"error": "unauthorized"}))
    out = ws.websearch("x")
    assert out.startswith("错误") and "Tavily" in out


def test_auto_skips_tavily_without_key(monkeypatch):
    """auto 且未配 Tavily key：跳过它（不算失败），直接走下一个后端。"""
    monkeypatch.setenv("SMITHCODE_SEARCH_BACKEND", "auto")
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return _page(BRAVE_PAGE)

    _patch_client(monkeypatch, handler)
    out = ws.websearch("python")
    assert "HTTPX" in out
    assert not any("tavily" in u for u in calls)  # 没配 key，tavily 不该被请求


def test_auto_prefers_tavily_when_key_present(monkeypatch):
    """auto 且配了 key：Tavily 优先（第一个尝试且命中即停）。"""
    monkeypatch.setenv("SMITHCODE_SEARCH_BACKEND", "auto")
    monkeypatch.setenv("SMITHCODE_TAVILY_KEY", "tvly-test")
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "tavily" in str(request.url):
            return _tavily_response([
                {"title": "Tavily 结果", "url": "https://example.com/t", "content": "c"},
            ])
        return _page(BRAVE_PAGE)

    _patch_client(monkeypatch, handler)
    out = ws.websearch("python")
    assert "Tavily 结果" in out
    assert len(calls) == 1  # 首选命中即停，不再试 brave


# ---------- 统一入口与格式化 ----------


def test_websearch_formats_numbered_results(monkeypatch):
    _patch_client(monkeypatch, lambda request: _page(BRAVE_PAGE))
    out = ws.websearch("python")
    assert "1. HTTPX" in out
    assert "https://www.python-httpx.org/" in out
    assert "A next-generation HTTP client." in out
    assert "2. Direct Title" in out


def test_websearch_respects_max_results(monkeypatch):
    _patch_client(monkeypatch, lambda request: _page(BRAVE_PAGE))
    out = ws.websearch("python", max_results=1)
    assert "HTTPX" in out
    assert "Direct Title" not in out


def test_websearch_empty_query():
    assert "错误" in ws.websearch("   ")


# ---------- auto 回退 ----------


def test_auto_falls_back_when_first_backend_errors(monkeypatch):
    """auto：首选 Brave 被限流时自动改用 Bing，用户无感。"""
    monkeypatch.setenv("SMITHCODE_SEARCH_BACKEND", "auto")
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "brave.com" in str(request.url):
            return httpx2.Response(429, content=b"slow down")
        return _page(BING_PAGE.format(ck=_ck_a("https://example.org/direct")))

    _patch_client(monkeypatch, handler)
    out = ws.websearch("python", max_results=2)
    assert not out.startswith("错误")
    assert "HTTPX" in out
    assert any("brave.com" in u for u in calls) and any("bing.com" in u for u in calls)


def test_auto_falls_back_when_first_backend_has_no_results(monkeypatch):
    """auto：首选后端解析为空（被反爬 / 改版）时继续试下一个。"""
    monkeypatch.setenv("SMITHCODE_SEARCH_BACKEND", "auto")

    def handler(request):
        if "brave.com" in str(request.url):
            return _page("<html><body>nothing here</body></html>")
        return _page(BING_PAGE.format(ck=_ck_a("https://example.org/direct")))

    _patch_client(monkeypatch, handler)
    out = ws.websearch("python", max_results=1)
    assert "HTTPX" in out


def test_auto_reports_all_backends_failed(monkeypatch):
    """auto：所有后端都失败时，错误里带上各后端的原因。"""
    monkeypatch.setenv("SMITHCODE_SEARCH_BACKEND", "auto")
    _patch_client(monkeypatch, lambda request: httpx2.Response(429, content=b"slow down"))
    out = ws.websearch("python")
    assert out.startswith("错误")
    assert "Brave" in out and "Bing" in out


def test_fixed_backend_error_suggests_auto(monkeypatch):
    """固定后端失败时，提示可改用 auto（不静默回退，尊重用户选择）。"""
    _patch_client(monkeypatch, lambda request: httpx2.Response(429, content=b"slow down"))
    out = ws.websearch("python")
    assert out.startswith("错误") and "brave" in out and "auto" in out


def test_websearch_bing_challenge_page_is_reported(monkeypatch):
    """Bing 反爬页（无结果容器）要如实报错，不能伪装成「无搜索结果」。"""
    monkeypatch.setenv("SMITHCODE_SEARCH_BACKEND", "bing")
    _patch_client(monkeypatch, lambda request: _page(
        "<html><body><h1>请验证你是人类</h1></body></html>"
    ))
    out = ws.websearch("python")
    assert out.startswith("错误") and "反爬" in out


def test_websearch_no_results_when_page_is_normal(monkeypatch):
    """页面正常但确实没有结果 → 「（无搜索结果）」，而非反爬报错。"""
    _patch_client(monkeypatch, lambda request: _page("<html><body>nothing</body></html>"))
    assert "无搜索结果" in ws.websearch("zzz")


# ---------- 请求细节与错误处理 ----------


def test_websearch_gets_with_query_and_user_agent(monkeypatch):
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["ua"] = request.headers.get("User-Agent", "")
        return _page(BRAVE_PAGE)

    _patch_client(monkeypatch, handler)
    ws.websearch("中文 查询")
    assert seen["method"] == "GET"
    assert "q=%E4%B8%AD%E6%96%87" in seen["url"]
    assert seen["ua"] == ws._USER_AGENT


def test_websearch_network_error(monkeypatch):
    def handler(request):
        raise httpx2.ConnectError("getaddrinfo failed")

    _patch_client(monkeypatch, handler)
    assert "错误" in ws.websearch("python")


def test_websearch_http_error(monkeypatch):
    _patch_client(monkeypatch, lambda request: httpx2.Response(500, content=b"boom"))
    out = ws.websearch("python")
    assert "HTTP 500" in out


def test_websearch_uses_environment_proxy(monkeypatch, fake_http_proxy):
    """回归：检索必须走环境代理（.invalid 域名不走代理必然 DNS 失败）。"""
    for name in ("all_proxy", "ALL_PROXY", "https_proxy", "HTTPS_PROXY",
                 "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("http_proxy", fake_http_proxy.url)
    monkeypatch.setenv("HTTP_PROXY", fake_http_proxy.url)
    # 端点换成 http：https 目标经 HTTP 代理要先发 CONNECT 建隧道，本地假代理只能
    # 中继明文请求；路径含 search 以便假代理解析出结果页
    monkeypatch.setattr(ws, "_BRAVE", "http://search-proxy.invalid/search")

    out = ws.websearch("python")
    assert "代理结果" in out
    forwarded = fake_http_proxy.requests[0]
    parsed = urllib.parse.urlparse(forwarded["path"])
    assert parsed.path == "/search"
    assert parsed.query == "q=python"


# ---------- 配置解析 ----------


def test_load_search_backend_env_and_default(monkeypatch, tmp_path):
    from smithcode import config

    monkeypatch.setenv("SMITHCODE_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.delenv("SMITHCODE_SEARCH_BACKEND", raising=False)
    assert config.load_search_backend() == "auto"  # 无配置 → 默认

    monkeypatch.setenv("SMITHCODE_SEARCH_BACKEND", "bing")
    assert config.load_search_backend() == "bing"

    monkeypatch.setenv("SMITHCODE_SEARCH_BACKEND", "BOGUS")
    assert config.load_search_backend() == "auto"  # 非法值 → 降级


def test_load_search_backend_from_toml(monkeypatch, tmp_path):
    from smithcode import config

    monkeypatch.delenv("SMITHCODE_SEARCH_BACKEND", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text("[search]\nbackend = \"brave\"\n", encoding="utf-8")
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    assert config.load_search_backend() == "brave"

    (home / "config.toml").write_text("[search]\nbackend = \"nope\"\n", encoding="utf-8")
    assert config.load_search_backend() == "auto"


def test_load_tavily_key_priority(monkeypatch, tmp_path):
    """Tavily key 优先级：env > [search].tavily_key > credentials.json > 空。"""
    from smithcode import config

    monkeypatch.delenv("SMITHCODE_TAVILY_KEY", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    assert config.load_tavily_key() == ""  # 都没配

    # credentials.json 兜底
    (home / "credentials.json").write_text(
        json.dumps({"search": {"tavily_key": "from-cred"}}), encoding="utf-8"
    )
    assert config.load_tavily_key() == "from-cred"

    # config.toml 覆盖凭据文件
    (home / "config.toml").write_text(
        '[search]\ntavily_key = "from-toml"\n', encoding="utf-8"
    )
    assert config.load_tavily_key() == "from-toml"

    # 环境变量优先级最高
    monkeypatch.setenv("SMITHCODE_TAVILY_KEY", "from-env")
    assert config.load_tavily_key() == "from-env"


def test_write_credentials_preserves_extra_fields(tmp_path):
    """setup 写 LLM key 时不能冲掉已有其他字段（同一文件不同字段）。"""
    from smithcode import wizard

    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"search": {"tavily_key": "tavily-key"}}), encoding="utf-8")
    wizard._write_credentials(path, "llm-key")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["key"] == "llm-key"
    assert data["search"] == {"tavily_key": "tavily-key"}
