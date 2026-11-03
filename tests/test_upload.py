"""上传流程的单元测试：用假服务端跑完整分片上传。"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from qunhuisync import pc189  # noqa: E402
from qunhuisync.pc189 import Pc189Client, Pc189Error  # noqa: E402


class FakeResponse:
    def __init__(self, payload=None, text="", content=b"", status_code=200, url=""):
        self._payload = payload
        self.text = text
        self.content = content
        self.status_code = status_code
        self.url = url

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class UploadServer:
    """模拟天翼上传接口，记录所有收到的分片。"""

    def __init__(self, slice_size=10 * 1024 * 1024, fail_part=None, disconnect_parts=None):
        self.slice_size = slice_size
        self.fail_part = fail_part
        # 这些分片第一次 PUT 时模拟「服务端中途断开」
        self.disconnect_parts = dict(disconnect_parts or {})
        self.uploaded = {}          # partNumber -> bytes
        self.init_params = None
        self.commit_params = None
        self.put_urls = []
        self.put_headers = []
        self.put_attempts = {}      # partNumber -> 尝试次数

    def handle(self, method, url, params=None, headers=None, data=None, query=None):
        if "initMultiUpload" in url:
            self.init_params = params
            return {"data": {"uploadFileId": "UF1", "fileDataExists": 0, "uploadHost": "h"}}
        if "getMultiUploadUrls" in url:
            part_info = params.get("partInfo") or ""
            number = int(part_info.split("-", 1)[0])
            return {"uploadUrls": {
                "partNumber_%d" % number: {
                    "requestURL": "https://upload.example/part/%d" % number,
                    "requestHeader": "x-token=abc&x-date=2026",
                }}}
        if "commitMultiUploadFile" in url:
            self.commit_params = params
            return {"code": "SUCCESS", "file": {"userFileId": "F9"}}
        raise AssertionError("未预期的请求: %s %s" % (method, url))

    def put(self, url, headers=None, content=None):
        self.put_urls.append(url)
        self.put_headers.append(dict(headers or {}))
        path = url.split("?", 1)[0]
        number = int(path.rsplit("/", 1)[-1])
        self.put_attempts[number] = self.put_attempts.get(number, 0) + 1
        # 分片是流式发送的，这里把迭代器收成字节
        if hasattr(content, "__iter__") and not isinstance(content, (bytes, bytearray)):
            content = b"".join(content)
        # 模拟服务端在传输中途断开（httpx.RemoteProtocolError）
        remaining = self.disconnect_parts.get(number)
        if remaining:
            self.disconnect_parts[number] = remaining - 1
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        if self.fail_part == number:
            return FakeResponse({"code": "FAILED", "msg": "分片写入失败"})
        self.uploaded[number] = content
        return FakeResponse({"code": "SUCCESS"})


def make_client(server):
    client = Pc189Client({"sessionKey": "SK", "sessionSecret": "S" * 32})
    client.request = lambda url, method="GET", params=None, query=None: server.handle(
        method, url, params=params)
    client.http = server
    return client


def make_file(size):
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    handle.write(b"X" * size)
    handle.close()
    return handle.name


def test_upload_sends_all_parts():
    server = UploadServer()
    client = make_client(server)
    path = make_file(25 * 1024 * 1024)      # 25MB -> 3 片
    try:
        client.upload(path, "-11")
    finally:
        os.unlink(path)

    assert len(server.uploaded) == 3
    assert sorted(server.uploaded) == [1, 2, 3]
    # 末片是余数大小
    assert len(server.uploaded[1]) == 10 * 1024 * 1024
    assert len(server.uploaded[3]) == 5 * 1024 * 1024


def test_put_url_has_client_suffix():
    """分片 PUT 的 URL 必须带 clientType 等参数，否则服务端不认这个分片。"""
    server = UploadServer()
    client = make_client(server)
    path = make_file(1024)
    try:
        client.upload(path, "-11")
    finally:
        os.unlink(path)

    url = server.put_urls[0]
    assert "clientType=TELEPC" in url
    assert "version=" in url
    assert "channelId=web_cloud.189.cn" in url
    assert "rand=" in url


def test_commit_sends_correct_md5():
    server = UploadServer()
    client = make_client(server)
    path = make_file(25 * 1024 * 1024)
    with open(path, "rb") as handle:
        content = handle.read()
    try:
        client.upload(path, "-11")
    finally:
        os.unlink(path)

    params = server.commit_params
    # fileMd5 = 整个文件的 MD5（大写）
    assert params["fileMd5"] == hashlib.md5(content).hexdigest().upper()
    # sliceMd5 = 各分片 MD5 大写、换行拼接后再 MD5（大写）
    slices = []
    size = 10 * 1024 * 1024
    for offset in range(0, len(content), size):
        slices.append(hashlib.md5(content[offset:offset + size]).hexdigest().upper())
    expected = hashlib.md5("\n".join(slices).encode("utf-8")).hexdigest().upper()
    assert params["sliceMd5"] == expected
    assert params["opertype"] == "3"
    assert params["lazyCheck"] == "1"


def test_single_slice_uses_file_md5_for_slice_md5():
    """只有一片时 sliceMd5 直接等于 fileMd5。"""
    server = UploadServer()
    client = make_client(server)
    path = make_file(1024)
    try:
        client.upload(path, "-11")
    finally:
        os.unlink(path)

    assert server.commit_params["sliceMd5"] == server.commit_params["fileMd5"]


def test_upload_raises_when_part_fails():
    """分片返回错误体时要报错，不能当成成功。"""
    server = UploadServer(fail_part=2)
    client = make_client(server)
    path = make_file(25 * 1024 * 1024)
    try:
        with pytest.raises(Pc189Error) as exc:
            client.upload(path, "-11")
    finally:
        os.unlink(path)
    assert "分片" in str(exc.value)


def test_part_size_grows_with_file():
    """天翼限制分片数，文件越大分片要越大。"""
    client = Pc189Client({"sessionKey": "SK", "sessionSecret": "S" * 32})
    assert client._part_size(1024) == 10 * 1024 * 1024
    assert client._part_size(10 * 1024 * 1024 * 100) == 10 * 1024 * 1024
    # 超过 999 片 -> 20MB
    assert client._part_size(10 * 1024 * 1024 * 1000) == 20 * 1024 * 1024
    # 超大文件 -> 按分片数上限放大
    big = client._part_size(10 * 1024 * 1024 * 2000 * 10)
    assert big > 20 * 1024 * 1024
    assert 2000 * 10 * 1024 * 1024 / big <= 1999


def test_init_sends_slice_size():
    server = UploadServer()
    client = make_client(server)
    path = make_file(1024)
    try:
        client.upload(path, "-11")
    finally:
        os.unlink(path)
    assert server.init_params["sliceSize"] == str(10 * 1024 * 1024)
    assert server.init_params["fileSize"] == "1024"


def test_put_uses_wildcard_accept():
    """分片 PUT 的 Accept 必须是 */*。

    天翼的对象存储在 Accept 是 application/json 时会走另一条分支：
    PUT 返回 200、etag 也正常，但不会把分片登记到 uploadFileId，
    最后提交时报「uploaded part number less than total slice part number」。
    """
    server = UploadServer()
    client = make_client(server)
    path = make_file(1024)
    try:
        client.upload(path, "-11")
    finally:
        os.unlink(path)

    assert server.put_headers
    for headers in server.put_headers:
        assert headers.get("Accept") == "*/*"


def test_put_sets_content_length():
    """分片是流式发送的，必须显式给 Content-Length，否则会退化成 chunked。"""
    server = UploadServer()
    client = make_client(server)
    path = make_file(1024)
    try:
        client.upload(path, "-11")
    finally:
        os.unlink(path)

    assert server.put_headers[0].get("Content-Length") == "1024"


def test_progress_is_reported_incrementally():
    """进度要分多次上报，否则进度条在一个分片内一直是 0。"""
    server = UploadServer()
    client = make_client(server)
    path = make_file(700 * 1024)
    progress = []
    try:
        client.upload(path, "-11", on_progress=progress.append)
    finally:
        os.unlink(path)

    assert len(progress) >= 3, "进度应该分多次上报"
    assert progress == sorted(progress), "进度只能递增"
    assert progress[-1] == 700 * 1024, "最终进度应等于文件大小"


def test_put_keeps_server_headers():
    """服务端下发的鉴权头必须原样带上。"""
    server = UploadServer()
    client = make_client(server)
    path = make_file(1024)
    try:
        client.upload(path, "-11")
    finally:
        os.unlink(path)

    headers = server.put_headers[0]
    assert headers.get("x-token") == "abc"
    assert headers.get("x-date") == "2026"


def test_upload_retries_transient_failure():
    """天翼偶发「服务暂时不可用」，要自动重试而不是直接失败。"""
    server = UploadServer()
    client = make_client(server)
    calls = {"init": 0}
    original = server.handle

    def flaky(method, url, **kwargs):
        if "initMultiUpload" in url:
            calls["init"] += 1
            if calls["init"] == 1:
                raise Pc189Error("天翼接口返回错误：-1 / 非常抱歉服务暂时不可用")
        return original(method, url, **kwargs)

    server.handle = flaky
    client.request = lambda url, method="GET", params=None, query=None: server.handle(
        method, url, params=params)

    path = make_file(1024)
    try:
        client.upload(path, "-11")
    finally:
        os.unlink(path)

    assert calls["init"] == 2
    assert server.commit_params is not None


def test_put_retries_when_server_disconnects():
    """服务端中途断开连接时要重传该分片，而不是整个上传失败。

    这是 NAS 上传大文件时最常见的故障：对象存储或中间网关会在
    传输过程中关掉连接（httpx 报 RemoteProtocolError）。
    """
    server = UploadServer(disconnect_parts={1: 1})
    client = make_client(server)
    path = make_file(1024)
    try:
        client.upload(path, "-11")
    finally:
        os.unlink(path)

    assert server.put_attempts.get(1) == 2, "断开后应该重传该分片"
    assert 1 in server.uploaded, "重传后分片应该写成功"
    assert server.commit_params is not None


def test_put_gives_up_after_max_attempts():
    """一直断开会明确报错，不会无限重试。"""
    server = UploadServer(disconnect_parts={1: 99})
    client = make_client(server)
    path = make_file(1024)
    try:
        with pytest.raises(Pc189Error) as exc:
            client.upload(path, "-11")
    finally:
        os.unlink(path)
    assert "断开" in str(exc.value) or "disconnected" in str(exc.value).lower()


def test_progress_resets_on_retry():
    """重传时进度要归零重来，不能把两次的字节数累加。"""
    server = UploadServer(disconnect_parts={1: 1})
    client = make_client(server)
    path = make_file(700 * 1024)
    progress = []
    try:
        client.upload(path, "-11", on_progress=progress.append)
    finally:
        os.unlink(path)

    assert 0 in progress, "重传前应把进度归零"
    assert progress[-1] == 700 * 1024, "最终进度应等于文件大小"


def test_slice_size_follows_server_rule():
    """分片大小由天翼按文件大小硬性规定，不能让用户改。

    实测：文件 ≤ 10GB 必须 10MB，~100GB 必须 20MB，~200GB 必须 50MB，
    其它值一律报 InvalidPartSize。
    """
    client = Pc189Client({"sessionKey": "SK", "sessionSecret": "S" * 32})
    mb = 1024 * 1024
    assert client._part_size(1 * mb) == 10 * mb
    assert client._part_size(25 * mb) == 10 * mb
    assert client._part_size(9990 * mb) == 10 * mb
    assert client._part_size(10000 * mb) == 20 * mb
    assert client._part_size(20000 * mb) == 50 * mb


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
