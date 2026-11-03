"""本地预览：一条命令启动 Web 服务（默认已登录天翼网盘）。

用法：
    python build/preview.py            # 启动，浏览器打开 http://127.0.0.1:8791/
    python build/preview.py --port 8792
    python build/preview.py --fresh     # 忽略已保存的令牌，重新扫码登录
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import webbrowser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build")
CONFIG = os.path.join(BUILD, "preview-config.json")
TOKEN = os.path.join(BUILD, "token.json")
SESSION = os.path.join(BUILD, "dev-session.json")


def make_config(port: int) -> None:
    """生成预览用的配置文件。"""
    cloud = {
        "username": "", "password": "", "root_folder_id": "-11",
        "upload_threads": 2, "speed_limit": 0,
    }
    server = {"host": "127.0.0.1", "port": port, "password": ""}
    if os.path.exists(SESSION):
        with open(SESSION, encoding="utf-8") as handle:
            session = json.load(handle)
        cloud["username"] = session.get("loginName") or ""
        if os.path.exists(TOKEN) and not os.path.exists(SESSION + ".bak"):
            shutil.copy(TOKEN, SESSION + ".bak")
        with open(TOKEN, "w", encoding="utf-8") as handle:
            json.dump(session, handle, ensure_ascii=False, indent=2)
    config = {"version": 1, "cloud": cloud, "server": server, "rules": []}
    with open(CONFIG, "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(description="本地预览")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--fresh", action="store_true",
                        help="不复制已保存的令牌，重新扫码登录")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    if args.fresh and os.path.exists(TOKEN):
        os.unlink(TOKEN)

    make_config(args.port)

    sys.path.insert(0, os.path.join(ROOT, "src"))
    from qunhuisync.__main__ import main as cli_main

    url = "http://127.0.0.1:%d/" % args.port
    print("预览地址：%s" % url)
    print("按 Ctrl+C 停止")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    return cli_main(["serve", "--config", CONFIG,
                     "--host", "127.0.0.1", "--port", str(args.port)])


if __name__ == "__main__":
    raise SystemExit(main())
