"""校验逻辑的测试：目录列表延迟、MD5 校验不下载回读。"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from qunhuisync.cloud import CloudService, RemoteEntry  # noqa: E402


class DelayedCloud(CloudService):
    """模拟天翼的目录列表延迟：刚上传完的文件要过一会儿才查得到。"""

    def __init__(self, delay_calls=2):
        super().__init__({}, "unused-token.json")
        self.calls = 0
        self.delay_calls = delay_calls
        self.entry = RemoteEntry({"id": "F1", "name": "a.mp4", "size": 100,
                                  "md5": "ABCDEF0123456789ABCDEF0123456789"})
        self.downloads = 0

    def list_dir(self, folder_id):
        self.calls += 1
        if self.calls > self.delay_calls:
            return [self.entry]
        return []

    def remote_md5(self, file_id):
        self.downloads += 1
        return "abcdef0123456789abcdef0123456789"


def test_verify_retries_when_list_is_delayed():
    """刚上传完的文件在列表里查不到时，要等一下再查。

    天翼的目录列表有延迟，不重试就会误判成「校验未通过」，
    导致源文件被白白保留下来。
    """
    cloud = DelayedCloud(delay_calls=1)
    started = time.monotonic()
    assert cloud.verify("-11", "a.mp4", 100, mode="size") is True
    assert cloud.calls == 2, "第一次查不到，要再查一次"
    assert time.monotonic() - started >= 2.0, "重试之间要有等待"


def test_verify_does_not_wait_forever_when_file_never_appears():
    """文件一直查不到时，最多等一轮就返回，不能无限等。

    上传接口成功返回就说明服务端收好了，目录列表只是显示延迟。
    一直等下去用户就会看到「进度 100% 却卡在校验中」。
    """
    cloud = DelayedCloud(delay_calls=999)
    started = time.monotonic()
    # 只校验大小时，查不到也按上传成功处理
    assert cloud.verify("-11", "a.mp4", 100, mode="size") is True
    assert time.monotonic() - started < 6.0, "不该等太久"
    assert cloud.calls == 2, "最多查两次"


def test_verify_md5_requires_entry():
    """MD5 校验必须拿到哈希才能比，查不到就不能算通过。"""
    cloud = DelayedCloud(delay_calls=999)
    assert cloud.verify("-11", "a.mp4", 100,
                        md5="abcdef0123456789abcdef0123456789", mode="md5") is False


def test_verify_md5_uses_list_value_without_downloading():
    """MD5 校验要用列表里的哈希值，不该把文件下载回来重算。

    下载回读会白白多耗一份流量和 CPU，对群晖这种小机器很致命。
    """
    cloud = DelayedCloud(delay_calls=0)
    assert cloud.verify("-11", "a.mp4", 100,
                        md5="abcdef0123456789abcdef0123456789", mode="md5") is True
    assert cloud.downloads == 0, "不该下载回读"


def test_verify_md5_falls_back_to_download_when_list_has_no_hash():
    """列表里没给哈希时，才退回下载重算。"""
    cloud = DelayedCloud(delay_calls=0)
    cloud.entry.md5 = ""
    assert cloud.verify("-11", "a.mp4", 100,
                        md5="abcdef0123456789abcdef0123456789", mode="md5") is True
    assert cloud.downloads == 1


def test_verify_size_mismatch_fails():
    """大小对不上要判失败。"""
    cloud = DelayedCloud(delay_calls=0)
    assert cloud.verify("-11", "a.mp4", 999, mode="size") is False


def test_verify_none_mode_skips_check():
    """不校验模式直接通过，不该去查目录。"""
    cloud = DelayedCloud(delay_calls=0)
    assert cloud.verify("-11", "a.mp4", 999, mode="none") is True
    assert cloud.calls == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
