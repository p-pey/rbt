"""Trend detection on the trading timeframe."""

from __future__ import annotations

from collections.abc import Sequence

from xauassist.config.models import TrendConfig
from xauassist.domain.bar import Bar
from xauassist.domain.enums import Direction


def on_trend_side(bar: Bar, direction: Direction) -> bool:
    """SELL trend side = close below MA; BUY trend side = close above MA."""
    if direction is Direction.SELL:
        return bar.candle.close < bar.ma
    return bar.candle.close > bar.ma


def slope_at(bars: Sequence[Bar], index: int, lookback: int) -> float | None:
    """MA change per bar over `lookback` bars ending at `index`; None if not enough history."""
    start = index - lookback
    if start < 0 or index >= len(bars):
        return None
    return (bars[index].ma - bars[start].ma) / lookback


def trend_matches(direction: Direction, bar: Bar, slope: float | None, cfg: TrendConfig) -> bool:
    """Price on the trend side of the MA and (if enabled) MA sloping in the trend direction."""
    if not on_trend_side(bar, direction):
        return False
    if not cfg.slope_filter_enabled:
        return True
    if slope is None:
        return False
    if direction is Direction.SELL:
        return slope < -cfg.min_slope
    return slope > cfg.min_slope
