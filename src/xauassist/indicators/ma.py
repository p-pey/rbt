"""Incremental moving averages (EMA seeded like MetaTrader 5, and SMA)."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from xauassist.domain.enums import MaMethod


class MovingAverage:
    """Feed one price at a time; `update` returns the MA, or None until `period` prices were seen.

    EMA: alpha = 2 / (period + 1), seeded with the first price (as MT5's built-in MA does),
    so values converge to the chart's once ~5 * period bars have been processed.
    """

    def __init__(self, period: int, method: MaMethod) -> None:
        if period < 2:
            raise ValueError("period must be >= 2")
        self._period = period
        self._method = method
        self._alpha = 2.0 / (period + 1)
        self._count = 0
        self._ema: float | None = None
        self._window: deque[float] = deque(maxlen=period)

    def update(self, price: float) -> float | None:
        self._count += 1
        if self._method is MaMethod.EMA:
            if self._ema is None:
                self._ema = price
            else:
                self._ema = self._alpha * price + (1.0 - self._alpha) * self._ema
            return self._ema if self._count >= self._period else None
        self._window.append(price)
        if len(self._window) < self._period:
            return None
        return sum(self._window) / self._period


def compute_ma(prices: Iterable[float], period: int, method: MaMethod) -> list[float | None]:
    ma = MovingAverage(period, method)
    return [ma.update(p) for p in prices]
