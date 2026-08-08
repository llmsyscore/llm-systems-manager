#!/usr/bin/env python3
"""
gen_pwa_icons.py — regenerate the PWA icon set (#522) from the brand badge.

Stdlib-only PNG decode → area-average resize → encode. Emits into
llm-systems-manager/frontend/icons/: icon-512.png (badge as-is),
icon-192.png (192² resize), apple-touch-icon.png (180² on opaque #111).
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "design" / "brand" / "logo-badge-512.png"
OUT_DIR = REPO / "llm-systems-manager" / "frontend" / "icons"
BG = (0x11, 0x11, 0x11)  # opaque backing color for apple-touch/maskable icons
SAFE_ZONE = 0.8          # maskable art occupies the center 80%


def read_png_rgba(path: Path) -> "tuple[int, int, bytearray]":
    if not path.exists():
        raise SystemExit(f"{path}: missing (design/ is untracked; source badge "
                         "exists only on checkouts with the brand assets)")
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit(f"{path}: not a PNG")
    pos, idat, w, h = 8, b"", 0, 0
    while pos < len(data):
        (ln,), typ = struct.unpack(">I", data[pos:pos + 4]), data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            w, h, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
            if (depth, color, interlace) != (8, 6, 0):
                raise SystemExit(f"{path}: need 8-bit RGBA non-interlaced")
        elif typ == b"IDAT":
            idat += chunk
        pos += 12 + ln
    if not (w and h):
        raise SystemExit(f"{path}: no IHDR chunk")
    raw = zlib.decompress(idat)
    stride = w * 4
    out = bytearray(h * stride)
    prev = bytearray(stride)
    for y in range(h):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for x in range(stride):
            a = line[x - 4] if x >= 4 else 0
            b = prev[x]
            c = prev[x - 4] if x >= 4 else 0
            if f == 1:
                line[x] = (line[x] + a) & 0xFF
            elif f == 2:
                line[x] = (line[x] + b) & 0xFF
            elif f == 3:
                line[x] = (line[x] + (a + b) // 2) & 0xFF
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[x] = (line[x] + pred) & 0xFF
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return w, h, out


def resize_rgba(px: bytearray, size: int, new: int) -> bytearray:
    """Area-average downscale of a square RGBA buffer."""
    out = bytearray(new * new * 4)
    ratio = size / new
    for oy in range(new):
        y0, y1 = oy * ratio, (oy + 1) * ratio
        for ox in range(new):
            x0, x1 = ox * ratio, (ox + 1) * ratio
            acc = [0.0, 0.0, 0.0, 0.0]
            area = 0.0
            for sy in range(int(y0), min(int(y1) + 1, size)):
                wy = min(y1, sy + 1) - max(y0, sy)
                if wy <= 0:
                    continue
                row = sy * size * 4
                for sx in range(int(x0), min(int(x1) + 1, size)):
                    wx = min(x1, sx + 1) - max(x0, sx)
                    if wx <= 0:
                        continue
                    wgt = wx * wy
                    o = row + sx * 4
                    alpha = px[o + 3] / 255.0
                    # accumulate alpha-premultiplied color
                    acc[0] += px[o] * alpha * wgt
                    acc[1] += px[o + 1] * alpha * wgt
                    acc[2] += px[o + 2] * alpha * wgt
                    acc[3] += px[o + 3] * wgt
                    area += wgt
            o = (oy * new + ox) * 4
            a = acc[3] / area if area else 0.0
            out[o] = min(255, round(acc[0] / area * (255.0 / a) if a else 0))
            out[o + 1] = min(255, round(acc[1] / area * (255.0 / a) if a else 0))
            out[o + 2] = min(255, round(acc[2] / area * (255.0 / a) if a else 0))
            out[o + 3] = min(255, round(a))
    return out


def paste_center(src: bytearray, src_size: int, canvas_size: int,
                 bg: "tuple[int, int, int]") -> bytearray:
    """Center src on an opaque canvas_size square filled with bg."""
    out = bytearray()
    for _ in range(canvas_size * canvas_size):
        out += bytes((bg[0], bg[1], bg[2], 255))
    off = (canvas_size - src_size) // 2
    for y in range(src_size):
        for x in range(src_size):
            s = (y * src_size + x) * 4
            a = src[s + 3] / 255.0
            d = ((y + off) * canvas_size + (x + off)) * 4
            for c in range(3):
                out[d + c] = round(src[s + c] * a + out[d + c] * (1 - a))
    return out


def flatten(px: bytearray, bg: "tuple[int, int, int]") -> bytearray:
    out = bytearray(len(px))
    for i in range(0, len(px), 4):
        a = px[i + 3] / 255.0
        for c in range(3):
            out[i + c] = round(px[i + c] * a + bg[c] * (1 - a))
        out[i + 3] = 255
    return out


def write_png_rgba(path: Path, px: bytearray, size: int) -> None:
    stride = size * 4
    raw = b"".join(b"\x00" + bytes(px[y * stride:(y + 1) * stride])
                   for y in range(size))

    def chunk(typ: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + typ + body
                + struct.pack(">I", zlib.crc32(typ + body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                     + chunk(b"IDAT", zlib.compress(raw, 9))
                     + chunk(b"IEND", b""))


def main() -> None:
    w, h, px = read_png_rgba(SRC)
    if w != h:
        raise SystemExit(f"{SRC}: expected square, got {w}x{h}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "icon-512.png").write_bytes(SRC.read_bytes())
    write_png_rgba(OUT_DIR / "icon-192.png", resize_rgba(px, w, 192), 192)
    apple = flatten(resize_rgba(px, w, 180), BG)
    write_png_rgba(OUT_DIR / "apple-touch-icon.png", apple, 180)
    # Maskable: art inside the 80% safe zone so launcher masks can't clip it.
    inner = round(512 * SAFE_ZONE)
    write_png_rgba(OUT_DIR / "icon-maskable-512.png",
                   paste_center(resize_rgba(px, w, inner), inner, 512, BG), 512)
    print(f"wrote {OUT_DIR}/icon-512.png, icon-192.png, "
          "apple-touch-icon.png, icon-maskable-512.png")


if __name__ == "__main__":
    sys.exit(main())
