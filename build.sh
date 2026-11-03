#!/bin/sh
# 群晖电信同步工具 SPK 构建入口（Linux / macOS / 群晖 SSH 下均可用）
#
# 用法：
#   ./build.sh                 完整构建
#   ./build.sh --offline       使用 build/wheels 缓存，不联网
#   ./build.sh --arch x86_64   只打包指定架构
#   ./build.sh --set-version 1.1.0
#   ./build.sh --clean         清理中间产物
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PYTHON:-python3}"

if ! command -v "${PYTHON}" >/dev/null 2>&1; then
    echo "找不到 python3，请先安装 Python 3.8+" >&2
    exit 1
fi

echo "使用 Python: $(command -v "${PYTHON}")"
"${PYTHON}" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 8) else 1)' \
    || { echo "需要 Python 3.8 及以上版本" >&2; exit 1; }

exec "${PYTHON}" "${ROOT}/build/build_spk.py" "$@"
