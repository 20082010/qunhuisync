"""网页访问密码的回归测试。

用户反馈：**设了访问密码之后就没有访问入口可以输入**，
打开网页直接提示「密码错误」，完全进不去。

根因：服务端以前是「**先鉴权再分发**」，于是设了密码之后连
``index.html`` 本身都返回 401 —— 浏览器只看到一句「密码错误」的
JSON，前端 JS 根本没机会运行，自然也没有输入密码的地方。

修法：
1. 静态文件（页面外壳）不鉴权，只有 ``/api/*`` 接口鉴权；
2. 前端接口收到 401 时弹**页面内**的登录框（不是 prompt()）。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from qunhuisync import server as server_mod  # noqa: E402
from qunhuisync.config import Config  # noqa: E402

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(server_mod.__file__)), "web")


@pytest.fixture()
def site(tmp_path):
    """起一个真实的 HTTP 服务，配置了访问密码。"""
    config = Config(str(tmp_path / "config.json"))
    config.set_server({"password": "s3cret"})

    app = server_mod.AppState(str(tmp_path / "config.json"))
    app.config = config

    class Bound(server_mod.Handler):
        state = app
        routes = server_mod.build_routes()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Bound)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    try:
        yield base
    finally:
        httpd.shutdown()
        httpd.server_close()


def fetch(url, password=None, headers=None):
    req = urllib.request.Request(url)
    if password:
        req.add_header("X-QHS-Password", password)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


# ---------------- 静态页面不鉴权 ----------------

def test_index_html_is_served_without_password(site):
    """设了密码之后，主页面要能打开（否则用户连输入框都看不到）。"""
    status, body = fetch(site + "/")
    assert status == 200, "主页面应该能打开，实际 %d" % status
    text = body.decode("utf-8", "replace")
    assert "<html" in text.lower(), "返回的不是网页"


def test_logs_page_is_served_without_password(site):
    """日志页面也要能打开。"""
    status, body = fetch(site + "/logs.html")
    assert status == 200, "日志页应该能打开，实际 %d" % status


def test_static_assets_are_served_without_password(site):
    """样式和脚本也要能拿到，否则页面是「裸」的。"""
    for path in ("/style.css", "/app.js"):
        status, _ = fetch(site + path)
        assert status == 200, "%s 应该能访问，实际 %d" % (path, status)


# ---------------- 接口仍然要密码 ----------------

def test_api_requires_password(site):
    """数据接口仍然受密码保护。"""
    status, body = fetch(site + "/api/state")
    assert status == 401, "没密码不该拿到数据，实际 %d" % status
    assert "密码错误" in body.decode("utf-8", "replace")


def test_api_accepts_correct_password(site):
    """密码对了就能正常访问。"""
    status, body = fetch(site + "/api/state", password="s3cret")
    assert status == 200, "密码对了应该能访问，实际 %d" % status
    data = json.loads(body.decode("utf-8"))
    assert "rules" in data


def test_api_rejects_wrong_password(site):
    """密码错了要拦住。"""
    status, _ = fetch(site + "/api/state", password="wrong")
    assert status == 401, "密码错了不该放行，实际 %d" % status


def test_config_api_requires_password(site):
    """配置接口（含网盘账号信息）也必须受保护。"""
    status, _ = fetch(site + "/api/config")
    assert status == 401, "配置接口不该裸奔，实际 %d" % status


def test_post_api_requires_password(site):
    """写操作也要密码。"""
    req = urllib.request.Request(site + "/api/rules", data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    assert status == 401, "写接口不该裸奔，实际 %d" % status


# ---------------- 没有密码时一切照旧 ----------------

def test_no_password_means_open(tmp_path):
    """没设密码时，接口直接可访问（不能被这次的改动影响）。"""
    config = Config(str(tmp_path / "config.json"))
    app = server_mod.AppState(str(tmp_path / "config.json"))
    app.config = config

    class Bound(server_mod.Handler):
        state = app
        routes = server_mod.build_routes()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Bound)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    try:
        status, _ = fetch(base + "/api/state")
        assert status == 200, "没设密码应该直接可访问，实际 %d" % status
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------- 前端要有登录框 ----------------

def test_frontend_has_login_modal():
    """主页面要有登录框（不是浏览器 prompt）。"""
    with open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8") as handle:
        html = handle.read()
    assert 'id="loginModal"' in html, "主页面缺少登录框"
    assert 'id="loginPassword"' in html, "主页面缺少密码输入框"
    assert 'id="btnLogin"' in html, "主页面缺少登录按钮"


def test_logs_page_has_login_modal():
    """日志页也要有登录框。"""
    with open(os.path.join(WEB_DIR, "logs.html"), encoding="utf-8") as handle:
        html = handle.read()
    assert 'id="loginModal"' in html, "日志页缺少登录框"
    assert 'id="loginPassword"' in html, "日志页缺少密码输入框"


def test_frontend_does_not_use_prompt():
    """不能再用 prompt()：用户反馈「没有访问入口可以输入」。

    只看代码行（注释里提到 prompt() 不算）。
    """
    for name in ("index.html", "logs.html", "app.js"):
        path = os.path.join(WEB_DIR, name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                code = line.split("//", 1)[0]
                assert "prompt(" not in code, \
                    "%s 第 %d 行还在用 prompt()" % (name, number)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
