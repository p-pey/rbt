"""Average True Range (Wilder smoothing)."""

from __future__ import annotations


class AverageTrueRange:
    """`update` returns None until `period` candles were seen; seeded with the mean TR."""

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("period must be >= 1")
        self._period = period
        self._prev_close: float | None = None
        self._count = 0
        self._sum = 0.0
        self._atr: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
        self._prev_close = close
        self._count += 1
        if self._atr is None:
            self._sum += tr
            if self._count >= self._period:
                self._atr = self._sum / self._period
            return self._atr
        self._atr = (self._atr * (self._period - 1) + tr) / self._period
        return self._atr
