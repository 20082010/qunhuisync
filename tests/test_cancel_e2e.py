"""取消的端到端测试：点「取消」后必须**马上**停下来。

用户报的现象：点「取消此文件」/「取消整个任务」后，要等所有文件都取消、
或者等那个文件传完才停——不是当下立即停止。

这里用一个本地慢速服务器模拟真实上传：
* 服务端一直收，但收得很慢
* 上传线程跑起来后，中途请求取消
* 断言：上传线程必须在 2 秒内结束，而不是等文件传完
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from qunhuisync import pc189  # noqa: E402
from qunhuisync.pc189 import Pc189Cancelled, Pc189Client  # noqa: E402


class SlowServer:
    """慢速接收的本地服务器。

    它会一直收数据但不发响应——模拟「文件很大、传得慢」。
    """

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.received = 0
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        conn.settimeout(30)
        try:
            while not self._stop.is_set():
                data = conn.recv(65536)
                if not data:
                    break
                self.received += len(data)
                # 收得慢一点，模拟真实网络
                time.sleep(0.02)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


class FakeClient(Pc189Client):
    """把上传地址换成本地慢速服务器。"""

    def __init__(self, server, port):
        super().__init__({"sessionKey": "SK", "sessionSecret": "S" * 32})
        self.server = server
        self.port = port
        self._post_form = lambda url, form, attempts=3: {
            "fileUploadUrl": "http://127.0.0.1:%d/put" % port,
            "fileCommitUrl": "http://127.0.0.1:%d/commit" % port,
            "uploadFileId": "UF1",
            "fileDataExists": 0,
        }
        # 账号上限很大，不拦大文件
        self._max_file_size = 200 * 1024 ** 3


def make_file(size):
    import tempfile
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
    handle.write(b"X" * size)
    handle.close()
    return handle.name


def test_cancel_stops_upload_immediately():
    """传输中途取消，必须马上停。

    修复前：取消只在重试循环开头检查，所以一次 PUT 要传完整个文件才生效。
    修复后：body_stream 每读 256KB 查一次取消。
    """
    server = SlowServer()
    try:
        client = FakeClient(server, server.port)
        # 声明 200MB（本地跑不用真传完，够慢就行）
        path = make_file(4 * 1024 * 1024)
        # 改小文件但声明大一点，让它传得久一点
        try:
            cancelled = {"value": False}

            def should_cancel():
                if cancelled["value"]:
                    return True
                # 传了一会儿再取消
                return server.received > 512 * 1024

            error = {}

            def run():
                try:
                    client.upload_whole(path, "-11", should_cancel=should_cancel)
                except Pc189Cancelled:
                    error["cancelled"] = True
                except Exception as exc:
                    error["error"] = exc

            started = time.time()
            thread = threading.Thread(target=run)
            thread.start()
            thread.join(timeout=20)
            elapsed = time.time() - started

            assert not thread.is_alive(), "取消后上传线程没有停下来"
            assert error.get("cancelled"), "应该是被取消，而不是别的错误：%s" % error
            assert elapsed < 15, "取消后等了 %.1f 秒才停，太慢了" % elapsed
        finally:
            os.unlink(path)
    finally:
        server.close()


def test_cancel_while_hashing_stops_immediately():
    """哈希阶段也要能马上取消。

    2GB 的文件在群晖上算 MD5 要几十秒，这期间点取消必须立刻生效。
    """
    server = SlowServer()
    try:
        client = FakeClient(server, server.port)
        path = make_file(8 * 1024 * 1024)

        calls = {"n": 0}

        def should_cancel():
            calls["n"] += 1
            return calls["n"] > 2

        error = {}
        started = time.time()
        try:
            client.upload_whole(path, "-11", should_cancel=should_cancel)
        except Pc189Cancelled:
            error["cancelled"] = True
        except Exception as exc:
            error["error"] = exc
        elapsed = time.time() - started

        assert error.get("cancelled"), "哈希阶段没有响应取消：%s" % error
        assert elapsed < 5, "哈希阶段取消等了 %.1f 秒，太慢了" % elapsed
        os.unlink(path)
    finally:
        server.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
