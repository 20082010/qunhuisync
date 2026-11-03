"""通用小工具：文件匹配、大小格式化、MD5、原子写 JSON。"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
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


# 天翼网盘不接受文件名里的 emoji（4 字节的字符，比如 ������）。
#
# 实测：文件名里只要有任何一个 emoji，commit 阶段必定报
# ``SaveFileError``，而且重试多少次都没用（PUT 阶段是成功的，
# 是保存的时候失败）。中文、日文、全角引号、@、#、超长名（200 字符）
# 都正常，只有 emoji 不行。
#
# 所以上传前把 emoji 换成下划线，保证文件能传上去。
_EMOJI_PATTERN = re.compile(
    "[" "\U0001F000-\U0001FAFF"      # 各类 emoji 图形
    "\U00002600-\U000027BF"          # 杂项符号与箭头
    "\U0001F1E6-\U0001F1FF"          # 地区指示符号
    "\U0000FE0F"                     # 变体选择符
    "\U0001F3FB-\U0001F3FF"          # 肤色修饰
    "]"
)


def has_emoji(name: str) -> bool:
    """文件名里是否含 emoji。"""
    return bool(_EMOJI_PATTERN.search(name or ""))


def sanitize_remote_name(name: str) -> str:
    """把文件名里的 emoji 换成下划线。

    天翼网盘存不了带 emoji 的文件名（实测必报 SaveFileError），
    所以上传前先替换掉，保证文件能传上去。

    只动 emoji，其余字符（中文、日文、标点）原样保留。
    """
    if not name:
        return name
    cleaned = _EMOJI_PATTERN.sub("_", name)
    # 万一替换后变成空名字，给个兜底
    return cleaned or "unnamed"


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
