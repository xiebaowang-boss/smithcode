"""websearch 工具：用 DuckDuckGo HTML 版检索网页，纯标准库实现。

与 webfetch 的分工：websearch 只给候选结果的标题 / 链接 / 摘要，需要正文时
再对结果链接调用 webfetch。无 API key、无第三方依赖（webfetch 同款哲学）；
DuckDuckGo 返回的跳转链接（`//duckduckgo.com/l/?uddg=<真实地址>`）会被还原。
"""
from __future__ import annotations

import html
import re
import urllib.error
import urllib.parse
import urllib.request

from .base import register
from .web import _USER_AGENT, MAX_FETCH_BYTES

_ENDPOINT = "https://html.duckduckgo.com/html/"
SEARCH_TIMEOUT = 20  # 单次检索超时（秒）
DEFAULT_RESULTS = 5  # 默认返回条数
MAX_RESULTS = 10  # 单次最多返回条数
MAX_SNIPPET_LEN = 300  # 单条摘要展示上限

_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    """去标签 + 反转义 + 折叠空白（标题 / 摘要的内联文本清理）。"""
    text = _TAG_RE.sub("", text or "")
    text = html.unescape(text)
    return " ".join(text.split())


def _real_url(href: str) -> str:
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


def _parse_results(page: str, limit: int) -> list:
    """从结果页解析 [{title, url, snippet}]。

    按文档顺序遍历锚点：result__a 开一条结果，紧随其后的 result__snippet
    补其摘要（DDG 块内顺序固定为标题在前、摘要随后）。按 URL 去重。
    """
    results: list = []
    seen: set = set()
    for match in _ANCHOR_RE.finditer(page):
        attrs = dict(_ATTR_RE.findall(match.group(1)))
        classes = attrs.get("class", "")
        if "result__a" in classes:
            url = _real_url(attrs.get("href", ""))
            if not url or url in seen:
                continue
            seen.add(url)
            results.append({"title": _clean(match.group(2)), "url": url, "snippet": ""})
        elif "result__snippet" in classes and results and not results[-1]["snippet"]:
            results[-1]["snippet"] = _clean(match.group(2))[:MAX_SNIPPET_LEN]
    return results[:limit]


def _fetch(query: str) -> str:
    """POST 检索 DuckDuckGo HTML 版并返回解码后的页面文本。"""
    data = urllib.parse.urlencode({"q": query}).encode("utf-8")
    req = urllib.request.Request(
        _ENDPOINT, data=data, headers={"User-Agent": _USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=SEARCH_TIMEOUT) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read(MAX_FETCH_BYTES).decode(charset, errors="replace")


def _describe(args: dict) -> str:
    query = " ".join(str(args.get("query", "?")).split())
    return f"search {query[:60]}"


@register(
    {
        "name": "websearch",
        "describe": _describe,
        "description": "用 DuckDuckGo 检索网页，返回若干结果的标题、链接与摘要。"
        "用于查找公开资料、最新信息、报错方案等；需要网页正文时再对结果链接调用 webfetch。"
        f"max_results 控制返回条数（默认 {DEFAULT_RESULTS}，上限 {MAX_RESULTS}）。",
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
    try:
        page = _fetch(query)
    except urllib.error.HTTPError as e:
        return f"错误: 搜索请求失败: HTTP {e.code} {e.reason}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", None) or e
        return f"错误: 搜索请求失败: {reason}"
    results = _parse_results(page, limit)
    if not results:
        return "（无搜索结果）"
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title'] or r['url']}")
        lines.append(f"   {r['url']}")
        if r["snippet"]:
            lines.append(f"   {r['snippet']}")
    return "\n".join(lines)
