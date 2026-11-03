#!/bin/sh
# 移除 add-proxy.sh 写入的 DSM 反向代理配置
. "$(dirname "$0")/common.sh"

CONF="/usr/local/etc/nginx/conf.d/${PKG}.conf"
if [ -f "${CONF}" ]; then
    rm -f "${CONF}"
    echo "已移除 ${CONF}"
else
    echo "没有找到代理配置"
fi
exit 0
