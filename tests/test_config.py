"""config 测试：缺 API Key 时的启动校验与修复指引。"""

import pytest

from smithcode import config


def test_missing_key_raises_with_hint(monkeypatch):
    """完全没配 key：抛 ConfigError 且指引说清两种配置途径。"""
    monkeypatch.setattr(config, "KEY", "")
    with pytest.raises(config.ConfigError) as excinfo:
        config.ensure_api_key()
    message = str(excinfo.value)
    assert "smithcode setup" in message
    assert "SMITHCODE_KEY" in message


def test_blank_key_also_rejected(monkeypatch):
    """纯空白的 key 视同缺失，不能漏到 OpenAI SDK 那边才报错。"""
    monkeypatch.setattr(config, "KEY", "   ")
    with pytest.raises(config.ConfigError):
        config.ensure_api_key()


def test_present_key_passes(monkeypatch):
    monkeypatch.setattr(config, "KEY", "sk-test")
    config.ensure_api_key()  # 不抛即通过
