"""Price-vs-MA geometry shared by both scenarios. Every function is direction-aware.

SELL = bearish trend: the "trend side" is below the MA.  BUY mirrors it.
"""

from __future__ import annotations

from xauassist.config.models import ZoneConfig
from xauassist.domain.bar import Bar
from xauassist.domain.enums import Direction, ZoneMode


def zone_half_width(cfg: ZoneConfig, ma: float, atr: float | None) -> float | None:
    """Half-width of the MA proximity zone, or None if it cannot be computed yet."""
    if cfg.mode is ZoneMode.PRICE:
        return cfg.value
    if cfg.mode is ZoneMode.PERCENT:
        return abs(ma) * cfg.value / 100.0
    return None if atr is None else cfg.value * atr


def reaches_zone_from_trend_side(bar: Bar, direction: Direction, half_width: float) -> bool:
    """Price rising into the zone from below (SELL) / falling into it from above (BUY)."""
    if direction is Direction.SELL:
        return bar.candle.high >= bar.ma - half_width
    return bar.candle.low <= bar.ma + half_width


def reaches_zone_from_break_side(bar: Bar, direction: Direction, half_width: float) -> bool:
    """Pullback into the zone after a break: falling into it (SELL) / rising into it (BUY)."""
    if direction is Direction.SELL:
        return bar.candle.low <= bar.ma + half_width
    return bar.candle.high >= bar.ma - half_width


def closes_on_trend_side(bar: Bar, direction: Direction, tolerance: float) -> bool:
    """Close at or beyond the MA on the trend side, allowing `tolerance` the wrong way."""
    if direction is Direction.SELL:
        return bar.candle.close <= bar.ma + tolerance
    return bar.candle.close >= bar.ma - tolerance


def closes_beyond_ma(bar: Bar, direction: Direction, buffer: float) -> bool:
    """Close on the opposite side of the MA to the trend (a break), by more than `buffer`."""
    if direction is Direction.SELL:
        return bar.candle.close > bar.ma + buffer
    return bar.candle.close < bar.ma - buffer
