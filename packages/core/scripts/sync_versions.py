"""同步三处版本号（core `__init__.py` 是唯一真相）。

用法：`uv run python scripts/sync_versions.py`（packages/core 下）。
桌面端三处（package.json / tauri.conf.json / Cargo.toml 的 package.version，
不含依赖 pin）与 core 对齐。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CORE_INIT = REPO_ROOT / "packages" / "core" / "src" / "smithcode" / "__init__.py"


def core_version() -> str:
    text = CORE_INIT.read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("core __init__.py 里找不到 __version__")
    return match.group(1)


def sync(version: str) -> list[str]:
    changed: list[str] = []
    desktop = REPO_ROOT / "packages" / "desktop"

    package_json = desktop / "package.json"
    data = json.loads(package_json.read_text(encoding="utf-8"))
    if data.get("version") != version:
        data["version"] = version
        package_json.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        changed.append("package.json")

    tauri_conf = desktop / "src-tauri" / "tauri.conf.json"
    data = json.loads(tauri_conf.read_text(encoding="utf-8"))
    if data.get("version") != version:
        data["version"] = version
        tauri_conf.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        changed.append("tauri.conf.json")

    cargo_toml = desktop / "src-tauri" / "Cargo.toml"
    text = cargo_toml.read_text(encoding="utf-8")
    new_text, count = re.subn(
        r'(?m)^version\s*=\s*"[^"]+"',
        f'version = "{version}"',
        text,
        count=1,
    )
    if count and new_text != text:
        cargo_toml.write_text(new_text, encoding="utf-8")
        changed.append("Cargo.toml")
    return changed


def main(argv: list[str] | None = None) -> int:
    check = "--check" in (argv or sys.argv[1:])
    version = core_version()
    if check:
        mismatched: list[str] = []
        desktop = REPO_ROOT / "packages" / "desktop"
        data = json.loads((desktop / "package.json").read_text(encoding="utf-8"))
        if data.get("version") != version:
            mismatched.append("package.json")
        data = json.loads((desktop / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
        if data.get("version") != version:
            mismatched.append("tauri.conf.json")
        text = (desktop / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
        if f'version = "{version}"' not in text.split("[build-dependencies]")[0]:
            mismatched.append("Cargo.toml")
        serve_text = (REPO_ROOT / "packages" / "core" / "src" / "smithcode" / "serve.py").read_text(
            encoding="utf-8"
        )
        # server_version 已从 __version__ 运行时派生：硬编码旧值即漂移
        if 'SmithServe/0.9"' in serve_text and version != "0.9":
            mismatched.append("serve.py(server_version 硬编码)")
        if mismatched:
            print(f"版本不一致（core={version}）：{mismatched}，请跑 scripts/sync_versions.py")
            return 1
        print(f"版本一致：{version}")
        return 0
    changed = sync(version)
    print(f"已同步到 {version}：{changed or '无变化'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
