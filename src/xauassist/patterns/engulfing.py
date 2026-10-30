"""Engulfing candle detection (real bodies, completed candles only)."""

from __future__ import annotations

from xauassist.config.models import EngulfingConfig
from xauassist.domain.candle import Candle


def is_bearish_engulfing(prev: Candle, cur: Candle, cfg: EngulfingConfig) -> bool:
    if not (prev.is_bullish and cur.is_bearish) or cur.body < cfg.min_body:
        return False
    if cfg.strict:
        return cur.open > prev.close and cur.close < prev.open
    return cur.open >= prev.close and cur.close <= prev.open


def is_bullish_engulfing(prev: Candle, cur: Candle, cfg: EngulfingConfig) -> bool:
    if not (prev.is_bearish and cur.is_bullish) or cur.body < cfg.min_body:
        return False
    if cfg.strict:
        return cur.open < prev.close and cur.close > prev.open
    return cur.open <= prev.close and cur.close >= prev.open
