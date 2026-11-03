"""同步引擎与扫描逻辑的单元测试（使用假的天翼网盘）。"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from qunhuisync.config import Config, Rule  # noqa: E402
from qunhuisync.scanner import scan  # noqa: E402
from qunhuisync.scheduler import Scheduler  # noqa: E402
from qunhuisync.syncer import SyncEngine, SyncJob  # noqa: E402
from qunhuisync.util import match_any  # noqa: E402


class FakeEntry:
    def __init__(self, name, size, is_folder=False, entry_id="1"):
        self.name = name
        self.size = size
        self.is_folder = is_folder
        self.id = entry_id
        self.modified = time.time()


class FakeCloud:
    """模拟天翼网盘：记录上传内容，可控制校验结果。"""

    def __init__(self, verify_ok=True):
        self.files = {}          # (folder_id, name) -> FakeEntry
        self.folders = {"-11": []}
        self.uploads = []
        self.verify_ok = verify_ok
        self.root_id = "-11"

    def ensure_folder(self, path):
        parts = [p for p in (path or "").split("/") if p]
        current = self.root_id
        for part in parts:
            names = [f[1] for f in self.folders.get(current, [])]
            if part not in names:
                self.folders.setdefault(current, []).append((part, part))
                self.folders.setdefault(part, [])
            current = part
        return current

    def list_dir(self, folder_id):
        entries = []
        for name, fid in self.folders.get(folder_id, []):
            entries.append(FakeEntry(name, 0, True, fid))
        for (fid, name), entry in self.files.items():
            if fid == folder_id:
                entries.append(entry)
        return entries

    def find_file(self, folder_id, name):
        return self.files.get((folder_id, name))

    def upload(self, local_path, folder_id, rename=None, on_progress=None,
               should_cancel=None, md5=None, on_hash_progress=None,
               speed_limit=0, hash_gate=None):
        self.uploads.append(local_path)
        size = os.path.getsize(local_path)
        name = os.path.basename(local_path)
        entry = FakeEntry(name, size, False, str(len(self.uploads)))
        self.files[(folder_id, name)] = entry
        if on_progress:
            on_progress(size)
        return entry.id

    def verify(self, folder_id, name, size, md5=None, mode="size"):
        if not self.verify_ok:
            return False
        entry = self.find_file(folder_id, name)
        return bool(entry and entry.size == size)

    def delete_remote(self, file_id, name):
        for key, entry in list(self.files.items()):
            if entry.name == name:
                del self.files[key]


def make_config(tmp_path) -> Config:
    return Config(str(tmp_path / "config.json"))


def make_rule(tmp_path, **kwargs) -> Rule:
    data = {
        "name": "测试规则",
        "local_path": str(tmp_path / "src"),
        "remote_path": "/备份",
        "include": ["*.jpg"],
        "delete_source": True,
        "verify_mode": "size",
        "schedule": {"type": "manual"},
    }
    data.update(kwargs)
    return Rule.from_dict(data)


def write_file(path, content=b"hello"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)


def test_match_any():
    assert match_any("photo.JPG", "sub/photo.JPG", ["*.jpg"])
    assert match_any("photo.jpg", "sub/photo.jpg", ["*.png", "*.jpg"])
    assert not match_any("photo.jpg", "sub/photo.jpg", ["*.png"])
    assert match_any("photo.jpg", "sub/photo.jpg", ["sub/*.jpg"])
    assert match_any("photo.jpg", "a/b/photo.jpg", ["**/*.jpg"])


def test_scan_filters(tmp_path):
    root = tmp_path / "src"
    write_file(str(root / "a.jpg"))
    write_file(str(root / "b.png"))
    write_file(str(root / "@eaDir" / "c.jpg"))
    write_file(str(root / "sub" / "d.jpg"))

    rule = make_rule(tmp_path, include=["*.jpg"], exclude=[])
    files = scan(rule)
    names = sorted(os.path.basename(f.path) for f in files)
    assert names == ["a.jpg", "d.jpg"]


def test_scan_min_size(tmp_path):
    root = tmp_path / "src"
    write_file(str(root / "small.jpg"), b"x")
    write_file(str(root / "big.jpg"), b"x" * 2048)
    rule = make_rule(tmp_path, min_size_mb=0.001, include=["*.jpg"])
    files = scan(rule)
    assert [os.path.basename(f.path) for f in files] == ["big.jpg"]


def test_sync_uploads_verifies_and_deletes_source(tmp_path):
    root = tmp_path / "src"
    write_file(str(root / "a.jpg"), b"data")
    config = make_config(tmp_path)
    rule = make_rule(tmp_path)
    cloud = FakeCloud(verify_ok=True)
    engine = SyncEngine(config, cloud)

    job = SyncJob(id="t1", rule_id=rule.id)
    engine.run(rule, job)

    assert job.status == "success"
    assert job.uploaded == 1
    assert job.deleted == 1
    # 源文件直接删除，不进回收站
    assert not os.path.exists(str(root / "a.jpg"))


def test_sync_does_not_leave_trash(tmp_path):
    """取消回收站机制：同步后不留下任何 .qunhuisync_trash 目录。"""
    root = tmp_path / "src"
    write_file(str(root / "a.jpg"), b"data")
    config = make_config(tmp_path)
    rule = make_rule(tmp_path)
    cloud = FakeCloud(verify_ok=True)
    engine = SyncEngine(config, cloud)
    engine.run(rule, SyncJob(id="t1b", rule_id=rule.id))

    trash_dirs = [d for d, _s, _f in os.walk(str(tmp_path))
                  if os.path.basename(d) == ".qunhuisync_trash"]
    assert not trash_dirs, "不应该再有回收站目录"


def test_sync_keeps_source_when_verify_fails(tmp_path):
    root = tmp_path / "src"
    write_file(str(root / "a.jpg"), b"data")
    config = make_config(tmp_path)
    rule = make_rule(tmp_path)
    cloud = FakeCloud(verify_ok=False)
    engine = SyncEngine(config, cloud)

    job = SyncJob(id="t2", rule_id=rule.id)
    engine.run(rule, job)

    assert job.uploaded == 1
    assert job.deleted == 0
    assert job.failed == 1
    assert os.path.exists(str(root / "a.jpg"))


def test_sync_skips_identical_remote_file(tmp_path):
    root = tmp_path / "src"
    write_file(str(root / "a.jpg"), b"data")
    config = make_config(tmp_path)
    rule = make_rule(tmp_path)
    cloud = FakeCloud()
    engine = SyncEngine(config, cloud)

    job1 = SyncJob(id="t3", rule_id=rule.id)
    engine.run(rule, job1)
    assert job1.uploaded == 1

    write_file(str(root / "a.jpg"), b"data")  # 内容相同，重新写入触发新 mtime
    job2 = SyncJob(id="t4", rule_id=rule.id)
    engine.run(rule, job2)
    assert job2.skipped == 1
    assert job2.uploaded == 0


def test_trash_is_disabled(tmp_path):
    """回收站机制已取消，相关接口一律空操作。"""
    root = tmp_path / "src"
    write_file(str(root / "a.jpg"), b"data")
    config = make_config(tmp_path)
    rule = make_rule(tmp_path)
    cloud = FakeCloud()
    engine = SyncEngine(config, cloud)
    engine.run(rule, SyncJob(id="t5", rule_id=rule.id))

    assert engine.trash_files(rule) == []
    assert engine.purge_trash(rule) == 0
    assert engine.trash_size(rule) == 0
    assert engine.delete_trash_files(rule, ["a.jpg"]) == 0


def test_config_rule_crud(tmp_path):
    config = make_config(tmp_path)
    rule = config.upsert_rule({"name": "r1", "local_path": str(tmp_path)})
    assert len(config.rules()) == 1
    rule.name = "r2"
    config.upsert_rule(rule.to_dict())
    assert config.rules()[0].name == "r2"
    assert config.delete_rule(rule.id)
    assert config.rules() == []


def _daily_target(now: float, hour: int, minute: int) -> float:
    """今天 hh:mm 的时间戳（与调度实现保持一致）。"""
    current = time.localtime(now)
    return time.mktime((current.tm_year, current.tm_mon, current.tm_mday, hour, minute, 0,
                        current.tm_wday, current.tm_yday, -1))


def test_schedule_daily_due(tmp_path):
    """每天定时：到点后当天只触发一次。"""
    now = time.time()
    rule = make_rule(tmp_path, schedule={"type": "daily", "time": "02:00"})
    target = _daily_target(now, 2, 0)

    # 今天还没到点 -> 不触发
    if target > now:
        rule.last_run = now - 86400
        assert not Scheduler._due(rule, now)

    # 已到点且今天还没跑过 -> 触发
    rule.last_run = now - 86400
    assert Scheduler._due(rule, now) == (now >= target)

    # 今天已经跑过了 -> 不再触发
    rule.last_run = now
    assert not Scheduler._due(rule, now)


def test_schedule_daily_not_yet(tmp_path):
    """每天定时：还没到点时不应触发。"""
    now = time.time()
    # 找一个今天还没到的时刻
    for minute_of_day in range(24 * 60):
        hour, minute = divmod(minute_of_day, 60)
        target = _daily_target(now, hour, minute)
        if target > now:
            break
    else:
        pytest.skip("当前时间已接近当天结束，跳过")

    rule = make_rule(tmp_path, schedule={"type": "daily", "time": "%02d:%02d" % (hour, minute)})
    rule.last_run = now - 86400
    assert not Scheduler._due(rule, now)


def test_schedule_interval_due(tmp_path):
    rule = make_rule(tmp_path, schedule={"type": "interval", "minutes": 30})
    now = time.time()
    rule.last_run = now - 10 * 60
    assert not Scheduler._due(rule, now)
    rule.last_run = now - 31 * 60
    assert Scheduler._due(rule, now)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
