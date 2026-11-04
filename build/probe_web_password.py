"""手动验证：设了密码之后，网页能打开并弹出登录框。

用真实的 HTTP 服务 + 真实浏览器（Playwright）检查：
1. 打开首页 -> 应该能看到「需要访问密码」的登录框
2. 输入错误密码 -> 应该提示「密码错误，请重新输入」
3. 输入正确密码 -> 应该进入主界面

用法：python build/probe_web_password.py
"""
from __future__ import annotations

import os
import sys
import threading
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from qunhuisync import server as server_mod  # noqa: E402
from qunhuisync.config import Config  # noqa: E402


def main() -> int:
    import tempfile

    tmp = tempfile.mkdtemp(prefix="qhs-pwd-")
    config = Config(os.path.join(tmp, "config.json"))
    config.set_server({"password": "s3cret"})

    app = server_mod.AppState(os.path.join(tmp, "config.json"))
    app.config = config

    class Bound(server_mod.Handler):
        state = app
        routes = server_mod.build_routes()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Bound)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    url = "http://127.0.0.1:%d/" % port
    print("测试地址：%s（密码 s3cret）" % url)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("没装 playwright，跳过浏览器验证")
        print("（静态文件和接口的鉴权行为已经由 tests/test_web_password.py 覆盖）")
        return 0

    errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(url, wait_until="domcontentloaded")

        # 1. 登录框应该出现
        page.wait_for_selector("#loginModal:not([hidden])", timeout=8000)
        print("[PASS] 打开网页即出现访问密码输入框")

        # 2. 密码错了应该提示
        page.fill("#loginPassword", "wrong")
        page.click("#btnLogin")
        page.wait_for_selector("#loginError:not([hidden])", timeout=8000)
        print("[PASS] 密码错误时提示「密码错误，请重新输入」")

        # 3. 密码对了应该进入
        page.fill("#loginPassword", "s3cret")
        page.click("#btnLogin")
        page.wait_for_function(
            "() => document.getElementById('loginModal').hidden === true",
            timeout=8000)
        page.wait_for_timeout(1500)
        body = page.inner_text("body")
        if "需要访问密码" in body:
            print("[FAIL] 密码正确后仍停在登录页")
            browser.close()
            return 1
        print("[PASS] 密码正确后进入主界面")

        if errors:
            print("[WARN] 浏览器控制台报错：%s" % errors[:5])
        browser.close()

    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
