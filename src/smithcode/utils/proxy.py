"""代理环境变量归一化：把非标准的 socks:// 改写为 httpx 认的 socks5://。

Clash / V2RayN / GNOME 手动代理等常见工具会往环境里写
`ALL_PROXY=socks://host:port`（少了版本号），而 httpx（含 httpx2）在构造
client 时就会急切校验 scheme，只认 http / https / socks5 / socks5h，遇到
`socks://` 直接抛 `ValueError: Unknown scheme for proxy URL`；校验发生在
NO_PROXY 匹配之前，所以把目标域名加进 NO_PROXY 也救不了。

故在所有 HTTP client 构造之前（CLI 入口 + LLM 客户端）统一调用本模块，
把 `socks://` 就地归一化为 `socks5://`，避免用户设置系统代理后整个程序
无法启动。
"""
import contextlib
import os
import re

# 覆盖大小写两种键名：Clash 写 ALL_PROXY，urllib 的 getproxies() 也会读小写。
_PROXY_ENV_KEYS = (
    "ALL_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "all_proxy",
    "http_proxy",
    "https_proxy",
)

# 只匹配开头的 scheme，保留 host / 端口 / 认证信息与多余空白。
_SCHEME_RE = re.compile(r"^(\s*)([a-zA-Z][a-zA-Z0-9+.-]*)://")


def normalize_proxy_env() -> None:
    """把代理环境变量里的 `socks://` 就地改写为 `socks5://`。

    幂等：`socks5://` 不会再匹配 `socks://`，重复调用安全。
    任何异常一律吞掉——代理格式问题不该阻断程序启动。
    """
    with contextlib.suppress(Exception):  # 启动辅助函数，静默兜底
        for key in _PROXY_ENV_KEYS:
            raw = os.environ.get(key)
            if not raw:
                continue
            match = _SCHEME_RE.match(raw)
            if match and match.group(2).lower() == "socks":
                os.environ[key] = raw[: match.start(2)] + "socks5" + raw[match.end(2):]
