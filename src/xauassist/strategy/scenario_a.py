"""Scenario A: trend + MA retest + confirmation.

`recent` is oldest -> newest and ends with the just-closed confirmation candle.
"""

from __future__ import annotations

from collections.abc import Sequence

from xauassist.config.models import StrategyConfig
from xauassist.domain.bar import Bar
from xauassist.domain.enums import Direction, Scenario
from xauassist.patterns.confirmation import confirmation_patterns
from xauassist.strategy.candidate import Candidate
from xauassist.strategy.geometry import closes_on_trend_side, reaches_zone_from_trend_side
from xauassist.trend.trend import on_trend_side, slope_at, trend_matches


def detect_scenario_a(
    recent: Sequence[Bar], direction: Direction, cfg: StrategyConfig, half_width: float
) -> Candidate | None:
    a = cfg.scenario_a
    last = len(recent) - 1
    if last < max(a.prior_bars_below, cfg.trend.slope_lookback + 1):
        return None
    cur, prev = recent[last], recent[last - 1]

    if not closes_on_trend_side(cur, direction, a.close_tolerance):
        return None
    # Price was on the trend side of the MA before the retest.
    if not all(on_trend_side(b, direction) for b in recent[last - a.prior_bars_below : last]):
        return None
    touch_window = recent[max(0, last - a.touch_lookback) : last + 1]
    if not any(reaches_zone_from_trend_side(b, direction, half_width) for b in touch_window):
        return None
    # Trend is judged on the bar before the confirmation candle.
    slope = slope_at(recent, last - 1, cfg.trend.slope_lookback)
    if not trend_matches(direction, prev, slope, cfg.trend):
        return None

    patterns = confirmation_patterns(prev.candle, cur.candle, direction, cfg.pin_bar, cfg.engulfing)
    if not patterns:
        return None
    return Candidate(direction, Scenario.A, patterns)
