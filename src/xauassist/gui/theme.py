"""Colours and small drawing helpers."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap

from xauassist.gui.view_models import Level

LEVEL_COLORS: dict[Level, str] = {
    Level.OK: "#2e7d32",
    Level.INFO: "#1565c0",
    Level.WARN: "#b26a00",
    Level.ERROR: "#c62828",
    Level.IDLE: "#616161",
}
BUY_COLOR = "#2e7d32"
SELL_COLOR = "#c62828"


def pill_style(level: Level) -> str:
    return (
        f"background-color: {LEVEL_COLORS[level]}; color: white; border-radius: 9px;"
        " padding: 3px 10px; font-weight: 600;"
    )


def app_pixmap(size: int = 64) -> QPixmap:
    """A simple gold coin drawn at runtime (no image asset needed)."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    margin = max(1, round(size * 0.05))
    painter.setBrush(QColor("#d4a017"))
    painter.setPen(QColor("#8a6508"))
    painter.drawEllipse(margin, margin, size - 2 * margin, size - 2 * margin)
    painter.setPen(QColor("#4a3500"))
    font = QFont()
    font.setBold(True)
    font.setPixelSize(max(6, round(size * 0.41)))
    painter.setFont(font)
    painter.drawText(pixmap.rect(), int(Qt.AlignmentFlag.AlignCenter), "Au")
    painter.end()
    return pixmap


def app_icon() -> QIcon:
    return QIcon(app_pixmap(64))
