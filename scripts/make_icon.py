"""Render assets/icon.ico (multi-size, PNG-compressed) from the app's drawn icon.

python scripts/make_icon.py
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from xauassist.gui.theme import app_pixmap  # noqa: E402

SIZES = (16, 24, 32, 48, 64, 128, 256)


def png_bytes(size: int) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    app_pixmap(size).save(buffer, "PNG")
    return bytes(data.data())


def main() -> None:
    _app = QApplication.instance() or QApplication([])
    images = [(size, png_bytes(size)) for size in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, payload = b"", b""
    for size, png in images:
        dim = 0 if size >= 256 else size  # 0 means 256 in the ICO format
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset + len(payload))
        payload += png
    out = Path(__file__).resolve().parents[1] / "assets" / "icon.ico"
    out.write_bytes(header + entries + payload)
    print(f"wrote {out} ({out.stat().st_size} bytes, sizes {SIZES})")


if __name__ == "__main__":
    main()
