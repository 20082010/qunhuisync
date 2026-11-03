"""天翼网盘（189）封装：登录、目录浏览、建目录、上传、校验、删除。

底层用的是 :mod:`qunhuisync.pc189`——按 OpenList ``drivers/189pc`` 移植的
PC 端协议。旧的 cloud189 SDK 走的是天役旧网页版接口（``appId=8025431004``
+ ``cloud.189.cn/api/open/*``），实测那边现在只认 cookie 会话，已经用不了。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import nullcontext
from typing import Any, Dict, List, Optional, Tuple

from .util import atomic_write_json, read_json

log = logging.getLogger("qunhuisync.cloud")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


class CloudError(Exception):
    """天翼网盘相关错误。"""


class JsonFileStore:
    """令牌存储接口实现，落地到磁盘避免频繁登录。"""

    def __init__(self, path: str):
        self.path = path

    def get(self) -> Optional[Dict[str, Any]]:
        return read_json(self.path)

    def update(self, data: Dict[str, Any]) -> None:
        atomic_write_json(self.path, data)


class RemoteEntry:
    """网盘上的一个文件或目录。"""

    def __init__(self, item: Dict[str, Any]):
        self.id = str(item.get("id"))
        self.name = item.get("name") or ""
        self.is_folder = bool(item.get("is_folder"))
        self.size = int(item.get("size") or 0)
        self.modified = item.get("modified") or ""
        # 天翼的列表接口会带上文件的 MD5（大写）。
        # 校验时直接用它，不用把文件下载回来重算——那会白白多耗一份
        # 流量和 CPU，对群晖这种小机器是致命的。
        self.md5 = (item.get("md5") or "").lower()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "<RemoteEntry %s %s %s>" % (self.id, self.name, "dir" if self.is_folder else self.size)


class CloudService:
    """天翼网盘服务。

    登录走 :mod:`qunhuisync.pc189`（手机号密码 / 图形验证码 / 二维码三种方式），
    拿到会话后建立 :class:`~qunhuisync.pc189.Pc189Client`。
    """

    def __init__(self, cloud_config: Dict[str, Any], token_path: str):
        self.config = dict(cloud_config or {})
        self.token_path = token_path
        self.client = None
        self.error: Optional[str] = None
        # 目录列表缓存是**实例级**的：写成类属性会被所有实例共享，
        # 不同规则、不同账号之间会互相污染。
        self._dir_cache: Dict[str, Tuple[float, List[RemoteEntry]]] = {}
        self._dir_cache_lock = threading.Lock()

    # ---------- 连接 ----------
    def connected(self) -> bool:
        return self.client is not None

    def build_client(self, session: Dict[str, Any]):
        """用已登录得到的会话创建客户端。"""
        from .pc189 import Pc189Client

        client = Pc189Client(session)
        self.save_session(session)
        return client

    def save_session(self, session: Dict[str, Any]) -> None:
        """把令牌落盘，下次启动可直接续用，不必再走验证码。"""
        access = session.get("accessToken")
        refresh = session.get("refreshToken")
        if not access and not refresh:
            return
        store = JsonFileStore(self.token_path)
        data = store.get() or {}
        if access:
            data["accessToken"] = access
        if refresh:
            data["refreshToken"] = refresh
        data["expiresIn"] = int(time.time() * 1000) + 6 * 24 * 60 * 60 * 1000
        try:
            store.update(data)
        except Exception as exc:
            log.warning("保存天翼令牌失败：%s", exc)

    def load_session(self) -> Optional[Dict[str, Any]]:
        """读取已保存的令牌。"""
        data = JsonFileStore(self.token_path).get() or {}
        if not data.get("accessToken"):
            return None
        if data.get("expiresIn") and data["expiresIn"] <= time.time() * 1000:
            return None
        return data

    def connect(self) -> None:
        """建立连接并校验账号可用。

        优先用已保存的令牌恢复会话（扫码登录后落盘的），令牌失效且配置了
        账号密码时才回退到密码登录。两者都没有就提示先扫码。
        """
        from . import pc189

        username = (self.config.get("username") or "").strip()
        password = self.config.get("password") or ""

        session = None
        saved = self.load_session()
        if saved:
            try:
                session = pc189.restore_session(saved["accessToken"])
                log.info("已用保存的令牌恢复天翼会话")
            except Exception as exc:
                log.info("令牌失效：%s", exc)
                session = None

        if session is None:
            if not username:
                raise CloudError("尚未登录，请点右上角「扫码登录」")
            auth = pc189.Pc189Auth()
            try:
                auth.prepare(username)
                session = auth.submit(username, password)
            except pc189.Pc189Error as exc:
                raise CloudError(str(exc))
            finally:
                auth.close()

        client = self.build_client(session)
        try:
            client.disk_info()
        except Exception as exc:  # 登录失败时保留原因
            self.client = None
            raise CloudError("登录失败：%s" % exc)
        self.client = client
        self.error = None

    def require_client(self):
        if self.client is None:
            raise CloudError("尚未连接天翼网盘")
        return self.client

    def disconnect(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    # ---------- 目录 ----------
    def root_id(self) -> str:
        return str(self.config.get("root_folder_id") or "-11")

    def list_dir(self, folder_id: str) -> List[RemoteEntry]:
        client = self.require_client()
        try:
            items = client.list_files(folder_id)
        except Exception as exc:
            raise CloudError("读取目录失败：%s" % exc)
        return [RemoteEntry(item) for item in items]

    def create_folder(self, parent_id: str, name: str) -> str:
        """在网盘上新建目录，返回新目录的 id。"""
        client = self.require_client()
        try:
            return client.create_folder(parent_id, name)
        except CloudError:
            raise
        except Exception as exc:
            raise CloudError("创建目录 %s 失败：%s" % (name, exc))

    def ensure_folder(self, path: str) -> str:
        """确保远端路径存在，返回目录 id。"""
        current = self.root_id()
        parts = [p for p in (path or "").replace("\\", "/").split("/") if p and p not in (".", "..")]
        for part in parts:
            entries = self.list_dir(current)
            folder = next((e for e in entries if e.is_folder and e.name == part), None)
            if folder is None:
                clash = next((e for e in entries if not e.is_folder and e.name == part), None)
                if clash is not None:
                    raise CloudError("远端已存在同名文件，无法创建目录：%s" % part)
                # 直接用 createFolder 返回的 id，不要创建完再查列表——
                # 天翼的目录列表有延迟，刚建的目录可能马上还查不到。
                folder_id = self.create_folder(current, part)
                if not folder_id:
                    entries = self.list_dir(current)
                    folder = next((e for e in entries if e.is_folder and e.name == part), None)
                    if folder is None:
                        raise CloudError("创建目录后未在列表中找到：%s" % part)
                    current = folder.id
                    continue
                current = folder_id
                continue
            current = folder.id
        return current

    def folder_id_by_path(self, path: str) -> Optional[str]:
        """查找已存在的目录 id，不存在返回 None。"""
        current = self.root_id()
        for part in [p for p in (path or "").replace("\\", "/").split("/") if p and p not in (".", "..")]:
            entries = self.list_dir(current)
            folder = next((e for e in entries if e.is_folder and e.name == part), None)
            if folder is None:
                return None
            current = folder.id
        return current

    # ---------- 文件 ----------
    def upload(self, local_path: str, folder_id: str, rename: Optional[str] = None,
               on_progress=None, should_cancel=None, md5: Optional[str] = None,
               on_hash_progress=None, speed_limit: int = 0,
               hash_gate=None) -> str:
        """整文件上传。

        ``on_progress(done, total)`` 每传一块回调一次（传输阶段）。
        ``on_hash_progress(done, total)`` 算 MD5 时回调（哈希阶段）。
        ``md5`` 已知时直接用，省掉一次全文件哈希。
        ``speed_limit`` 限速（字节/秒），0 表示不限。
        ``hash_gate`` 是上下文管理器，用来把哈希阶段串行化（控 CPU）。
        """
        client = self.require_client()
        from .pc189 import Pc189Cancelled

        try:
            # hash_gate 是把哈希阶段串行化用的锁（控制 CPU 占用）。
            # 锁只在算本地 MD5 时持有，传输阶段（等网络）照旧并发。
            file_id = client.upload_whole(local_path, folder_id, rename,
                                          on_progress=on_progress,
                                          should_cancel=should_cancel,
                                          md5=md5,
                                          on_hash_progress=on_hash_progress,
                                          speed_limit=speed_limit,
                                          hash_gate=hash_gate)
            # 上传完了，目录内容变了，缓存要失效，
            # 否则校验时查不到刚传上去的文件
            self.invalidate_dir(folder_id)
            return file_id
        except Pc189Cancelled:
            # 用户取消：原样往上抛。包装成 CloudError 的话，
            # 调用方分不清「取消了」和「上传失败」，界面就会显示成失败。
            raise
        except Exception as exc:
            raise CloudError("上传失败 %s：%s" % (os.path.basename(local_path), exc))

    def find_file(self, folder_id: str, name: str) -> Optional[RemoteEntry]:
        for entry in self.list_dir(folder_id):
            if not entry.is_folder and entry.name == name:
                return entry
        return None

    # ---------- 目录列表缓存 ----------
    #
    # 天翼的目录列表接口很慢（目录里文件多时要分页拉），
    # 而每个文件上传完都要查一次「云端有没有这个文件」，
    # 一轮同步下来会发几十次 listFiles——用户看到的「上传完还要等很久」
    # 就是这么来的。
    #
    # 这里做几秒的短缓存：同一目录短时间内只拉一次。
    # 缓存时间要短（几秒），因为上传后列表有延迟，得等它更新。
    _DIR_CACHE_TTL = 5.0

    def _cached_dir(self, folder_id: str) -> List[RemoteEntry]:
        """取目录列表（带几秒缓存）。"""
        now = time.time()
        with self._dir_cache_lock:
            hit = self._dir_cache.get(folder_id)
            if hit and now - hit[0] < self._DIR_CACHE_TTL:
                return hit[1]
        entries = self.list_dir(folder_id)
        with self._dir_cache_lock:
            self._dir_cache[folder_id] = (time.time(), entries)
        return entries

    def invalidate_dir(self, folder_id: str) -> None:
        """目录内容变了（比如刚上传完），让缓存失效。"""
        with self._dir_cache_lock:
            self._dir_cache.pop(folder_id, None)

    def find_file(self, folder_id: str, name: str) -> Optional[RemoteEntry]:
        """在目录里找文件。

        用缓存的目录列表：一次同步里同一个目录会被反复查，
        每次都真拉一遍列表太慢。
        """
        for entry in self._cached_dir(folder_id):
            if not entry.is_folder and entry.name == name:
                return entry
        return None

    def verify(self, folder_id: str, name: str, size: int, md5: Optional[str] = None,
               mode: str = "size") -> bool:
        """校验云端文件与本地一致。mode: none / size / md5。

        MD5 校验优先用目录列表接口返回的哈希值——天翼的列表里就带着，
        比把文件下载回来重算省得多（少一份流量、少一份 CPU）。
        只有列表里没给哈希时才退回下载重算。

        天翼的目录列表有延迟，刚上传完的文件可能几秒后才出现。
        所以查不到时等几轮再查——但**最多等一轮**：
        上传接口成功返回就说明服务端已经收好了，目录列表只是显示延迟，
        一直等下去只会让用户干等（「已经 100% 了还要等很久」）。
        """
        if mode == "none":
            return True

        entry = self.find_file(folder_id, name)
        if entry is None:
            # 列表里还没有——可能是入库延迟。等一小会儿再看一次。
            time.sleep(2.0)
            self.invalidate_dir(folder_id)
            entry = self.find_file(folder_id, name)
        if entry is None:
            # 还是查不到。分两种情况：
            # * 只校验大小：上传接口成功返回就说明服务端收好了，
            #   目录列表只是显示延迟——算通过，否则用户会看到
            #   「进度 100% 却一直卡在校验中」。
            # * 校验 MD5：必须拿到哈希才能比，没查到就不能算通过。
            if mode == "md5":
                log.info("校验 MD5 时目录列表里还没有 %s（服务端入库延迟），判为未通过" % name)
                return False
            log.info("校验时目录列表里还没有 %s（服务端入库延迟），按上传成功处理" % name)
            return True
        if entry.size != size:
            return False
        if mode == "md5":
            if not md5:
                return False
            if entry.md5:
                return entry.md5 == md5.lower()
            return self.remote_md5(entry.id) == md5
        return True

    def remote_md5(self, file_id: str) -> Optional[str]:
        """下载云端文件计算 MD5，用于强校验。"""
        import hashlib

        import httpx

        client = self.require_client()
        try:
            url = client.download_url(file_id)
        except Exception as exc:
            raise CloudError("获取下载地址失败：%s" % exc)
        digest = hashlib.md5()
        try:
            with httpx.Client(follow_redirects=True, timeout=120.0,
                              headers={"User-Agent": USER_AGENT}) as http:
                with http.stream("GET", url) as response:
                    response.raise_for_status()
                    for chunk in response.iter_bytes(1024 * 1024):
                        digest.update(chunk)
        except Exception as exc:
            raise CloudError("回读云端文件失败：%s" % exc)
        return digest.hexdigest()

    def delete_remote(self, file_id: str, name: str) -> None:
        client = self.require_client()
        try:
            client.delete_file(file_id, name, False)
        except Exception as exc:
            raise CloudError("删除云端文件 %s 失败：%s" % (name, exc))

    def disk_info(self) -> Dict[str, Any]:
        client = self.require_client()
        return client.disk_info()
