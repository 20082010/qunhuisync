#!/bin/sh
# 为服务账号授予目录读写权限（需要 root）
#
# 用法：
#   sudo /var/packages/qunhuisync/target/scripts/grant-access.sh /volume1/photos
#   sudo /var/packages/qunhuisync/target/scripts/grant-access.sh /volume1/photos /volume1/videos
#
# DSM 7 强制套件降权运行，服务账号默认没有任何共享目录权限。
# 本脚本用 synoacltool 授予 rw 权限，是官方推荐的手动授权方式。
#
# 本脚本独立运行（不 source common.sh）：它同时被放到 target/scripts/ 下，
# 那个目录没有 common.sh。

PKG="qunhuisync"
SERVICE_USER="sc-${PKG}"

if [ "$(id -u)" != "0" ]; then
    echo "请用 root 运行：sudo $0 <目录>"
    exit 1
fi

if ! command -v synoacltool >/dev/null 2>&1; then
    echo "找不到 synoacltool，无法设置 ACL"
    echo "请改用 DSM 界面：控制面板 → 共享文件夹 → 权限 → 系统内部用户 → ${SERVICE_USER} → 读写"
    exit 1
fi

if [ "$#" -eq 0 ]; then
    echo "用法: sudo $0 <目录> [更多目录...]"
    exit 1
fi

RC=0
for target in "$@"; do
    if [ ! -d "${target}" ]; then
        echo "跳过（目录不存在）：${target}"
        RC=1
        continue
    fi
    # 父目录需要遍历权限，否则无法进入子目录
    parent="$(dirname "${target}")"
    synoacltool -enforce "allow" "user:${SERVICE_USER}:r-xpd---:fd--" "${parent}" >/dev/null 2>&1
    if synoacltool -enforce "allow" "user:${SERVICE_USER}:rwxpd---:fd--" "${target}" >/dev/null 2>&1; then
        echo "已授权：${target}"
    else
        echo "授权失败：${target}"
        RC=1
    fi
done

if [ "${RC}" = "0" ]; then
    echo "完成。服务账号 ${SERVICE_USER} 现在可以读写上述目录。"
else
    echo "部分目录授权失败，可改在 DSM 界面手动设置："
    echo "  控制面板 → 共享文件夹 → 权限 → 系统内部用户 → ${SERVICE_USER} → 读写"
fi
exit "${RC}"
