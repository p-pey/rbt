"""Strategy engine: consumes closed candles one at a time and emits candidate signals.

The same engine runs live and in backtests. It only ever sees candles up to the one that just
closed, so look-ahead and repainting are structurally impossible. It knows nothing about
MetaTrader, Telegram, sound, spreads or de-duplication - those are separate layers.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta

from xauassist.config.models import StrategyConfig
from xauassist.domain.bar import Bar
from xauassist.domain.candle import Candle
from xauassist.domain.enums import Direction
from xauassist.domain.signal import InfoValue, Signal
from xauassist.indicators.atr import AverageTrueRange
from xauassist.indicators.ma import MovingAverage
from xauassist.strategy.candidate import Candidate
from xauassist.strategy.errors import IncompleteCandleError, OutOfOrderCandleError
from xauassist.strategy.geometry import zone_half_width
from xauassist.strategy.reasons import build_reason
from xauassist.strategy.scenario_a import detect_scenario_a
from xauassist.strategy.scenario_b import detect_scenario_b, is_breakout
from xauassist.trend.htf import HtfTrendTracker
from xauassist.trend.trend import slope_at

_DIRECTIONS = (Direction.SELL, Direction.BUY)


class StrategyEngine:
    def __init__(self, config: StrategyConfig, symbol: str) -> None:
        self._cfg = config
        self._symbol = symbol
        self._ma = MovingAverage(config.ma.period, config.ma.method)
        self._atr = AverageTrueRange(config.zone.atr_period)
        self._htf = (
            HtfTrendTracker(config.timeframe, config.ma, config.trend)
            if config.trend.htf_enabled
            else None
        )
        self._warmup_bars = max(
            config.ma.period * config.ma.warmup_multiplier, config.zone.atr_period + 1
        )
        history = (
            config.scenario_b.max_pullback_bars
            + config.trend.slope_lookback
            + max(config.scenario_a.prior_bars_below, config.scenario_a.touch_lookback)
            + 10
        )
        self._bars: deque[Bar] = deque(maxlen=history)
        self._count = 0
        self._last_time: datetime | None = None
        # direction -> index (bar count) of the candle that broke the MA
        self._pending: dict[Direction, int] = {}

    # ---- state accessors (for GUI / monitoring) -------------------------------------------

    @property
    def latest_ma(self) -> float | None:
        return self._bars[-1].ma if self._bars else None

    @property
    def last_candle_time(self) -> datetime | None:
        return self._last_time

    @property
    def is_warmed_up(self) -> bool:
        return self._count >= self._warmup_bars

    def reset_setups(self) -> None:
        """Forget pending Scenario B breakouts (call after a data gap inside a session)."""
        self._pending.clear()

    # ---- main entry point -----------------------------------------------------------------

    def on_candle_closed(self, candle: Candle) -> list[Signal]:
        if not candle.is_closed:
            raise IncompleteCandleError(f"Candle {candle.time_open} is not closed")
        if self._last_time is not None:
            if candle.time_open == self._last_time:
                return []  # already processed (live polling re-delivers the last bar)
            if candle.time_open < self._last_time:
                raise OutOfOrderCandleError(
                    f"Candle {candle.time_open} is older than last processed {self._last_time}"
                )
        self._last_time = candle.time_open
        self._count += 1

        ma = self._ma.update(candle.close)
        atr = self._atr.update(candle.high, candle.low, candle.close)
        if self._htf is not None:
            self._htf.update(candle)
        if ma is None:
            return []
        self._bars.append(Bar(candle, ma, atr))
        if not self.is_warmed_up:
            return []

        recent = list(self._bars)
        signals: list[Signal] = []
        for direction in _DIRECTIONS:
            candidate = self._evaluate(direction, recent)
            if candidate is not None:
                signals.append(self._make_signal(candidate, recent))
        return signals

    # ---- internals ------------------------------------------------------------------------

    def _evaluate(self, direction: Direction, recent: list[Bar]) -> Candidate | None:
        cfg = self._cfg
        cur = recent[-1]
        half_width = zone_half_width(cfg.zone, cur.ma, cur.atr)
        if half_width is None:
            return None
        htf_ok = self._htf is None or self._htf.allows(direction)
        candidate: Candidate | None = None

        if cfg.scenario_b.enabled:
            breakout_index = self._pending.get(direction)
            if breakout_index is not None:
                age = self._count - breakout_index
                if age > cfg.scenario_b.max_pullback_bars:
                    del self._pending[direction]
                else:
                    candidate = detect_scenario_b(recent, direction, cfg, half_width, age)
                    if candidate is not None:
                        if htf_ok:
                            del self._pending[direction]
                        else:
                            candidate = None  # filtered; setup stays pending until it expires
            if candidate is None and is_breakout(recent, direction, cfg):
                self._pending[direction] = self._count  # a new break restarts the window

        # A and B are mutually exclusive: while a breakout is pending, B owns the setup.
        if (
            candidate is None
            and cfg.scenario_a.enabled
            and direction not in self._pending
            and htf_ok
        ):
            candidate = detect_scenario_a(recent, direction, cfg, half_width)
        return candidate

    def _make_signal(self, candidate: Candidate, recent: list[Bar]) -> Signal:
        cfg = self._cfg
        cur = recent[-1]
        half_width = zone_half_width(cfg.zone, cur.ma, cur.atr)
        info: dict[str, InfoValue] = {
            "ma_label": cfg.ma.label,
            "spread_points": cur.candle.spread_points,
            "atr": cur.atr,
            "zone_half_width": half_width,
            "ma_slope": slope_at(recent, len(recent) - 1, cfg.trend.slope_lookback),
            "htf_trend": self._htf.label() if self._htf is not None else "off",
            "pullback_bars": None
            if candidate.pullback_bars is None
            else float(candidate.pullback_bars),
        }
        return Signal(
            direction=candidate.direction,
            scenario=candidate.scenario,
            symbol=self._symbol,
            timeframe=cfg.timeframe,
            bar_open_time=cur.candle.time_open,
            time=cur.candle.time_open + timedelta(minutes=cfg.timeframe.minutes),
            entry_price=cur.candle.close,
            ma_value=cur.ma,
            patterns=candidate.patterns,
            reason=build_reason(candidate, cfg.ma.label),
            info=info,
        )
