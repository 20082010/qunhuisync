"""空目录清理的**时机**回归测试。

用户要求：「空文件夹的判断要在最后全部上传完成后执行，
因为删除文件后就会有空文件夹的情况出现。」

问题：以前 ``_cleanup_empty_dirs()`` 在**上传之前**跑。
但「同步成功后删除源文件」是在**上传过程中**发生的——
文件传完就被删掉，目录于是变空。
那时候清理已经跑完了，新产生的空目录就漏掉了，
本地会留下一堆空目录。

修法：清理挪到**全部上传结束之后**。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from qunhuisync.config import Config, Rule  # noqa: E402
from qunhuisync.syncer import SyncEngine, SyncJob  # noqa: E402


class FakeCloud:
    """记录上传顺序的假网盘。"""

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


def build(tmp_path):
    config = Config(str(tmp_path / "config.json"))
    config.set_cloud({"username": "u", "password": "p", "upload_threads": 1})
    config.save()
    return config


def make_tree(tmp_path):
    root = tmp_path / "videos"
    root.mkdir()
    (root / "a.mp4").write_bytes(b"x" * 100)
    (root / "sub").mkdir()
    (root / "sub" / "b.mp4").write_bytes(b"y" * 100)
    return root


# ---------------- 核心：删除产生的空目录也要清理 ----------------

def test_dirs_emptied_by_deletion_are_cleaned(tmp_path):
    """上传后源文件被删掉，目录变空 -> 也要被清理掉。

    修复前：清理在上传之前跑，删文件产生的空目录全漏掉。
    """
    config = build(tmp_path)
    cloud = FakeCloud()
    engine = SyncEngine(config, cloud)
    root = make_tree(tmp_path)

    rule = Rule.from_dict({
        "id": "r1", "name": "删源文件", "local_path": str(root),
        "delete_source": True,
        "delete_empty_dirs": True,
    })
    job = SyncJob(id="j1", rule_id="r1")
    engine.run(rule, job)

    assert job.uploaded == 2, "应该上传 2 个文件，实际 %d" % job.uploaded
    assert job.deleted == 2, "应该删除 2 个源文件，实际 %d" % job.deleted
    # 源文件删光后，目录就空了，应该一并清掉
    assert not (root / "sub").exists(), "sub 目录空了却没清理"
    assert not root.exists() or not any(root.iterdir()), "根目录空了却没清理"


def test_cleanup_runs_after_upload(tmp_path):
    """清理必须发生在**全部上传完成之后**。

    从日志顺序就能看出来：清理的日志要在「同步结束」之前，
    并且在上传之后。
    """
    config = build(tmp_path)
    cloud = FakeCloud()
    engine = SyncEngine(config, cloud)
    root = make_tree(tmp_path)

    rule = Rule.from_dict({
        "id": "r1", "name": "顺序", "local_path": str(root),
        "delete_source": True,
        "delete_empty_dirs": True,
    })
    job = SyncJob(id="j1", rule_id="r1")
    engine.run(rule, job)

    logs = job.logs
    cleanup_idx = next((i for i, line in enumerate(logs)
                        if "清理空目录" in line), None)
    end_idx = next((i for i, line in enumerate(logs)
                    if "同步结束" in line), None)
    assert cleanup_idx is not None, "日志里没有「清理空目录」：%s" % logs
    assert end_idx is not None, "日志里没有「同步结束」：%s" % logs
    assert cleanup_idx < end_idx, "清理应该在「同步结束」之前"


def test_pre_existing_empty_dir_still_cleaned(tmp_path):
    """本来就存在的空目录（没有文件可传）也要清理。

    挪到上传之后不代表不清理了 —— 只是清理时机变了。
    """
    config = build(tmp_path)
    cloud = FakeCloud()
    engine = SyncEngine(config, cloud)
    root = make_tree(tmp_path)
    (root / "empty-dir").mkdir()

    rule = Rule.from_dict({
        "id": "r1", "name": "空目录", "local_path": str(root),
        "delete_empty_dirs": True,
    })
    job = SyncJob(id="j1", rule_id="r1")
    engine.run(rule, job)

    assert not (root / "empty-dir").exists(), "本来就空的目录没清理"


def test_nested_empty_dirs_all_removed(tmp_path):
    """多层嵌套的空目录要一层层都清掉。"""
    config = build(tmp_path)
    cloud = FakeCloud()
    engine = SyncEngine(config, cloud)
    root = tmp_path / "videos"
    root.mkdir()
    (root / "a" / "b" / "c").mkdir(parents=True)

    rule = Rule.from_dict({
        "id": "r1", "name": "嵌套", "local_path": str(root),
        "delete_empty_dirs": True,
    })
    job = SyncJob(id="j1", rule_id="r1")
    engine.run(rule, job)

    assert not (root / "a").exists(), "嵌套空目录没清干净：%s" % list(root.rglob("*"))


def test_dir_with_remaining_file_is_kept(tmp_path):
    """目录里还有文件（没传成功）就不能删。"""
    config = build(tmp_path)
    cloud = FakeCloud()
    engine = SyncEngine(config, cloud)
    root = make_tree(tmp_path)
    # 让上传不删源文件，目录里就还有文件
    rule = Rule.from_dict({
        "id": "r1", "name": "保留", "local_path": str(root),
        "delete_source": False,
        "delete_empty_dirs": True,
    })
    job = SyncJob(id="j1", rule_id="r1")
    engine.run(rule, job)

    assert (root / "sub" / "b.mp4").exists(), "还有文件的目录被删了"


def test_excluded_dir_is_not_removed(tmp_path):
    """排除的目录（如 @eaDir）不能被清理。"""
    config = build(tmp_path)
    cloud = FakeCloud()
    engine = SyncEngine(config, cloud)
    root = make_tree(tmp_path)
    (root / "@eaDir").mkdir()

    rule = Rule.from_dict({
        "id": "r1", "name": "排除", "local_path": str(root),
        "delete_empty_dirs": True,
    })
    job = SyncJob(id="j1", rule_id="r1")
    engine.run(rule, job)

    assert (root / "@eaDir").exists(), "@eaDir 被误删了"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
