"""天翼网盘（189）封装：登录、目录浏览、建目录、上传、校验、删除。

底层用的是 :mod:`qunhuisync.pc189`——按 OpenList ``drivers/189pc`` 移植的
PC 端协议。旧的 cloud189 SDK 走的是天役旧网页版接口（``appId=8025431004``
+ ``cloud.189.cn/api/open/*``），实测那边现在只认 cookie 会话，已经用不了。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

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
               on_progress=None, should_cancel=None) -> str:
        """整文件上传。

        ``on_progress(done, total)`` 每传一块回调一次。
        """
        client = self.require_client()
        try:
            return client.upload_whole(local_path, folder_id, rename,
                                       on_progress=on_progress,
                                       should_cancel=should_cancel)
        except Exception as exc:
            raise CloudError("上传失败 %s：%s" % (os.path.basename(local_path), exc))

    def find_file(self, folder_id: str, name: str) -> Optional[RemoteEntry]:
        for entry in self.list_dir(folder_id):
            if not entry.is_folder and entry.name == name:
                return entry
        return None

    def verify(self, folder_id: str, name: str, size: int, md5: Optional[str] = None,
               mode: str = "size") -> bool:
        """校验云端文件与本地一致。mode: none / size / md5。"""
        if mode == "none":
            return True
        entry = self.find_file(folder_id, name)
        if entry is None:
            return False
        if entry.size != size:
            return False
        if mode == "md5":
            if not md5:
                return False
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
