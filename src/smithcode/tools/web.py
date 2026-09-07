"""webfetch 工具：抓取网页并转为纯文本，供模型查公开文档与资料。

用标准库实现（urllib + 正则去标签），不引入新依赖。
仅放行 http/https，拒绝其他协议（防止 file:// 等变相读本地文件）。
"""
from __future__ import annotations

import html
import re
import urllib.error
import urllib.request

from .base import register

FETCH_TIMEOUT = 30  # 单次抓取超时（秒）
MAX_FETCH_CHARS = 20_000  # 默认返回的最大字符数（超出截断）
MAX_FETCH_BYTES = 2_000_000  # 响应体最多读取的字节数

_USER_AGENT = "Mozilla/5.0 (compatible; SmithCode/1.0; +terminal coding agent)"


def _strip_html(raw: str) -> str:
    """极简 HTML 转纯文本：去 script/style，块级标签转换行，其余标签删除。"""
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1\s*>", " ", raw)
    text = re.sub(r"(?i)<(?:br|/p|/div|/li|/h[1-6]|/tr|/table|/pre)[^>]*>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


@register(
    {
        "name": "webfetch",
        "describe": lambda args: f"fetch {args.get('url', '?')}",
        "description": "抓取一个网页（仅 http/https）并转为纯文本返回。"
        "用于查阅公开文档、规范、报错方案等；内容过长会被截断，可用 max_chars 控制。",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "完整的 http/https URL"},
                "max_chars": {
                    "type": "integer",
                    "description": "最多返回的字符数，默认 20000",
                },
            },
            "required": ["url"],
        },
    }
)
def webfetch(url: str, max_chars: int | None = None) -> str:
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return f"错误: 仅支持 http/https URL，收到: {url[:100]}"
    limit = max(500, int(max_chars) if max_chars else MAX_FETCH_CHARS)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            if not re.match(r"^https?://", resp.geturl() or "", re.IGNORECASE):
                return "错误: 重定向到了非 http/https 地址，已中止"
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read(MAX_FETCH_BYTES).decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        return f"错误: HTTP {e.code} {e.reason} ({url[:100]})"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", None) or e
        return f"错误: 抓取失败: {reason} ({url[:100]})"
    return _strip_html(raw)[:limit]
