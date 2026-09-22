"""utils/http.py 的客户端工厂测试：代理归一化、ALPN 屏蔽与限流读取（不联网）。"""
import datetime
import os
import socket
import ssl
import threading

import httpx2
import pytest

from smithcode.utils import http as http_util
from smithcode.utils.proxy import normalize_proxy_env

# ---------- 代理归一化：socks:// → socks5://（直测归一化函数） ----------

def test_rewrites_socks_scheme(monkeypatch):
    """Clash 等写入的 socks:// 会被改写为 socks5://，host/端口保持不变。"""
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:7890")
    normalize_proxy_env()
    assert os.environ["ALL_PROXY"] == "socks5://127.0.0.1:7890"


def test_accepts_auth_and_uppercase_scheme(monkeypatch):
    """带认证信息与大小写混合的 scheme 同样只换 scheme。"""
    monkeypatch.setenv("all_proxy", "SOCKS://user:pass@127.0.0.1:1080")
    normalize_proxy_env()
    assert os.environ["all_proxy"] == "socks5://user:pass@127.0.0.1:1080"


def test_idempotent_and_does_not_touch_others(monkeypatch):
    """已归一化的 socks5:// 与 http 代理、NO_PROXY 都不受影响。"""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:8080")
    monkeypatch.setenv("HTTPS_PROXY", "socks5://127.0.0.1:7890")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    normalize_proxy_env()
    normalize_proxy_env()  # 幂等：再跑一次结果不变
    assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:8080"
    assert os.environ["HTTPS_PROXY"] == "socks5://127.0.0.1:7890"
    assert os.environ["NO_PROXY"] == "127.0.0.1,localhost"


def test_missing_vars_are_safe(monkeypatch):
    """没有代理变量时不报错、不新建变量。"""
    for key in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(key, raising=False)
    normalize_proxy_env()


# ---------- 代理归一化：经客户端工厂的回归（构造期不抛异常） ----------


def test_client_normalizes_nonstandard_socks_scheme(monkeypatch):
    """回归：系统代理写入的 `socks://` 必须在构造客户端前归一化。

    httpx 在构造期就急切校验 scheme，未归一化会抛
    `ValueError: Unknown scheme for proxy URL`——这正是「一开系统代理就崩」的
    根因，故这条用例断言的是"构造函数不抛异常"本身。
    """
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                 "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:7890")

    with http_util.client(timeout=5):
        pass  # 不抛即通过
    assert os.environ["ALL_PROXY"] == "socks5://127.0.0.1:7890"


def test_client_keeps_standard_scheme_untouched(monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:7890")
    with http_util.client(timeout=5):
        pass
    assert os.environ["http_proxy"] == "http://127.0.0.1:7890"


def test_read_limited_caps_bytes():
    """读满上限即停：超大响应体不整份读进内存。"""
    client = httpx2.Client(
        transport=httpx2.MockTransport(lambda request: httpx2.Response(200, content=b"x" * 5000))
    )
    with client, client.stream("GET", "http://example.test/") as resp:
        assert http_util.read_limited(resp, 100) == b"x" * 100


def test_read_limited_returns_short_body_as_is():
    client = httpx2.Client(
        transport=httpx2.MockTransport(lambda request: httpx2.Response(200, content=b"abc"))
    )
    with client, client.stream("GET", "http://example.test/") as resp:
        assert http_util.read_limited(resp, 100) == b"abc"


# ---------- TLS 上下文：不发 ALPN、证书校验照常（本地握手，不联网） ----------


def _self_signed_cert(tmp_path) -> tuple[str, str]:
    """生成 localhost 自签证书，返回 (cert_pem, key_pem)。"""
    x509 = pytest.importorskip("cryptography.x509")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    return str(cert_path), str(key_path)


def _tls_handshake_probe(cert: str, key: str, client_ctx: ssl.SSLContext):
    """用给定客户端上下文与本地 TLS 服务握手，返回服务端观察到的协商结果。

    服务端声明支持 ALPN `http/1.1`：客户端发了就协商出该值，没发则是 None——
    这正是离线验证"我们不发送 ALPN"的手段。返回 None / 协议名 / ("error", 说明)。
    """
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert, key)
    server_ctx.set_alpn_protocols(["http/1.1"])

    seen: list = []
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve():
        try:
            conn, _ = server.accept()
        except OSError:
            return
        try:
            with server_ctx.wrap_socket(conn, server_side=True) as tls:
                seen.append(tls.selected_alpn_protocol())
        except Exception as e:  # noqa: BLE001 - 握手失败也记录，供断言
            seen.append(("error", type(e).__name__))
        finally:
            server.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as raw, \
                client_ctx.wrap_socket(raw, server_hostname="localhost") as tls:
            tls.close()
    except Exception:  # noqa: BLE001, S110 - 客户端侧失败由服务端记录体现，不在此中断
        pass
    thread.join(timeout=10)
    return seen[0] if seen else None


def test_ssl_context_sends_no_alpn(tmp_path):
    """回归：屏蔽 ALPN 是为消除反爬指纹（历史触发点是 DDG），且对照证明了观测手段有效。"""
    cert, key = _self_signed_cert(tmp_path)

    ours = http_util.ssl_context()
    ours.load_verify_locations(cert)  # 仅测试：信任自签证书
    assert _tls_handshake_probe(cert, key, ours) is None

    control = ssl.create_default_context()
    control.load_verify_locations(cert)
    control.set_alpn_protocols(["http/1.1"])  # 对照组：发了就应被服务端看到
    assert _tls_handshake_probe(cert, key, control) == "http/1.1"


def test_ssl_context_still_verifies_certificates(tmp_path):
    """屏蔽 ALPN 不得削弱证书校验：未信任的自签证书必须握手失败。"""
    cert, key = _self_signed_cert(tmp_path)
    result = _tls_handshake_probe(cert, key, http_util.ssl_context())
    assert result is not None and result[0] == "error"


# ---------- SSRF：非公网地址判定 ----------


def test_is_public_address_allows_global_addresses():
    for ip in ("8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700::1111"):
        assert http_util.is_public_address(ip) is True, ip


def test_is_public_address_blocks_non_global():
    """回环 / 私网 / 链路本地（云元数据）/ CGNAT / 保留 / 多播 / 未指定都要拦。"""
    for ip in (
        "127.0.0.1", "::1", "10.0.0.5", "172.16.3.9", "192.168.1.1",
        "169.254.169.254", "fe80::1", "100.64.0.1", "0.0.0.0",
        "192.0.2.1", "224.0.0.1", "fc00::1", "::ffff:127.0.0.1",
    ):
        assert http_util.is_public_address(ip) is False, ip


def test_is_public_address_blocks_embedded_private_ipv4():
    """回归：末 32 位嵌着内网 IPv4 的地址按嵌入的 IPv4 判定。

    `64:ff9b::/96`（NAT64 well-known 前缀）与 `::ffff:0:0:0/96`
    （IPv4-translated）的 `is_global` 都为 True——只看 IPv6 本身会放行，
    而在 DNS64 网络里它们实际连到嵌入的那个（内网）IPv4。
    """
    for ip in (
        "64:ff9b::7f00:1",          # NAT64 → 127.0.0.1
        "64:ff9b::a00:1",           # NAT64 → 10.0.0.1
        "64:ff9b::a9fe:a9fe",       # NAT64 → 169.254.169.254（云元数据）
        "::ffff:0:127.0.0.1",       # IPv4-translated
        "::ffff:0:10.0.0.1",
    ):
        assert http_util.is_public_address(ip) is False, ip


def test_is_public_address_allows_embedded_public_ipv4():
    """嵌入公网 IPv4 的过渡地址仍放行（不能连坐）。"""
    for ip in ("64:ff9b::5db8:d822", "::ffff:93.184.216.34", "::ffff:8.8.8.8"):
        assert http_util.is_public_address(ip) is True, ip


def test_private_target_blocks_embedded_private_ipv4(monkeypatch):
    """字面量与域名解析结果两条路径都要拦住嵌入内网的地址。"""
    assert http_util.private_target("64:ff9b::a9fe:a9fe") == "64:ff9b::a9fe:a9fe"

    monkeypatch.setattr(http_util, "resolve_host", lambda host: ["64:ff9b::7f00:1"])
    assert http_util.private_target("rebind.example.com") == "64:ff9b::7f00:1"


def test_is_public_address_treats_garbage_as_untrusted():
    assert http_util.is_public_address("not-an-ip") is False


def test_private_target_for_ip_literal():
    assert http_util.private_target("169.254.169.254") == "169.254.169.254"
    assert http_util.private_target("127.0.0.1") == "127.0.0.1"
    assert http_util.private_target("8.8.8.8") is None
    assert http_util.private_target("") is None


def test_private_target_checks_every_resolved_address(monkeypatch):
    """域名解析出多个地址时，任一落在内网即拒绝（防公网域名指向内网）。"""
    monkeypatch.setattr(
        http_util, "resolve_host", lambda host: ["93.184.216.34", "10.0.0.7"]
    )
    assert http_util.private_target("mixed.example.com") == "10.0.0.7"

    monkeypatch.setattr(http_util, "resolve_host", lambda host: ["93.184.216.34"])
    assert http_util.private_target("good.example.com") is None


def test_resolve_host_dedupes_and_tolerates_failure(monkeypatch):
    infos = [
        (2, 1, 6, "", ("93.184.216.34", 0)),
        (2, 1, 6, "", ("93.184.216.34", 0)),
        (10, 1, 6, "", ("2606:4700::1111", 0, 0, 0)),
    ]
    monkeypatch.setattr(http_util.socket, "getaddrinfo", lambda *a, **kw: infos)
    assert http_util.resolve_host("example.com") == ["93.184.216.34", "2606:4700::1111"]

    def boom(*args, **kwargs):
        raise socket.gaierror("名字解析失败")

    monkeypatch.setattr(http_util.socket, "getaddrinfo", boom)
    assert http_util.resolve_host("nonexistent.invalid") == []
    # 解析失败不拦：请求本就连不上，交给网络层报常规错误
    assert http_util.private_target("nonexistent.invalid") is None
