"""websearch 工具：网页检索，支持多个后端并可按网络环境切换。

与 webfetch 的分工：websearch 只给候选结果的标题 / 链接 / 摘要，需要正文时
再对结果链接调用 webfetch。多数后端免 API key；所有后端的 HTTP 层与 webfetch 共用
`utils/http.py` 的客户端工厂（httpx2 + 环境代理，含 socks5），代理语义一致。

**为什么要多后端**：不同网络下各引擎的可达性与结果质量差异极大——实测同一条
查询，无代理时部分引擎域名不可达，走代理时 Bing 会对约 10% 的查询返回完全
无关的结果、而 Brave 稳定。单一后端在换网络后就可能整体失效，故支持
`auto`（按 tavily → brave → bing 依次尝试，命中即用）与固定后端，见
`config.load_search_backend()`。

**Tavily** 是唯一需要 key 的后端（免费 1000 次/月），返回结构化 JSON 而非
HTML，结果质量最高且不受反爬影响；未配 key 时在 auto 模式下直接跳过（不算
失败）。其余两个是抓 HTML 页面，各后端结构不同、各自一个解析函数。
"""
from __future__ import annotations

import base64
import binascii
import html
import json
import re
import urllib.parse

import httpx2

from .. import config
from ..utils.http import client as http_client
from ..utils.http import read_limited
from .base import register
from .webfetch import _USER_AGENT, MAX_FETCH_BYTES

SEARCH_TIMEOUT = 20  # 单次检索超时（秒）
DEFAULT_RESULTS = 5  # 默认返回条数
MAX_RESULTS = 10  # 单次最多返回条数
MAX_SNIPPET_LEN = 300  # 单条摘要展示上限

_TAG_RE = re.compile(r"<[^>]+>")

# auto 模式的尝试顺序：Tavily（结构化、质量最好，需 key）→ Brave（免 key 里质量
# 最好，但有速率限制）→ Bing（可达性最广，但对部分查询有软降级）。未配 Tavily
# key 时该项自动跳过。
_AUTO_ORDER = ("tavily", "brave", "bing")


def _clean(text: str) -> str:
    """去标签 + 反转义 + 折叠空白（标题 / 摘要的内联文本清理）。"""
    text = _TAG_RE.sub("", text or "")
    text = html.unescape(text)
    return " ".join(text.split())


def _get(url: str) -> str:
    """GET 抓取页面文本（各后端共用）。"""
    with http_client(timeout=SEARCH_TIMEOUT,
                     headers={"User-Agent": _USER_AGENT}) as client, \
            client.stream("GET", url) as resp:
        resp.raise_for_status()
        charset = resp.charset_encoding or "utf-8"
        raw = read_limited(resp, MAX_FETCH_BYTES)
    return raw.decode(charset, errors="replace")


# ---------- Tavily（结构化 JSON API，需 key）----------

_TAVILY = "https://api.tavily.com/search"


class _MissingTavilyKey(Exception):
    """未配置 Tavily key；auto 模式下应跳过该后端而非算作失败。"""


def _fetch_tavily(query: str, limit: int) -> list:
    """调 Tavily 搜索 API，直接返回 [{title, url, snippet}]。

    与其余后端的差异：Tavily 返回 JSON 而非 HTML，故不需要 parse 阶段——
    这里直接产出结果列表。key 从 `config.load_tavily_key()` 取，未配时抛
    `_MissingTavilyKey`（由调用方转成"跳过"而非失败）。
    """
    key = config.load_tavily_key()
    if not key:
        raise _MissingTavilyKey
    payload = {
        "query": query,
        "max_results": limit,
        "search_depth": "basic",  # basic 1 credit/次；advanced 2 credits，用不上
    }
    with http_client(timeout=SEARCH_TIMEOUT,
                     headers={"User-Agent": _USER_AGENT}) as client:
        resp = client.post(_TAVILY, json=payload,
                           headers={"Authorization": f"Bearer {key}"})
        resp.raise_for_status()
        data = json.loads(resp.text)
    results = []
    for item in data.get("results", []):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        results.append({
            "title": str(item.get("title") or "").strip(),
            "url": url,
            "snippet": " ".join(str(item.get("content") or "").split())[:MAX_SNIPPET_LEN],
        })
        if len(results) >= limit:
            break
    return results


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


# ---------- 后端注册表与统一入口 ----------

_BACKENDS = {
    "brave": (_fetch_brave, _parse_brave),
    "bing": (_fetch_bing, _parse_bing),
}

_DISPLAY_NAMES = {
    "tavily": "Tavily",
    "brave": "Brave",
    "bing": "Bing",
}


def _try_backend(name: str, query: str, limit: int) -> tuple[list, str | None]:
    """跑单个后端，返回 (结果, 错误说明)。

    错误说明非 None 表示这次尝试没拿到可用结果（网络失败 / 被反爬 / 解析为空），
    auto 模式下会据此继续试下一个后端。未配 Tavily key 时返回 (空, None)——不是
    错误，只是这个后端现在不可用。
    """
    if name == "tavily":
        try:
            return _fetch_tavily(query, limit), None
        except _MissingTavilyKey:
            return [], None  # 未配 key：静默跳过，不算失败
        except httpx2.HTTPStatusError as e:
            code = e.response.status_code
            if code in (401, 403):
                return [], f"Tavily key 无效或额度用尽（HTTP {code}）"
            if code == 429:
                return [], "Tavily 触发限流（HTTP 429）"
            return [], f"Tavily 返回 HTTP {code}"
        except (httpx2.RequestError, httpx2.InvalidURL, OSError) as e:
            return [], f"Tavily 请求失败: {e}"
        except (json.JSONDecodeError, ValueError):
            return [], "Tavily 返回内容无法解析"
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
    return [], None  # 页面正常、只是没有结果


def _order(backend: str) -> tuple:
    """解析配置为实际尝试的后端序列。"""
    if backend == "auto":
        return _AUTO_ORDER
    return (backend,)


def _backend_hint() -> str:
    """固定后端失败时给模型的提示：可改用 auto 或其他后端。"""
    choices = "/".join(b for b in config.SEARCH_BACKENDS)
    return f"可改用其他后端：config.toml 的 [search].backend 设为 {choices}"


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
    # 固定 tavily 但没配 key：直接给出可操作提示，不当作"无结果"
    if backend == "tavily" and not config.load_tavily_key():
        return (
            "错误: 未配置 Tavily API key，tavily 后端不可用。"
            "可运行 smith setup 配置，或设环境变量 SMITHCODE_TAVILY_KEY；"
            f"也可改用其他后端：{_backend_hint()}"
        )
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
            return f"错误: {'；'.join(errors)}。{_backend_hint()}"
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
