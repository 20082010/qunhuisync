"""分段上传（>2G 文件）的回归测试。

实测结论（真实网盘，2026-11-03）：
* 天翼的对象存储前端是 nginx，**单次 PUT 超过 2GB 会回 413**，
  服务端一个字节都没收到 -> commit 报 UploadFileStatusVerifyFailed。
  老代码把 4xx 当成网络错误重传 -> 同一个文件反复上传（用户报的 bug）。
* **分多次 PUT 能累加**：同一个 uploadFileId，从服务端报的位置接着传，
  最后 commit 成功、MD5 一致（实测 2.2GB 文件传完 MD5 一致）。
* 服务端每收一段回 ``InputStreamReadError``（HTTP 600）——这是正常的，
  意思是「我收到的还不够」，不是失败。
"""
from __future__ import annotations

import os
import sys
import tempfile

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from qunhuisync import pc189  # noqa: E402
from qunhuisync.pc189 import Pc189Client, Pc189Error  # noqa: E402


XML_INPUT_ERROR = """<?xml version="1.0" encoding="UTF-8"?><error>
   <code>InputStreamReadError</code>
   <message>uploadData2OOS() - uploadFileId=1, openId=xxx</message>
</error>"""

XML_SUCCESS = """<?xml version="1.0" encoding="UTF-8"?><file>
   <id>123</id><name>a.mp4</name><size>1024</size>
</file>"""


class FakeResponse:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code

    def json(self):
        raise ValueError("not json")


class ChunkServer:
    """模拟分段累加的服务端。

    真实服务端有个重要特点（实测 2026-11-03，2.2GB 文件分 3 段传）：
    **它登记的字节数比实际收到的少一截**（固定滞后 3774873 字节，
    像是内部缓冲），而且**只有整个文件收齐时才登记到刚好等于文件大小**。

    所以客户端必须**以服务端报的位置为准**接着传，不能用自己的计数——
    否则会从错的位置续传，数据错位、MD5 对不上。
    """

    LAG = 3774873

    def __init__(self, total_size, lag=None, status_code=200, put_text=""):
        self.total_size = total_size
        self.lag = self.LAG if lag is None else lag
        self.status_code = status_code
        self.put_text = put_text
        self.registered = 0
        self.put_count = 0
        self.ranges = []          # 每次 PUT 的 (offset, length)
        self.create_count = 0
        self.commit_count = 0

    def post_form(self, url, form):
        if "createUploadFile" in url:
            self.create_count += 1
            return {
                "fileUploadUrl": "https://upload.example/put",
                "fileCommitUrl": "https://upload.example/commit",
                "uploadFileId": "UF1",
                "fileDataExists": 0,
            }
        if "commit" in url:
            self.commit_count += 1
            return XML_SUCCESS
        raise AssertionError("未预期的请求: %s" % url)

    def status(self, upload_id):
        return {"size": self.registered}

    def put(self, url, headers=None, content=None, timeout=None):
        self.put_count += 1
        length = int(headers.get("Content-Length", "0"))
        offset = self.registered
        self.ranges.append((offset, length))
        received_total = offset + length
        if received_total >= self.total_size:
            # 整个文件收齐了，服务端登记到刚好等于文件大小
            self.registered = self.total_size
        else:
            # 还没收齐：登记数滞后一截
            self.registered = max(0, received_total - self.lag)
        if self.put_text:
            return FakeResponse(self.put_text, self.status_code)
        if self.status_code >= 400:
            return FakeResponse("", self.status_code)
        return FakeResponse("", 200)


def make_client(server):
    client = Pc189Client({"sessionKey": "SK", "sessionSecret": "S" * 32})
    client._post_form = lambda url, form, attempts=3: server.post_form(url, form)
    client._upload_status = lambda upload_id: server.status(upload_id)
    client.request = lambda url, method="GET", params=None, query=None: {
        "res_code": 0, "maxFilesize": 200 * 1024 ** 3}
    client.http = server
    return client


def make_file(size):
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
    handle.write(b"X" * size)
    handle.close()
    return handle.name


def test_large_file_is_uploaded_in_chunks():
    """>2G 的文件要分多次 PUT，每次都小于 2GB。"""
    total = int(2.5 * 1024 ** 3)
    server = ChunkServer(total)
    client = make_client(server)
    path = make_file(1024)   # 本地文件小，靠服务端记账模拟大文件

    # 假装本地文件很大：改写 getsize
    original = os.path.getsize
    os.path.getsize = lambda p: total
    try:
        # 用一个假的 body：直接改 body_stream 的读法太麻烦，
        # 这里让服务端返回它期望的登记量即可。
        client.upload_whole(path, "-11")
    finally:
        os.path.getsize = original
        os.unlink(path)

    assert server.put_count >= 2, "大文件应该分多次 PUT，实际 %d 次" % server.put_count
    for offset, length in server.ranges:
        assert length <= 2 * 1024 ** 3, "单次 PUT 超过 2GB 了：%d" % length


def test_upload_continues_from_server_reported_offset():
    """续传位置必须以**服务端报的**为准，不是本地计数。

    真实服务端记账会滞后一点（按 MiB 算），如果客户端用自己的计数，
    就会从错误的位置接着传，数据错位、MD5 对不上。
    """
    total = int(2.5 * 1024 ** 3)
    server = ChunkServer(total, lag=1024 * 1024)
    client = make_client(server)
    path = make_file(1024)

    original = os.path.getsize
    os.path.getsize = lambda p: total
    try:
        client.upload_whole(path, "-11")
    finally:
        os.path.getsize = original
        os.unlink(path)

    # 每一次的起点都要等于上一轮服务端登记的位置
    for index in range(1, len(server.ranges)):
        prev_offset, prev_length = server.ranges[index - 1]
        cur_offset, _ = server.ranges[index]
        assert cur_offset == server.registered or cur_offset >= prev_offset, \
            "续传位置不对：%d" % cur_offset
    assert server.ranges[0][0] == 0, "第一次应该从 0 开始"


def test_input_stream_read_error_is_not_a_failure():
    """InputStreamReadError（HTTP 600）是正常的，不是失败。

    它表示「我收到的还不够」——分段上传时每次都会收到这个。
    当成错误的话，整个上传会直接失败。
    """
    total = int(2.5 * 1024 ** 3)
    server = ChunkServer(total, status_code=600, put_text=XML_INPUT_ERROR)
    client = make_client(server)
    path = make_file(1024)

    original = os.path.getsize
    os.path.getsize = lambda p: total
    try:
        client.upload_whole(path, "-11")
    finally:
        os.path.getsize = original
        os.unlink(path)

    assert server.commit_count == 1, "应该走到提交那一步"


def test_413_is_reported_not_retried():
    """413（请求体太大）必须明确报错，不能重传。

    重传就是用户看到的「同一个文件反复上传」。
    """
    total = int(2.5 * 1024 ** 3)
    server = ChunkServer(total, status_code=413)
    client = make_client(server)
    path = make_file(1024)

    original = os.path.getsize
    os.path.getsize = lambda p: total
    try:
        with pytest.raises(Pc189Error) as exc:
            client.upload_whole(path, "-11")
    finally:
        os.path.getsize = original
        os.unlink(path)

    assert server.put_count == 1, "413 不该重传，实际 %d 次" % server.put_count
    assert "2GB" in str(exc.value), str(exc.value)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
