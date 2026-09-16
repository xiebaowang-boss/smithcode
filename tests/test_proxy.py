"""proxy 测试：系统代理写入的 socks:// 归一化为 httpx 认的 socks5://。"""

import os

from smithcode.utils.proxy import normalize_proxy_env


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
