"""本地调试用：用保存的会话测试上传。

只操作根目录下的 test 文件夹，不碰其它目录。

用法：
    python build/upload_test.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from qunhuisync import pc189  # noqa: E402

SESSION_FILE = os.path.join(ROOT, "build", "dev-session.json")
TEST_DIR = "test"


def load_session():
    with open(SESSION_FILE, encoding="utf-8") as handle:
        return json.load(handle)


def main() -> int:
    if not os.path.exists(SESSION_FILE):
        print("没有会话文件，请先运行 build/login_test.py 扫码登录")
        return 1
    session = load_session()
    client = pc189.Pc189Client(session)

    try:
        print("=== 1. 容量 ===")
        info = client.disk_info()
        cap = info.get("cloudCapacityInfo") or {}
        print("  已用 %.1f GB / 共 %.1f GB"
              % (cap.get("usedSize", 0) / 1024 ** 3, cap.get("totalSize", 0) / 1024 ** 3))

        print("\n=== 2. 根目录列表（确认 test 是否已存在）===")
        root_items = client.list_files("-11")
        for item in root_items[:20]:
            print("  %-30s %s" % (item["name"], "dir" if item["is_folder"] else item["size"]))

        print("\n=== 3. 建 test 文件夹 ===")
        folder_id = ""
        for item in root_items:
            if item["name"] == TEST_DIR and item["is_folder"]:
                folder_id = str(item["id"])
                print("  test 已存在，id=%s" % folder_id)
                break
        if not folder_id:
            folder_id = client.create_folder("-11", TEST_DIR)
            print("  创建 test，id=%s" % folder_id)
        if not folder_id:
            print("  !! 拿不到 test 目录 id")
            return 2

        print("\n=== 4. 上传测试文件到 test/ ===")
        work = os.path.join(ROOT, "build", "tmp-upload")
        os.makedirs(work, exist_ok=True)
        target = os.path.join(work, "probe-3mb.bin")
        payload = os.urandom(3 * 1024 * 1024)      # 3MB -> 1 片
        with open(target, "wb") as handle:
            handle.write(payload)

        started = time.time()
        progress = []

        def on_progress(done):
            progress.append(done)
            print("    进度 %d / %d 字节" % (done, len(payload)))

        try:
            file_id = client.upload(target, folder_id, "probe-3mb.bin",
                                    on_progress=on_progress)
            print("  上传成功，文件 id=%s，用时 %.1f 秒"
                  % (file_id, time.time() - started))
        except Exception as exc:
            print("  !! 上传失败：%s" % exc)
            print("     类型：%s" % type(exc).__name__)
            return 3

        print("\n=== 5. 回读校验 ===")
        items = client.list_files(folder_id)
        for item in items:
            print("  %-24s %10d 字节  id=%s" % (item["name"], item["size"], item["id"]))
            if item["name"] == "probe-3mb.bin":
                same = item["size"] == len(payload)
                print("  大小一致：%s（本地 %d / 云端 %d）"
                      % (same, len(payload), item["size"]))
                if same and item.get("md5"):
                    local_md5 = hashlib.md5(payload).hexdigest().upper()
                    print("  MD5 一致：%s" % (item["md5"].upper() == local_md5))

        print("\n=== 6. 清理测试文件 ===")
        for item in items:
            if item["name"] == "probe-3mb.bin":
                client.delete_file(str(item["id"]), item["name"], False)
                print("  已删除 probe-3mb.bin")
        os.unlink(target)

        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
