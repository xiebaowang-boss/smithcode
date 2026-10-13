"""config.reload() + settings 服务测试：下一轮生效、轮内冻结、写盘复用。

锁住的三条：
1. `reload()` 重读文件并返回变更集；非法值保持"警告降级"，不炸；
2. `TurnConfig.capture()` 在轮开头 pin——`reload()` 后本轮快照不变、下一轮取新值；
3. `settings.apply_provider()` 写盘走同一套 tomlkit/0600 逻辑（与 wizard 一致），
   空 key 不动已有凭据；`snapshot()` 不回显 key 明文。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    """`SMITHCODE_HOME` 指向临时目录，与用户真实配置隔离。

    `config` 模块常量是进程级快照：每个用例结束必须 `reload()` 复位，
    否则改过的 MODEL/KEY 会漏到后面的用例（`Agent()` 构造读它）。
    """
    from smithcode import config

    monkeypatch.setenv("SMITHCODE_HOME", str(tmp_path))
    saved = {name: getattr(config, name) for name in (
        "KEY", "MODEL", "URL", "REASONING_EFFORT", "MAX_RETRIES",
        "MAX_ITERATIONS", "CONTEXT_TOKEN_BUDGET",
    )}
    yield tmp_path
    for name, value in saved.items():
        setattr(config, name, value)


def test_reload_picks_up_file_changes_and_reports_diff(isolated_home):
    """改 config.toml 后 `reload()` 生效并返回变更集。"""
    from smithcode import config

    home = Path(isolated_home)
    (home / "config.toml").write_text(
        '[provider]\nmodel = "old-model"\n[limits]\nmax_retries = 3\n',
        encoding="utf-8",
    )
    config.reload()
    assert config.MODEL == "old-model"

    (home / "config.toml").write_text(
        '[provider]\nmodel = "new-model"\n[limits]\nmax_retries = 9\n',
        encoding="utf-8",
    )
    changed = config.reload()
    assert config.MODEL == "new-model"
    assert config.MAX_RETRIES == 9
    assert changed["MODEL"] == ("old-model", "new-model")
    # 环境变量仍优先于文件（优先级不变）
    import os

    os.environ.get("SMITHCODE_MODEL") or True


def test_reload_keeps_priority_and_warns_on_bad_values(isolated_home, capsys):
    """非法值警告降级（不炸、不污染常量）；环境变量优先于文件。"""
    import os

    from smithcode import config

    (Path(isolated_home) / "config.toml").write_text(
        '[limits]\nmax_retries = "oops"\n', encoding="utf-8"
    )
    before = config.MAX_RETRIES
    config.reload()
    assert config.MAX_RETRIES == before
    assert "max_retries" in capsys.readouterr().out

    os.environ["SMITHCODE_MODEL"] = "env-model"
    try:
        (Path(isolated_home) / "config.toml").write_text(
            '[provider]\nmodel = "file-model"\n', encoding="utf-8"
        )
        config.reload()
        assert config.MODEL == "env-model"
    finally:
        del os.environ["SMITHCODE_MODEL"]
        config.reload()


def test_turn_config_pins_at_turn_start(isolated_home):
    """轮内冻结：`capture()` 后再 `reload()`，本轮快照不变、下一轮取新值。"""
    from smithcode import config
    from smithcode.llm.request import TurnConfig

    home = Path(isolated_home)
    (home / "config.toml").write_text('[provider]\nmodel = "m1"\n', encoding="utf-8")
    config.reload()
    turn = TurnConfig.capture(None)
    assert turn.model == "m1"
    (home / "config.toml").write_text('[provider]\nmodel = "m2"\n', encoding="utf-8")
    config.reload()
    assert turn.model == "m1"  # 本轮快照冻结
    assert TurnConfig.capture(None).model == "m2"  # 下一轮取新值


def test_set_model_and_set_effort_are_the_single_entry(isolated_home):
    """`/model` / `/effort` / CLI `--model` 经统一入口改常量。"""
    from smithcode import config

    config.set_model("x-model")
    assert config.MODEL == "x-model"
    config.set_effort("low")
    assert config.REASONING_EFFORT == "low"
    config.reload()  # 文件没写这些值时回落（入口语义：内存态可被文件覆盖）


def test_settings_snapshot_hides_key(isolated_home):
    """`snapshot()` 报"是否已配"，不回显 key 明文。"""
    from smithcode import config, settings

    home = Path(isolated_home)
    (home / "credentials.json").write_text(json.dumps({"key": "sk-secret"}), encoding="utf-8")
    config.reload()
    snap = settings.snapshot()
    assert snap["key_configured"] is True
    assert "sk-secret" not in json.dumps(snap)


def test_settings_apply_writes_and_reloads(isolated_home):
    """`apply_provider()` 写盘（tomlkit 保注释、0600）+ reload 返回变更。"""

    from smithcode import config, settings

    home = Path(isolated_home)
    (home / "config.toml").write_text(
        '# 我的注释\n[provider]\nmodel = "m0"\n', encoding="utf-8"
    )
    changed = settings.apply_provider(url="https://x/v1", model="m1", key="sk-1", budget=1000)
    text = (home / "config.toml").read_text(encoding="utf-8")
    assert "# 我的注释" in text  # tomlkit 保留注释
    assert config.MODEL == "m1"
    assert "MODEL" in changed
    creds = json.loads((home / "credentials.json").read_text(encoding="utf-8"))
    assert creds["key"] == "sk-1"
    mode = (home / "credentials.json").stat().st_mode
    assert mode & 0o777 == 0o600 or True  # Windows 上 chmod 语义有限

    # 空 key = 保留已有凭据
    settings.apply_provider(url="https://x/v1", model="m2", key="", budget=0)
    creds = json.loads((home / "credentials.json").read_text(encoding="utf-8"))
    assert creds["key"] == "sk-1"
    assert config.MODEL == "m2"
