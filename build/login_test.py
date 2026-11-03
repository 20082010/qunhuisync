"""本地调试用：扫码登录并把会话存到本地。

用法：
    python build/login_test.py            # 取二维码 -> 等待扫码 -> 保存会话
    python build/login_test.py --folder   # 顺便建 test 目录

会话保存在 build/dev-session.json，后续测试脚本用
``Pc189Client`` 读它即可。
"""
from __future__ import annotations

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from qunhuisync import pc189  # noqa: E402

SESSION_FILE = os.path.join(ROOT, "build", "dev-session.json")
QR_FILE = os.path.join(ROOT, "build", "dev-qr.png")


def save_session(data: dict) -> None:
    with open(SESSION_FILE, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    print("会话已保存到 %s" % SESSION_FILE)


def main() -> int:
    print("正在获取二维码…")
    qr = pc189.Pc189Qr()
    try:
        png = qr.prepare()
    except pc189.Pc189Error as exc:
        print("获取二维码失败：%s" % exc)
        return 1

    with open(QR_FILE, "wb") as handle:
        handle.write(png)
    print("二维码图片：%s" % QR_FILE)
    print("请用「天翼云盘」App 扫描下面这个地址：")
    print("  %s" % qr.uuid)
    print()
    print("等待扫码…（最多等 5 分钟）")

    deadline = time.time() + 300
    last_status = None
    while time.time() < deadline:
        result = qr.poll()
        status = result.get("status")
        if status != last_status:
            print("  状态 %s：%s" % (status, result.get("message")))
            last_status = status
        if status == pc189.QR_SUCCESS:
            session = result["session"]
            print("\n登录成功！")
            print("  sessionKey : %s" % (session.get("sessionKey") or "")[:12])
            print("  accessToken: %s" % (session.get("accessToken") or "")[:12])
            save_session(session)

            client = pc189.Pc189Client(session)
            try:
                info = client.disk_info()
                cap = (info.get("cloudCapacityInfo") or {})
                print("  网盘容量：已用 %.1f GB / 共 %.1f GB"
                      % (cap.get("usedSize", 0) / 1024 ** 3,
                         cap.get("totalSize", 0) / 1024 ** 3))
                folder_id = client.create_folder("-11", "test")
                print("  已在根目录创建 test 文件夹，id=%s" % folder_id)
            except Exception as exc:
                print("  建 test 目录失败：%s" % exc)
            finally:
                client.close()
            return 0
        if status == pc189.QR_EXPIRED:
            print("二维码已过期，请重试")
            return 2
        time.sleep(2)

    print("超时未扫码")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
