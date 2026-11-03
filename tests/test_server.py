"""Web 服务端的接口测试（用真实 HTTP 请求打本地服务）。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import httpx
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)


@pytest.fixture(scope="module")
def server():
    """起一个真实的服务进程供测试使用。"""
    workdir = tempfile.mkdtemp(prefix="qhs-test-")
    config_path = os.path.join(workdir, "config.json")
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump({
            "version": 1,
            "cloud": {"username": "", "password": "", "root_folder_id": "-11",
                      "upload_threads": 3},
            "server": {"host": "127.0.0.1", "port": 0, "password": ""},
            "trash": {"retention_days": 7, "path": ""},
            "rules": [],
        }, handle)

    port = 8799
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC
    env["QHS_CONFIG"] = config_path
    process = subprocess.Popen(
        [sys.executable, "-m", "qunhuisync", "serve", "--port", str(port)],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base = "http://127.0.0.1:%d" % port
    for _ in range(60):
        try:
            httpx.get(base + "/api/state", timeout=2.0)
            break
        except Exception:
            time.sleep(0.25)
    else:
        process.kill()
        shutil.rmtree(workdir, ignore_errors=True)
        pytest.fail("服务没能启动")

    yield base

    process.kill()
    process.wait(timeout=10)
    shutil.rmtree(workdir, ignore_errors=True)


def test_state_reports_version(server):
    data = httpx.get(server + "/api/state", timeout=10).json()
    assert "version" in data
    assert "rules" in data


def test_keepalive_survives_post_without_body_read(server):
    """连续 POST 同一个连接，不能出现 501。

    HTTP/1.1 keep-alive 下，如果某个接口没读掉请求体，残留字节会被
    下一个请求当成请求行解析，报 501 Unsupported method。

    这里用「保存配置」这类纯本地接口来测，不依赖外网，速度也快。
    """
    with httpx.Client(timeout=30) as client:
        for index in range(6):
            resp = client.post(server + "/api/rules/toggle", json={"id": "nope-%d" % index})
            # 404 是预期的（规则不存在），关键是别出现 501
            assert resp.status_code in (200, 404), resp.text
            assert "501" not in resp.text
            # 紧接着用同一个连接发下一个请求
            resp = client.post(server + "/api/cloud/connect", json={})
            assert resp.status_code == 200, resp.text
            assert "ok" in resp.json()


def test_connect_without_credentials_reports_error(server):
    resp = httpx.post(server + "/api/cloud/connect", json={}, timeout=30)
    assert resp.status_code == 200
    assert resp.json().get("ok") is False


def test_password_login_endpoint_is_gone(server):
    """只用扫码登录，密码登录接口不再提供。"""
    resp = httpx.post(server + "/api/cloud/test", json={}, timeout=30)
    assert resp.status_code == 404


def test_trash_endpoints_are_gone(server):
    """回收站机制已取消，相关接口不再提供。"""
    assert httpx.get(server + "/api/trash", timeout=10).status_code == 404
    assert httpx.post(server + "/api/trash/delete", json={}, timeout=10).status_code == 404
    assert httpx.post(server + "/api/trash/purge", json={}, timeout=10).status_code == 404


def test_state_is_always_json(server):
    """/api/state 必须立刻返回可解析的 JSON——它每 4 秒被轮询一次，
    一旦卡住反向代理就会 502，界面跟着崩。"""
    with httpx.Client(timeout=10) as client:
        for _ in range(5):
            resp = client.get(server + "/api/state")
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data.get("rules"), list)
            assert isinstance(data.get("jobs"), list)


def test_config_roundtrip(server):
    saved = httpx.get(server + "/api/config", timeout=10).json()
    assert "cloud" in saved
    resp = httpx.put(server + "/api/config", json={
        "cloud": {"username": "", "password": "", "root_folder_id": "-11",
                  "upload_threads": 5},
        "server": {"password": ""},
    }, timeout=10)
    assert resp.status_code == 200
    again = httpx.get(server + "/api/config", timeout=10).json()
    assert again["cloud"]["upload_threads"] == 5


def test_qr_login_returns_image_and_polls(server):
    """二维码登录：能取到图片，轮询返回等待扫码。"""
    resp = httpx.post(server + "/api/cloud/qr", json={}, timeout=60)
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("ok") is True
    assert data.get("qr_id")
    # base64 的 PNG
    assert data.get("qr", "").startswith("iVBOR")

    poll = httpx.post(server + "/api/cloud/qr/poll",
                      json={"qr_id": data["qr_id"]}, timeout=60).json()
    assert poll.get("ok") is True
    assert poll.get("status") == -106        # 等待扫码


def test_index_page_contains_qr_login(server):
    html = httpx.get(server + "/", timeout=10).text
    assert "btnQrLogin" in html
    assert "qrBox" in html


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
