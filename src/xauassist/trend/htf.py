"""Higher-timeframe trend filter built from the base-timeframe candles.

HTF candles are aggregated from closed base candles only, and an HTF candle is emitted only when
its last base candle has closed, so the filter can never see an unfinished HTF bar.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime

from xauassist.config.models import MaConfig, TrendConfig
from xauassist.domain.candle import Candle
from xauassist.domain.enums import Direction, Timeframe
from xauassist.indicators.ma import MovingAverage


class HtfAggregator:
    def __init__(self, base: Timeframe, htf: Timeframe) -> None:
        if htf.minutes <= base.minutes or htf.minutes % base.minutes != 0:
            raise ValueError("htf must be a larger multiple of the base timeframe")
        self._base_min = base.minutes
        self._htf_min = htf.minutes
        self._bucket: int | None = None
        self._start = datetime.fromtimestamp(0, tz=UTC)
        self._open = self._high = self._low = self._close = 0.0
        self._volume = 0
        self._spread = 0.0

    def update(self, candle: Candle) -> list[Candle]:
        """Add a closed base candle; return HTF candles that completed as a result."""
        done: list[Candle] = []
        minute = int(candle.time_open.timestamp() // 60)
        bucket = minute // self._htf_min
        if self._bucket is not None and bucket != self._bucket:
            done.append(self._emit())  # earlier bucket ended short (market gap)
        if self._bucket is None:
            self._bucket = bucket
            self._start = datetime.fromtimestamp(bucket * self._htf_min * 60, tz=UTC)
            self._open, self._high, self._low = candle.open, candle.high, candle.low
            self._volume = 0
        self._high = max(self._high, candle.high)
        self._low = min(self._low, candle.low)
        self._close = candle.close
        self._volume += candle.tick_volume
        self._spread = candle.spread_points
        if minute + self._base_min >= (bucket + 1) * self._htf_min:
            done.append(self._emit())
        return done

    def _emit(self) -> Candle:
        self._bucket = None
        return Candle(
            time_open=self._start,
            open=self._open,
            high=self._high,
            low=self._low,
            close=self._close,
            tick_volume=self._volume,
            spread_points=self._spread,
        )


class HtfTrendTracker:
    def __init__(self, base: Timeframe, ma_cfg: MaConfig, trend_cfg: TrendConfig) -> None:
        self._agg = HtfAggregator(base, trend_cfg.htf_timeframe)
        self._ma = MovingAverage(ma_cfg.period, ma_cfg.method)
        self._lookback = trend_cfg.htf_slope_lookback
        self._mas: deque[float] = deque(maxlen=self._lookback + 1)
        self._close: float | None = None

    def update(self, candle: Candle) -> None:
        for htf_candle in self._agg.update(candle):
            value = self._ma.update(htf_candle.close)
            if value is not None:
                self._mas.append(value)
                self._close = htf_candle.close

    def allows(self, direction: Direction) -> bool:
        """True if the latest closed HTF bar is on the trend side of a MA sloping that way."""
        if self._close is None or len(self._mas) < self._lookback + 1:
            return False
        ma = self._mas[-1]
        slope = ma - self._mas[0]
        if direction is Direction.SELL:
            return self._close < ma and slope < 0
        return self._close > ma and slope > 0

    def label(self) -> str:
        if self.allows(Direction.SELL):
            return "bearish"
        if self.allows(Direction.BUY):
            return "bullish"
        return "neutral"
