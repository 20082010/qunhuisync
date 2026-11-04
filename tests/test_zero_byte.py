"""删除 0 字节文件规则的测试。"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from qunhuisync.config import Config, Rule  # noqa: E402
from qunhuisync.syncer import SyncEngine, SyncJob  # noqa: E402


class FakeCloud:
    def __init__(self):
        self.uploads = []
        self.folders = {"-11": []}

    def ensure_folder(self, path):
        return "-11"

    def list_dir(self, folder_id):
        return []

    def find_file(self, folder_id, name):
        return None

    def upload(self, local_path, folder_id, rename=None, on_progress=None,
               should_cancel=None, md5=None, on_hash_progress=None,
               speed_limit=0, hash_gate=None):
        self.uploads.append(local_path)
        size = os.path.getsize(local_path)
        if on_progress:
            on_progress(size)
        return "1"

    def verify(self, *args, **kwargs):
        return True

    def delete_remote(self, *args, **kwargs):
        pass


def build(tmp_path, **rule_data):
    config = Config(str(tmp_path / "config.json"))
    config.set_cloud({"username": "u", "password": "p", "upload_threads": 1})
    config.save()
    return config


def make_tree(tmp_path):
    root = tmp_path / "videos"
    root.mkdir()
    (root / "empty.mp4").write_bytes(b"")
    (root / "ok.mp4").write_bytes(b"x" * 100)
    (root / "sub").mkdir()
    (root / "sub" / "empty2.mp4").write_bytes(b"")
    return root


def test_zero_byte_file_deleted_when_enabled(tmp_path):
    config = build(tmp_path)
    cloud = FakeCloud()
    engine = SyncEngine(config, cloud)
    root = make_tree(tmp_path)
    rule = Rule.from_dict({
        "id": "r1", "name": "零字节清理", "local_path": str(root),
        "delete_zero_byte": True,
    })
    job = SyncJob(id="j1", rule_id="r1")
    engine.run(rule, job)

    # 空文件被移走，不再存在
    assert not (root / "empty.mp4").exists()
    assert not (root / "sub" / "empty2.mp4").exists()
    # 非空文件保留（这条规则没开删除源文件）
    assert (root / "ok.mp4").exists()
    # 空文件没上传
    assert all("empty" not in path for path in cloud.uploads)
    assert job.zero_deleted == 2
    assert job.uploaded == 1


def test_zero_byte_file_kept_when_disabled(tmp_path):
    config = build(tmp_path)
    cloud = FakeCloud()
    engine = SyncEngine(config, cloud)
    root = make_tree(tmp_path)
    rule = Rule.from_dict({
        "id": "r2", "name": "不清理", "local_path": str(root),
        "delete_zero_byte": False,
    })
    job = SyncJob(id="j2", rule_id="r2")
    engine.run(rule, job)

    assert (root / "empty.mp4").exists()
    # 没开这条规则时，0 字节文件不会被删；但天翼不接受空文件，也不上传
    assert all("empty" not in path for path in cloud.uploads)
    assert job.deleted == 0


def test_zero_byte_deleted_directly_not_to_trash(tmp_path):
    """0 字节文件直接删除，不进回收站。"""
    config = build(tmp_path)
    cloud = FakeCloud()
    engine = SyncEngine(config, cloud)
    root = make_tree(tmp_path)
    rule = Rule.from_dict({
        "id": "r3", "name": "直接删除", "local_path": str(root),
        "delete_zero_byte": True,
    })
    job = SyncJob(id="j3", rule_id="r3")
    engine.run(rule, job)

    # 整个目录树里都不该有回收站残留
    for dirpath, _dirs, files in os.walk(str(tmp_path)):
        assert not files or "empty" not in "".join(files)
        assert os.path.basename(dirpath) != ".qunhuisync_trash"
    assert not (root / "empty.mp4").exists()
    assert not (root / "sub" / "empty2.mp4").exists()


def test_delete_zero_byte_flag_roundtrip():
    rule = Rule.from_dict({"delete_zero_byte": True})
    assert rule.delete_zero_byte is True
    assert rule.to_dict()["delete_zero_byte"] is True
    assert Rule.from_dict({}).delete_zero_byte is False


def test_zero_byte_deleted_even_with_min_size_limit(tmp_path):
    """设了最小体积门槛时，0 字节文件也要能被清理掉。

    否则扫描阶段就把空文件过滤掉了，「删除 0 字节文件」这条规则形同虚设。
    """
    config = build(tmp_path)
    cloud = FakeCloud()
    engine = SyncEngine(config, cloud)
    root = make_tree(tmp_path)
    rule = Rule.from_dict({
        "id": "r4", "name": "带门槛", "local_path": str(root),
        "delete_zero_byte": True, "min_size_bytes": 1024,
    })
    job = SyncJob(id="j4", rule_id="r4")
    engine.run(rule, job)

    assert not (root / "empty.mp4").exists()
    assert not (root / "sub" / "empty2.mp4").exists()
    assert job.zero_deleted == 2
    # 100 字节的文件达不到 1KB 门槛，不上传也不删除
    assert job.uploaded == 0
    assert (root / "ok.mp4").exists()


def test_source_deleted_directly_after_sync(tmp_path):
    """同步成功后源文件直接删除，不留任何副本。"""
    config = build(tmp_path)
    cloud = FakeCloud()
    engine = SyncEngine(config, cloud)
    root = make_tree(tmp_path)
    rule = Rule.from_dict({
        "id": "r7", "name": "直接删", "local_path": str(root),
        "delete_source": True,
    })
    job = SyncJob(id="j7", rule_id="r7")
    engine.run(rule, job)

    # ok.mp4 同步成功并被删除
    assert not (root / "ok.mp4").exists()
    # 没有任何回收站残留
    trash_dirs = [d for d, _s, _f in os.walk(str(tmp_path))
                  if os.path.basename(d) == ".qunhuisync_trash"]
    assert not trash_dirs


def test_empty_dirs_checked_after_upload(tmp_path):
    """空目录检查放在**上传完成之后**。

    「同步成功后删除源文件」是在上传过程中发生的：文件传完就被删掉，
    目录于是变空。清理必须排在这些删除之后，否则新产生的空目录会漏掉。
    """
    config = build(tmp_path)
    cloud = FakeCloud()
    engine = SyncEngine(config, cloud)
    root = make_tree(tmp_path)
    (root / "empty-dir").mkdir()
    rule = Rule.from_dict({
        "id": "r9", "name": "空目录", "local_path": str(root),
        "delete_empty_dirs": True, "delete_zero_byte": True,
    })
    job = SyncJob(id="j9", rule_id="r9")
    engine.run(rule, job)

    assert not (root / "empty-dir").exists()
    # 空目录清理的日志应该出现在「同步结束」之前（在上传之后）
    logs = job.logs
    cleanup_idx = next((i for i, line in enumerate(logs) if "清理空目录" in line), None)
    end_idx = next((i for i, line in enumerate(logs) if "同步结束" in line), None)
    assert cleanup_idx is not None
    assert end_idx is None or cleanup_idx < end_idx


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
