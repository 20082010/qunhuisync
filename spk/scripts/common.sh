#!/bin/sh
# 公共变量与函数（被其它脚本 source）

PKG="qunhuisync"
APP_NAME="群晖电信同步工具"

# DSM 7 约定：target 放只读程序文件，var 放可写数据
APP_DIR="${SYNOPKG_PKGDEST:-/var/packages/${PKG}/target}"
APP_CODE_DIR="${APP_DIR}/app"
DEPS_DIR="${APP_DIR}/deps"

# 可写数据目录：DSM 7 为 /var/packages/<pkg>/var，降权后只有这里可写
VAR_DIR="${SYNOPKG_PKGVAR:-/var/packages/${PKG}/var}"
CONFIG_DIR="${VAR_DIR}/config"
CONFIG_FILE="${CONFIG_DIR}/config.json"
PID_FILE="${VAR_DIR}/${PKG}.pid"
LOG_FILE="${SYNOPKG_TEMP_LOGFILE:-/var/log/packages/${PKG}.log}"
PORT_FILE="${CONFIG_DIR}/port"
DEFAULT_PORT=8686

# target/ 由 root 拥有，降权后不可写；缺依赖时装到这里
EXTRA_DEPS_DIR="${VAR_DIR}/deps"

# 服务账号（DSM 7 自动创建的 sc-<包名> 系统内部用户）
SERVICE_USER="sc-${PKG}"

# 授权脚本位置：target/scripts/grant-access.sh
GRANT_SCRIPT="${APP_DIR}/scripts/grant-access.sh"

get_port() {
    if [ -f "${PORT_FILE}" ]; then
        cat "${PORT_FILE}"
    else
        echo "${DEFAULT_PORT}"
    fi
}

find_python() {
    for candidate in \
        "/var/packages/Python3/target/usr/local/bin/python3" \
        "/usr/local/bin/python3" \
        "/usr/bin/python3" \
        "$(command -v python3 2>/dev/null)"
    do
        [ -x "${candidate}" ] || continue
        if "${candidate}" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 8) else 1)' 2>/dev/null; then
            echo "${candidate}"
            return 0
        fi
    done
    return 1
}

get_arch() {
    case "$(uname -m)" in
        x86_64|amd64) echo "x86_64" ;;
        aarch64|arm64) echo "aarch64" ;;
        armv7l|armv7) echo "armv7" ;;
        *) echo "" ;;
    esac
}
