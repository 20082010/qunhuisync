"""本地扫描：按目录、文件类型、排除规则挑出待同步文件。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List

from .config import Rule
from .util import match_any


@dataclass
class LocalFile:
    path: str
    rel_path: str
    size: int
    mtime: float

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


def scan(rule: Rule, include_zero: bool = False) -> List[LocalFile]:
    """扫描规则对应的本地目录，返回待同步文件列表。

    ``include_zero`` 为 True 时额外返回 0 字节文件——它们用来做「本地直接删除」，
    与「最小文件大小」这个上传筛选条件无关：0 字节文件永远不会被上传
    （天翼不接受空文件），要不要删由规则单独决定。
    """
    root = os.path.abspath(os.path.expanduser(rule.local_path))
    if not os.path.isdir(root):
        raise FileNotFoundError("本地目录不存在：%s" % root)

    excluded_dirs = set(rule.exclude_dirs or [])
    min_size = int(rule.min_size_bytes or 0)
    results: List[LocalFile] = []

    if not rule.recursive:
        walk_root = root
        entries = []
        for name in sorted(os.listdir(walk_root)):
            full = os.path.join(walk_root, name)
            if os.path.isfile(full):
                entries.append(full)
    else:
        entries = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in excluded_dirs)
            for name in sorted(filenames):
                entries.append(os.path.join(dirpath, name))

    for path in entries:
        name = os.path.basename(path)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        if not os.path.isfile(path):
            continue
        if stat.st_size == 0:
            # 空文件不会上传，只在需要清理它时才收进来
            if not include_zero:
                continue
        elif stat.st_size < min_size:
            continue
        rel_path = os.path.relpath(path, root).replace("\\", "/")
        if rule.include and not match_any(name, rel_path, rule.include):
            continue
        if rule.exclude and match_any(name, rel_path, rule.exclude):
            continue
        results.append(LocalFile(path=path, rel_path=rel_path, size=stat.st_size, mtime=stat.st_mtime))
    return results
