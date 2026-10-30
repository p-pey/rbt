"""Pin bar detection."""

from __future__ import annotations

from xauassist.config.models import PinBarConfig
from xauassist.domain.candle import Candle


def is_bearish_pin_bar(candle: Candle, cfg: PinBarConfig) -> bool:
    return _is_pin_bar(candle, cfg, bearish=True)


def is_bullish_pin_bar(candle: Candle, cfg: PinBarConfig) -> bool:
    return _is_pin_bar(candle, cfg, bearish=False)


def _is_pin_bar(candle: Candle, cfg: PinBarConfig, *, bearish: bool) -> bool:
    rng = candle.range
    if rng <= 0 or rng < cfg.min_range:
        return False
    # A zero-sized body must not make the ratio test trivially true or divide by zero.
    body = max(candle.body, cfg.min_body_frac * rng)
    if bearish:
        dominant, opposite = candle.upper_wick, candle.lower_wick
    else:
        dominant, opposite = candle.lower_wick, candle.upper_wick
    if dominant < cfg.wick_body_ratio * body:
        return False
    if opposite > cfg.opposite_wick_max_frac * rng:
        return False
    if bearish:
        directional = candle.is_bearish
        rejected = candle.close <= candle.low + cfg.close_zone_frac * rng
    else:
        directional = candle.is_bullish
        rejected = candle.close >= candle.high - cfg.close_zone_frac * rng
    return directional or rejected
