"""全局配置测试：~/.smithcode/ 发现、默认回退、env > 文件、credentials.json、损坏降级。

所有用例经 SMITHCODE_HOME 指向临时目录隔离，绝不读写真实家目录。
"""

import importlib
import json

import pytest

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


# ---------- provider.headers 自定义请求头 ----------

def test_provider_headers_absent_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("SMITHCODE_HOME", str(tmp_path))
    assert config.load_provider_headers() == {}


def test_provider_headers_read_nested_table(tmp_path, monkeypatch):
    """[provider.headers] 嵌套表原样返回，占位符不在此处求值。"""
    home = _make_home(
        tmp_path,
        '[provider.headers]\n"x-opencode-session" = "{$session}"\n',
    )
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    assert config.load_provider_headers() == {"x-opencode-session": "{$session}"}


def test_provider_headers_wrong_type_warns(tmp_path, monkeypatch, capsys):
    home = _make_home(tmp_path, '[provider]\nheaders = "nope"\n')
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    assert config.load_provider_headers() == {}
    assert "警告" in capsys.readouterr().out


def test_provider_headers_non_str_value_ignored(tmp_path, monkeypatch, capsys):
    home = _make_home(tmp_path, '[provider.headers]\ncount = 3\nname = "ok"\n')
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    assert config.load_provider_headers() == {"name": "ok"}
    assert "警告" in capsys.readouterr().out


# ---------- provider.models（/model 候选的显式配置） ----------

def test_read_configured_models_dedupes_in_order(tmp_path, monkeypatch):
    home = _make_home(tmp_path, '[provider]\nmodels = ["a", "b", "a"]\n')
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    assert config.read_configured_models() == ["a", "b"]


def test_read_configured_models_absent_returns_none(tmp_path, monkeypatch):
    """未配置返回 None（区别于空列表），由 ModelCatalog 决定后续来源。"""
    monkeypatch.setenv("SMITHCODE_HOME", str(tmp_path))
    assert config.read_configured_models() is None


def test_read_configured_models_empty_list_returns_none(tmp_path, monkeypatch):
    home = _make_home(tmp_path, '[provider]\nmodels = []\n')
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    assert config.read_configured_models() is None


def test_read_configured_models_wrong_type_warns(tmp_path, monkeypatch, capsys):
    home = _make_home(tmp_path, '[provider]\nmodels = "nope"\n')
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    assert config.read_configured_models() is None
    assert "警告" in capsys.readouterr().out


def test_read_configured_models_skips_non_string_items(tmp_path, monkeypatch, capsys):
    home = _make_home(tmp_path, '[provider]\nmodels = ["a", 3]\n')
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    assert config.read_configured_models() == ["a"]
    assert "警告" in capsys.readouterr().out


# ---------- [sessions] 配置与会话 id 采用 ----------

def test_sessions_config_defaults(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    cfg = config.load_sessions_config()
    assert cfg.enabled is True
    assert cfg.cleanup_days == 30
    assert cfg.persist_state is True
    assert cfg.list_limit == 20
    assert cfg.auto_title is True
    assert cfg.title_model == ""
    assert cfg.title_max_chars == 60


def test_sessions_config_reads_values(tmp_path, monkeypatch):
    home = _make_home(tmp_path, toml_text=(
        "[sessions]\n"
        "enabled = false\n"
        "cleanup_days = 7\n"
        "persist_state = false\n"
        "list_limit = 5\n"
        "auto_title = false\n"
        'title_model = "fast-model"\n'
        "title_max_chars = 20\n"
    ))
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    cfg = config.load_sessions_config()
    assert cfg.enabled is False
    assert cfg.cleanup_days == 7
    assert cfg.persist_state is False
    assert cfg.list_limit == 5
    assert cfg.auto_title is False
    assert cfg.title_model == "fast-model"
    assert cfg.title_max_chars == 20


def test_sessions_config_invalid_values_degrade(tmp_path, monkeypatch, capsys):
    home = _make_home(tmp_path, toml_text=(
        "[sessions]\n"
        'enabled = "yes"\n'
        "cleanup_days = -5\n"
        "title_max_chars = 0\n"
        "list_limit = -1\n"
    ))
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    cfg = config.load_sessions_config()
    assert cfg.enabled is True
    assert cfg.cleanup_days == 30
    assert cfg.title_max_chars == 60
    assert cfg.list_limit == 20
    assert "警告" in capsys.readouterr().out


def test_use_session_id_adopts_persisted_id():
    before = config.SESSION_ID
    assert config.use_session_id("abc123") == "abc123"
    assert config.SESSION_ID == "abc123"
    config.use_session_id(before)  # 还原，避免污染其他用例


# ---------- [instructions] 项目指令配置 ----------

def test_instructions_config_defaults(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    cfg = config.load_instructions_config()
    assert cfg.enabled is True
    assert cfg.files == ("AGENTS.md",)
    assert cfg.paths == ()
    assert cfg.max_chars == 8000


def test_instructions_config_reads_values(tmp_path, monkeypatch):
    home = _make_home(tmp_path, toml_text=(
        "[instructions]\n"
        "enabled = false\n"
        'files = ["AGENTS.md", "CLAUDE.md"]\n'
        'paths = ["docs/team.md", "C:/abs/notes.md"]\n'
        "max_chars = 4000\n"
    ))
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    cfg = config.load_instructions_config()
    assert cfg.enabled is False
    assert cfg.files == ("AGENTS.md", "CLAUDE.md")
    assert cfg.paths == ("docs/team.md", "C:/abs/notes.md")
    assert cfg.max_chars == 4000


def test_instructions_config_files_empty_list_disables_default_names(tmp_path, monkeypatch):
    """显式空列表 = 不探测默认文件名（仅保留 paths 追加文件）。"""
    home = _make_home(tmp_path, toml_text="[instructions]\nfiles = []\n")
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    assert config.load_instructions_config().files == ()


def test_instructions_config_invalid_values_degrade(tmp_path, monkeypatch, capsys):
    home = _make_home(tmp_path, toml_text=(
        "[instructions]\n"
        'enabled = "yes"\n'
        'files = "AGENTS.md"\n'
        "max_chars = 0\n"
        "paths = 3\n"
    ))
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    cfg = config.load_instructions_config()
    assert cfg.enabled is True
    assert cfg.files == ("AGENTS.md",)
    assert cfg.paths == ()
    assert cfg.max_chars == 8000
    assert "警告" in capsys.readouterr().out


def test_instructions_config_non_table_section_warns(tmp_path, monkeypatch, capsys):
    home = _make_home(tmp_path, toml_text="instructions = 3\n")
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    assert config.load_instructions_config() == config.InstructionsConfig()
    assert "警告" in capsys.readouterr().out


# ---------- 启动校验：缺 API Key 时的报错与修复指引 ----------

def test_missing_key_raises_with_hint(monkeypatch):
    """完全没配 key：抛 ConfigError 且指引说清两种配置途径。"""
    monkeypatch.setattr(config, "KEY", "")
    with pytest.raises(config.ConfigError) as excinfo:
        config.ensure_api_key()
    message = str(excinfo.value)
    assert "smith setup" in message
    assert "SMITHCODE_KEY" in message


def test_blank_key_also_rejected(monkeypatch):
    """纯空白的 key 视同缺失，不能漏到 OpenAI SDK 那边才报错。"""
    monkeypatch.setattr(config, "KEY", "   ")
    with pytest.raises(config.ConfigError):
        config.ensure_api_key()


def test_present_key_passes(monkeypatch):
    monkeypatch.setattr(config, "KEY", "sk-test")
    config.ensure_api_key()  # 不抛即通过
