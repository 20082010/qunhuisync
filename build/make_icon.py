"""生成 SPK 图标（纯标准库写 PNG，无需 Pillow）。

DSM 7 要求：
- PACKAGE_ICON.PNG      64 x 64
- PACKAGE_ICON_256.PNG  256 x 256
"""
from __future__ import annotations

import struct
import zlib
from typing import List, Tuple

BG_TOP = (76, 141, 255)
BG_BOTTOM = (55, 208, 166)
CLOUD = (255, 255, 255)
MARK = (32, 96, 208)

# DSM 7 要求的图标尺寸
ICON_SIZE = 64
ICON_256_SIZE = 256

SS = 4  # 超采样倍数


def _in_rrect(x: float, y: float, x0: float, y0: float, x1: float, y1: float, r: float) -> bool:
    """点是否落在圆角矩形内。"""
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    near_left = x < x0 + r
    near_right = x > x1 - r
    near_top = y < y0 + r
    near_bottom = y > y1 - r
    if (near_left or near_right) and (near_top or near_bottom):
        cx = x0 + r if near_left else x1 - r
        cy = y0 + r if near_top else y1 - r
        return (x - cx) ** 2 + (y - cy) ** 2 <= r * r
    return True


def _in_circle(x: float, y: float, cx: float, cy: float, r: float) -> bool:
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _in_segment(x: float, y: float, x1: float, y1: float, x2: float, y2: float, t: float) -> bool:
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return False
    pos = ((x - x1) * dx + (y - y1) * dy) / length_sq
    pos = max(0.0, min(1.0, pos))
    px, py = x1 + pos * dx, y1 + pos * dy
    return (x - px) ** 2 + (y - py) ** 2 <= t * t


def _blend(top: Tuple[int, int, int], bottom: Tuple[int, int, int], alpha: float):
    return tuple(int(t + (b - t) * alpha) for t, b in zip(top, bottom))


def _render(size: int) -> List[List[Tuple[int, int, int, int]]]:
    """按 128 单位坐标系渲染，再降采样到目标尺寸。"""
    big = size * SS
    s = big / 128.0

    bg = (8 * s, 8 * s, 120 * s, 120 * s, 28 * s)
    cloud_circles = [
        (47 * s, 68 * s, 18 * s),
        (70 * s, 57 * s, 24 * s),
        (92 * s, 70 * s, 16 * s),
    ]
    cloud_rect = (30 * s, 66 * s, 94 * s, 90 * s, 12 * s)
    check = (
        (52 * s, 74 * s, 63 * s, 84 * s, 4.5 * s),
        (63 * s, 84 * s, 80 * s, 62 * s, 4.5 * s),
    )

    pixels: List[List[Tuple[int, int, int, int]]] = []
    for py in range(big):
        row = []
        y = float(py) + 0.5
        for px in range(big):
            x = float(px) + 0.5
            if not _in_rrect(x, y, *bg):
                row.append((0, 0, 0, 0))
                continue
            color = _blend(BG_TOP, BG_BOTTOM, y / big)
            alpha = 255
            in_cloud = (_in_rrect(x, y, *cloud_rect)
                        or any(_in_circle(x, y, *circle) for circle in cloud_circles))
            if in_cloud:
                color = CLOUD
                if any(_in_segment(x, y, *segment) for segment in check):
                    color = MARK
            row.append((color[0], color[1], color[2], alpha))
        pixels.append(row)

    # 降采样
    out: List[List[Tuple[int, int, int, int]]] = []
    for y in range(size):
        row = []
        for x in range(size):
            r = g = b = a = 0
            for sy in range(SS):
                for sx in range(SS):
                    pr, pg, pb, pa = pixels[y * SS + sy][x * SS + sx]
                    r += pr * pa
                    g += pg * pa
                    b += pb * pa
                    a += pa
            if a == 0:
                row.append((0, 0, 0, 0))
            else:
                row.append((r // a, g // a, b // a, a // (SS * SS)))
        out.append(row)
    return out


def _write_png(path: str, pixels: List[List[Tuple[int, int, int, int]]]) -> None:
    """写 RGBA PNG（color type 6），圆角处透明。"""
    size = len(pixels)
    raw = bytearray()
    for row in pixels:
        raw.append(0)  # filter type 0
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))
    with open(path, "wb") as handle:
        handle.write(png)


def create_icon(path: str, size: int = ICON_SIZE) -> None:
    """生成指定尺寸的 RGBA PNG 图标。"""
    _write_png(path, _render(size))


def create_icons(directory: str) -> None:
    """生成 DSM 7 需要的一整套图标。"""
    import os

    create_icon(os.path.join(directory, "PACKAGE_ICON.PNG"), ICON_SIZE)
    create_icon(os.path.join(directory, "PACKAGE_ICON_256.PNG"), ICON_256_SIZE)


if __name__ == "__main__":
    import os

    target = os.path.dirname(os.path.abspath(__file__))
    create_icons(target)
    print("icons written to: %s" % target)
