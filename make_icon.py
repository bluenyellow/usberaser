#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate floppy.ico — a small floppy-disk icon for USB Eraser.

Pure standard library (struct + zlib), no Pillow required. Writes a
multi-resolution .ico containing PNG images at 16, 32, 48, 64, 128 and 256
pixels so Windows can pick the right size for the taskbar, Explorer and the
window title bar.

Run it once before building the exe:

    python make_icon.py
"""

import os
import struct
import zlib

# Master drawing canvas. Everything is authored at 256x256 and downscaled.
MASTER = 256

# --- Palette -------------------------------------------------------------
BODY          = (34, 34, 38, 255)       # dark charcoal body
BODY_EDGE     = (18, 18, 22, 255)       # darker bevel
SHUTTER       = (192, 197, 206, 255)    # silver metal shutter
SHUTTER_HI    = (232, 236, 242, 255)    # highlight
SHUTTER_LOW   = (130, 135, 145, 255)    # shadow
SHUTTER_SLOT  = (46, 48, 56, 255)       # dark slot in the shutter
LABEL         = (245, 240, 224, 255)    # cream paper label
LABEL_LINE    = (170, 168, 156, 255)    # grey lines drawn on the label
CLEAR         = (0, 0, 0, 0)

def _rrect(x, y, x0, y0, x1, y1, r):
    """True when (x, y) lies inside the rounded rectangle (x0,y0)-(x1,y1)."""
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    if x < x0 + r and y < y0 + r:
        return (x - (x0 + r)) ** 2 + (y - (y0 + r)) ** 2 <= r * r
    if x > x1 - r and y < y0 + r:
        return (x - (x1 - r)) ** 2 + (y - (y0 + r)) ** 2 <= r * r
    if x < x0 + r and y > y1 - r:
        return (x - (x0 + r)) ** 2 + (y - (y1 - r)) ** 2 <= r * r
    if x > x1 - r and y > y1 - r:
        return (x - (x1 - r)) ** 2 + (y - (y1 - r)) ** 2 <= r * r
    return True

def _pixel(x, y):
    """Return RGBA for one pixel of the 256x256 master image."""
    # --- Body ----------------------------------------------------------
    if not _rrect(x, y, 20, 20, 235, 235, 22):
        return CLEAR

    color = BODY
    if x < 25 or y < 25 or x > 230 or y > 230:
        color = BODY_EDGE

    # --- Metal shutter at the top -------------------------------------
    if 84 <= x < 172 and 28 <= y < 116:
        color = SHUTTER
        if 28 <= y < 34 or 84 <= x < 90:          # top / left highlight
            color = SHUTTER_HI
        if 110 <= y < 116 or 166 <= x < 172:      # bottom / right shadow
            color = SHUTTER_LOW
        if 100 <= x < 156 and 52 <= y < 92:       # slot through the shutter
            color = SHUTTER_SLOT

    # --- Paper label on the lower half --------------------------------
    if 48 <= x < 208 and 132 <= y < 218:
        color = LABEL
        if 68 <= x < 188 and 150 <= y < 156:      # first ruled line
            color = LABEL_LINE
        if 68 <= x < 188 and 170 <= y < 176:      # second ruled line
            color = LABEL_LINE
        if 68 <= x < 156 and 190 <= y < 196:      # short third line
            color = LABEL_LINE

    return color

def _render(size):
    """Render the icon at `size` x `size` and return raw RGBA bytes."""
    if size == MASTER:
        out = bytearray()
        for y in range(MASTER):
            for x in range(MASTER):
                out.extend(_pixel(x, y))
        return bytes(out)

    # Nearest-neighbour downscale — crisp enough for this flat icon.
    scale = MASTER / size
    out = bytearray()
    for dy in range(size):
        sy = int(dy * scale)
        for dx in range(size):
            sx = int(dx * scale)
            out.extend(_pixel(sx, sy))
    return bytes(out)

def _png_chunk(typ, data):
    return (struct.pack(">I", len(data)) + typ + data +
            struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

def _encode_png(rgba, w, h):
    """Encode raw RGBA bytes as an 8-bit PNG."""
    raw = bytearray()
    stride = w * 4
    for y in range(h):
        raw.append(0)                              # filter: None
        raw.extend(rgba[y * stride:(y + 1) * stride])
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" +
            _png_chunk(b"IHDR", ihdr) +
            _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9)) +
            _png_chunk(b"IEND", b""))

def _write_ico(path, images):
    """images = [(size, png_bytes), ...]"""
    out = bytearray()
    out += struct.pack("<HHH", 0, 1, len(images))          # ICONDIR
    offset = 6 + 16 * len(images)
    for size, data in images:
        w = 0 if size >= 256 else size                     # 0 means 256 in ICO
        h = 0 if size >= 256 else size
        out += struct.pack("<BBBBHHII",
                           w, h, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for _size, data in images:
        out += data
    with open(path, "wb") as fh:
        fh.write(out)

def main():
    sizes = (16, 32, 48, 64, 128, 256)
    images = [(s, _encode_png(_render(s), s, s)) for s in sizes]

    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "floppy.ico")
    _write_ico(path, images)
    print(f"Wrote {path}  ({os.path.getsize(path)} bytes, {len(sizes)} sizes)")

if __name__ == "__main__":
    main()
