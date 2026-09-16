"""websearch 工具：网页检索，支持多个免 key 后端并可按网络环境切换。

与 webfetch 的分工：websearch 只给候选结果的标题 / 链接 / 摘要，需要正文时
再对结果链接调用 webfetch。全部后端免 API key，HTTP 层与 webfetch 共用
`utils/http.py` 的客户端工厂（httpx2 + 环境代理，含 socks5），代理语义一致。

**为什么要多后端**：不同网络下各引擎的可达性与结果质量差异极大——实测同一条
查询，无代理时 DuckDuckGo 域名不可达，走代理时 Bing 会对约 10% 的查询返回完全
无关的结果、而 Brave 稳定。单一后端在换网络后就可能整体失效，故支持
`auto`（按 brave → bing → ddg 依次尝试，命中即用）与固定后端，见
`config.load_search_backend()`。

各后端的页面结构不同，各自一个解析函数（`_parse_brave` / `_parse_bing` /
`_parse_ddg`），公共的取标题、摘要、真实链接逻辑各自实现；`auto` 模式下
某个后端解析为空（被反爬或改版）就自动试下一个。
"""
from __future__ import annotations

import base64
import binascii
import html
import re
import urllib.parse

import httpx2

from .. import config
from ..utils.http import client as http_client
from ..utils.http import read_limited
from .base import register
from .web import _USER_AGENT, MAX_FETCH_BYTES

SEARCH_TIMEOUT = 20  # 单次检索超时（秒）
DEFAULT_RESULTS = 5  # 默认返回条数
MAX_RESULTS = 10  # 单次最多返回条数
MAX_SNIPPET_LEN = 300  # 单条摘要展示上限

_TAG_RE = re.compile(r"<[^>]+>")
_ATTR_RE = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')

# auto 模式的尝试顺序：Brave（结果质量最好）→ Bing（可达性最广，但有软降级）
# → DuckDuckGo（反爬最严）。
_AUTO_ORDER = ("brave", "bing", "ddg")


def _clean(text: str) -> str:
    """去标签 + 反转义 + 折叠空白（标题 / 摘要的内联文本清理）。"""
    text = _TAG_RE.sub("", text or "")
    text = html.unescape(text)
    return " ".join(text.split())


def _get(url: str) -> str:
    """GET 抓取页面文本（各后端共用；POST 表单的 DDG 单独实现）。"""
    with http_client(timeout=SEARCH_TIMEOUT,
                     headers={"User-Agent": _USER_AGENT}) as client, \
            client.stream("GET", url) as resp:
        resp.raise_for_status()
        charset = resp.charset_encoding or "utf-8"
        raw = read_limited(resp, MAX_FETCH_BYTES)
    return raw.decode(charset, errors="replace")


def _post(url: str, data: dict) -> str:
    """POST 表单抓取页面文本（DuckDuckGo HTML 版）。"""
    with http_client(timeout=SEARCH_TIMEOUT,
                     headers={"User-Agent": _USER_AGENT}) as client, \
            client.stream("POST", url, data=data) as resp:
        resp.raise_for_status()
        charset = resp.charset_encoding or "utf-8"
        raw = read_limited(resp, MAX_FETCH_BYTES)
    return raw.decode(charset, errors="replace")


# ---------- Brave ----------

_BRAVE = "https://search.brave.com/search"
# 结果块：data-type="web" 分隔；标题在 <div class="title search-snippet-title" ...>；
# 摘要在 <div class="content ... line-clamp-dynamic">；主链接是块内首个外链锚点。
_BRAVE_BLOCK_SEP = 'data-type="web"'
_BRAVE_TITLE_RE = re.compile(
    r'<div class="title search-snippet-title[^"]*"[^>]*>(.*?)</div>', re.IGNORECASE | re.DOTALL
)
_BRAVE_SNIPPET_RE = re.compile(
    r'<div class="content[^"]*line-clamp-dynamic[^"]*"[^>]*>(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
_BRAVE_HREF_RE = re.compile(r'<a[^>]*href="(https?://[^"]+)"', re.IGNORECASE)


def _parse_brave(page: str, limit: int) -> list:
    results: list = []
    seen: set = set()
    for block in page.split(_BRAVE_BLOCK_SEP)[1:]:
        hrefs = [h for h in _BRAVE_HREF_RE.findall(block)
                 if "brave.com" not in h and "imgs.search" not in h]
        if not hrefs:
            continue
        url = html.unescape(hrefs[0])
        if url in seen:
            continue
        seen.add(url)
        title_m = _BRAVE_TITLE_RE.search(block)
        snippet_m = _BRAVE_SNIPPET_RE.search(block)
        results.append({
            "title": _clean(title_m.group(1)) if title_m else "",
            "url": url,
            "snippet": _clean(snippet_m.group(1))[:MAX_SNIPPET_LEN] if snippet_m else "",
        })
        if len(results) >= limit:
            break
    return results


def _fetch_brave(query: str) -> str:
    return _get(f"{_BRAVE}?{urllib.parse.urlencode({'q': query})}")


# ---------- Bing ----------

_BING = "https://www.bing.com/search"
_BING_BLOCK_SEP = re.compile(r'<li class="b_algo"', re.IGNORECASE)
_BING_H2_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.IGNORECASE | re.DOTALL)
_BING_HREF_RE = re.compile(r'<a\b[^>]*?href="([^"]*)"', re.IGNORECASE)
_BING_SNIPPET_RE = re.compile(
    r'<p\b[^>]*class="[^"]*b_lineclamp[^"]*"[^>]*>(.*?)</p>', re.IGNORECASE | re.DOTALL
)
_BING_P_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
# 正常结果页一定含结果容器 id="b_results"；没有它又没解析出结果，说明拿到的是
# 反爬 / 验证页（而非"真的没有结果"），此时如实报错而非伪装成"无结果"。
_BING_RESULTS_CONTAINER = 'id="b_results"'


def _decode_ck_a(href: str) -> str:
    """还原 Bing 的点击跳转链接（`/ck/a?...&u=a1<base64>`) 为真实地址。

    `u` 参数形如 `a1` + URL-safe base64（无填充）编码的真实地址；解析失败时
    返回原地址（宁可让模型看到跳转链接，也不要抛错中断检索）。
    """
    try:
        query = urllib.parse.urlparse(href).query
        u = urllib.parse.parse_qs(query).get("u", [""])[0]
        if not u.startswith("a1"):
            return href
        padded = u[2:] + "=" * (-len(u[2:]) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8", "replace")
        return decoded if decoded.startswith("http") else href
    except (ValueError, binascii.Error, UnicodeError):
        return href


def _bing_real_url(href: str) -> str:
    href = html.unescape(href or "").strip()
    if href.startswith("//"):
        href = "https:" + href
    elif href.startswith("/"):
        href = "https://www.bing.com" + href
    if "bing.com/ck/a" in href:
        href = _decode_ck_a(href)
    return href


def _parse_bing(page: str, limit: int) -> list:
    results: list = []
    seen: set = set()
    for block in _BING_BLOCK_SEP.split(page)[1:]:
        h2 = _BING_H2_RE.search(block)
        if not h2:
            continue
        href = _BING_HREF_RE.search(h2.group(1))
        if not href:
            continue
        url = _bing_real_url(href.group(1))
        if not url or url in seen:
            continue
        seen.add(url)
        snippet = _BING_SNIPPET_RE.search(block) or _BING_P_RE.search(block)
        results.append({
            "title": _clean(h2.group(1)),
            "url": url,
            "snippet": _clean(snippet.group(1))[:MAX_SNIPPET_LEN] if snippet else "",
        })
        if len(results) >= limit:
            break
    return results


def _fetch_bing(query: str) -> str:
    return _get(f"{_BING}?{urllib.parse.urlencode({'q': query})}")


# ---------- DuckDuckGo（HTML 版）----------

_DDG = "https://html.duckduckgo.com/html/"
_DDG_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_DDG_CHALLENGE_MARKERS = ("bots use duckduckgo too", "anomaly-modal", "select all squares")


def _ddg_real_url(href: str) -> str:
    """还原 DuckDuckGo 跳转链接为真实地址；普通地址原样返回。"""
    href = html.unescape(href or "").strip()
    if href.startswith("//"):
        href = "https:" + href
    elif href.startswith("/"):
        href = "https://duckduckgo.com" + href
    parsed = urllib.parse.urlparse(href)
    if parsed.path.startswith("/l/"):
        uddg = urllib.parse.parse_qs(parsed.query).get("uddg")
        if uddg:
            return uddg[0]
    return href


def _parse_ddg(page: str, limit: int) -> list:
    """按文档顺序遍历锚点：result__a 开一条结果，紧随其后的 result__snippet 补摘要。"""
    results: list = []
    seen: set = set()
    for match in _DDG_ANCHOR_RE.finditer(page):
        attrs = dict(_ATTR_RE.findall(match.group(1)))
        classes = attrs.get("class", "")
        if "result__a" in classes:
            url = _ddg_real_url(attrs.get("href", ""))
            if not url or url in seen:
                continue
            seen.add(url)
            results.append({"title": _clean(match.group(2)), "url": url, "snippet": ""})
        elif "result__snippet" in classes and results and not results[-1]["snippet"]:
            results[-1]["snippet"] = _clean(match.group(2))[:MAX_SNIPPET_LEN]
        if len(results) >= limit:
            break
    return results


def _fetch_ddg(query: str) -> str:
    return _post(_DDG, {"q": query})


# ---------- 后端注册表与统一入口 ----------

_BACKENDS = {
    "brave": (_fetch_brave, _parse_brave),
    "bing": (_fetch_bing, _parse_bing),
    "ddg": (_fetch_ddg, _parse_ddg),
}

_DISPLAY_NAMES = {"brave": "Brave", "bing": "Bing", "ddg": "DuckDuckGo"}


def _try_backend(name: str, query: str, limit: int) -> tuple[list, str | None]:
    """跑单个后端，返回 (结果, 错误说明)。

    错误说明非 None 表示这次尝试没拿到可用结果（网络失败 / 被反爬 / 解析为空），
    auto 模式下会据此继续试下一个后端。
    """
    fetch, parse = _BACKENDS[name]
    try:
        page = fetch(query)
    except httpx2.HTTPStatusError as e:
        code = e.response.status_code
        if code in (202, 401, 403, 429):
            return [], f"{_DISPLAY_NAMES[name]} 反爬拦截（HTTP {code}）"
        return [], f"{_DISPLAY_NAMES[name]} 返回 HTTP {code}"
    except (httpx2.RequestError, httpx2.InvalidURL, OSError) as e:
        return [], f"{_DISPLAY_NAMES[name]} 请求失败: {e}"
    results = parse(page, limit)
    if results:
        return results, None
    # 解析为空：区分"真无结果"与"被反爬 / 改版"
    low = page.lower()
    if name == "bing" and _BING_RESULTS_CONTAINER not in low:
        return [], "Bing 返回反爬验证页"
    if name == "ddg" and any(m in low for m in _DDG_CHALLENGE_MARKERS):
        return [], "DuckDuckGo 返回反爬验证页"
    return [], None  # 页面正常、只是没有结果


def _order(backend: str) -> tuple:
    """解析配置为实际尝试的后端序列。"""
    if backend == "auto":
        return _AUTO_ORDER
    return (backend,)


def _describe(args: dict) -> str:
    query = " ".join(str(args.get("query", "?")).split())
    return f"search {query[:60]}"


@register(
    {
        "name": "websearch",
        "describe": _describe,
        "description": "检索网页，返回若干结果的标题、链接与摘要。"
        "用于查找公开资料、最新信息、报错方案等；需要网页正文时再对结果链接调用 webfetch。"
        "默认按可用后端自动选择；max_results 控制返回条数"
        f"（默认 {DEFAULT_RESULTS}，上限 {MAX_RESULTS}）。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词"},
                "max_results": {
                    "type": "integer",
                    "description": f"返回条数，默认 {DEFAULT_RESULTS}，上限 {MAX_RESULTS}",
                },
            },
            "required": ["query"],
        },
    }
)
def websearch(query: str, max_results: int | None = None) -> str:
    query = str(query or "").strip()
    if not query:
        return "错误: query 不能为空"
    limit = max(
        1, min(int(max_results) if max_results else DEFAULT_RESULTS, MAX_RESULTS)
    )
    backend = config.load_search_backend()
    errors: list[str] = []
    results: list = []
    for name in _order(backend):
        results, err = _try_backend(name, query, limit)
        if results:
            break
        if err:
            errors.append(err)
    if not results:
        if backend != "auto" and errors:
            return (
                f"错误: {'；'.join(errors)}。"
                "可改用其他后端：config.toml 的 [search].backend 设为 auto/brave/bing/ddg"
            )
        if errors:
            return (
                f"错误: 所有检索后端均不可用（{'；'.join(errors)}）。"
                "检查网络 / 代理，或改用 webfetch 直接抓取已知网址"
            )
        return "（无搜索结果）"
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title'] or r['url']}")
        lines.append(f"   {r['url']}")
        if r["snippet"]:
            lines.append(f"   {r['snippet']}")
    return "\n".join(lines)
