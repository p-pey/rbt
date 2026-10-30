"""OHLC candle model."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Candle:
    """A single OHLC candle. `time_open` must be timezone-aware (UTC recommended)."""

    time_open: datetime
    open: float
    high: float
    low: float
    close: float
    tick_volume: int = 0
    spread_points: float = 0.0
    is_closed: bool = True

    def __post_init__(self) -> None:
        if self.time_open.tzinfo is None:
            raise ValueError("Candle.time_open must be timezone-aware")
        prices = (self.open, self.high, self.low, self.close)
        if not all(math.isfinite(p) for p in prices):
            raise ValueError(f"Candle has non-finite price: {prices}")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError(f"Candle high below open/close/low: {prices}")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError(f"Candle low above open/close/high: {prices}")

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open
