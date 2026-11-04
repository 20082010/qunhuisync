"""「上传成功了但源文件没删掉」的回归测试。

用户反馈：在群晖上**一部分文件上传成功了但并未删除**。

根因有两处（都是「删掉了就忘了」的状态机漏洞）：

1. **删除失败后被记成「已同步」**。
   ``_remember()`` 在删除之前就执行了，所以删除失败（权限不足、
   文件被占用）时，同步状态里已经记下「这个文件同步过了」。
   下一轮扫描时命中记录 -> 直接 skip -> **再也不会去删它**。
   文件就这样永远留在本地。

2. **云端已有同名同大小的文件时被 skip，同样不删**。
   ``existing.size == item.size`` 走的是「已同步，跳过」分支，
   那个分支直接 return，没有删源文件。
   上一轮上传成功但删除失败的文件，正好会命中这个分支。

修法：同步状态里记录**源文件是否已删除**，没删掉的下一轮补删。
"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from qunhuisync.config import Config, Rule  # noqa: E402
from qunhuisync.syncer import SyncEngine, SyncJob  # noqa: E402


class FakeEntry:
    def __init__(self, name, size, is_folder=False, entry_id="1"):
        self.name = name
        self.size = size
        self.is_folder = is_folder
        self.id = entry_id
        self.modified = time.time()


class FakeCloud:
    """模拟天翼网盘。"""

    def __init__(self):
        self.files = {}
        self.folders = {"-11": []}
        self.uploads = []

    def ensure_folder(self, path):
        return self.root_id

    @property
    def root_id(self):
        return "-11"

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
        name = rename or os.path.basename(local_path)
        entry = FakeEntry(name, size, False, str(len(self.uploads)))
        self.files[(folder_id, name)] = entry
        if on_progress:
            on_progress(size)
        return entry.id

    def verify(self, folder_id, name, size, md5=None, mode="size"):
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
        "id": "r1",
        "name": "测试",
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


# ---------------- 删除失败要被记住，下一轮补删 ----------------

def test_failed_delete_is_retried_next_run(tmp_path, monkeypatch):
    """删除失败（权限不足）时，下一轮要**补删**，不能永远跳过。

    修复前：``_remember()`` 在删除之前执行，删除失败也记成了「已同步」，
    下一轮扫描命中记录直接 skip，源文件永远留在本地。
    """
    root = tmp_path / "src"
    target = str(root / "a.jpg")
    write_file(target, b"data")

    config = make_config(tmp_path)
    rule = make_rule(tmp_path)
    cloud = FakeCloud()
    engine = SyncEngine(config, cloud)

    # 第一次跑：删除时权限不足
    real_unlink = os.unlink
    calls = {"n": 0}

    def flaky_unlink(path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(13, "Permission denied")
        return real_unlink(path)

    monkeypatch.setattr(os, "unlink", flaky_unlink)

    job1 = SyncJob(id="t1", rule_id=rule.id)
    engine.run(rule, job1)
    assert os.path.exists(target), "第一次删除失败，源文件应该还在"
    assert job1.deleted == 0, "第一次没删成功"

    # 第二轮：同样的规则再跑一次，这次应该补删
    job2 = SyncJob(id="t2", rule_id=rule.id)
    engine.run(rule, job2)

    assert not os.path.exists(target), "第二轮应该补删源文件（删除失败后没再试）"
    assert job2.deleted == 1, "第二轮应该删掉 1 个，实际 %d" % job2.deleted


def test_cloud_already_has_file_still_deletes_source(tmp_path):
    """云端已有同名同大小的文件时，源文件也要删。

    修复前：这个分支直接 return（「已同步，跳过」），不删源文件。
    上一轮上传成功但删除失败的文件，正好会命中这个分支 ——
    于是它就永远留在本地了。
    """
    root = tmp_path / "src"
    target = str(root / "a.jpg")
    write_file(target, b"data")

    config = make_config(tmp_path)
    rule = make_rule(tmp_path)
    cloud = FakeCloud()
    engine = SyncEngine(config, cloud)

    # 云端已经有这个文件了（上一轮传上去的）
    cloud.files[("-11", "a.jpg")] = FakeEntry("a.jpg", 4, False, "9")

    job = SyncJob(id="t1", rule_id=rule.id)
    engine.run(rule, job)

    assert not os.path.exists(target), "云端已有该文件时，源文件仍应被删除"
    assert job.deleted == 1, "应该删掉 1 个，实际 %d" % job.deleted


def test_delete_source_off_leaves_file(tmp_path):
    """没开「同步后删除源文件」时，源文件必须留着。"""
    root = tmp_path / "src"
    target = str(root / "a.jpg")
    write_file(target, b"data")

    config = make_config(tmp_path)
    rule = make_rule(tmp_path, delete_source=False)
    cloud = FakeCloud()
    engine = SyncEngine(config, cloud)

    job = SyncJob(id="t1", rule_id=rule.id)
    engine.run(rule, job)

    assert os.path.exists(target), "没开删除开关，源文件必须保留"
    assert job.deleted == 0


def test_legacy_state_record_gets_source_deleted(tmp_path):
    """修复前写的同步状态（没有 pending_delete 字段）也要补删。

    用户那边已经积累了一批「上传成功却没删掉」的文件，
    它们的状态记录是旧格式。升级后必须能把它们删掉。
    """
    root = tmp_path / "src"
    target = str(root / "a.jpg")
    write_file(target, b"data")

    config = make_config(tmp_path)
    rule = make_rule(tmp_path)
    cloud = FakeCloud()
    engine = SyncEngine(config, cloud)

    # 手工写一份旧格式的状态：只有 size/mtime/name
    state = config.load_state()
    state.setdefault("rules", {})[rule.id] = {
        "a.jpg": {"size": 4, "mtime": os.path.getmtime(target), "name": "a.jpg"},
    }
    config.save_state(state)

    job = SyncJob(id="t1", rule_id=rule.id)
    engine.run(rule, job)

    assert not os.path.exists(target), "旧状态记录里的文件应该被补删"
    assert job.deleted == 1, "应该删掉 1 个，实际 %d" % job.deleted


def test_normal_case_deletes_source(tmp_path):
    """正常情况：上传 -> 校验 -> 删除，一气呵成。"""
    root = tmp_path / "src"
    target = str(root / "a.jpg")
    write_file(target, b"data")

    config = make_config(tmp_path)
    rule = make_rule(tmp_path)
    cloud = FakeCloud()
    engine = SyncEngine(config, cloud)

    job = SyncJob(id="t1", rule_id=rule.id)
    engine.run(rule, job)

    assert job.status == "success"
    assert job.uploaded == 1
    assert job.deleted == 1
    assert not os.path.exists(target)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
