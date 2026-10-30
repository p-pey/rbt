"""Synthetic XAUUSD-like M5 candles for tests and as an honesty check.

A random walk has NO exploitable pattern by construction, so a correctly built backtester must
find no edge on it (and, with spread costs, a slightly negative result). Never use this data to
judge the strategy; it only exercises the machinery. Includes the daily break and weekend gaps.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from xauassist.domain.candle import Candle


def _market_open(t: datetime) -> bool:
    minute_of_day = t.hour * 60 + t.minute
    if t.weekday() == 5:  # Saturday
        return False
    if t.weekday() == 4 and minute_of_day >= 22 * 60:  # Friday close 22:00 UTC
        return False
    if t.weekday() == 6 and minute_of_day < 23 * 60:  # Sunday open 23:00 UTC
        return False
    return not (22 * 60 <= minute_of_day < 23 * 60)  # daily break


def generate_candles(
    n: int,
    *,
    seed: int = 1,
    start: datetime = datetime(2024, 1, 1, tzinfo=UTC),  # a Monday
    volatility: float = 0.8,
    start_price: float = 2000.0,
) -> list[Candle]:
    rng = random.Random(seed)
    candles: list[Candle] = []
    t = start
    price = start_price
    while len(candles) < n:
        if _market_open(t):
            # Student-t-ish returns (fat tails) with slowly varying volatility.
            shock = rng.gauss(0, 1) / max(0.3, (rng.gammavariate(2.0, 0.5)) ** 0.5)
            vol = volatility * (1 + 0.5 * rng.random())
            close = price + shock * vol * 0.6
            wick_up = abs(rng.gauss(0, vol * 0.35))
            wick_dn = abs(rng.gauss(0, vol * 0.35))
            high = max(price, close) + wick_up
            low = min(price, close) - wick_dn
            candles.append(
                Candle(
                    time_open=t,
                    open=round(price, 2),
                    high=round(high, 2),
                    low=round(low, 2),
                    close=round(close, 2),
                    tick_volume=rng.randint(50, 400),
                    spread_points=float(min(45, max(15, int(rng.gauss(25, 5))))),
                )
            )
            price = round(close, 2)
            # Rounding can pull open/close outside [low, high] by a cent: normalise.
            c = candles[-1]
            candles[-1] = Candle(
                c.time_open,
                c.open,
                max(c.high, c.open, c.close),
                min(c.low, c.open, c.close),
                c.close,
                c.tick_volume,
                c.spread_points,
            )
        t += timedelta(minutes=5)
    return candles
