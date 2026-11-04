"""目录名带 emoji 的回归测试。

实测（真实网盘，2026-11-04）：天翼**拒绝带 emoji 的目录名**，
报 ``EmojiArgumentError / Emoji Argument Error``：

    创建目录 '🦄'            -> EmojiArgumentError
    创建目录 '🦄双子lily'     -> EmojiArgumentError
    创建目录 '🦄️双子lily'    -> EmojiArgumentError
    创建目录 '双子lily'       -> 成功

文件名那边早就处理了（commit 阶段报 ``SaveFileError``），
但**目录名漏了**——用户日志里就是这么报的：

    跳过 抖音/🦄️双子lily/[...].mp4：创建目录 🦄️双子lily 失败：
    天翼接口返回错误：EmojiArgumentError / Emoji Argument Error
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from qunhuisync.cloud import CloudService  # noqa: E402
from qunhuisync.pc189 import Pc189Client  # noqa: E402
from qunhuisync.util import sanitize_remote_name  # noqa: E402

UNICORN = "\U0001F984"
UNICORN_VS = "\U0001F984️"   # 带变体选择符 U+FE0F


class FakeEntry:
    def __init__(self, name, is_folder=False, entry_id="1"):
        self.name = name
        self.is_folder = is_folder
        self.id = entry_id


class FakeCloudClient:
    """记录建目录时用的名字。"""

    def __init__(self):
        self.created = []      # 建出来的目录名
        self.existing = {}     # (parent, name) -> FakeEntry

    def list_files(self, folder_id="-11", page_size=1000):
        result = []
        for (parent, name), entry in self.existing.items():
            if parent == folder_id:
                result.append({"name": name, "is_folder": True, "id": entry.id})
        return result

    def create_folder(self, parent_id, name):
        self.created.append(name)
        entry = FakeEntry(name, True, "F%d" % len(self.created))
        self.existing[(parent_id, name)] = entry
        return entry.id

    def close(self):
        pass


def make_service():
    service = CloudService({}, os.path.join(os.devnull, "token.json"))
    client = FakeCloudClient()
    service.client = client
    return service, client


# ---------------- ensure_folder ----------------

def test_ensure_folder_replaces_emoji_in_dir_name():
    """建目录时要把 emoji 换掉，否则天翼直接拒绝。"""
    service, client = make_service()
    service.ensure_folder("/抖音/%s双子lily" % UNICORN)

    assert UNICORN not in client.created[1], "目录名里还有 emoji：%r" % client.created[1]
    assert client.created[1] == "_双子lily", client.created[1]


def test_ensure_folder_handles_variation_selector():
    """带变体选择符（U+FE0F）的 emoji 也要换掉。

    用户日志里的目录名就是 ``🦄️双子lily``（带 FE0F）。
    变体选择符本身没有字形，替换成下划线后名字会多一个下划线，
    但**不能留着 emoji**——留着就还是会被天翼拒绝。
    """
    service, client = make_service()
    service.ensure_folder("/抖音/%s双子lily" % UNICORN_VS)

    name = client.created[1]
    assert UNICORN not in name, "目录名里还有 emoji：%r" % name
    assert "双子lily" in name, name
    # 变体选择符也不能留在名字里
    assert "️" not in name, name


def test_ensure_folder_keeps_normal_names():
    """普通名字不能被改。"""
    service, client = make_service()
    service.ensure_folder("/抖音/双子lily")

    assert client.created == ["抖音", "双子lily"], client.created


def test_ensure_folder_reuses_existing_sanitized_folder():
    """已经建好的（去 emoji 后的）目录要能直接找到，不要重复建。"""
    service, client = make_service()
    client.existing[("-11", "_双子lily")] = FakeEntry("_双子lily", True, "F9")

    service.ensure_folder("/%s双子lily" % UNICORN)

    assert client.created == [], "不该重复建目录：%r" % client.created


def test_folder_id_by_path_uses_sanitized_name():
    """查目录也要用去 emoji 后的名字，否则跟实际建出来的不一致。"""
    service, client = make_service()
    client.existing[("-11", "_双子lily")] = FakeEntry("_双子lily", True, "F9")

    folder_id = service.folder_id_by_path("/%s双子lily" % UNICORN)

    assert folder_id == "F9", folder_id


# ---------------- 底层 create_folder ----------------

def test_client_create_folder_replaces_emoji():
    """最底层再建一道防线：发出去的名字一定不带 emoji。"""
    client = Pc189Client({"sessionKey": "SK", "sessionSecret": "S" * 32})
    sent = {}

    def fake_request(url, method="GET", params=None, query=None, attempts=3):
        sent.update(query or {})
        return {"id": "F1"}

    client.request = fake_request
    client.create_folder("-11", "%s双子lily" % UNICORN)

    assert sent["folderName"] == "_双子lily", sent


# ---------------- 名字替换本身 ----------------

def test_sanitize_replaces_emoji_in_dir_name():
    assert sanitize_remote_name("%s双子lily" % UNICORN) == "_双子lily"
    assert sanitize_remote_name("双子lily") == "双子lily"
    # 带变体选择符的：emoji 和选择符各换一个下划线
    assert sanitize_remote_name("%s双子lily" % UNICORN_VS) == "__双子lily"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
