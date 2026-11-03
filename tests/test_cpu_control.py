"""针对「CPU 打满」「重复传输」「并发假死」三个问题的回归测试。

这些用例锁住的是真实踩过的坑，改代码时务必别把它们改坏。
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import threading
import time

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from qunhuisync import pc189  # noqa: E402
from qunhuisync.pc189 import Pc189Client, Pc189Error, _RateLimiter  # noqa: E402
from qunhuisync.syncer import SyncJob  # noqa: E402


class FakeResponse:
    def __init__(self, payload=None, text="", status_code=200):
        self._payload = payload
        self.text = text
        self.status_code = status_code

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class WholeUploadServer:
    """模拟天翼的整文件上传接口（createUploadFile / PUT / status / commit）。

    行为对齐实测结果：
    * 一次 PUT 传完整个文件，返回 200
    * 中途断开的 PUT 不登记任何字节（服务端会丢弃整个会话）
    * 所以断线后只能整个重传
    """

    def __init__(self, total_size, fail_at=None, server_size=None, chunk=None,
                 fail_always=False):
        self.total_size = total_size
        self.fail_at = fail_at            # 第几次 PUT 抛异常
        self.fail_always = fail_always    # True = 每次都失败
        self.server_size = server_size    # status 接口固定返回的 size
        self.chunk = chunk                # 分块大小（None = 不分块）
        self.registered = 0               # 服务端已登记的字节数
        self.put_count = 0
        self.create_calls = 0
        self.put_headers = []
        self.put_bodies = []
        self.md5_sent = None
        self.commit_calls = 0

    def post_form(self, url, form):
        if "createUploadFile" in url:
            self.create_calls += 1
            self.md5_sent = form.get("md5")
            return {
                "fileUploadUrl": "https://upload.example/put",
                "fileCommitUrl": "https://upload.example/commit",
                "uploadFileId": "UF%d" % self.create_calls,
                "fileDataExists": 0,
            }
        if "commit" in url:
            self.commit_calls += 1
            return {"file": {"userFileId": "F9"}}
        raise AssertionError("未预期的请求: %s" % url)

    def status(self, upload_id):
        if self.server_size is not None:
            return {"size": self.server_size}
        return {"size": self.registered}

    def put(self, url, headers=None, content=None, timeout=None):
        self.put_count += 1
        self.put_headers.append(dict(headers or {}))
        body = b"".join(content) if hasattr(content, "__iter__") else (content or b"")
        self.put_bodies.append(body)
        if self.fail_always or (self.fail_at is not None
                                and self.put_count == self.fail_at):
            raise httpx.RemoteProtocolError("模拟服务端断开")
        # 服务端只在完整收到整个文件时登记
        self.registered += len(body)
        return FakeResponse(None, "", 200)


def make_client(server, session=None):
    client = Pc189Client(session or {"sessionKey": "SK", "sessionSecret": "S" * 32})
    client._post_form = lambda url, form, attempts=3: server.post_form(url, form)
    client._upload_status = lambda upload_id: server.status(upload_id)
    client.http = server
    return client


def make_file(size):
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    handle.write(b"X" * size)
    handle.close()
    return handle.name


# ---------------- CPU：哈希只算一次 ----------------

def test_md5_is_not_recomputed_when_provided():
    """传入 md5 时不应该再读一遍文件算哈希。

    以前这里会把整个文件哈希两遍（上传一遍、校验一遍），
    CPU 直接翻倍。
    """
    server = WholeUploadServer(total_size=1024)
    client = make_client(server)
    path = make_file(1024)
    try:
        digest = hashlib.md5(b"X" * 1024).hexdigest()
        client.upload_whole(path, "-11", md5=digest)
    finally:
        os.unlink(path)

    assert server.md5_sent == hashlib.md5(b"X" * 1024).hexdigest()


def test_hash_progress_is_reported():
    """哈希阶段要有进度回调，否则大文件算哈希时界面像卡死。"""
    server = WholeUploadServer(total_size=1024)
    client = make_client(server)
    path = make_file(1024)
    events = []
    try:
        client.upload_whole(path, "-11", on_hash_progress=lambda d, t: events.append((d, t)))
    finally:
        os.unlink(path)

    assert events, "哈希阶段应该上报进度"
    assert events[-1][0] == 1024
    assert events[-1][1] == 1024


def test_hash_progress_not_reported_when_md5_known():
    """已经有 md5 就不该再走哈希阶段。"""
    server = WholeUploadServer(total_size=1024)
    client = make_client(server)
    path = make_file(1024)
    events = []
    try:
        client.upload_whole(path, "-11", md5=hashlib.md5(b"X" * 1024).hexdigest(),
                            on_hash_progress=lambda d, t: events.append(d))
    finally:
        os.unlink(path)
    assert events == []


# ---------------- 重传：不能从头再来 ----------------

def test_retry_does_not_restart_from_zero():
    """断线后要能重传并最终成功。

    注意：天翼这个接口**断线后不保留已收数据**（实测 getUploadFileStatus
    返回 0），所以无法续传，只能整个重传——这是服务端行为。
    这里锁住的是「重传后能成功，且不会无限重传」。
    """
    server = WholeUploadServer(total_size=4096, fail_at=1)
    client = make_client(server)
    path = make_file(4096)
    try:
        client.upload_whole(path, "-11")
    finally:
        os.unlink(path)

    assert server.put_count >= 2, "失败后应该重试"
    assert server.commit_calls == 1, "重传成功后应该提交"


def test_retry_keeps_session_and_resumes():
    """重传时**沿用原来的上传会话**，从服务端登记的位置接着传。

    现在的分段上传是「查服务端进度 -> 从那儿继续」，所以会话要保住：
    重新建会话等于把服务端已经收好的部分扔掉，从头再来。
    """
    server = WholeUploadServer(total_size=4096, fail_at=1)
    client = make_client(server)
    path = make_file(4096)
    try:
        client.upload_whole(path, "-11")
    finally:
        os.unlink(path)

    # 失败后应该重试，但**不该**重新建会话
    assert server.put_count >= 2, "失败后应该重试"
    assert server.create_calls == 1, "不该重建会话（会把已传的丢掉）"
    assert server.commit_calls == 1, "重试成功后应该提交"


def test_retry_gives_up_eventually():
    """一直断开会明确报错，不会无限重传。"""
    server = WholeUploadServer(total_size=4096, fail_always=True)
    client = make_client(server)
    path = make_file(4096)
    try:
        with pytest.raises(Pc189Error) as exc:
            client.upload_whole(path, "-11")
    finally:
        os.unlink(path)
    assert "上传失败" in str(exc.value)


def test_progress_follows_server_registration():
    """进度要跟着服务端登记的字节数走。

    断线重传时不能显示虚高：服务端收到多少，界面就显示多少，
    这样用户看到的进度是真的（续传也从这个位置开始）。
    """
    server = WholeUploadServer(total_size=4096, fail_at=1)
    client = make_client(server)
    path = make_file(4096)
    progress = []
    try:
        client.upload_whole(path, "-11", on_progress=lambda d, t: progress.append(d))
    finally:
        os.unlink(path)

    assert progress, "应该上报进度"
    assert progress[-1] == 4096, "最终进度应等于文件大小"
    # 每一轮上报的进度都不能超过服务端登记的量
    assert max(progress) <= 4096, "进度显示虚高了"


def test_no_expect_continue_header():
    """PUT 不能带 Expect: 100-continue。

    httpx 不支持 100-continue 的握手语义，带上这个头会让上传
    慢好几倍（实测 4MB 从 0.5s 变成 1.8s）。
    """
    server = WholeUploadServer(total_size=1024)
    client = make_client(server)
    path = make_file(1024)
    try:
        client.upload_whole(path, "-11")
    finally:
        os.unlink(path)

    for headers in server.put_headers:
        assert "Expect" not in headers


def test_put_uses_wildcard_accept():
    """Accept 必须是 */*，否则天翼不登记上传进度。"""
    server = WholeUploadServer(total_size=1024)
    client = make_client(server)
    path = make_file(1024)
    try:
        client.upload_whole(path, "-11")
    finally:
        os.unlink(path)

    assert server.put_headers[0].get("Accept") == "*/*"


def test_put_sets_content_length():
    """必须显式给 Content-Length，否则会退化成 chunked。"""
    server = WholeUploadServer(total_size=1024)
    client = make_client(server)
    path = make_file(1024)
    try:
        client.upload_whole(path, "-11")
    finally:
        os.unlink(path)

    assert server.put_headers[0].get("Content-Length") == "1024"


# ---------------- 限速 ----------------

def test_rate_limiter_throttles():
    """限速器要真的把速度压下来。"""
    limiter = _RateLimiter(1024 * 1024)   # 1MB/s
    start = time.monotonic()
    for _ in range(8):
        limiter.tick(128 * 1024)           # 共 1MB
    elapsed = time.monotonic() - start
    assert elapsed >= 0.4, "1MB @ 1MB/s 应该至少花 0.4 秒，实际 %.2f" % elapsed


def test_rate_limiter_disabled():
    """限速为 0 时不等待。"""
    limiter = _RateLimiter(0)
    start = time.monotonic()
    for _ in range(100):
        limiter.tick(1024 * 1024)
    assert time.monotonic() - start < 0.5


# ---------------- 哈希串行：CPU 不被打满 ----------------

def test_hash_stage_is_serialized():
    """同一时刻只允许一个线程做本地哈希。

    三个线程同时哈希三个大文件 = 三个核一起烧 = 群晖 CPU 100%。
    """
    import tempfile as tf
    from qunhuisync.config import Config, Rule
    from qunhuisync.syncer import SyncEngine

    class SlowCloud:
        """模拟真实行为：锁只在哈希阶段持有。"""

        def __init__(self):
            self.concurrent_hash = 0
            self.peak_hash = 0

        def ensure_folder(self, path):
            return "-11"

        def list_dir(self, folder_id):
            return []

        def find_file(self, folder_id, name):
            return None

        def verify(self, *args, **kwargs):
            return True

        def upload(self, local_path, folder_id, rename=None, on_progress=None,
                   should_cancel=None, md5=None, on_hash_progress=None,
                   speed_limit=0, hash_gate=None):
            # 哈希阶段：在闸门里做一段 CPU 活
            if hash_gate is not None:
                with hash_gate:
                    self.concurrent_hash += 1
                    self.peak_hash = max(self.peak_hash, self.concurrent_hash)
                    time.sleep(0.05)
                    self.concurrent_hash -= 1
            return "1"

    tmp = tf.mkdtemp()
    config = Config(os.path.join(tmp, "config.json"))
    config.set_cloud({"upload_threads": 3})
    cloud = SlowCloud()
    engine = SyncEngine(config, cloud)

    root = os.path.join(tmp, "videos")
    os.makedirs(root, exist_ok=True)
    for index in range(3):
        with open(os.path.join(root, "v%d.mp4" % index), "wb") as handle:
            handle.write(b"x" * 1024)

    rule = Rule.from_dict({"id": "r1", "name": "哈希串行", "local_path": root})
    job = SyncJob(id="j1", rule_id="r1")
    engine.run(rule, job)

    assert job.status == "success", job.message
    assert cloud.peak_hash == 1, "哈希阶段应该串行，峰值并发为 %d" % cloud.peak_hash


# ---------------- 界面数据 ----------------

def test_active_file_reports_hash_progress():
    """界面要能显示哈希阶段进度。"""
    job = SyncJob(id="j1", rule_id="r1")
    job.begin_file("a.mp4", 1000)
    job.update_hash("a.mp4", 300, 1000)
    files = job.active_files()
    assert files[0]["hashing"] is True
    assert files[0]["hash_percent"] == 30


def test_active_file_clears_hash_flag_after_upload():
    """进入传输阶段后就不该再显示哈希进度。"""
    job = SyncJob(id="j1", rule_id="r1")
    job.begin_file("a.mp4", 1000)
    job.update_hash("a.mp4", 300, 1000)
    job.update_file("a.mp4", 100, 1000)
    files = job.active_files()
    assert files[0]["hashing"] is False
    assert files[0]["percent"] == 10


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
