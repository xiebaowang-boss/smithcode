"""HTTP 客户端工厂：轻量网络工具（webfetch / websearch）统一经此出网。

与 LLM 客户端共用同一套代理语义——httpx2 在 `trust_env=True` 下读取
`ALL_PROXY` / `HTTP(S)_PROXY` 等环境变量，socks5 由 socksio 支持。此前这两个
工具走 urllib，而 urllib 既不认 `ALL_PROXY` 也不支持 socks：用户设了系统代理时
LLM 能连、搜索与抓取却连不上。

注意系统代理（Clash / FlClash / GNOME 等）常写入非标准的 `socks://`，而 httpx
在**构造客户端时**就急切校验 scheme 并抛 `ValueError: Unknown scheme for proxy
URL`，故构造前必须先跑 `utils.proxy.normalize_proxy_env()`。

另有一处与代理无关的坑：TLS 握手默认会带 ALPN 扩展 `["http/1.1"]`。早期
websearch 只走 DuckDuckGo 时，该指纹会被判定为机器人、直接返回反爬 challenge 页
（实测：带 ALPN 被拦、不带则正常返回结果；改造前的 urllib 本就不发 ALPN），
故这里统一屏蔽 ALPN，证书校验照常。该后端已移除，此屏蔽作为历史对策保留——
对现有后端无害，也省得日后新后端再踩同一个坑。
"""
from __future__ import annotations

import ipaddress
import socket
import ssl

import httpx2

from .proxy import normalize_proxy_env


class _NoAlpnContext(ssl.SSLContext):
    """屏蔽 ALPN 扩展的 TLS 上下文（httpcore 会调用 set_alpn_protocols，这里吞掉）。"""

    def set_alpn_protocols(self, protocols) -> None:
        """有意屏蔽：见模块 docstring（避免 ALPN 指纹被反爬判定）。"""
        return


def ssl_context() -> ssl.SSLContext:
    """默认证书校验、但不发送 ALPN 的 TLS 上下文。

    与 `ssl.create_default_context()` 等价（`CERT_REQUIRED` + 主机名校验 +
    系统 CA），唯一差别是不发 ALPN 扩展。
    """
    ctx = _NoAlpnContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_default_certs()
    return ctx


def client(*, timeout: float, headers: dict | None = None,
           follow_redirects: bool = True) -> httpx2.Client:
    """构造走环境代理的同步客户端（调用方负责关闭，建议 `with`）。

    每次现建现用、不做进程级缓存：代理环境变量在构造期读取，用户中途改代理后
    下一次工具调用即可生效。`trust_env=True` 是 httpx 的默认值，这里显式写出，
    表明"读环境代理"是本工厂的契约而非巧合。
    """
    normalize_proxy_env()  # 无 CLI 入口（库直用 / 测试）时同样兜底一次
    return httpx2.Client(
        timeout=timeout,
        headers=headers,
        follow_redirects=follow_redirects,
        trust_env=True,
        verify=ssl_context(),
    )


def read_limited(response: httpx2.Response, limit: int) -> bytes:
    """按上限读取响应体（已解压），超大页面不占满内存。

    需在 `client.stream(...)` 上下文内调用：读满 limit 即停，剩余数据由上下文
    关闭连接时丢弃。返回的字节数不超过 limit。
    """
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total >= limit:
            break
    return b"".join(chunks)[:limit]


# ---------- SSRF：非公网地址判定 ----------

# 末 32 位承载一个 IPv4 地址的 IPv6 前缀：这类地址的"真实目的地"是那个 IPv4，
# 只按 IPv6 自身判定会漏（见 _embedded_ipv4）。
#   ::ffff:0:0/96      IPv4-mapped（RFC 4291）—— CPython 的 is_global 已按嵌入
#                      IPv4 判定，列在这里是为了不依赖版本行为
#   ::ffff:0:0:0/96    IPv4-translated（RFC 2765）—— is_global 会误判为 True
#   64:ff9b::/96       NAT64 well-known 前缀（RFC 6052）—— is_global 会误判为 True，
#                      在 DNS64 网络里实际连到嵌入的 IPv4
_IPV4_EMBEDDING_PREFIXES = (
    ipaddress.ip_network("::ffff:0:0/96"),
    ipaddress.ip_network("::ffff:0:0:0/96"),
    ipaddress.ip_network("64:ff9b::/96"),
)


def _embedded_ipv4(addr: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """地址若把 IPv4 嵌在末 32 位则返回该 IPv4，否则 None。

    只认 `_IPV4_EMBEDDING_PREFIXES` 里的固定布局，不做 RFC 6052 那种可变前缀长度
    的推算：布局不固定就没法可靠判断"嵌入的是哪个地址"，与其猜错不如交给
    下一层判定（6to4 / Teredo / NAT64 local-use 等前缀 CPython 已判为非全局）。
    """
    for prefix in _IPV4_EMBEDDING_PREFIXES:
        if addr in prefix:
            return ipaddress.IPv4Address(addr.packed[12:16])
    return None


def is_public_address(ip: str) -> bool:
    """IP 字面量是否为可全局路由的地址（webfetch 的 SSRF 判定基础）。

    非公网地址包括：私网（10/172.16/192.168）、回环（127/::1）、链路本地
    （169.254/fe80::，含云元数据 169.254.169.254）、CGNAT（100.64/10）、
    保留与文档段（0.0.0.0、192.0.2.0/24、2001:db8::）、多播与未指定地址等。
    解析失败按"不可信"处理（返回 False）。

    嵌入 IPv4 的过渡 / 转换地址（如 `64:ff9b::7f00:1` → 127.0.0.1）按嵌入的
    IPv4 判定：`is_global` 只看 IPv6 本身，这类地址会被误判为可访问，而实际
    目的地是内网。
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    # is_multicast 的 is_global 为 True（如 224.0.0.1），需显式排除
    if not addr.is_global or addr.is_multicast:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(addr)
        if embedded is not None:
            return embedded.is_global and not embedded.is_multicast
    return True


def resolve_host(host: str) -> list[str]:
    """解析主机名的全部 IP（去重保序）；解析失败返回空列表。

    单独成函数是为了让测试可以替换掉它——SSRF 用例不应依赖真实 DNS。
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError, UnicodeError):
        return []
    seen: set = set()
    result: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in seen:
            seen.add(ip)
            result.append(ip)
    return result


def private_target(host: str) -> str | None:
    """主机名若指向非公网地址则返回该地址（供调用方拒绝），否则 None。

    - IP 字面量直接判定；
    - 域名走 DNS 解析，任一解析结果落在内网即拒绝（防「公网域名指向内网」）；
    - 解析失败返回 None——请求本就连不上，交给网络层报常规错误即可。

    局限：不做连接期地址固定，理论上存在 DNS rebinding（校验后解析结果变化），
    对本场景（终端里的只读抓取）按可接受处理。嵌入 IPv4 的地址（IPv4-mapped /
    IPv4-translated / NAT64 well-known）由 is_public_address 按嵌入的 IPv4 判定。
    """
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return host if not is_public_address(host) else None
    for ip in resolve_host(host):
        if not is_public_address(ip):
            return ip
    return None
