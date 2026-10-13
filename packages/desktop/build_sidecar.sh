#!/bin/bash
# 打 sidecar 二进制（Tauri externalBin 用）：pyinstaller --onefile 打 smithcode.cli:main。
# 产物：src-tauri/binaries/smith-<triple>（gitignore，现打现用，不提交）。
# 用法：在 packages/desktop 下执行 ./build_sidecar.sh
#
# 可覆盖的环境变量：
#   SMITH_VENV / VIRTUAL_ENV   venv 根（默认找仓库根 .venv；不写死 python 版本）
#   SMITH_BUILD_DIR            构建目录（默认 mktemp，用完即删）
#   TAURI_ENV_TARGET_TRIPLE    目标三元组（Tauri 交叉编译时自动带；默认取 rustc host）
set -euo pipefail

DESKTOP_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$DESKTOP_DIR/../.." && pwd)"
CORE_DIR="$(cd "$DESKTOP_DIR/../core" && pwd)"
OUT_DIR="$DESKTOP_DIR/src-tauri/binaries"

# 找 site-packages 而不写死 `python3.14`：python 小版本一升，硬编码就失效。
find_site_packages() {
    local root="$1"
    [ -d "$root/lib" ] || return 1
    local candidate
    for candidate in "$root"/lib/python*/site-packages; do
        if [ -d "$candidate" ]; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    return 1
}

VENV_SITE=""
if [ -n "${SMITH_VENV:-}" ]; then
    VENV_SITE="$(find_site_packages "$SMITH_VENV" || true)"
elif [ -n "${VIRTUAL_ENV:-}" ]; then
    VENV_SITE="$(find_site_packages "$VIRTUAL_ENV" || true)"
fi
if [ -z "$VENV_SITE" ]; then
    VENV_SITE="$(find_site_packages "$REPO_ROOT/.venv" || true)"
fi
if [ -z "$VENV_SITE" ]; then
    echo "找不到 venv site-packages：设 SMITH_VENV / VIRTUAL_ENV，或先在仓库根跑 uv sync 建 .venv" >&2
    exit 1
fi

command -v pyinstaller >/dev/null 2>&1 || {
    echo "缺 pyinstaller：在 venv 里 pip install pyinstaller" >&2
    exit 1
}

# 构建目录：默认 mktemp（并发/多次构建互不干扰），显式给了才复用并保留
if [ -n "${SMITH_BUILD_DIR:-}" ]; then
    BUILD_DIR="$SMITH_BUILD_DIR"
    mkdir -p "$BUILD_DIR"
    CLEANUP=0
else
    BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/smith-build.XXXXXX")"
    CLEANUP=1
fi
cleanup() { [ "$CLEANUP" = 1 ] && rm -rf "$BUILD_DIR"; }
trap cleanup EXIT

# 目标三元组：交叉编译时以 Tauri 传入的为准，否则取 rustc host（不硬编码架构）
TRIPLE="${TAURI_ENV_TARGET_TRIPLE:-}"
if [ -z "$TRIPLE" ]; then
    TRIPLE="$(rustc -vV 2>/dev/null | awk '/^host:/{print $2}')"
fi
TRIPLE="${TRIPLE:-x86_64-unknown-linux-gnu}"

ENTRY="$BUILD_DIR/smith_entry.py"
mkdir -p "$OUT_DIR" "$BUILD_DIR"
cat > "$ENTRY" <<'EOF'
from smithcode.cli import main
main()
EOF

export PATH="$HOME/.local/bin:$PATH"
# hidden-import / collect-all 与 core 的运行时依赖对齐（openai / httpx2 / mcp 是
# 动态导入点，pyinstaller 静态分析抓不到）。新增核心依赖时要同步这里。
pyinstaller --noconfirm --clean --onefile --name smith \
    --distpath "$BUILD_DIR" --workpath "$BUILD_DIR/work" --specpath "$BUILD_DIR" \
    --paths "$CORE_DIR/src" --paths "$VENV_SITE" \
    --hidden-import openai --hidden-import httpx2 --hidden-import mcp \
    --collect-all smithcode --collect-all openai --collect-all httpx2 --collect-all mcp \
    --collect-data textual --collect-data prompt_toolkit \
    "$ENTRY"

mv -f "$BUILD_DIR/smith" "$OUT_DIR/smith-$TRIPLE"
chmod +x "$OUT_DIR/smith-$TRIPLE"
"$OUT_DIR/smith-$TRIPLE" --version
echo "sidecar 就绪：$OUT_DIR/smith-$TRIPLE"
