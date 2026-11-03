"""三个用户反馈的回归测试。

1. 上传完确认很慢 -> 目录列表缓存
2. 文件名含 emoji -> 必须把改名后的名字传给上传
3. 网页密码留空仍要密码 -> /api/config 要回传「有没有设」
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from qunhuisync.cloud import CloudService, RemoteEntry  # noqa: E402
from qunhuisync.config import Config  # noqa: E402
from qunhuisync.server import Handler, build_routes  # noqa: E402
from qunhuisync.syncer import SyncEngine, SyncJob  # noqa: E402


# ---------------- 1. 目录列表缓存 ----------------

class CountingCloud(CloudService):
    """记录 list_dir 被调了几次。"""

    def __init__(self):
        super().__init__({}, "unused-token.json")
        self.list_calls = 0
        self.entry = RemoteEntry({"id": "F1", "name": "a.mp4", "size": 100,
                                  "md5": "ABCDEF0123456789ABCDEF0123456789"})

    def list_dir(self, folder_id):
        self.list_calls += 1
        return [self.entry]

    def remote_md5(self, file_id):
        return "abcdef0123456789abcdef0123456789"


def test_dir_cache_avoids_repeated_list_calls():
    """同一目录短时间内不该反复拉列表。

    天翼的列表接口慢，每个文件上传完都查一次会拖慢整轮同步
    （用户反馈「已经 100% 还要等很久」）。
    """
    cloud = CountingCloud()
    for _ in range(10):
        cloud.find_file("-11", "a.mp4")
    assert cloud.list_calls == 1, "缓存期内只该拉一次，实际 %d 次" % cloud.list_calls


def test_dir_cache_expires():
    """缓存要会过期，否则刚上传的文件永远查不到。"""
    cloud = CountingCloud()
    cloud._DIR_CACHE_TTL = 0.2
    cloud.find_file("-11", "a.mp4")
    time.sleep(0.3)
    cloud.find_file("-11", "a.mp4")
    assert cloud.list_calls == 2, "过期后要重新拉，实际 %d 次" % cloud.list_calls


def test_invalidate_dir_forces_refetch():
    """手动失效后要重新拉列表。"""
    cloud = CountingCloud()
    cloud.find_file("-11", "a.mp4")
    cloud.invalidate_dir("-11")
    cloud.find_file("-11", "a.mp4")
    assert cloud.list_calls == 2, "失效后要重新拉，实际 %d 次" % cloud.list_calls


# ---------------- 2. 文件名改名要传给上传 ----------------

class RenameCloud(CloudService):
    """记录上传时用的是什么文件名。"""

    def __init__(self):
        super().__init__({}, "unused-token.json")
        self.uploaded_name = None

    def ensure_folder(self, path):
        return "-11"

    def list_dir(self, folder_id):
        return []

    def find_file(self, folder_id, name):
        return None

    def upload(self, local_path, folder_id, rename=None, on_progress=None,
               should_cancel=None, md5=None, on_hash_progress=None,
               speed_limit=0, hash_gate=None):
        self.uploaded_name = rename
        return "F1"

    def verify(self, *args, **kwargs):
        return True

    def delete_remote(self, file_id, name):
        pass


def test_emoji_name_is_passed_to_upload():
    """改名后的文件名必须传给上传。

    之前只改了本地变量，云端还是用原始名（带 emoji），
    结果还是 SaveFileError。
    """
    tmp = tempfile.mkdtemp()
    config = Config(os.path.join(tmp, "config.json"))
    config.set_cloud({"upload_threads": 1})
    cloud = RenameCloud()
    engine = SyncEngine(config, cloud)

    src = os.path.join(tmp, "videos")
    os.makedirs(src)
    path = os.path.join(src, "南瓜\U0001F383.mp4")
    with open(path, "wb") as handle:
        handle.write(b"x" * 100)

    from qunhuisync.config import Rule
    rule = Rule.from_dict({"id": "r1", "name": "改名", "local_path": src,
                           "remote_path": "/t", "verify_mode": "size"})
    job = SyncJob(id="j1", rule_id="r1")
    engine.run(rule, job)

    assert job.status == "success", job.message
    assert cloud.uploaded_name is not None, "应该调用了上传"
    assert "\U0001F383" not in cloud.uploaded_name, \
        "传给上传的名字不该含 emoji，实际 %r" % cloud.uploaded_name
    assert cloud.uploaded_name == "南瓜_.mp4", \
        "实际 %r" % cloud.uploaded_name


# ---------------- 3. 网页密码 ----------------

class FakeHandler(BaseHTTPRequestHandler):
    """最小化的 handler，用来调 api_get_config。"""

    def log_message(self, *args):
        pass


def build_app(tmp):
    config_path = os.path.join(tmp, "config.json")
    config = Config(config_path)
    config.set_server({"password": "secret", "host": "127.0.0.1", "port": 8791})
    config.save()

    class App:
        def __init__(self):
            self.config = config

    app = App()
    handler = FakeHandler.__new__(FakeHandler)
    handler.state = app
    handler._json = lambda data, status=200: setattr(handler, "_result", data)
    return handler, config


def test_get_config_reports_has_password():
    """/api/config 要告诉前端「密码有没有设」。

    之前根本不回传 password 字段，前端读到 undefined，
    用户看到的输入框永远是空的，分不清是没设还是没显示。
    """
    tmp = tempfile.mkdtemp()
    handler, config = build_app(tmp)

    from qunhuisync.server import api_get_config
    api_get_config(handler, None, None)
    server = handler._result["server"]

    assert "password" not in server, "密码本身不该回传"
    assert server["has_password"] is True, "应告知前端已启用密码"


def test_get_config_has_password_false_when_unset():
    """没设密码时 has_password 应为 False。"""
    tmp = tempfile.mkdtemp()
    config_path = os.path.join(tmp, "config.json")
    config = Config(config_path)
    config.set_server({"host": "127.0.0.1", "port": 8791})
    config.save()

    class App:
        def __init__(self):
            self.config = config

    handler = FakeHandler.__new__(FakeHandler)
    handler.state = App()
    handler._json = lambda data, status=200: setattr(handler, "_result", data)

    from qunhuisync.server import api_get_config
    api_get_config(handler, None, None)
    assert handler._result["server"]["has_password"] is False


def test_clearing_password_actually_clears_it():
    """清空密码保存后，密码要真的被清掉（不再要求输入）。"""
    tmp = tempfile.mkdtemp()
    config_path = os.path.join(tmp, "config.json")
    config = Config(config_path)
    config.set_server({"password": "secret"})
    config.save()

    # 模拟前端保存时传空串
    config.set_server({"password": ""})
    config.save()

    reloaded = Config(config_path)
    assert not (reloaded.server.get("password") or "").strip(), \
        "留空保存后密码应被清掉，实际 %r" % reloaded.server.get("password")


def test_set_server_accepts_empty_string():
    """set_server 要接受空串（用来关闭密码），不能当成 None 跳过。"""
    tmp = tempfile.mkdtemp()
    config = Config(os.path.join(tmp, "config.json"))
    config.set_server({"password": "secret"})
    assert config.server.get("password") == "secret"
    config.set_server({"password": ""})
    assert config.server.get("password") == "", "空串应该能写进去"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
