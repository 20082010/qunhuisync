"""通用小工具：文件匹配、大小格式化、MD5、原子写 JSON。"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import tempfile
import time
from typing import Iterable


def match_any(name: str, rel_path: str, patterns: Iterable[str]) -> bool:
    """判断文件名/相对路径是否命中任一规则。

    - 规则不含 "/" 时按文件名匹配（大小写不敏感），例如 ``*.jpg``
    - 规则含 "/" 时按相对路径匹配，例如 ``子目录/*.mp4``、``**/*.png``
    """
    for raw in patterns or []:
        pattern = (raw or "").strip().replace("\\", "/")
        if not pattern:
            continue
        low = pattern.lower()
        target = rel_path.replace("\\", "/").lower()
        if "/" in low:
            if fnmatch.fnmatchcase(target, low):
                return True
            # "**/x" 同时匹配顶层与任意层级
            if low.startswith("**/") and fnmatch.fnmatchcase(target, low[3:]):
                return True
        else:
            if fnmatch.fnmatchcase(name.lower(), low):
                return True
    return False


def human_size(num: float) -> str:
    """字节数转可读字符串。"""
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return "%.1f %s" % (size, unit)
        size /= 1024
    return "%.1f TB" % size


def file_md5(path: str, chunk_size: int = 1024 * 1024) -> str:
    """计算文件 MD5。"""
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def ensure_dir(path: str) -> str:
    """创建目录（含父级），返回该目录。"""
    if path:
        os.makedirs(path, exist_ok=True)
    return path


def atomic_write_json(path: str, data) -> None:
    """原子写 JSON，避免进程被杀导致配置损坏。"""
    ensure_dir(os.path.dirname(path))
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=os.path.dirname(path) or ".", delete=False, suffix=".tmp"
    )
    try:
        with handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        if os.path.exists(handle.name):
            os.unlink(handle.name)
        raise


def read_json(path: str, default=None):
    """读取 JSON，失败时返回默认值。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def unique_path(path: str) -> str:
    """目标已存在时追加 -1、-2 后缀。"""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    index = 1
    while True:
        candidate = "%s-%d%s" % (base, index, ext)
        if not os.path.exists(candidate):
            return candidate
        index += 1


def now() -> float:
    return time.time()
