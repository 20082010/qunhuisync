"""大文件上传失败的回归测试。

三个真实踩过的坑：
1. ReadTimeout——普通 60 秒超时对 1GB 以上的文件不够
2. SaveFileError——文件名里有 emoji 就必然失败
3. 错误信息里的「已传 0」是假的（offset 只有传完才推进）
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from qunhuisync import pc189  # noqa: E402
from qunhuisync.pc189 import Pc189Client, Pc189Error, human_bytes  # noqa: E402
from qunhuisync.util import has_emoji, sanitize_remote_name  # noqa: E402


class FakeResponse:
    def __init__(self, payload=None, text="", status_code=200):
        self._payload = payload
        self.text = text
        self.status_code = status_code

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class UploadServer:
    def __init__(self, total_size=1024):
        self.total_size = total_size
        self.put_count = 0
        self.timeout_used = None
        # 服务端登记的字节数（真实服务端会跟着收到的数据增长）
        self.registered = 0

    def post_form(self, url, form):
        if "createUploadFile" in url:
            return {
                "fileUploadUrl": "https://upload.example/put",
                "fileCommitUrl": "https://upload.example/commit",
                "uploadFileId": "UF1",
                "fileDataExists": 0,
            }
        if "commit" in url:
            return {"file": {"userFileId": "F9"}}
        raise AssertionError("未预期的请求: %s" % url)

    def status(self, upload_id):
        return {"size": self.registered}

    def put(self, url, headers=None, content=None, timeout=None):
        self.put_count += 1
        self.timeout_used = timeout
        body = b"".join(content) if hasattr(content, "__iter__") else (content or b"")
        self.registered += len(body)
        return FakeResponse(None, "", 200)


def make_client(server):
    client = Pc189Client({"sessionKey": "SK", "sessionSecret": "S" * 32})
    client._post_form = lambda url, form, attempts=3: server.post_form(url, form)
    client._upload_status = lambda upload_id: server.status(upload_id)
    client.http = server
    return client


def make_file(size):
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    handle.write(b"X" * size)
    handle.close()
    return handle.name


# ---------------- 超时 ----------------

def test_upload_uses_long_timeout():
    """上传必须用长超时。

    普通接口的 60 秒读超时对 1GB 以上的文件不够——传完后服务端还要处理，
    等响应就会超时。实测用户那边 1.3GB/1.5GB/1.9GB 全部报 ReadTimeout。
    """
    server = UploadServer()
    client = make_client(server)
    path = make_file(1024)
    try:
        client.upload_whole(path, "-11")
    finally:
        os.unlink(path)

    timeout = server.timeout_used
    assert timeout is not None, "上传请求应该显式指定超时"
    assert timeout.read >= 600, "读超时至少 10 分钟，实际 %s" % timeout.read
    assert timeout.write >= 600, "写超时至少 10 分钟，实际 %s" % timeout.write
    assert timeout.connect <= 60, "连接超时不用放宽"


def test_normal_request_keeps_short_timeout():
    """普通接口调用仍用短超时——接口卡住时要快速失败，界面才有反应。"""
    client = Pc189Client({"sessionKey": "SK", "sessionSecret": "S" * 32})
    assert client.http.timeout.read == 60


def test_upload_timeout_is_configurable():
    client = Pc189Client({"sessionKey": "SK", "sessionSecret": "S" * 32}, timeout=30.0)
    timeout = client._upload_timeout()
    assert timeout.connect == 30.0
    assert timeout.read >= 600


# ---------------- emoji ----------------

def test_emoji_filename_is_detected():
    assert has_emoji("南瓜\U0001F383.mp4") is True
    assert has_emoji("normal.mp4") is False
    assert has_emoji("测试：文件.mp4") is False


def test_emoji_is_replaced_with_underscore():
    """天翼存不了带 emoji 的文件名，必须在上传前替换掉。"""
    assert sanitize_remote_name("南瓜\U0001F383.mp4") == "南瓜_.mp4"
    assert sanitize_remote_name("normal.mp4") == "normal.mp4"
    # 中文、日文、标点都不动
    assert sanitize_remote_name("测试：文件.mp4") == "测试：文件.mp4"
    assert sanitize_remote_name("テスト.mp4") == "テスト.mp4"


def test_multiple_emoji_all_replaced():
    assert sanitize_remote_name("a\U0001F383b\U0001F383c.mp4") == "a_b_c.mp4"


def test_sanitize_keeps_extension():
    assert sanitize_remote_name("x\U0001F383.mp4").endswith(".mp4")


def test_sanitize_empty_name():
    assert sanitize_remote_name("") == ""
    assert sanitize_remote_name("\U0001F383") == "_"


# ---------------- 错误信息 ----------------

def test_error_message_shows_real_bytes_sent():
    """失败时要报真实已发字节数。

    之前报的是 offset，而 offset 只有整个文件传完才推进，
    中途失败时恒为 0，用户完全看不出传了多少。
    """
    server = UploadServer(total_size=1024)

    def failing_put(url, headers=None, content=None, timeout=None):
        server.put_count += 1
        # 消费一部分就断开
        for piece in content:
            break
        raise httpx.ReadTimeout("模拟超时")

    server.put = failing_put
    client = make_client(server)
    path = make_file(1024)
    try:
        with pytest.raises(Pc189Error) as exc:
            client.upload_whole(path, "-11")
    finally:
        os.unlink(path)

    message = str(exc.value)
    assert "已发" in message, "应报「已发多少字节」：%s" % message
    assert "B" in message, "应带可读单位：%s" % message


def test_human_bytes_format():
    assert human_bytes(0) == "0.0 B"
    assert human_bytes(1024) == "1.0 KB"
    assert human_bytes(1024 ** 2) == "1.0 MB"
    assert human_bytes(1024 ** 3) == "1.0 GB"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
