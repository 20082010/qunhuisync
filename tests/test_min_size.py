"""最小文件大小（字节）的测试：单位换算与旧配置兼容。"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from qunhuisync.config import Rule  # noqa: E402
from qunhuisync.scanner import scan  # noqa: E402


def test_reads_bytes_directly():
    rule = Rule.from_dict({"min_size_bytes": 1024})
    assert rule.min_size_bytes == 1024


def test_converts_legacy_mb_config():
    """旧配置存的是 MB，升级后不能丢设置。"""
    rule = Rule.from_dict({"min_size_mb": 10})
    assert rule.min_size_bytes == 10 * 1024 * 1024


def test_bytes_takes_precedence_over_mb():
    rule = Rule.from_dict({"min_size_bytes": 2048, "min_size_mb": 10})
    assert rule.min_size_bytes == 2048


def test_defaults_to_zero():
    assert Rule.from_dict({}).min_size_bytes == 0


def test_negative_becomes_zero():
    assert Rule.from_dict({"min_size_bytes": -5}).min_size_bytes == 0


def test_to_dict_writes_bytes():
    rule = Rule.from_dict({"min_size_bytes": 1024})
    assert rule.to_dict()["min_size_bytes"] == 1024


def test_scanner_filters_by_bytes(tmp_path):
    """扫描时按字节阈值过滤。"""
    root = tmp_path / "videos"
    root.mkdir()
    (root / "small.mp4").write_bytes(b"x" * 1023)
    (root / "exact.mp4").write_bytes(b"x" * 1024)
    (root / "big.mp4").write_bytes(b"x" * 2048)

    rule = Rule.from_dict({
        "local_path": str(root),
        "include": ["*.mp4"],
        "min_size_bytes": 1024,
    })
    names = sorted(item.name for item in scan(rule))
    assert names == ["big.mp4", "exact.mp4"]


def test_scanner_with_one_kb_threshold(tmp_path):
    """1KB 阈值：1024 字节保留，1023 字节跳过。"""
    root = tmp_path / "videos"
    root.mkdir()
    (root / "a.mp4").write_bytes(b"x" * 1023)
    (root / "b.mp4").write_bytes(b"x" * 1024)
    (root / "c.mp4").write_bytes(b"x" * 1025)

    rule = Rule.from_dict({
        "local_path": str(root),
        "include": ["*.mp4"],
        "min_size_bytes": 1024,
    })
    names = sorted(item.name for item in scan(rule))
    assert names == ["b.mp4", "c.mp4"]


def test_include_filters_by_extension(tmp_path):
    """文件类型过滤：只留 .mp4。"""
    root = tmp_path / "mixed"
    root.mkdir()
    (root / "a.mp4").write_bytes(b"x" * 2048)
    (root / "b.avi").write_bytes(b"x" * 2048)
    (root / "c.mp4.part").write_bytes(b"x" * 2048)

    rule = Rule.from_dict({
        "local_path": str(root),
        "include": ["*.mp4"],
        "min_size_bytes": 1024,
    })
    names = sorted(item.name for item in scan(rule))
    assert names == ["a.mp4"]


def test_include_is_case_insensitive(tmp_path):
    root = tmp_path / "mixed"
    root.mkdir()
    (root / "A.MP4").write_bytes(b"x" * 2048)
    rule = Rule.from_dict({"local_path": str(root), "include": ["*.mp4"]})
    assert [item.name for item in scan(rule)] == ["A.MP4"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
