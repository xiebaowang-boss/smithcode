"""smithcode setup 向导测试：写盘内容、幂等保留、坏输入降级、非交互取消。"""

import json

import pytest

from smithcode import config, wizard


@pytest.fixture
def home(tmp_path, monkeypatch):
    """隔离的配置根目录 + 钉死的环境变量与 config 常量（不依赖真实家目录）。"""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("SMITHCODE_HOME", str(h))
    for name in ("SMITHCODE_KEY", "SMITHCODE_MODEL", "SMITHCODE_URL"):
        monkeypatch.delenv(name, raising=False)
    # 向导提示符的默认值来自这些常量（真实 CLI 中为 import 时解析结果），钉死以保证确定性
    monkeypatch.setattr(config, "URL", None)
    monkeypatch.setattr(config, "MODEL", "deepseek-v4-flash")
    monkeypatch.setattr(config, "KEY", "")
    monkeypatch.setattr(config, "CONTEXT_TOKEN_BUDGET", 65536)
    return h


def _feed(monkeypatch, replies):
    """按顺序返回输入行；getpass（key 一问）与 input 共用同一队列。"""
    queue = iter(replies)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(queue))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(queue))


def test_setup_writes_both_files(home, monkeypatch, capsys):
    _feed(monkeypatch, ["", "", "sk-abc", ""])  # url/model/budget 取默认，仅填 key
    assert wizard.run_setup() == 0

    credentials = json.loads((home / "credentials.json").read_text(encoding="utf-8"))
    assert credentials["key"] == "sk-abc"

    toml_text = (home / "config.toml").read_text(encoding="utf-8")
    assert 'url = "https://api.deepseek.com/v1"' in toml_text
    assert 'model = "deepseek-v4-flash"' in toml_text
    assert "budget = 65536" in toml_text
    assert "完成" in capsys.readouterr().out


def test_setup_custom_values_and_k_suffix(home, monkeypatch):
    _feed(monkeypatch, ["https://api.moonshot.cn/v1", "kimi-k2", "sk-xyz", "128k"])
    assert wizard.run_setup() == 0

    toml_text = (home / "config.toml").read_text(encoding="utf-8")
    assert 'url = "https://api.moonshot.cn/v1"' in toml_text
    assert 'model = "kimi-k2"' in toml_text
    assert "budget = 128000" in toml_text
    assert json.loads((home / "credentials.json").read_text(encoding="utf-8"))["key"] == "sk-xyz"


def test_setup_rerun_preserves_sections_and_comments(home, monkeypatch):
    """重跑只更新 [provider]/[context] 段；用户手写的 permissions 与注释原样保留。"""
    (home / "config.toml").write_text(
        "# 我的注释\n"
        "[provider]\n"
        'url = "https://old.example"\n'
        'model = "old-model"\n'
        "\n"
        "[permissions]\n"
        'run_command = { "*" = "ask", "git status" = "allow" }\n',
        encoding="utf-8",
    )
    # 模拟新进程：config 常量已从文件解析为当前值，向导提示符里回车即保留
    monkeypatch.setattr(config, "URL", "https://old.example")
    monkeypatch.setattr(config, "MODEL", "old-model")
    monkeypatch.setattr(config, "KEY", "sk-old")
    monkeypatch.setattr(config, "CONTEXT_TOKEN_BUDGET", 4096)
    _feed(monkeypatch, ["", "", "", ""])  # 全部回车 = 什么都不改

    assert wizard.run_setup() == 0
    toml_text = (home / "config.toml").read_text(encoding="utf-8")
    assert "https://old.example" in toml_text  # 幂等：仍是旧值
    assert 'run_command = { "*" = "ask", "git status" = "allow" }' in toml_text
    assert "# 我的注释" in toml_text  # 注释保留
    assert "budget = 4096" in toml_text

    credentials = json.loads((home / "credentials.json").read_text(encoding="utf-8"))
    assert credentials["key"] == "sk-old"  # 回车保留旧 key


def test_setup_empty_key_keeps_existing_credentials(home, monkeypatch):
    (home / "credentials.json").write_text(
        json.dumps({"key": "sk-old", "extra": 1}), encoding="utf-8"
    )
    _feed(monkeypatch, ["", "", "", ""])  # key 一问回车 = 保留
    assert wizard.run_setup() == 0

    credentials = json.loads((home / "credentials.json").read_text(encoding="utf-8"))
    assert credentials == {"key": "sk-old", "extra": 1}  # 未动文件，其他字段保留


def test_setup_non_numeric_budget_degrades(home, monkeypatch, capsys):
    _feed(monkeypatch, ["", "", "sk-abc", "不是数字"])
    assert wizard.run_setup() == 0
    assert "不是有效数字" in capsys.readouterr().out
    assert "budget = 65536" in (home / "config.toml").read_text(encoding="utf-8")


def test_setup_eof_cancels_without_writing(home, monkeypatch):
    def _eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    assert wizard.run_setup() == 1
    assert not (home / "config.toml").exists()
    assert not (home / "credentials.json").exists()


def test_cli_dispatch_setup(home, monkeypatch, capsys):
    """`smithcode setup` 走向导并以向导退出码结束，不进入 Agent 构建。"""
    _feed(monkeypatch, ["", "", "sk-cli", ""])
    from smithcode.cli import main

    with pytest.raises(SystemExit) as excinfo:
        main(["setup"])
    assert excinfo.value.code == 0
    assert "完成" in capsys.readouterr().out
    assert json.loads((home / "credentials.json").read_text(encoding="utf-8"))["key"] == "sk-cli"
