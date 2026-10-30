"""Directional confirmation: which candlestick patterns confirm a BUY or SELL."""

from __future__ import annotations

from xauassist.config.models import EngulfingConfig, PinBarConfig
from xauassist.domain.candle import Candle
from xauassist.domain.enums import Direction, Pattern
from xauassist.patterns.engulfing import is_bearish_engulfing, is_bullish_engulfing
from xauassist.patterns.pin_bar import is_bearish_pin_bar, is_bullish_pin_bar


def confirmation_patterns(
    prev: Candle,
    cur: Candle,
    direction: Direction,
    pin_cfg: PinBarConfig,
    engulf_cfg: EngulfingConfig,
) -> tuple[Pattern, ...]:
    found: list[Pattern] = []
    if direction is Direction.SELL:
        if is_bearish_pin_bar(cur, pin_cfg):
            found.append(Pattern.BEARISH_PIN_BAR)
        if is_bearish_engulfing(prev, cur, engulf_cfg):
            found.append(Pattern.BEARISH_ENGULFING)
    else:
        if is_bullish_pin_bar(cur, pin_cfg):
            found.append(Pattern.BULLISH_PIN_BAR)
        if is_bullish_engulfing(prev, cur, engulf_cfg):
            found.append(Pattern.BULLISH_ENGULFING)
    return tuple(found)
