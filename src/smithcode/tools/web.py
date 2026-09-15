"""webfetch 工具：抓取网页并转为结构化文本，供模型查公开文档与资料。

HTTP 层走 httpx2（见 `utils/http.py`），与 LLM 客户端共用同一套代理语义
（`ALL_PROXY` / `HTTP(S)_PROXY`，含 socks5）——此前用 urllib，它既不认
`ALL_PROXY` 也不支持 socks，用户设了系统代理就会出现「模型能连、抓取连不上」。
仅放行 http/https，拒绝其他协议（防止 file:// 等变相读本地文件）。
默认拒访内网 / 本机地址（SSRF 防护，见 `_blocked_reason`），并对每一跳重定向
重新校验，防止「公网地址 302 到内网」。
"""
from __future__ import annotations

import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import httpx2

from .. import config
from ..utils import htmltext
from ..utils.http import client as http_client
from ..utils.http import private_target, read_limited
from .base import register

FETCH_TIMEOUT = 30  # 单次抓取超时（秒）
MAX_FETCH_CHARS = 20_000  # 默认返回的最大字符数（超出截断）
MAX_FETCH_BYTES = 2_000_000  # 响应体最多读取的字节数
MAX_URLS = 5  # 单次调用最多并行抓取的 URL 数（更多请分多次调用）
MAX_REDIRECTS = 5  # 手动跟随重定向的上限（每跳都要过 SSRF 校验）

_USER_AGENT = "Mozilla/5.0 (compatible; SmithCode/1.0; +terminal coding agent)"


def _blocked_reason(url: str) -> str | None:
    """SSRF 防护：url 指向本机 / 内网时返回给模型的拒访说明，否则 None。

    逐跳调用（含重定向落点），因为「公网地址 → 302 到内网」是绕过单点校验的
    经典手法。域名走 DNS 解析后按全部结果判定；解析失败不拦——请求本就连不上，
    交给网络层报常规错误。
    """
    if config.load_allow_private_urls():
        return None
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return None
    hit = private_target(host)
    if hit is None:
        return None
    return (
        f"错误: 拒绝访问内网 / 本机地址（{host} → {hit}）({url[:100]})；"
        "如确需抓取本地服务（如本机开发服务器），可在 config.toml 设 allow_private_urls = true"
    )


@register(
    {
        "name": "webfetch",
        "describe": lambda args: f"fetch {args.get('url', '?')}",
        "description": "抓取网页（仅 http/https）并转为结构化文本（Markdown 风格，保留标题 / 链接 / "
        "代码块 / 列表）返回。url 传单个地址或地址列表（一次最多并行抓 5 个）；"
        "要抓很多网页时建议并行多次调用本工具，每次不超过 5 个地址。"
        "用于查阅公开文档、规范、报错方案等；内容过长会被截断，可用 max_chars 控制。"
        "默认拒访内网与本机地址（防 SSRF）。",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "oneOf": [
                        {"type": "string", "description": "完整的 http/https URL"},
                        {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "URL 列表（一次最多并行抓 5 个）",
                        },
                    ],
                    "description": "完整的 http/https URL，或 URL 列表",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "最多返回的字符数（单个地址的截断上限），默认 20000",
                },
            },
            "required": ["url"],
        },
    }
)
def webfetch(url: str | list[str], max_chars: int | None = None) -> str:
    urls = [url] if isinstance(url, str) else list(url)
    if not urls:
        return "错误: url 列表为空"
    if len(urls) > MAX_URLS:
        return (
            f"错误: 一次最多并行抓取 {MAX_URLS} 个 URL，收到 {len(urls)} 个；"
            f"请拆成多次调用，每次不超过 {MAX_URLS} 个"
        )
    if len(urls) == 1:
        return _fetch_one(urls[0], max_chars)
    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        results = list(pool.map(lambda u: _fetch_one(u, max_chars), urls))
    return "\n\n".join(
        f"===== [{i + 1}] {u} =====\n{r}" for i, (u, r) in enumerate(zip(urls, results))
    )


def _fetch_one(url: str, max_chars: int | None = None) -> str:
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return f"错误: 仅支持 http/https URL，收到: {url[:100]}"
    limit = max(500, int(max_chars) if max_chars else MAX_FETCH_CHARS)
    try:
        # 手动跟随重定向（follow_redirects=False）：每一跳都重新过 SSRF 校验，
        # 否则「公网地址 302 到 169.254.169.254」就能绕过首次检查
        with http_client(timeout=FETCH_TIMEOUT, headers={"User-Agent": _USER_AGENT},
                         follow_redirects=False) as client:
            current, raw, charset = url, None, "utf-8"
            for _ in range(MAX_REDIRECTS + 1):
                blocked = _blocked_reason(current)
                if blocked:
                    return blocked
                with client.stream("GET", current) as resp:
                    if resp.is_redirect:
                        target = urllib.parse.urljoin(
                            str(resp.url), resp.headers.get("location", "")
                        )
                        if not re.match(r"^https?://", target, re.IGNORECASE):
                            return "错误: 重定向到了非 http/https 地址，已中止"
                        current = target
                        continue
                    resp.raise_for_status()
                    charset = resp.charset_encoding or "utf-8"
                    raw = read_limited(resp, MAX_FETCH_BYTES)
                break
            else:
                return f"错误: 重定向次数过多（超过 {MAX_REDIRECTS} 次）({url[:100]})"
    except httpx2.HTTPStatusError as e:
        return f"错误: HTTP {e.response.status_code} {e.response.reason_phrase} ({url[:100]})"
    except (httpx2.RequestError, httpx2.InvalidURL, OSError) as e:
        return f"错误: 抓取失败: {e} ({url[:100]})"
    return htmltext.to_markdown(raw.decode(charset, errors="replace"))[:limit]
