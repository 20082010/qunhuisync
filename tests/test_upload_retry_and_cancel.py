"""上传重传与取消的回归测试。

1. 服务端明确拒绝时，不能反复重传（用户看到的「反复上传」）
2. HTTP 4xx/5xx 是明确拒绝，不该重传；413 要给出人话提示
3. 单文件上限以**服务端说的**为准（普通用户 2G、黄金会员 200G），
   不能写死 2G 把黄金会员的大文件拦下来
4. 取消要立即生效（传输阶段和哈希阶段都要查）
"""
from __future__ import annotations

import os
import sys
import tempfile

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from qunhuisync import pc189  # noqa: E402
from qunhuisync.pc189 import Pc189Cancelled, Pc189Client, Pc189Error  # noqa: E402


class FakeResponse:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code

    def json(self):
        raise ValueError("not json")


XML_ERROR = """<?xml version="1.0" encoding="UTF-8"?><error>
   <code>UploadFileCompeletedError</code>
   <message>uploadDataClient() - requestId=xxx, fileStatus=2, data upload completed.</message>
</error>"""

XML_SUCCESS = """<?xml version="1.0" encoding="UTF-8"?><file>
   <id>123</id><name>a.mp4</name><size>1024</size>
</file>"""


class Server:
    """模拟上传接口。

    真实服务端按「收到的字节数」登记进度（``getUploadFileStatus`` 返回
    登记量），所以这里也要跟着登记，否则客户端会以为没进展。
    """

    def __init__(self, put_text="", put_status=200, fail_put_times=0,
                 max_filesize=0):
        self.put_text = put_text
        self.put_status = put_status
        self.fail_put_times = fail_put_times
        self.max_filesize = max_filesize
        self.put_count = 0
        self.create_count = 0
        self.commit_count = 0
        self.registered = 0

    def post_form(self, url, form):
        if "createUploadFile" in url:
            self.create_count += 1
            return {
                "fileUploadUrl": "https://upload.example/put",
                "fileCommitUrl": "https://upload.example/commit",
                "uploadFileId": "UF%d" % self.create_count,
                "fileDataExists": 0,
            }
        if "commit" in url:
            self.commit_count += 1
            return XML_SUCCESS
        raise AssertionError("未预期的请求: %s" % url)

    def get(self, url, **kwargs):
        """模拟 getUserInfo（返回账号的单文件上限）。"""
        return {"res_code": 0, "maxFilesize": self.max_filesize}

    def status(self, upload_id):
        return {"size": self.registered}

    def put(self, url, headers=None, content=None, timeout=None):
        self.put_count += 1
        if self.fail_put_times and self.put_count <= self.fail_put_times:
            raise httpx.RemoteProtocolError("模拟断线")
        length = int(headers.get("Content-Length", "0"))
        # 服务端登记进度
        self.registered += length
        if self.put_text:
            return FakeResponse(self.put_text, self.put_status)
        return FakeResponse("", 200)


def make_client(server):
    client = Pc189Client({"sessionKey": "SK", "sessionSecret": "S" * 32})
    client._post_form = lambda url, form, attempts=3: server.post_form(url, form)
    client._upload_status = lambda upload_id: server.status(upload_id)
    client.request = lambda url, method="GET", params=None, query=None: server.get(url)
    client.http = server
    return client


def make_file(size):
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    handle.write(b"X" * size)
    handle.close()
    return handle.name


# ---------------- 服务端明确拒绝：不重传 ----------------

def test_server_rejection_does_not_retry():
    """服务端明确拒绝时，不能反复重传。

    之前 PUT 返回 HTTP 200 + XML 错误，代码当成成功了，
    commit 失败后又重传——用户看到的就是「同一个文件反复上传」。
    """
    server = Server(put_text=XML_ERROR)
    client = make_client(server)
    path = make_file(1024)
    try:
        with pytest.raises(Pc189Error) as exc:
            client.upload_whole(path, "-11")
    finally:
        os.unlink(path)

    assert server.put_count == 1, "服务端拒绝后不该重传，实际 %d 次" % server.put_count
    message = str(exc.value)
    assert "服务端" in message, message


def test_http_4xx_does_not_retry():
    """HTTP 4xx 是明确拒绝，不该重传。"""
    server = Server(put_text="error", put_status=403)
    client = make_client(server)
    path = make_file(1024)
    try:
        with pytest.raises(Pc189Error):
            client.upload_whole(path, "-11")
    finally:
        os.unlink(path)
    assert server.put_count == 1, "不该重传，实际 %d 次" % server.put_count


def test_http_413_gives_clear_message():
    """413（请求体太大）要给出人话提示，且不重传。

    实测：天翼的对象存储前端（nginx）只收 2GB 以内的请求体。
    更大的文件会被直接拒绝——重传多少次都没用。
    """
    server = Server(put_text="<html>413 Request Entity Too Large</html>",
                    put_status=413)
    client = make_client(server)
    path = make_file(1024)
    try:
        with pytest.raises(Pc189Error) as exc:
            client.upload_whole(path, "-11")
    finally:
        os.unlink(path)

    assert server.put_count == 1, "413 不该重传，实际 %d 次" % server.put_count
    message = str(exc.value)
    assert "2GB" in message, "要说明单次上传的上限：%s" % message


def test_network_error_does_retry():
    """网络断线要重试。

    断线时服务端可能已经收了部分数据，所以重试前**先查服务端登记到哪**，
    从那儿接着传，而不是盲目重传整个文件。
    """
    server = Server(fail_put_times=1)
    client = make_client(server)
    path = make_file(1024)
    try:
        client.upload_whole(path, "-11")
    finally:
        os.unlink(path)
    assert server.put_count == 2, "断线后应该重试，实际 %d 次" % server.put_count
    assert server.commit_count == 1, "重试后应该走到提交"


# ---------------- 单文件上限以服务端为准 ----------------

def test_limit_comes_from_server_not_hardcoded():
    """上限必须问服务端，不能写死 2G。

    天翼按会员等级给额度：普通用户 2G、黄金会员 200G。
    写死 2G 会把黄金会员的 4G 文件全拦下来，用户根本传不上去。
    """
    server = Server(max_filesize=200 * 1024 ** 3)
    client = make_client(server)
    path = make_file(1024)

    original = os.path.getsize
    os.path.getsize = lambda p: 4 * 1024 ** 3   # 假装是 4G 的文件
    try:
        client.upload_whole(path, "-11")
    finally:
        os.path.getsize = original
        os.unlink(path)

    # 4G 文件会分段传（每次 <2GB），关键是**不能被拦下来**：
    # 服务端说能传 200G，就该真的把它传完。
    assert server.commit_count == 1, "服务端说能传 200G，就不该拦下来"
    assert server.put_count >= 2, "4G 文件应该分段传"


def test_unknown_limit_does_not_block_upload():
    """查不到上限时**不能拦**。

    ``getUserInfo.action`` 偶尔会返回 InvalidSessionKey（实测同一秒内
    时好时坏）。这时候如果退回 2G，就会把服务端明明允许的大文件
    误判成超限给拦下来——用户看到的是「我的 4G 文件传不上去」。
    拿不准就不拦，交给服务端自己判断。
    """
    server = Server(max_filesize=0)     # 查不到
    client = make_client(server)
    path = make_file(1024)

    original = os.path.getsize
    os.path.getsize = lambda p: 4 * 1024 ** 3
    try:
        client.upload_whole(path, "-11")
    finally:
        os.path.getsize = original
        os.unlink(path)

    assert server.commit_count == 1, "查不到上限时不该拦下来"


def test_oversized_file_is_rejected_early():
    """真的超过账号上限时提前报错，不浪费带宽。"""
    server = Server(max_filesize=2 * 1024 ** 3)
    client = make_client(server)
    path = make_file(1024)

    original = os.path.getsize
    os.path.getsize = lambda p: 4 * 1024 ** 3   # 账号上限只有 2G
    try:
        with pytest.raises(Pc189Error) as exc:
            client.upload_whole(path, "-11")
    finally:
        os.path.getsize = original
        os.unlink(path)

    assert server.put_count == 0, "超限文件不该真的上传"
    message = str(exc.value)
    assert "上限" in message, "错误信息要说明上限：%s" % message


# ---------------- 取消立即生效 ----------------

def test_cancel_checked_in_body_stream():
    """body_stream 里每读一块都要检查取消。

    取消检查放在重试循环开头的话，一次 PUT 要传完整个文件才生效。
    """
    server = Server()
    client = make_client(server)
    path = make_file(1024 * 1024)

    calls = {"n": 0}

    def should_cancel():
        calls["n"] += 1
        return calls["n"] > 5   # 第 5 次就返回 True

    try:
        with pytest.raises(Pc189Cancelled):
            client.upload_whole(path, "-11", should_cancel=should_cancel)
    finally:
        os.unlink(path)

    assert calls["n"] >= 5, "取消检查次数太少（%d 次），说明只在开头检查了" % calls["n"]


def test_cancel_during_hash_stage():
    """哈希阶段也要能取消。

    2GB 的文件在群晖上算 MD5 要几十秒，这期间用户点「取消此文件」，
    如果哈希阶段不查取消，就得干等几十秒才停——用户看到的就是「取消没反应」。
    """
    server = Server()
    client = make_client(server)
    path = make_file(2 * 1024 * 1024)

    calls = {"n": 0}

    def should_cancel():
        calls["n"] += 1
        return calls["n"] > 3

    try:
        with pytest.raises(Pc189Cancelled):
            client.upload_whole(path, "-11", should_cancel=should_cancel)
    finally:
        os.unlink(path)

    assert calls["n"] > 1, "哈希阶段没有检查取消（只查了 %d 次）" % calls["n"]


def test_cancel_is_not_retried():
    """取消不能被当成网络错误重传。

    取消时抛的是 Pc189Cancelled，不该落进重试循环再传一遍。
    """
    server = Server()
    client = make_client(server)
    path = make_file(1024 * 1024)

    def should_cancel():
        return True    # 一开始就取消

    try:
        with pytest.raises(Pc189Cancelled):
            client.upload_whole(path, "-11", should_cancel=should_cancel)
    finally:
        os.unlink(path)

    assert server.put_count == 0, "已经取消就不该发出 PUT，实际 %d 次" % server.put_count


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
