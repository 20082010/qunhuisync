#!/bin/sh
# 可选：把 Web 界面挂到 DSM 的 /webman/3rdparty/qunhuisync/ 路径下
# 用法： sudo /var/packages/qunhuisync/scripts/add-proxy.sh
. "$(dirname "$0")/common.sh"

CONF_DIR="/usr/local/etc/nginx/conf.d"
CONF="${CONF_DIR}/${PKG}.conf"
PORT="$(get_port)"

if [ ! -d "${CONF_DIR}" ]; then
    echo "未找到 ${CONF_DIR}，当前系统可能不支持该方式，请直接访问 http://<群晖IP>:${PORT}/"
    exit 1
fi

if [ -f "${CONF}" ]; then
    echo "代理配置已存在：${CONF}"
    exit 0
fi

cat > "${CONF}" <<EOF
location /webman/3rdparty/${PKG}/ {
    proxy_pass http://127.0.0.1:${PORT}/;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    client_max_body_size 0;
}
EOF

echo "已写入 ${CONF}，请重载 DSM Web 服务（控制面板 > 登录门户 > 高级 > 反向代理 或 重启套件）"
exit 0
