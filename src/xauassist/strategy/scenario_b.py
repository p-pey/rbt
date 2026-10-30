"""Scenario B: MA break, then pullback to the MA within N candles, then confirmation.

The engine tracks the pending breakout; these functions are pure checks on `recent`
(oldest -> newest, ending with the just-closed candle).
"""

from __future__ import annotations

from collections.abc import Sequence

from xauassist.config.models import StrategyConfig
from xauassist.domain.bar import Bar
from xauassist.domain.enums import Direction, Scenario
from xauassist.patterns.confirmation import confirmation_patterns
from xauassist.strategy.candidate import Candidate
from xauassist.strategy.geometry import (
    closes_beyond_ma,
    closes_on_trend_side,
    reaches_zone_from_break_side,
)
from xauassist.trend.trend import slope_at, trend_matches


def is_breakout(recent: Sequence[Bar], direction: Direction, cfg: StrategyConfig) -> bool:
    """Latest bar is the first close beyond the MA, out of an established trend.

    SELL: bar before was in a bearish trend (below a falling MA) and this bar closes above MA.
    """
    last = len(recent) - 1
    if last < cfg.trend.slope_lookback + 1:
        return False
    if not closes_beyond_ma(recent[last], direction, cfg.scenario_b.breakout_buffer):
        return False
    prev_slope = slope_at(recent, last - 1, cfg.trend.slope_lookback)
    return trend_matches(direction, recent[last - 1], prev_slope, cfg.trend)


def detect_scenario_b(
    recent: Sequence[Bar],
    direction: Direction,
    cfg: StrategyConfig,
    half_width: float,
    bars_since_breakout: int,
) -> Candidate | None:
    """`bars_since_breakout` >= 1 is the age of the pending breakout at the latest bar."""
    b = cfg.scenario_b
    last = len(recent) - 1
    if bars_since_breakout < 1 or bars_since_breakout > b.max_pullback_bars or last < 1:
        return None
    cur, prev = recent[last], recent[last - 1]
    if b.require_close_reenter and not closes_on_trend_side(cur, direction, b.close_tolerance):
        return None
    # Only candles after the breakout candle count as the pullback.
    n_window = min(b.touch_lookback + 1, bars_since_breakout)
    if not any(
        reaches_zone_from_break_side(bar, direction, half_width) for bar in recent[-n_window:]
    ):
        return None
    patterns = confirmation_patterns(prev.candle, cur.candle, direction, cfg.pin_bar, cfg.engulfing)
    if not patterns:
        return None
    return Candidate(direction, Scenario.B, patterns, pullback_bars=bars_since_breakout)
