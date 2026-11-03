"""同步引擎：扫描 -> 上传 -> 校验 -> （可选）删除源文件。"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .cloud import CloudError, CloudService
from .config import Config, Rule
from .scanner import LocalFile, scan
from .util import file_md5, human_size

log = logging.getLogger("qunhuisync.sync")


class UploadCancelled(Exception):
    """上传被用户取消。"""


@dataclass
class SyncJob:
    """一次同步任务的运行状态。"""

    id: str
    rule_id: str
    rule_name: str = ""
    status: str = "pending"
    started: float = 0.0
    finished: float = 0.0
    scanned: int = 0
    uploaded: int = 0
    skipped: int = 0
    deleted: int = 0
    zero_deleted: int = 0
    failed: int = 0
    bytes_uploaded: int = 0
    message: str = ""
    logs: List[str] = field(default_factory=list)
    # 当前正在处理的文件
    current_index: int = 0
    current_total_files: int = 0
    # 取消标志
    cancel_requested: bool = False
    # 并发上传时保护计数器
    _counter_lock: threading.Lock = field(default_factory=threading.Lock,
                                         repr=False, compare=False)
    # 正在传的文件：rel_path -> {size, done, started_at, last_at, last_done}
    _active_files: Dict[str, Dict] = field(default_factory=dict, repr=False, compare=False)

    def begin_file(self, rel_path: str, size: int) -> None:
        """登记一个正在上传的文件。"""
        now = time.time()
        with self._counter_lock:
            self._active_files[rel_path] = {
                "size": size, "done": 0, "started_at": now,
                "last_at": now, "last_done": 0, "speed": 0.0,
            }

    def update_file(self, rel_path: str, done: int, total: int = 0) -> None:
        """更新某个文件的已传字节，并算瞬时速度。"""
        now = time.time()
        with self._counter_lock:
            entry = self._active_files.get(rel_path)
            if not entry:
                return
            if total:
                entry["size"] = total
            elapsed = now - entry["last_at"]
            if elapsed >= 0.5:
                # 用最近一段时间的增量算速度，避免抖动
                delta = done - entry["last_done"]
                if delta > 0:
                    entry["speed"] = delta / elapsed
                entry["last_at"] = now
                entry["last_done"] = done
            entry["done"] = done

    def end_file(self, rel_path: str) -> None:
        """文件处理完毕。"""
        with self._counter_lock:
            self._active_files.pop(rel_path, None)

    def cancel_file(self, rel_path: str) -> bool:
        """请求取消单个文件。返回 True 表示确实在传。"""
        with self._counter_lock:
            entry = self._active_files.get(rel_path)
            if entry is None:
                return False
            entry["cancelled"] = True
            return True

    def file_cancelled(self, rel_path: str) -> bool:
        """某个文件是否被请求取消。"""
        with self._counter_lock:
            entry = self._active_files.get(rel_path)
            return bool(entry and entry.get("cancelled"))

    def active_files(self) -> List[Dict]:
        """当前所有正在传的文件（按开始时间排序）。"""
        with self._counter_lock:
            items = list(self._active_files.items())
        items.sort(key=lambda kv: kv[1]["started_at"])
        result = []
        for path, entry in items:
            size = int(entry["size"])
            done = int(entry["done"])
            result.append({
                "path": path,
                "name": os.path.basename(path),
                "size": size,
                "done": done,
                "speed": float(entry.get("speed") or 0),
                "percent": int(done * 100 / size) if size else 0,
                "cancelled": bool(entry.get("cancelled")),
                "elapsed": max(0.0, time.time() - entry["started_at"]),
            })
        return result

    def active_file(self) -> Tuple[str, int, int]:
        """取最近活跃的文件：(相对路径, 总大小, 已传大小)。"""
        files = self.active_files()
        if not files:
            return "", 0, 0
        last = files[-1]
        return last["path"], last["size"], last["done"]

    def bump(self, name: str, amount: int = 1) -> None:
        """原子地累加一个计数器（并发上传时用）。"""
        with self._counter_lock:
            setattr(self, name, getattr(self, name) + amount)

    def set_progress(self, name: str, value: int) -> None:
        """原子地设置进度字段。"""
        with self._counter_lock:
            setattr(self, name, value)

    def clear_active(self) -> None:
        """清空正在上传的文件列表。"""
        with self._counter_lock:
            self._active_files.clear()

    def log(self, text: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = "[%s] %s" % (stamp, text)
        with self._counter_lock:
            self.logs.append(line)
            if len(self.logs) > 2000:
                del self.logs[:1000]
        log.info(text)

    def request_cancel(self) -> None:
        """请求取消当前任务。"""
        self.cancel_requested = True

    @property
    def cancelled(self) -> bool:
        return self.cancel_requested

    def to_dict(self) -> Dict:
        path, total, done = self.active_file()
        return {
            "id": self.id,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "status": self.status,
            "started": self.started,
            "finished": self.finished,
            "scanned": self.scanned,
            "uploaded": self.uploaded,
            "skipped": self.skipped,
            "deleted": self.deleted,
            "zero_deleted": self.zero_deleted,
            "failed": self.failed,
            "bytes_uploaded": self.bytes_uploaded,
            "message": self.message,
            "logs": self.logs[-300:],
            "current_file": path,
            "current_total": total,
            "current_done": done,
            "current_index": self.current_index,
            "current_total_files": self.current_total_files,
            "cancel_requested": self.cancel_requested,
            "percent": (int(done * 100 / total) if total else 0),
            # 正在上传的所有文件（各自带进度和速度）
            "files": self.active_files(),
        }


class SyncEngine:
    """执行同步规则。"""

    def __init__(self, config: Config, cloud: CloudService):
        self.config = config
        self.cloud = cloud
        self._lock = threading.Lock()
        self._running: Dict[str, str] = {}
        # 正在运行的任务，供取消操作查找
        self._active: Dict[str, SyncJob] = {}

    # ---------- 任务 ----------
    def running_rules(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._running)

    def is_running(self, rule_id: str) -> bool:
        with self._lock:
            return rule_id in self._running

    def _mark(self, rule_id: str, job_id: str) -> bool:
        with self._lock:
            if rule_id in self._running:
                return False
            self._running[rule_id] = job_id
            return True

    def _unmark(self, rule_id: str) -> None:
        with self._lock:
            self._running.pop(rule_id, None)

    # ---------- 取消 ----------
    def cancel(self, rule_id: str) -> bool:
        """请求取消某条规则正在运行的任务。"""
        with self._lock:
            job = self._active.get(rule_id)
        if job is None:
            return False
        job.request_cancel()
        job.log("收到取消请求，当前文件处理完后停止")
        return True

    def cancel_file(self, rule_id: str, rel_path: str) -> bool:
        """请求取消正在上传的**单个文件**。"""
        with self._lock:
            job = self._active.get(rule_id)
        if job is None:
            return False
        if not job.cancel_file(rel_path):
            return False
        job.log("收到取消请求：%s" % rel_path)
        return True

    # ---------- 上传线程数 ----------
    def upload_threads(self) -> int:
        """同时上传的文件数。

        线程数太多对天翼不友好（容易触发限流），太少又慢，这里默认 3，
        可在账号设置里调整，范围 1-8。
        """
        try:
            value = int(self.config.cloud.get("upload_threads") or 3)
        except (TypeError, ValueError):
            value = 3
        return max(1, min(8, value))

    # ---------- 主流程 ----------
    def run(self, rule: Rule, job: SyncJob) -> SyncJob:
        """同步一条规则（同步执行）。"""
        job.status = "running"
        job.started = time.time()
        job.rule_name = rule.name or rule.local_path
        if not self._mark(rule.id, job.id):
            job.status = "skipped"
            job.message = "该规则正在运行中"
            return job
        with self._lock:
            self._active[rule.id] = job
        try:
            self._run_rule(rule, job)
            if job.cancelled:
                job.status = "cancelled"
                job.message = "已取消"
            elif job.failed:
                job.status = "partial"
                job.message = "完成，但有 %d 个文件失败" % job.failed
            else:
                job.status = "success"
                job.message = "完成"
        except Exception as exc:
            job.status = "failed"
            job.message = str(exc)
            job.log("同步失败：%s" % exc)
        finally:
            job.finished = time.time()
            job.clear_active()
            with self._lock:
                self._active.pop(rule.id, None)
            self._unmark(rule.id)
            self.config.update_rule_state(rule.id, last_run=job.started, last_status=job.status)
        return job

    def _run_rule(self, rule: Rule, job: SyncJob) -> None:
        job.log("开始同步：本地 %s -> 云端 %s" % (rule.local_path, rule.remote_path))

        # ---- 第 1 步：预扫描，把空文件和空目录先处理掉 ----
        # 放在上传之前做，避免边传边判断，白白占用 CPU 和磁盘 IO。
        all_files = scan(rule, include_zero=True)
        empty_files = [f for f in all_files if f.size == 0]
        files = [f for f in all_files if f.size > 0]
        job.scanned = len(files)
        job.current_total_files = len(files)
        job.log("扫描到 %d 个待同步文件" % len(files))

        if rule.delete_zero_byte and empty_files:
            job.log("发现 %d 个 0 字节文件，先行删除" % len(empty_files))
            self._delete_zero_bytes(rule, job, empty_files)
        if rule.delete_empty_dirs:
            self._cleanup_empty_dirs(rule, job)

        if job.cancelled:
            job.log("任务已取消")
            return

        state = self.config.load_state()
        rule_state = state["rules"].setdefault(rule.id, {})
        remote_dirs: Dict[str, str] = {}
        dir_lock = threading.Lock()

        def remote_dir_for(target_dir: str) -> str:
            with dir_lock:
                if target_dir not in remote_dirs:
                    remote_dirs[target_dir] = self.cloud.ensure_folder(target_dir)
                return remote_dirs[target_dir]

        # 先把要传的文件挑出来（跳过已同步的），再并发上传
        pending = []
        for item in files:
            if job.cancelled:
                break
            record = rule_state.get(item.rel_path)
            if (record and record.get("size") == item.size
                    and abs(float(record.get("mtime") or 0) - item.mtime) < 1e-6):
                job.bump('skipped')
                continue
            pending.append(item)

        if pending:
            self._upload_all(rule, job, pending, remote_dir_for, rule_state)

        if job.cancelled:
            job.log("任务已取消")
            return

        try:
            self.config.save_state(state)
        except OSError as exc:
            job.log("保存同步状态失败：%s" % exc)

        job.log(
            "同步结束：上传 %d，跳过 %d，删除源文件 %d，删除 0 字节 %d，失败 %d，传输 %s"
            % (job.uploaded, job.skipped, job.deleted, job.zero_deleted,
               job.failed, human_size(job.bytes_uploaded))
        )

    def _upload_all(self, rule: Rule, job: SyncJob, pending: List[LocalFile],
                    remote_dir_for, rule_state: Dict[str, Dict]) -> None:
        """并发上传待同步的文件。"""
        threads = self.upload_threads()
        if threads > 1 and len(pending) > 1:
            job.log("并发上传：%d 个线程" % threads)
        else:
            threads = 1

        counter = {"index": 0}
        index_lock = threading.Lock()
        state_lock = threading.Lock()

        def worker(item: LocalFile) -> None:
            if job.cancelled:
                return
            with index_lock:
                counter["index"] += 1
                job.set_progress("current_index", counter["index"])
            self._sync_one(rule, job, item, remote_dir_for, rule_state, state_lock)

        if threads == 1:
            for item in pending:
                if job.cancelled:
                    break
                worker(item)
            return

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=threads, thread_name_prefix="qhs-up") as pool:
            list(pool.map(worker, pending))

    def _sync_one(self, rule: Rule, job: SyncJob, item: LocalFile, remote_dir_for,
                  rule_state: Dict[str, Dict], state_lock: Optional[threading.Lock] = None) -> None:
        # 同步记录命中：大小与修改时间都没变，说明上次已经同步过
        record = rule_state.get(item.rel_path)
        if (record and record.get("size") == item.size
                and abs(float(record.get("mtime") or 0) - item.mtime) < 1e-6):
            job.bump('skipped')
            return

        if job.cancelled:
            return

        # 并发时可能有多个文件同时在传，这里登记「正在传的文件」，
        # 界面会把所有并发文件都列出来。
        job.begin_file(item.rel_path, item.size)
        try:
            self._upload_one(rule, job, item, remote_dir_for, rule_state, state_lock)
        finally:
            # 无论成功、跳过还是失败，都要摘掉登记，
            # 否则界面会一直显示这个文件「上传中」
            job.end_file(item.rel_path)

    def _upload_one(self, rule: Rule, job: SyncJob, item: LocalFile, remote_dir_for,
                    rule_state: Dict[str, Dict], state_lock: Optional[threading.Lock]) -> None:
        """上传单个文件（调用方负责登记/摘除进度）。"""
        target_dir = rule.remote_path
        target_name = os.path.basename(item.rel_path)
        if rule.preserve_dir_structure:
            parent = os.path.dirname(item.rel_path)
            if parent:
                target_dir = "/".join(part for part in [rule.remote_path.rstrip("/"), parent] if part)
        else:
            target_name = os.path.basename(item.path)

        try:
            folder_id = remote_dir_for(target_dir)
        except CloudError as exc:
            job.bump('failed')
            job.log("跳过 %s：%s" % (item.rel_path, exc))
            return

        existing = None
        try:
            existing = self.cloud.find_file(folder_id, target_name)
        except CloudError as exc:
            job.log("读取云端目录失败：%s" % exc)

        if existing is not None and existing.size == item.size:
            job.bump('skipped')
            self._remember(rule_state, state_lock, item, target_name)
            return

        if existing is not None and rule.overwrite:
            try:
                self.cloud.delete_remote(existing.id, existing.name)
                job.log("覆盖远端同名文件：%s" % target_name)
            except CloudError as exc:
                job.bump('failed')
                job.log("删除云端文件失败 %s：%s" % (target_name, exc))
                return

        local_md5 = None
        if rule.verify_mode == "md5":
            try:
                local_md5 = file_md5(item.path)
            except OSError as exc:
                job.log("计算本地 MD5 失败：%s" % exc)

        def on_progress(done: int, total: int = 0) -> None:
            """上传进度回调。"""
            job.update_file(item.rel_path, done, total)

        def should_cancel() -> bool:
            """整个任务取消，或这个文件单独被取消。"""
            return job.cancelled or job.file_cancelled(item.rel_path)

        try:
            self.cloud.upload(item.path, folder_id, None, on_progress,
                              should_cancel=should_cancel)
        except CloudError as exc:
            if job.file_cancelled(item.rel_path):
                job.log("已取消上传 %s" % item.rel_path)
                return
            job.bump('failed')
            job.log("上传失败 %s：%s" % (item.rel_path, exc))
            return
        except UploadCancelled:
            job.log("已取消上传 %s" % item.rel_path)
            return

        job.bump('uploaded')
        job.bump('bytes_uploaded', item.size)

        if rule.verify_mode != "none":
            try:
                ok = self.cloud.verify(folder_id, target_name, item.size, local_md5, rule.verify_mode)
            except CloudError as exc:
                ok = False
                job.log("校验异常 %s：%s" % (target_name, exc))
            if not ok:
                job.bump('failed')
                job.log("校验未通过，保留源文件：%s" % item.rel_path)
                return

        self._remember(rule_state, state_lock, item, target_name)

        # 同步成功后直接删除源文件（不再走回收站）
        if rule.delete_source:
            try:
                os.unlink(item.path)
                job.bump('deleted')
            except OSError as exc:
                job.bump('failed')
                job.log("删除源文件失败 %s：%s" % (item.rel_path, exc))

    # ---------- 删除源文件 ----------
    def _delete_zero_bytes(self, rule: Rule, job: SyncJob,
                           items: List[LocalFile]) -> None:
        """直接删除 0 字节文件。

        空文件没有任何内容，直接删掉，不进回收站。删除失败会记进日志，
        不影响其它文件。
        """
        removed = 0
        for item in items:
            if job.cancelled:
                break
            try:
                os.unlink(item.path)
            except OSError as exc:
                job.bump('failed')
                job.log("删除 0 字节文件失败 %s：%s" % (item.rel_path, exc))
                continue
            removed += 1
            job.bump('zero_deleted')
        if removed:
            job.log("删除 0 字节文件 %d 个" % removed)

    @staticmethod
    def _remember(rule_state: Dict[str, Dict], state_lock: Optional[threading.Lock],
                  item: LocalFile, target_name: str) -> None:
        """记录已同步的文件（并发时加锁）。"""
        entry = {"size": item.size, "mtime": item.mtime, "name": target_name}
        if state_lock is not None:
            with state_lock:
                rule_state[item.rel_path] = entry
        else:
            rule_state[item.rel_path] = entry

    def _cleanup_empty_dirs(self, rule: Rule, job: SyncJob) -> None:
        root = os.path.abspath(rule.local_path)
        if not os.path.isdir(root):
            return
        removed = 0
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            if os.path.abspath(dirpath) == os.path.abspath(root):
                continue
            if os.path.basename(dirpath) in set(rule.exclude_dirs or []):
                continue
            if not dirnames and not filenames:
                try:
                    os.rmdir(dirpath)
                    removed += 1
                except OSError:
                    pass
        if removed:
            job.log("清理空目录 %d 个" % removed)

    # ---------- 回收站（已废弃） ----------
    def trash_files(self, rule: Rule) -> List[Dict[str, object]]:
        """回收站机制已取消：同步成功后直接删除源文件，不再保留副本。"""
        return []

    def delete_trash_files(self, rule: Rule, rel_paths: List[str]) -> int:
        return 0

    def purge_trash(self, rule: Rule, retention_days: Optional[int] = None) -> int:
        return 0

    def trash_size(self, rule: Rule) -> int:
        return 0
