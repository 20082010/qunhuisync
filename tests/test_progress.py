"""并发上传、进度上报、取消功能的测试。"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from qunhuisync.config import Config, Rule  # noqa: E402
from qunhuisync.pc189 import Pc189Client  # noqa: E402
from qunhuisync.syncer import SyncEngine, SyncJob  # noqa: E402


# ---------------- 请求头解码 ----------------

def test_parse_upload_headers_decodes_url_encoded():
    """天翼下发的 requestHeader 是 URL 编码的，必须先解码。"""
    raw = "x-token%3Dabc%26x-date%3D2026%26Content-Type%3Dapplication%2Foctet-stream"
    headers = Pc189Client._parse_upload_headers(raw)
    assert headers["x-token"] == "abc"
    assert headers["x-date"] == "2026"
    assert headers["Content-Type"] == "application/octet-stream"
    assert len(headers) == 3


def test_parse_upload_headers_handles_plain():
    headers = Pc189Client._parse_upload_headers("a=1&b=2")
    assert headers == {"a": "1", "b": "2"}


def test_parse_upload_headers_empty():
    assert Pc189Client._parse_upload_headers("") == {}


# ---------------- 任务进度与取消 ----------------

def make_job(rule_id="r1"):
    return SyncJob(id="j1", rule_id=rule_id, rule_name="测试")


def test_active_file_tracks_progress():
    job = make_job()
    job.begin_file("a/b.mp4", 1000)
    assert job.active_file() == ("a/b.mp4", 1000, 0)
    job.update_file("a/b.mp4", 400)
    assert job.active_file() == ("a/b.mp4", 1000, 400)
    job.end_file("a/b.mp4")
    assert job.active_file() == ("", 0, 0)


def test_active_file_picks_most_recent():
    job = make_job()
    job.begin_file("old.mp4", 100)
    time.sleep(0.02)
    job.begin_file("new.mp4", 200)
    path, total, _ = job.active_file()
    assert path == "new.mp4"
    assert total == 200


def test_to_dict_reports_percent():
    job = make_job()
    job.begin_file("a.mp4", 1000)
    job.update_file("a.mp4", 250)
    data = job.to_dict()
    assert data["current_file"] == "a.mp4"
    assert data["current_done"] == 250
    assert data["percent"] == 25


def test_cancel_flag():
    job = make_job()
    assert job.cancelled is False
    job.request_cancel()
    assert job.cancelled is True
    assert job.to_dict()["cancel_requested"] is True


def test_bump_is_atomic():
    """并发累加计数不能丢。"""
    job = make_job()

    def worker():
        for _ in range(500):
            job.bump("uploaded")

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert job.uploaded == 2000


# ---------------- 引擎并发 ----------------

class FakeEntry:
    def __init__(self, name, size, is_folder=False, entry_id="1"):
        self.name = name
        self.size = size
        self.is_folder = is_folder
        self.id = entry_id
        self.modified = time.time()


class FakeCloud:
    """模拟网盘：记录上传，记录并发峰值。"""

    def __init__(self, delay=0.05):
        self.files = {}
        self.folders = {"-11": []}
        self.uploads = []
        self.verify_ok = True
        self.delay = delay
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def ensure_folder(self, path):
        return "-11"

    def list_dir(self, folder_id):
        return []

    def find_file(self, folder_id, name):
        return self.files.get((folder_id, name))

    def upload(self, local_path, folder_id, rename=None, on_progress=None,
               should_cancel=None):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delay)
            if should_cancel and should_cancel():
                from qunhuisync.syncer import UploadCancelled
                raise UploadCancelled()
            with self.lock:
                self.uploads.append(local_path)
                size = os.path.getsize(local_path)
                name = os.path.basename(local_path)
                self.files[(folder_id, name)] = FakeEntry(name, size, False, str(len(self.uploads)))
            if on_progress:
                on_progress(size)
            return "1"
        finally:
            with self.lock:
                self.active -= 1

    def verify(self, folder_id, name, size, md5=None, mode="size"):
        return self.verify_ok

    def delete_remote(self, file_id, name):
        pass


def build_config(tmp_path, threads=3):
    config_path = tmp_path / "config.json"
    config = Config(str(config_path))
    config.set_cloud({"username": "u", "password": "p", "upload_threads": threads})
    config.save()
    return config


def make_files(tmp_path, count=6):
    root = tmp_path / "videos"
    root.mkdir()
    paths = []
    for index in range(count):
        path = root / ("v%d.mp4" % index)
        path.write_bytes(b"x" * 1024)
        paths.append(path)
    return root, paths


def test_uploads_multiple_files_concurrently(tmp_path):
    config = build_config(tmp_path, threads=3)
    cloud = FakeCloud(delay=0.1)
    engine = SyncEngine(config, cloud)
    root, _ = make_files(tmp_path, 6)
    rule = Rule.from_dict({"id": "r1", "name": "并发", "local_path": str(root),
                           "remote_path": "/v"})
    job = SyncJob(id="j1", rule_id="r1", rule_name="并发")
    engine.run(rule, job)

    assert job.status == "success", job.message
    assert job.uploaded == 6
    assert len(cloud.uploads) == 6
    # 并发峰值应大于 1
    assert cloud.peak > 1, "应该并发上传，实际峰值 %d" % cloud.peak


def test_single_thread_when_configured_one(tmp_path):
    config = build_config(tmp_path, threads=1)
    cloud = FakeCloud(delay=0.05)
    engine = SyncEngine(config, cloud)
    root, _ = make_files(tmp_path, 4)
    rule = Rule.from_dict({"id": "r2", "name": "单线程", "local_path": str(root)})
    job = SyncJob(id="j2", rule_id="r2")
    engine.run(rule, job)
    assert job.uploaded == 4
    assert cloud.peak == 1


def test_cancel_stops_task(tmp_path):
    config = build_config(tmp_path, threads=1)
    cloud = FakeCloud(delay=0.1)
    engine = SyncEngine(config, cloud)
    root, _ = make_files(tmp_path, 5)
    rule = Rule.from_dict({"id": "r3", "name": "取消", "local_path": str(root)})
    job = SyncJob(id="j3", rule_id="r3")
    engine.run(rule, job)

    # 新任务：上传第一个文件后立刻请求取消
    cloud2 = FakeCloud(delay=0.05)
    engine2 = SyncEngine(config, cloud2)
    job2 = SyncJob(id="j4", rule_id="r4")
    job2.request_cancel()
    engine2.run(rule, job2)
    assert job2.status == "cancelled"
    assert job2.uploaded == 0


def test_engine_cancel_marks_running_job(tmp_path):
    config = build_config(tmp_path, threads=1)
    cloud = FakeCloud(delay=0.2)
    engine = SyncEngine(config, cloud)
    root, _ = make_files(tmp_path, 3)
    rule = Rule.from_dict({"id": "r5", "name": "取消", "local_path": str(root)})
    job = SyncJob(id="j5", rule_id="r5")
    worker = threading.Thread(target=engine.run, args=(rule, job))
    worker.start()
    time.sleep(0.1)
    assert engine.cancel("r5") is True
    worker.join(timeout=10)
    assert job.cancel_requested is True


def test_upload_threads_clamped(tmp_path):
    config = build_config(tmp_path, threads=100)
    engine = SyncEngine(config, FakeCloud())
    assert engine.upload_threads() == 8
    config.set_cloud({"upload_threads": 0})
    assert engine.upload_threads() == 3


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
