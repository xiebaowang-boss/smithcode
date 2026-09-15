"""webfetch 工具测试：协议白名单、HTML 转文本、截断、错误处理与代理（不联网）。

HTTP 层用 httpx2 的 MockTransport 打桩（替换工具的客户端工厂），可断言请求
细节；代理用例另用 `fake_http_proxy` 做真实回环验证（见 conftest）。
"""
import httpx2
import pytest

from smithcode.tools import web


@pytest.fixture(autouse=True)
def _stub_dns(monkeypatch):
    """测试不查真实 DNS：域名默认解析为公网地址（SSRF 用例自行覆盖）。"""
    from smithcode.utils import http as http_util

    monkeypatch.setattr(http_util, "resolve_host", lambda host: ["93.184.216.34"])


@pytest.fixture(autouse=True)
def _allow_private_urls_off(monkeypatch):
    """默认按出厂配置（拦截内网）跑：用环境变量固定，不替换被测函数本身。"""
    monkeypatch.setenv("SMITHCODE_ALLOW_PRIVATE_URLS", "0")


def _patch_client(monkeypatch, handler):
    """把工具的客户端工厂换成 MockTransport 版：签名与真实工厂一致。"""

    def factory(*, timeout, headers=None, follow_redirects=True):
        return httpx2.Client(
            transport=httpx2.MockTransport(handler),
            timeout=timeout,
            headers=headers,
            follow_redirects=follow_redirects,
        )

    monkeypatch.setattr(web, "http_client", factory)


def _write_home_config(monkeypatch, tmp_path, text):
    """把内容写进隔离的 SMITHCODE_HOME 下的 config.toml。"""
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(text, encoding="utf-8")
    monkeypatch.setenv("SMITHCODE_HOME", str(home))


def _html(body: str, charset: str = "utf-8") -> httpx2.Response:
    return httpx2.Response(
        200,
        headers={"Content-Type": f"text/html; charset={charset}"},
        content=body.encode(charset),
    )


def test_webfetch_rejects_non_http_scheme():
    assert "仅支持" in web.webfetch("file:///C:/Windows/win.ini")
    assert "仅支持" in web.webfetch("ftp://example.com/a")


def test_webfetch_strips_html(monkeypatch):
    _patch_client(monkeypatch, lambda request: _html(
        "<html><head><script>var x=1;</script></head>"
        "<body><h1>标题</h1><p>Hello <b>world</b></p></body></html>"
    ))
    out = web.webfetch("https://example.com/x")
    assert "Hello" in out and "world" in out and "标题" in out
    assert "<" not in out
    assert "var x=1" not in out


def test_webfetch_truncates(monkeypatch):
    _patch_client(monkeypatch, lambda request: _html("<p>" + "字" * 5000 + "</p>"))
    out = web.webfetch("https://example.com/x", max_chars=700)
    assert len(out) == 700
    # 过小的值被抬到下限 500
    out2 = web.webfetch("https://example.com/x", max_chars=10)
    assert len(out2) == 500


def test_webfetch_honors_response_charset(monkeypatch):
    """非 UTF-8 页面按响应头声明的 charset 解码（此前同样读 Content-Type）。"""
    gbk = "<html><body><p>中文编码测试</p></body></html>"
    _patch_client(monkeypatch, lambda request: _html(gbk, charset="gbk"))
    assert "中文编码测试" in web.webfetch("https://example.com/gbk")


def test_webfetch_limits_response_bytes(monkeypatch):
    """超大响应体按 MAX_FETCH_BYTES 截断读取，不会整份读进内存。"""
    monkeypatch.setattr(web, "MAX_FETCH_BYTES", 1000)
    _patch_client(monkeypatch, lambda request: _html("<p>" + "a" * 50_000 + "</p>"))
    out = web.webfetch("https://example.com/big")
    assert out and len(out) <= 1000


def test_webfetch_sends_user_agent(monkeypatch):
    seen = {}

    def handler(request):
        seen["ua"] = request.headers.get("User-Agent", "")
        return _html("<p>x</p>")

    _patch_client(monkeypatch, handler)
    web.webfetch("https://example.com/x")
    assert seen["ua"] == web._USER_AGENT


def test_webfetch_http_error(monkeypatch):
    _patch_client(monkeypatch, lambda request: httpx2.Response(404, content=b"gone"))
    out = web.webfetch("https://example.com/missing")
    assert "404" in out and "Not Found" in out


def test_webfetch_network_error(monkeypatch):
    def handler(request):
        raise httpx2.ConnectError("getaddrinfo failed")

    _patch_client(monkeypatch, handler)
    out = web.webfetch("https://nonexistent.invalid/x")
    assert "错误" in out and "抓取失败" in out


def test_webfetch_rejects_non_http_redirect(monkeypatch):
    """重定向到非 http/https 协议时中止（Location 头逐跳校验）。"""
    _patch_client(
        monkeypatch,
        lambda request: httpx2.Response(
            302, headers={"Location": "ftp://evil/x"}, request=request
        ),
    )
    assert "重定向" in web.webfetch("https://example.com/x")


def test_webfetch_stops_after_too_many_redirects(monkeypatch):
    _patch_client(
        monkeypatch,
        lambda request: httpx2.Response(
            302, headers={"Location": "https://example.com/loop"}, request=request
        ),
    )
    out = web.webfetch("https://example.com/start")
    assert "重定向次数过多" in out


# ---------- SSRF 防护 ----------


def test_webfetch_blocks_loopback_and_cloud_metadata(monkeypatch):
    """本机与云元数据地址一律拒访（webfetch 默认免确认放行，必须默认安全）。"""
    _patch_client(monkeypatch, lambda request: _html("<p>不该被请求到</p>"))
    out = web.webfetch("http://127.0.0.1:8080/admin")
    assert out.startswith("错误:") and "内网" in out and "allow_private_urls" in out

    out2 = web.webfetch("http://169.254.169.254/latest/meta-data/")
    assert "169.254.169.254" in out2 and "内网" in out2


def test_webfetch_blocks_hostname_resolving_to_private(monkeypatch):
    """公网域名指向内网地址同样拦截（按解析结果判定，不只看字面量）。"""
    from smithcode.utils import http as http_util

    monkeypatch.setattr(http_util, "resolve_host", lambda host: ["10.1.2.3"])
    _patch_client(monkeypatch, lambda request: _html("<p>x</p>"))
    out = web.webfetch("http://internal.example.com/")
    assert "内网" in out and "10.1.2.3" in out


def test_webfetch_blocks_redirect_into_private(monkeypatch):
    """经典绕过手法：公网地址 302 到内网——每一跳都要重新校验。"""
    def handler(request):
        return httpx2.Response(
            302, headers={"Location": "http://169.254.169.254/"}, request=request
        )

    _patch_client(monkeypatch, handler)
    out = web.webfetch("https://public.example.com/")
    assert "内网" in out and "169.254.169.254" in out


def test_webfetch_allows_private_when_env_enables(monkeypatch):
    """显式打开开关后允许访问内网（本地开发服务器场景）。"""
    from smithcode import config

    monkeypatch.setattr(config, "load_allow_private_urls", lambda: True)
    _patch_client(monkeypatch, lambda request: _html("<p>本地服务</p>"))
    assert "本地服务" in web.webfetch("http://127.0.0.1:8000/")


def test_allow_private_urls_config_loader(monkeypatch, tmp_path):
    """开关的配置优先级：env > config.toml > 默认 False；非法值降级为默认。"""
    from smithcode import config

    monkeypatch.delenv("SMITHCODE_ALLOW_PRIVATE_URLS", raising=False)
    _write_home_config(monkeypatch, tmp_path, "allow_private_urls = true\n")
    assert config.load_allow_private_urls() is True

    monkeypatch.setenv("SMITHCODE_ALLOW_PRIVATE_URLS", "0")
    assert config.load_allow_private_urls() is False

    monkeypatch.setenv("SMITHCODE_ALLOW_PRIVATE_URLS", "maybe")
    assert config.load_allow_private_urls() is False  # 非法值 → 默认 False

    monkeypatch.delenv("SMITHCODE_ALLOW_PRIVATE_URLS", raising=False)
    (tmp_path / "home" / "config.toml").write_text("allow_private_urls = 'yes'\n", encoding="utf-8")
    assert config.load_allow_private_urls() is False  # 非布尔值 → 默认 False


# ---------- 结构化转换 ----------


def test_webfetch_preserves_document_structure(monkeypatch):
    """抓取结果保留标题 / 链接 / 代码块骨架（读文档时这些就是重点）。"""
    _patch_client(monkeypatch, lambda request: _html(
        "<h1>文档标题</h1>"
        '<p>见 <a href="https://ex.com/api">API 说明</a></p>'
        '<pre><code class="language-python">print(1)</code></pre>'
    ))
    out = web.webfetch("https://example.com/doc")
    assert "# 文档标题" in out
    assert "[API 说明](https://ex.com/api)" in out
    assert "```python" in out and "print(1)" in out


def test_webfetch_follows_redirects(monkeypatch):
    def handler(request):
        if request.url.path == "/old":
            return httpx2.Response(302, headers={"Location": "https://example.com/new"})
        return _html("<p>新地址内容</p>")

    _patch_client(monkeypatch, handler)
    assert "新地址内容" in web.webfetch("https://example.com/old")


def test_webfetch_batch_fetches_all_urls(monkeypatch):
    _patch_client(
        monkeypatch,
        lambda request: _html(f"<p>内容-{request.url}</p>"),
    )
    out = web.webfetch(["https://a.example.com/", "https://b.example.com/"])
    assert "内容-https://a.example.com/" in out
    assert "内容-https://b.example.com/" in out
    # 每个地址的结果有分隔头，可对应回原地址
    assert "===== [1] https://a.example.com/ =====" in out
    assert "===== [2] https://b.example.com/ =====" in out


def test_webfetch_batch_keeps_single_url_result_plain(monkeypatch):
    """单地址不套批量分隔头，直接返回正文（多地址才分段标注）。"""
    _patch_client(monkeypatch, lambda request: _html("<p>单页</p>"))
    out = web.webfetch("https://example.com/only")
    assert "单页" in out
    assert "=====" not in out


def test_webfetch_batch_rejects_too_many_urls():
    urls = [f"https://example.com/{i}" for i in range(web.MAX_URLS + 1)]
    out = web.webfetch(urls)
    assert "最多并行抓取" in out and "多次调用" in out


def test_webfetch_batch_empty_list():
    assert "url 列表为空" in web.webfetch([])


def test_webfetch_batch_partial_failure(monkeypatch):
    def handler(request):
        if "bad" in str(request.url):
            raise httpx2.ConnectError("getaddrinfo failed")
        return _html("<p>正常</p>")

    _patch_client(monkeypatch, handler)
    out = web.webfetch(["https://bad.invalid/", "https://good.example.com/"])
    # 失败的地址不拖垮整批：正常结果与错误信息都在，且各归各的分隔段
    assert "正常" in out
    assert "抓取失败" in out


def test_webfetch_uses_environment_proxy(monkeypatch, fake_http_proxy):
    """回归：抓取必须走环境代理（.invalid 域名不走代理必然 DNS 失败）。"""
    for name in ("all_proxy", "ALL_PROXY", "https_proxy", "HTTPS_PROXY",
                 "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("http_proxy", fake_http_proxy.url)
    monkeypatch.setenv("HTTP_PROXY", fake_http_proxy.url)

    out = web.webfetch("http://proxy-only.invalid/page")
    assert "来自代理" in out and "proxy-only.invalid/page" in out
    assert fake_http_proxy.requests[0]["path"] == "http://proxy-only.invalid/page"


def test_webfetch_proxy_error_is_reported(monkeypatch):
    """代理不可达时报「抓取失败」而不是裸异常（错误文案对模型可操作）。"""
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")  # 保留端口，必然连不上
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    out = web.webfetch("http://proxy-dead.invalid/x")
    assert out.startswith("错误:") and "抓取失败" in out
