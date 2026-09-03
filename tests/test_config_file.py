"""全局配置测试：~/.smithcode/ 发现、默认回退、env > 文件、credentials.json、损坏降级。

所有用例经 SMITHCODE_HOME 指向临时目录隔离，绝不读写真实家目录。
"""

import importlib
import json

from smithcode import config


def _make_home(tmp_path, toml_text=None, credentials=None):
    """造一个隔离的配置根目录并注入 SMITHCODE_HOME；返回目录路径。"""
    home = tmp_path / "smithcode-home"
    home.mkdir()
    if toml_text is not None:
        (home / "config.toml").write_text(toml_text, encoding="utf-8")
    if credentials is not None:
        (home / "credentials.json").write_text(json.dumps(credentials), encoding="utf-8")
    return home


# ---------- 路径与文件发现 ----------

def test_home_defaults_to_user_home(monkeypatch):
    monkeypatch.delenv("SMITHCODE_HOME", raising=False)
    assert config.smithcode_home() == config.Path.home() / ".smithcode"


def test_home_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("SMITHCODE_HOME", str(tmp_path))
    assert config.config_path() == tmp_path / "config.toml"
    assert config.credentials_path() == tmp_path / "credentials.json"


def test_missing_files_return_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("SMITHCODE_HOME", str(tmp_path))
    assert config._read_config_file() == {}
    assert config._read_credentials() == {}


def test_config_toml_is_read(tmp_path, monkeypatch):
    home = _make_home(tmp_path, '[provider]\nmodel = "test-model"\n')
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    assert config._read_config_file()["provider"]["model"] == "test-model"


# ---------- 损坏降级 ----------

def test_broken_toml_degrades_with_warning(tmp_path, monkeypatch, capsys):
    home = _make_home(tmp_path, "this is not valid toml")
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    assert config._read_config_file() == {}
    assert "警告" in capsys.readouterr().out


def test_broken_credentials_degrade_with_warning(tmp_path, monkeypatch, capsys):
    home = _make_home(tmp_path)
    (home / "credentials.json").write_text("not json", encoding="utf-8")
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    assert config._read_credentials() == {}
    assert "警告" in capsys.readouterr().out


def test_credentials_non_dict_degrades(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    (home / "credentials.json").write_text("[1, 2]", encoding="utf-8")
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    assert config._read_credentials() == {}


def test_credentials_non_str_key_ignored(tmp_path, monkeypatch):
    home = _make_home(tmp_path, credentials={"key": 42})
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    assert config._credentials_key() == ""


# ---------- key / model / url 三项：env > 文件 > 默认 ----------

def test_resolution_env_overrides_file(tmp_path, monkeypatch):
    """SMITHCODE_* 环境变量优先于同名文件配置（import 时解析一次，这里重载验证）。"""
    home = _make_home(
        tmp_path,
        '[provider]\nmodel = "file-model"\nurl = "https://file.example"\n',
        credentials={"key": "sk-file"},
    )
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    monkeypatch.setenv("SMITHCODE_MODEL", "env-model")
    monkeypatch.setenv("SMITHCODE_URL", "https://env.example")
    monkeypatch.setenv("SMITHCODE_KEY", "sk-env")
    try:
        mod = importlib.reload(config)
        assert mod.MODEL == "env-model"
        assert mod.URL == "https://env.example"
        assert mod.KEY == "sk-env"
    finally:
        # 先清干净 env 再还原模块，否则还原 reload 会把临时目录的值留在常量里污染后续测试
        for name in ("SMITHCODE_HOME", "SMITHCODE_MODEL", "SMITHCODE_URL", "SMITHCODE_KEY"):
            monkeypatch.delenv(name, raising=False)
        importlib.reload(config)


def test_resolution_file_overrides_default(tmp_path, monkeypatch):
    home = _make_home(tmp_path, '[provider]\nmodel = "file-model"\n', credentials={"key": "sk-file"})
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    for name in ("SMITHCODE_MODEL", "SMITHCODE_URL", "SMITHCODE_KEY"):
        monkeypatch.delenv(name, raising=False)
    try:
        mod = importlib.reload(config)
        assert mod.MODEL == "file-model"
        assert mod.KEY == "sk-file"
        assert mod.URL is None  # 文件没配也没有默认值，留给 SDK 走官方地址
    finally:
        for name in ("SMITHCODE_HOME", "SMITHCODE_MODEL", "SMITHCODE_URL", "SMITHCODE_KEY"):
            monkeypatch.delenv(name, raising=False)
        importlib.reload(config)


# ---------- 数值配置：默认 < config.toml ----------

def test_number_defaults_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("SMITHCODE_HOME", str(tmp_path))
    assert config._resolve_number("context", "budget", 65536) == 65536


def test_number_from_toml(tmp_path, monkeypatch):
    home = _make_home(tmp_path, "[context]\nbudget = 4096\n")
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    assert config._resolve_number("context", "budget", 65536) == 4096


def test_invalid_toml_value_degrades_to_default(tmp_path, monkeypatch, capsys):
    home = _make_home(tmp_path, '[context]\nbudget = "四千"\n')
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    assert config._resolve_number("context", "budget", 65536) == 65536
    assert "警告" in capsys.readouterr().out


def test_bool_is_not_a_number(tmp_path, monkeypatch):
    """TOML 的 true/false 是 bool，不能当数值用。"""
    home = _make_home(tmp_path, "[context]\ncompact_trigger = true\n")
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    assert config._resolve_number("context", "compact_trigger", 0.8) == 0.8


def test_file_int_converted_to_float_default_type(tmp_path, monkeypatch):
    """TOML 写整数 1、默认值是 float：解析结果保持 float 语义（类型由默认值决定）。"""
    home = _make_home(tmp_path, "[context]\ncompact_trigger = 1\n")
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    value = config._resolve_number("context", "compact_trigger", 0.8)
    assert value == 1.0 and isinstance(value, float)


# ---------- 字符串取值与权限解析 ----------

def test_file_str_wrong_type_warns(tmp_path, monkeypatch, capsys):
    home = _make_home(tmp_path, "[provider]\nmodel = 42\n")
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    assert config._file_str("provider", "model") is None
    assert "警告" in capsys.readouterr().out


def test_load_permissions_from_global(tmp_path, monkeypatch):
    """[permissions] 段：字符串简写与表写法混用，表内保持书写顺序；与工作区无关。"""
    home = _make_home(tmp_path, """\
[permissions]
read_file = "allow"
run_command = { "*" = "ask", "git status" = "allow" }
""")
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    assert config.load_permissions() == [
        ("read_file", "*", "allow"),
        ("run_command", "*", "ask"),
        ("run_command", "git status", "allow"),
    ]
