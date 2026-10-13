# SmithCode sidecar 打包（Tauri externalBin 用）。

唯一入口：`./build_sidecar.sh`（packages/desktop 下执行）。
产物：`src-tauri/binaries/smith-<target-triple>`（gitignore，不提交；
CI / 发布机上现打现用）。

流程：pyinstaller --onefile 打 `smithcode.cli:main` → 按本机 triple 重命名。
依赖：`uv tool install pyinstaller`（一次即可）。
