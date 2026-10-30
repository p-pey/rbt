"""Synthetic but realistic XAUUSD M5 candle builders (price levels ~2700, $0.2-3 ranges)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from xauassist.config.models import StrategyConfig, ZoneConfig
from xauassist.domain.candle import Candle
from xauassist.domain.enums import MaMethod, ZoneMode
from xauassist.domain.signal import Signal
from xauassist.indicators.ma import MovingAverage
from xauassist.strategy.engine import StrategyEngine

T0 = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
STEP = timedelta(minutes=5)
MIRROR_K = 5400.0


def candle(
    i: int, o: float, h: float, low: float, c: float, spread: float = 20.0, closed: bool = True
) -> Candle:
    return Candle(
        T0 + STEP * i, o, h, low, c, tick_volume=100, spread_points=spread, is_closed=closed
    )


def make_config(**overrides: object) -> StrategyConfig:
    """Deterministic zone: +/- $0.50 around the MA."""
    base: dict[str, object] = {"zone": ZoneConfig(mode=ZoneMode.PRICE, value=0.5)}
    base.update(overrides)
    return StrategyConfig(**base)  # type: ignore[arg-type]


def mirror(candles: list[Candle]) -> list[Candle]:
    """Price-mirror a series (SELL scenario -> BUY scenario). EMA/SMA are linear, so exact."""
    return [
        Candle(
            c.time_open,
            MIRROR_K - c.open,
            MIRROR_K - c.low,
            MIRROR_K - c.high,
            MIRROR_K - c.close,
            c.tick_volume,
            c.spread_points,
            c.is_closed,
        )
        for c in candles
    ]


class SellSeries:
    """Bearish-space series; a shadow EMA lets scenarios be placed relative to the MA."""

    def __init__(self, n_prefix: int = 130) -> None:
        self.candles: list[Candle] = []
        self._ma = MovingAverage(20, MaMethod.EMA)
        self.ma = 0.0
        price = 2750.0
        for i in range(n_prefix):  # steady decline with small alternating noise
            target = 2750.0 - 0.3 * (i + 1) + (0.15 if i % 2 else -0.15)
            self.add(price, max(price, target) + 0.3, min(price, target) - 0.3, target)
            price = target

    @property
    def last(self) -> Candle:
        return self.candles[-1]

    def add(self, o: float, h: float, low: float, c: float) -> Candle:
        cd = candle(len(self.candles), o, h, low, c)
        self.candles.append(cd)
        value = self._ma.update(c)
        self.ma = c if value is None else value
        return cd


def run(candles: list[Candle], config: StrategyConfig | None = None) -> list[Signal]:
    engine = StrategyEngine(config or make_config(), "XAUUSD")
    out: list[Signal] = []
    for cd in candles:
        out.extend(engine.on_candle_closed(cd))
    return out


def scenario_a_sell(pattern: str = "engulfing") -> list[Candle]:
    """Downtrend, rally toward the EMA (closing below it), then bearish confirmation at the EMA."""
    s = SellSeries()
    s.add(s.last.close, s.ma - 1.8, s.last.close - 0.2, s.ma - 2.0)  # r1: bullish, below MA
    s.add(s.last.close, s.ma - 1.0, s.last.close - 0.1, s.ma - 1.2)  # r2: bullish, below MA
    if pattern == "engulfing":
        o_p = s.ma - 1.5
        s.add(o_p, s.ma - 0.4, o_p - 0.1, s.ma - 0.5)  # prev: bullish, still below MA
        prev = s.last
        s.add(prev.close + 0.1, s.ma + 0.3, prev.open - 0.5, prev.open - 0.2)  # bearish engulfing
    else:
        s.add(s.last.close, s.ma - 0.6, s.last.close - 0.1, s.ma - 0.7)  # prev
        o = s.ma - 0.6
        s.add(o, s.ma + 0.4, s.ma - 1.0, s.ma - 0.9)  # bearish pin bar touching the MA
    return s.candles


def scenario_b_sell(hover_bars: int = 0) -> tuple[list[Candle], int]:
    """Downtrend, close above EMA (break), optional hover above it, pullback, bearish engulfing.

    Returns the candles and the index of the confirmation candle.
    """
    s = SellSeries()
    s.add(s.last.close, s.ma + 2.2, s.last.close - 0.2, s.ma + 2.0)  # b: breakout
    s.add(s.last.close, s.last.close + 0.2, s.ma + 0.9, s.ma + 1.2)  # p1: bearish drift
    for _ in range(hover_bars):  # stay well clear of the zone
        o = s.last.close
        c = s.ma + 3.0
        s.add(o, max(o, c) + 0.2, min(o, c) - 0.2, c)
    s.add(s.last.close, s.last.close + 0.7, s.last.close - 0.1, s.last.close + 0.6)  # prev: bullish
    prev = s.last
    s.add(prev.close + 0.1, prev.close + 0.3, s.ma - 0.8, s.ma - 0.5)  # bearish engulfing at the MA
    return s.candles, len(s.candles) - 1
